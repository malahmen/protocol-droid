"""api.py, the service's HTTP front door - what it refuses before anything is queued.

Needs fastapi, rq and an HTTP test client (requirements-ci.txt), not marker:
the queue is replaced, so no Redis is needed either. Skipped without them, as
the stdlib suite runs; CI's `api-and-queue` job installs them.

Run: python3 -m unittest discover -s tests
"""
import importlib
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import fastapi  # noqa: F401
    import rq  # noqa: F401
    from fastapi.testclient import TestClient
    HAVE_API = True
except ImportError:  # pragma: no cover - the stdlib-only run
    HAVE_API = False

TOKEN = "s3cret"


@unittest.skipUnless(HAVE_API, "fastapi, rq and an HTTP test client not installed")
class ApiCase(unittest.TestCase):
    token = TOKEN

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.inp = os.path.join(self.root, "input")
        os.makedirs(os.path.join(self.inp, "a-folder"))
        with open(os.path.join(self.inp, "doc.pdf"), "wb") as fh:
            fh.write(b"%PDF-1.7")
        env = {"INPUT_DIR": self.inp, "OUTPUT_DIR": os.path.join(self.root, "output"),
               "PROTOCOL_DROID_TOKEN": self.token or ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        # api.py reads its settings at import.
        import api
        self.api = importlib.reload(api)
        self.enqueue = mock.patch.object(self.api._queue, "enqueue").start()
        self.addCleanup(mock.patch.stopall)
        self.enqueue.return_value = mock.Mock(id="j1", get_status=lambda: "queued")
        self.client = TestClient(self.api.app)

    def auth(self):
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def post(self, path):
        return self.client.post("/jobs", json={"path": path}, headers=self.auth())


class OneFilePerJob(ApiCase):
    """Item 57: a folder was accepted and failed later, in the worker."""

    def test_a_file_is_queued(self):
        r = self.post(os.path.join(self.inp, "doc.pdf"))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.enqueue.called)

    def test_a_folder_is_refused_before_it_is_queued(self):
        r = self.post(os.path.join(self.inp, "a-folder"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("enqueue_batch.py", r.json()["detail"])
        self.assertFalse(self.enqueue.called)

    def test_a_missing_file_is_refused_before_it_is_queued(self):
        r = self.post(os.path.join(self.inp, "nope.pdf"))
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.enqueue.called)

    def test_outside_the_input_folder_is_still_refused_first(self):
        r = self.post("/etc/passwd")
        self.assertEqual(r.status_code, 400)
        self.assertIn("must be under", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
