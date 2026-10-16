"""port.py and markitdown_tasks.convert_bytes - protocol's converter port for
markitdown, in its own image (Dockerfile.markitdown; Kamino item 76).

markitdown is not installed in CI: a stand-in module records how it is
called, which is what these checks are about - convert_local and never
convert (which fetches URLs), plugins off, no options. That the real
markitdown converts DOCX, PPTX, XLSX, HTML, CSV and EPUB was checked by
building the image (see the commit that added it).

Run: python3 -m unittest discover -s tests   (stdlib only)
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import markitdown_tasks  # noqa: E402
import port  # noqa: E402


class Port(unittest.TestCase):
    def test_a_path_in_the_name_is_dropped(self):
        for name, want in (("report.docx", "report.docx"), ("../../etc/x.docx", "x.docx"),
                           ("/abs/x.docx", "x.docx"), ("a\\b\\x.docx", "x.docx")):
            self.assertEqual(port.safe_name(name), want, msg=name)

    def test_an_empty_or_dotted_name_becomes_document(self):
        for name in ("", "  ", ".", "..", None):
            self.assertEqual(port.safe_name(name), "document", msg=repr(name))

    def test_scratch_holds_the_bytes_and_is_removed(self):
        with port.scratch(b"abc", "../x.docx") as (tmp, src):
            self.assertTrue(src.startswith(tmp))
            self.assertEqual(os.path.basename(src), "x.docx")
            with open(src, "rb") as fh:
                self.assertEqual(fh.read(), b"abc")
        self.assertFalse(os.path.exists(tmp))

    def test_scratch_is_removed_on_failure_too(self):
        with self.assertRaises(RuntimeError):
            with port.scratch(b"abc", "x.docx") as (tmp, _src):
                raise RuntimeError("converter failed")
        self.assertFalse(os.path.exists(tmp))


class FakeMarkItDown:
    calls = []

    def __init__(self, **kwargs):
        FakeMarkItDown.calls.append(("init", kwargs))

    def convert_local(self, path, **kwargs):
        with open(path, "rb") as fh:
            FakeMarkItDown.calls.append(("convert_local", path, fh.read()))
        return types.SimpleNamespace(markdown="# Converted\n")

    def convert(self, *a, **kw):   # fetches URLs; must never be reached
        raise AssertionError("convert() called - it would fetch a URL given one")


class ConvertBytes(unittest.TestCase):
    def setUp(self):
        FakeMarkItDown.calls = []
        fake = types.ModuleType("markitdown")
        fake.MarkItDown = FakeMarkItDown
        patchers = [mock.patch.dict(sys.modules, {"markitdown": fake}),
                    mock.patch.object(markitdown_tasks, "tool_versions",
                                      lambda pkgs: {p: "0.1.8" for p in pkgs})]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_bytes_in_markdown_out(self):
        files, versions = markitdown_tasks.convert_bytes(b"PK\x03\x04docx", "Report Q3.docx")
        self.assertEqual(files, {"Report Q3.md": b"# Converted\n"})
        self.assertEqual(versions, {"markitdown": "0.1.8"})
        (_, path, data), = [c for c in FakeMarkItDown.calls if c[0] == "convert_local"]
        self.assertEqual(data, b"PK\x03\x04docx")
        self.assertEqual(os.path.basename(path), "Report Q3.docx")   # extension kept: it picks the reader

    def test_plugins_are_off(self):
        markitdown_tasks.convert_bytes(b"x", "a.docx")
        (_, kwargs), = [c for c in FakeMarkItDown.calls if c[0] == "init"]
        self.assertIs(kwargs.get("enable_plugins"), False)

    def test_options_are_refused_not_ignored(self):
        with self.assertRaises(markitdown_tasks.InvalidOption) as cm:
            markitdown_tasks.convert_bytes(b"x", "a.docx", {"page_range": "0", "llm": True})
        self.assertIn("llm, page_range", str(cm.exception))
        self.assertEqual(FakeMarkItDown.calls, [])
        for empty in (None, {}):
            markitdown_tasks.convert_bytes(b"x", "a.docx", empty)

    def test_a_path_in_the_name_does_not_choose_where_it_is_written(self):
        markitdown_tasks.convert_bytes(b"x", "../../etc/a.docx")
        (_, path, _), = [c for c in FakeMarkItDown.calls if c[0] == "convert_local"]
        self.assertTrue(path.startswith(tempfile.gettempdir()))
        self.assertEqual(os.path.basename(path), "a.docx")

    def test_nothing_is_left_on_the_worker(self):
        markitdown_tasks.convert_bytes(b"x", "a.docx")
        (_, path, _), = [c for c in FakeMarkItDown.calls if c[0] == "convert_local"]
        self.assertFalse(os.path.exists(os.path.dirname(os.path.dirname(path))))

    def test_a_nameless_document_still_gets_a_name(self):
        files, _ = markitdown_tasks.convert_bytes(b"x", "")
        self.assertEqual(list(files), ["document.md"])


if __name__ == "__main__":
    unittest.main()
