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
