"""tasks.convert_bytes - the seam protocol (the conversion API) calls.

protocol runs this image as its marker worker and talks to every converter
through one port: bytes in, every output file as bytes out, plus the tool
versions. It writes its own provenance.json under the same key afterwards, so
the engine's sidecar must not travel with the files.

Run: python3 -m unittest discover -s tests   (stdlib only; marker not needed)
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tasks  # noqa: E402

VERSIONS = {"marker-pdf": "2.0.0", "surya-ocr": "0.22.1", "torch": "2.14.1"}


class ConvertBytes(unittest.TestCase):
    def setUp(self):
        self.seen = {}

        def fake_convert(src, opts, input_root=None):
            # What marker + convert_document leave behind: the document, an
            # image in a subfolder, marker's meta file and our sidecar.
            with open(src, "rb") as fh:
                self.seen["data"] = fh.read()
            self.seen["src"], self.seen["opts"] = src, opts
            out = os.path.join(opts["output_dir"], "doc_pdf")
            os.makedirs(os.path.join(out, "images"))
            for rel, body in (("doc.md", b"# Doc\n"), ("doc_meta.json", b"{}"),
                              ("images/fig1.png", b"\x89PNG")):
                with open(os.path.join(out, rel), "wb") as fh:
                    fh.write(body)
            with open(os.path.join(out, "provenance.json"), "w", encoding="utf-8") as fh:
                json.dump({"versions": VERSIONS, "source": {"path": src}}, fh)
            return out

        patcher = mock.patch.object(tasks, "convert_document", fake_convert)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bytes_in_files_out(self):
        files, versions = tasks.convert_bytes(b"%PDF-1.7 ...", "doc.pdf", {"output_format": "markdown"})
        self.assertEqual(self.seen["data"], b"%PDF-1.7 ...")
        self.assertEqual(files, {"doc.md": b"# Doc\n", "doc_meta.json": b"{}",
                                 "images/fig1.png": b"\x89PNG"})

    def test_the_sidecar_stays_behind_and_its_versions_come_back(self):
        # protocol writes its own provenance.json at the same key afterwards.
        files, versions = tasks.convert_bytes(b"x", "doc.pdf")
        self.assertNotIn("provenance.json", files)
        self.assertEqual(versions, VERSIONS)

    def test_the_file_keeps_its_extension(self):
        # marker picks its provider by extension.
        tasks.convert_bytes(b"x", "Report Q3.PDF")
        self.assertEqual(os.path.basename(self.seen["src"]), "Report Q3.PDF")

    def test_a_path_in_the_name_does_not_choose_where_it_is_written(self):
        for name in ("../../etc/doc.pdf", "/abs/doc.pdf", "a\\b\\doc.pdf"):
            tasks.convert_bytes(b"x", name)
            src = self.seen["src"]
            self.assertEqual(os.path.basename(src), "doc.pdf", msg=name)
            self.assertTrue(src.startswith(tempfile.gettempdir()), msg=name)

    def test_an_empty_or_dotted_name_still_converts(self):
        for name in ("", "  ", ".", "..", None):
            tasks.convert_bytes(b"x", name)
            self.assertEqual(os.path.basename(self.seen["src"]), "document", msg=repr(name))

    def test_the_caller_cannot_choose_the_output_folder(self):
        tasks.convert_bytes(b"x", "doc.pdf", {"output_dir": "/data/output", "page_range": "0"})
        self.assertNotEqual(self.seen["opts"]["output_dir"], "/data/output")
        self.assertEqual(self.seen["opts"]["page_range"], "0")   # the rest passes through

    def test_nothing_is_left_on_the_worker(self):
        tasks.convert_bytes(b"x", "doc.pdf")
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(self.seen["src"]))))

    def test_nothing_is_left_when_the_conversion_fails(self):
        def boom(src, opts, input_root=None):
            self.seen["src"] = src
            raise RuntimeError("marker failed")
        with mock.patch.object(tasks, "convert_document", boom):
            with self.assertRaises(RuntimeError):   # the failure is the caller's to record
                tasks.convert_bytes(b"x", "doc.pdf")
        self.assertFalse(os.path.exists(self.seen["src"]))

    def test_two_calls_never_share_a_folder(self):
        tasks.convert_bytes(b"x", "doc.pdf")
        first = self.seen["opts"]["output_dir"]
        tasks.convert_bytes(b"x", "doc.pdf")
        self.assertNotEqual(first, self.seen["opts"]["output_dir"])


if __name__ == "__main__":
    unittest.main()
