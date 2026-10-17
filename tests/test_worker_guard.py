"""worker.py's GuardedWorker against a real Redis (Kamino item 78).

A plain RQ worker runs whatever function a job in Redis names - shown with a
job naming os.system, which ran pickled *and* as JSON. The service now
enqueues JSON jobs naming tasks.JOB_FUNC only, and its worker refuses any
other. These checks enqueue the hostile kinds and prove nothing ran; and that
the API's own jobs still do (marker replaced: none is installed here).

Needs rq (requirements-ci.txt) and a Redis to point at:

    PROTOCOL_DROID_TEST_REDIS_URL=redis://localhost:16379/14 python3 -m unittest discover -s tests

**The suite flushes that database.** No default URL, so it can never reach a
Redis that holds anything; without the variable these checks skip. CI starts
a throwaway Redis 7 for them.
"""
import json
import os
import sys
import tempfile
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tasks  # noqa: E402

URL = os.environ.get("PROTOCOL_DROID_TEST_REDIS_URL", "")
CANARY = []

try:
    from redis import Redis
    from rq import Callback, Queue
    from rq.registry import FailedJobRegistry
    from rq.serializers import JSONSerializer

    import worker
    HAVE_RQ = True
except ImportError:  # pragma: no cover - the stdlib-only run
    HAVE_RQ = False


def canary(*args, **kwargs):
    """Stands in for an attacker's callable: if a refused job reaches it, the list says so."""
    CANARY.append(args)


def reachable():
    if not (HAVE_RQ and URL):
        return False
    try:
        return Redis.from_url(URL).ping()
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(reachable(), "set PROTOCOL_DROID_TEST_REDIS_URL to a throwaway Redis (rq installed)")
class GuardCase(unittest.TestCase):
    def setUp(self):
        self.conn = Redis.from_url(URL)
        self.conn.flushdb()
        CANARY.clear()
        self.queue = Queue("marker", connection=self.conn, serializer=JSONSerializer)
        self.converted = []
        patcher = mock.patch.object(tasks, "convert_document",
                                    lambda path, opts, input_root=None: self.converted.append(path) or "/out")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.proof = os.path.join(tempfile.mkdtemp(), "ran")

    def work(self):
        worker.GuardedWorker([self.queue], connection=self.conn, serializer=JSONSerializer).work(burst=True)

    def failed(self):
        return FailedJobRegistry("marker", connection=self.conn, serializer=JSONSerializer).count


class TheServicesOwnJobs(GuardCase):
    def test_a_job_as_the_api_enqueues_it_is_json_and_runs(self):
        job = self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", {"output_format": "markdown"},
                                 input_root="/data/input")
        func, _instance, args, kwargs = json.loads(zlib.decompress(self.conn.hget(f"rq:job:{job.id}", "data")))
        self.assertEqual(func, "tasks.convert_document")
        self.assertEqual(kwargs, {"input_root": "/data/input"})
        self.work()
        self.assertEqual(self.converted, ["/data/input/a.pdf"])
        self.assertEqual(self.failed(), 0)

    def test_input_root_is_optional(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", {})
        self.work()
        self.assertEqual(self.converted, ["/data/input/a.pdf"])


class Refused(GuardCase):
    def assert_refused(self):
        self.work()
        self.assertFalse(os.path.exists(self.proof), "a refused job ran")
        self.assertEqual(CANARY, [], "a refused job's callable or callback ran")
        self.assertEqual(self.converted, [])
        self.assertEqual(self.failed(), 1)

    def test_another_function(self):
        self.queue.enqueue("os.system", f"touch {self.proof}")
        self.assert_refused()

    def test_a_pickled_job(self):
        # RQ's default serializer - and what every job looked like before this.
        Queue("marker", connection=self.conn).enqueue("os.system", f"touch {self.proof}")
        self.work()
        self.assertFalse(os.path.exists(self.proof), "a pickled job ran")

    def test_the_right_function_with_a_callback(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", {},
                           on_success=Callback("test_worker_guard.canary"),
                           on_failure=Callback("test_worker_guard.canary"))
        self.assert_refused()

    def test_the_right_function_with_the_wrong_arguments(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf")         # no options dict
        self.assert_refused()

    def test_the_right_function_with_another_keyword(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", {}, output_dir="/etc")
        self.assert_refused()

    def test_the_worker_goes_on_after_refusing(self):
        self.queue.enqueue("os.system", f"touch {self.proof}")
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/b.pdf", {})
        self.work()
        self.assertFalse(os.path.exists(self.proof))
        self.assertEqual(self.converted, ["/data/input/b.pdf"])


if __name__ == "__main__":
    unittest.main()
