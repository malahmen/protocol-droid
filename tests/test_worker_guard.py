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
import http.server
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import zlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# RQ imports a callback by name, as "test_worker_guard.canary": this folder
# must be importable however the suite was started (discover -s tests, or
# -m unittest tests.test_worker_guard), or a callback that runs is not seen.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasks  # noqa: E402

URL = os.environ.get("PROTOCOL_DROID_TEST_REDIS_URL", "")
# A file, not a list: the module RQ imports by name may be a second copy of
# this one, with a list of its own. The pid keeps parallel runs apart.
CANARY = os.path.join(tempfile.gettempdir(), f"protocol-droid-canary-{os.getpid()}")

try:
    from redis import Redis
    from rq import Callback, Queue
    from rq.webhook import Webhook
    from rq.registry import FailedJobRegistry
    from rq.serializers import JSONSerializer

    import worker
    HAVE_RQ = True
except ImportError:  # pragma: no cover - the stdlib-only run
    HAVE_RQ = False


def canary(*args, **kwargs):
    """Stands in for an attacker's callable: if a refused job reaches it, the list says so."""
    with open(CANARY, "a") as fh:
        fh.write(repr(args) + "\n")


def canary_calls():
    try:
        with open(CANARY) as fh:
            return fh.read().splitlines()
    except FileNotFoundError:
        return []


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
        if os.path.exists(CANARY):
            os.remove(CANARY)
        self.addCleanup(lambda: os.path.exists(CANARY) and os.remove(CANARY))
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
        self.assertEqual(canary_calls(), [], "a refused job's callable or callback ran")
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


class TheDoorsOutsideExecuteJob(GuardCase):
    """Item 78, reopened 2026-10-19: RQ reaches a job's callbacks and webhooks
    by paths that never call execute_job, so a check there alone missed them."""

    def test_a_failure_callback_planted_in_the_started_registry_never_runs(self):
        # The registry cleanup every worker runs as maintenance: an expired
        # in-progress entry has its failure callback imported and called.
        job = self.queue.enqueue("os.getcwd", on_failure=Callback("test_worker_guard.canary"))
        self.conn.lrem(self.queue.key, 0, job.id)
        self.conn.zadd(f"rq:wip:{self.queue.name}", {f"{job.id}:planted": 1})
        self.work()
        self.assertEqual(canary_calls(), [], "the registry cleanup ran a job's failure callback")

    def test_a_refused_jobs_webhooks_are_not_sent(self):
        hits = []

        class Hook(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.path)
                self.send_response(200)
                self.end_headers()
            do_POST = do_GET  # noqa: N815

            def log_message(self, *args):
                pass

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = http.server.HTTPServer(("127.0.0.1", port), Hook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.queue.enqueue("os.system", f"touch {self.proof}",
                           webhooks=[Webhook(f"http://127.0.0.1:{port}/forged", "failed")])
        self.work()
        time.sleep(0.3)
        self.assertEqual(hits, [], "a refused job's webhook was sent")
        self.assertFalse(os.path.exists(self.proof))

    def test_the_right_job_carrying_a_webhook_is_refused(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", {},
                           webhooks=[Webhook("http://127.0.0.1:9/never", "finished")])
        self.assertRefused()

    def assertRefused(self):  # noqa: N802
        self.work()
        self.assertEqual(self.converted, [], "a refused job was converted")
        self.assertEqual(self.failed(), 1)

    def test_options_naming_a_marker_class_are_refused(self):
        # marker's ConfigParser imports these by module path and builds them.
        for opts in ({"converter_cls": "os.getcwd"}, {"processors": "os.getcwd"}):
            with self.subTest(opts=opts):
                self.setUp()
                self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", opts)
                self.assertRefused()

    def test_an_option_of_the_wrong_type_is_refused(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", {"force_ocr": "yes"})
        self.assertRefused()

    def test_a_path_outside_input_dir_is_refused(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/etc/passwd", {})
        self.assertRefused()

    def test_a_path_climbing_out_of_input_dir_is_refused(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/../../etc/passwd", {})
        self.assertRefused()

    def test_an_input_root_outside_input_dir_is_refused(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", {}, input_root="/")
        self.assertRefused()

    def test_an_output_dir_outside_output_dir_is_refused(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf", {"output_dir": "/etc"})
        self.assertRefused()

    def test_the_apis_own_job_with_every_option_still_runs(self):
        self.queue.enqueue(tasks.JOB_FUNC, "/data/input/a.pdf",
                           {"output_format": "markdown", "output_dir": "/data/output/x",
                            "force_ocr": True, "page_range": "0-1"},
                           input_root="/data/input")
        self.work()
        self.assertEqual(self.converted, ["/data/input/a.pdf"])
        self.assertEqual(self.failed(), 0)


if __name__ == "__main__":
    unittest.main()
