"""The local engine's runs, in process, against the stub converters.

What the bash suites cannot reach: a clash that only a case-sensitive disk
can produce (the in-run `written` guard behind Kamino item 126), a client
clock that disagrees with the output disk's (item 204), and the folder walk
of the service's bulk producer (item 190).

Run: python3 -m unittest discover -s tests   (standard library only; the
enqueue_batch case needs rq and is skipped without it)
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pdroid import common, marker, ui  # noqa: E402
from pdroid import markitdown as mid  # noqa: E402

STUBS = os.path.join(ROOT, "tests", "stubs")
STUBS_MARKER = os.path.join(ROOT, "tests", "stubs-marker")


class Case(unittest.TestCase):
    def setUp(self):
        self.t = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.t, True)
        env = {"PATH": os.pathsep.join([STUBS, STUBS_MARKER, os.environ.get("PATH", "")]),
               "PROTOCOL_DROID_NO_OPEN": "1", "SURYA_INFERENCE_BACKEND": "vllm",
               "HF_HUB_OFFLINE": "1"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mk(self, rel, text=None):
        path = os.path.join(self.t, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text if text is not None else f"content of {rel}\n")
        return path

    def quiet(self, fn, *args, **kw):
        """fn's result and what it printed on stderr."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = fn(*args, **kw)
        return rc, err.getvalue()


class WrittenGuard(Case):
    """Kamino item 126: a second input of the run never replaces an output the
    first one wrote. Only a case-sensitive disk produces such a pair
    (Notes.HTML and notes.html), so the names are forced here."""

    def test_the_second_input_fails_and_the_first_output_survives(self):
        a, b = self.mk("in/a.html"), self.mk("in/b.html")
        out = os.path.join(self.t, "out")
        with mock.patch.object(mid, "output_names", return_value={a: "same.md", b: "SAME.md"}):
            rc, err = self.quiet(mid.convert, ["--output-dir", out, a, b], open_output=False)
        self.assertEqual(rc, ui.EX_FAILED)
        self.assertIn(f"Failed: {b}", err)
        self.assertIn("already written by another input", err)
        with open(os.path.join(out, "same.md")) as fh:
            self.assertIn("a.html", fh.read())
        self.assertEqual(sorted(os.listdir(out)), ["same.md", "same.md.provenance.json"])


class CaseFoldedStems(unittest.TestCase):
    """Stems are grouped case-insensitively: Notes.html and notes.docx would
    both write notes.md on macOS, so both take the <stem>_<ext> form
    (markitdown's case: test_output_folder.MarkitdownNames)."""

    def test_case_variants_in_marker_folders(self):
        self.assertEqual(common.output_stems(["/i/x/A.pdf", "/i/y/a.pdf"], "/i", flat=True),
                         {"/i/x/A.pdf": "x_A_pdf", "/i/y/a.pdf": "y_a_pdf"})


class OutputNames(unittest.TestCase):
    """Kamino item 204: names from the inputs alone, and no input loses its
    output to another's <stem>_<ext> name."""

    def test_a_plain_name_meeting_a_suffixed_one_takes_the_ext_form(self):
        n = mid.output_names(["/i/report.docx", "/i/report.pptx", "/i/report_docx.html"], "/i")
        self.assertEqual(n, {"/i/report.docx": "report_docx.md", "/i/report.pptx": "report_pptx.md",
                             "/i/report_docx.html": "report_docx_html.md"})

    def test_marker_names_follow_the_folder_not_the_order(self):
        files = ["/i/w/a.pdf", "/i/x/a.pdf", "/i/y/a.pdf"]
        want = {"/i/x/a.pdf": "x_a_pdf", "/i/y/a.pdf": "y_a_pdf"}
        self.assertEqual(common.output_stems(files[1:], "/i", flat=True), want)
        got = common.output_stems(list(reversed(files)), "/i", flat=True)
        self.assertEqual({f: got[f] for f in want}, want)

    def test_a_flattened_name_meeting_a_real_one(self):
        n = common.output_stems(["/i/x/a.pdf", "/i/y/a.pdf", "/i/x_a_pdf.docx"], "/i", flat=True)
        self.assertEqual(n["/i/x_a_pdf.docx"], "x_a_pdf_docx")
        self.assertEqual(common.name_clashes(n), set())

    def test_an_unresolvable_clash_is_reported(self):
        n = common.output_stems(["/i/Notes.HTML", "/i/notes.html"], "", flat=True)
        self.assertEqual(common.name_clashes(n), {"/i/Notes.HTML", "/i/notes.html"})


class ScanSkipsOutput(Case):
    """Kamino item 190: the run's output folder, wherever it is, and sidecars
    are never inputs."""

    def test_the_output_folder_and_sidecars_are_skipped(self):
        self.mk("in/a.html")
        self.mk("in/md/a.md.provenance.json", "{}")
        self.mk("in/md/a/_page_0_Picture_1.jpeg")
        self.mk("in/sub/provenance.json", "{}")
        self.mk("in/sub/data.json", "{}")
        root = os.path.join(self.t, "in")
        got = common.scan_files(root, 3, mid.EXTS, skip=(os.path.join(root, "md"),))
        self.assertEqual([os.path.relpath(f, root) for f in got], ["a.html", "sub/data.json"])

    def test_the_output_folder_is_matched_by_real_path(self):
        self.mk("in/out/x.pdf")
        os.symlink(os.path.join(self.t, "in", "out"), os.path.join(self.t, "link"))
        got = common.scan_files(os.path.join(self.t, "in"), 3, marker.EXTS,
                                skip=(os.path.join(self.t, "link"),))
        self.assertEqual(got, [])


class ClockSkew(Case):
    """Kamino item 204: on a network share the output's mtimes are the server's
    clock. A client clock ahead of it made every fresh folder look like an
    earlier run's; freshness is now measured on the output disk's own clock."""

    def test_a_client_clock_ahead_of_the_disk_is_not_a_failure(self):
        self.mk("in/a.pdf")
        self.mk("in/b.pdf")
        out = os.path.join(self.t, "out")
        ahead = int(time.time()) + 3600
        with mock.patch.object(common, "now_epoch", return_value=ahead):
            rc, err = self.quiet(marker.convert, ["--output-dir", out, os.path.join(self.t, "in")],
                                 open_output=False)
        self.assertEqual(rc, ui.EX_OK, err)
        self.assertTrue(os.path.isfile(os.path.join(out, "a", "provenance.json")))

    def test_a_folder_from_an_earlier_run_still_fails(self):
        self.mk("in/a.pdf")
        self.mk("in/boom.pdf")
        out = os.path.join(self.t, "out")
        old = self.mk("out/boom/boom.md", "old\n")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        rc, err = self.quiet(marker.convert, ["--output-dir", out, os.path.join(self.t, "in")],
                             open_output=False)
        self.assertEqual(rc, ui.EX_FAILED)
        self.assertIn("earlier run", err)


class StaleDuplicates(Case):
    """Kamino item 204: a folder an earlier run gave a document, under a name
    the document no longer has, is removed once the document has its own."""

    def sidecar_sources(self, out):
        found = []
        for entry in sorted(os.listdir(out)):
            p = os.path.join(out, entry, "provenance.json")
            if os.path.isfile(p):
                with open(p) as fh:
                    found.append((entry, json.load(fh)["source"]["path"]))
        return found

    def test_an_input_removed_and_its_neighbour_moves(self):
        x, y = self.mk("in/x/a.pdf"), self.mk("in/y/a.pdf")
        out, root = os.path.join(self.t, "out"), os.path.join(self.t, "in")
        self.quiet(marker.convert, ["--output-dir", out, root], open_output=False)
        os.remove(x)
        rc, _ = self.quiet(marker.convert, ["--output-dir", out, root], open_output=False)
        self.assertEqual(rc, ui.EX_OK)
        self.assertEqual(self.sidecar_sources(out), [("a", y), ("x_a_pdf", x)])

    def test_a_folder_of_another_backend_or_source_is_kept(self):
        a = self.mk("in/a.pdf")
        out = os.path.join(self.t, "out")
        self.mk("out/keep1/provenance.json",
                json.dumps({"source": {"path": a}, "conversion": {"backend": "markitdown"}}))
        self.mk("out/keep2/provenance.json",
                json.dumps({"source": {"path": "/elsewhere/a.pdf"}, "conversion": {"backend": "marker"}}))
        self.mk("out/drop/provenance.json",
                json.dumps({"source": {"path": a}, "conversion": {"backend": "marker"}}))
        rc, _ = self.quiet(marker.convert, ["--output-dir", out, os.path.join(self.t, "in")],
                           open_output=False)
        self.assertEqual(rc, ui.EX_OK)
        self.assertEqual(sorted(os.listdir(out)), ["a", "keep1", "keep2"])


try:
    import rq  # noqa: F401
    HAVE_RQ = True
except ImportError:
    HAVE_RQ = False


@unittest.skipUnless(HAVE_RQ, "rq not installed (requirements-ci.txt)")
class EnqueueBatchSkipsOutput(Case):
    """Kamino item 190, the service's producer: an output folder inside the
    input folder is not enqueued."""

    def test_the_output_folder_is_not_walked(self):
        import enqueue_batch
        a = self.mk("in/a.pdf")
        self.mk("in/out/a_pdf/_page_0_Picture_1.jpeg")
        q = mock.MagicMock()
        q.name = "marker"
        argv = ["enqueue_batch.py", os.path.join(self.t, "in"), "--output_dir", os.path.join(self.t, "in", "out")]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(enqueue_batch, "Queue", return_value=q), \
                mock.patch.object(enqueue_batch.tasks, "redis_connection"), \
                mock.patch.object(enqueue_batch.tasks, "job_timeout", return_value=60), \
                contextlib.redirect_stdout(io.StringIO()):
            enqueue_batch.main()
        self.assertEqual([call.args[1] for call in q.enqueue.call_args_list], [a])


if __name__ == "__main__":
    unittest.main()
