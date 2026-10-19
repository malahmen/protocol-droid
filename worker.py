"""marker RQ worker launcher.

Uses RQ's SimpleWorker (no per-job fork) so marker's models load once and are
reused across jobs — and so a GPU/CUDA context isn't corrupted by forking.
Warms up at startup so the first job isn't slow: the models, then one OCR'd
page with a table, which pays torch's warm-up, starts llama-server and runs the
table path once (tasks.prewarm).

Runs only the service's own job (Kamino item 78). An RQ job names a function
by import path and the worker calls it, so a plain worker runs whatever any
job in Redis names - shown with os.system, pickled and as JSON alike. Three
things close it, and all three must hold: jobs are JSON (a pickled payload
does not deserialize); the API and enqueue_batch.py enqueue tasks.JOB_FUNC
only; and GuardedWorker checks every job before running anything - that
function, a path and an options dict, no keyword but input_root, and no
success/failure/stopped callback (RQ imports and calls those by name too). A
refused job is failed without being run, and failing it runs no callback. A
job enqueued before this change (pickled) is refused the same way.

RQ has two more doors, both shut here (item 78, reopened 2026-10-19). Its
registry cleanup - every worker's maintenance - calls an expired job's failure
callback by name, never passing through execute_job; and failing a job sends
the webhooks it carries, a request to any URL from inside the network. So the
worker and its queues load jobs as GuardedJob, whose callbacks and webhooks do
nothing at all (the service uses neither), and check() refuses a job that
carries webhooks or dependencies. The path, input_root and output_dir must
also lie under INPUT_DIR / OUTPUT_DIR, as the API already demands; and
tasks.validate_options accepts only the five options the service uses, so a
job cannot name a marker class (processors, converter_cls) to import.

Env:
  REDIS_URL   default redis://localhost:6379/0
  REDIS_PASSWORD  Redis AUTH password (unset = no AUTH)
  MARKER_QUEUE default "marker"
  INPUT_DIR   default /data/input  - a job's path (and input_root) must lie under it
  OUTPUT_DIR  default /data/output - a job's output_dir must lie under it
  WORKER_PREWARM  default 1; 0 skips the OCR warm-up page (the first job pays it)
"""
import os

from rq import Queue, SimpleWorker
from rq.job import Job
from rq.serializers import JSONSerializer

import tasks


class JobRefused(Exception):
    """A job this service did not enqueue: run nothing, fail it."""


class GuardedJob(Job):
    """A Job whose callbacks and webhooks never run. RQ imports a callback by
    the name stored in the job and calls it - from execute_job, but also from
    the started-job registry's cleanup, which check() never sees - and it sends
    a job's webhooks when it fails it, refused or not. The service uses
    neither, so here neither exists."""

    @property
    def success_callback(self):
        return None

    @property
    def failure_callback(self):
        return None

    @property
    def stopped_callback(self):
        return None

    def execute_success_callback(self, *args, **kwargs):
        return None

    def execute_failure_callback(self, *args, **kwargs):
        return None

    def execute_stopped_callback(self, *args, **kwargs):
        return None

    def send_webhooks(self, *args, **kwargs):
        return None


def _root(var: str, default: str) -> str:
    return os.path.realpath(os.environ.get(var, default))


def _under(path: str, root: str) -> bool:
    real = os.path.realpath(path)
    return real == root or real.startswith(root + os.sep)


def check(job) -> None:
    """Raise JobRefused unless the job is one the API or enqueue_batch.py made."""
    try:
        func_name = job.func_name            # deserializes: a pickled job fails here
        args, kwargs = job.args, job.kwargs
    except Exception as exc:  # noqa: BLE001 - unreadable is refused, whatever the reason
        raise JobRefused(f"cannot read the job ({type(exc).__name__})") from exc
    if func_name != tasks.JOB_FUNC:
        raise JobRefused(f"names {func_name!r}, not {tasks.JOB_FUNC}")
    if len(args) != 2 or not isinstance(args[0], str) or not isinstance(args[1], dict):
        raise JobRefused("arguments are not a path and an options dict")
    if set(kwargs) - {"input_root"} or not isinstance(kwargs.get("input_root"), (str, type(None))):
        raise JobRefused("keyword arguments other than input_root")
    for which in ("success", "failure", "stopped"):
        if getattr(job, f"_{which}_callback_name", None):
            raise JobRefused(f"names a {which} callback")
    if getattr(job, "webhooks", None):
        raise JobRefused("carries webhooks")
    if getattr(job, "_dependency_ids", None):
        raise JobRefused("depends on other jobs")
    # The API confines these already; a job that reached Redis another way
    # must not choose what the worker reads or where it writes.
    inputs, outputs = _root("INPUT_DIR", "/data/input"), _root("OUTPUT_DIR", "/data/output")
    if not _under(args[0], inputs):
        raise JobRefused(f"path outside {inputs}")
    if kwargs.get("input_root") and not _under(kwargs["input_root"], inputs):
        raise JobRefused(f"input_root outside {inputs}")
    out_dir = args[1].get("output_dir")
    if out_dir is not None and (not isinstance(out_dir, str) or not _under(out_dir, outputs)):
        raise JobRefused(f"output_dir outside {outputs}")
    try:
        tasks.validate_options(args[1])
    except tasks.InvalidOption as exc:
        raise JobRefused(f"options: {exc}") from exc


class GuardedWorker(SimpleWorker):
    """SimpleWorker - no fork, so the models load once - running only tasks.JOB_FUNC."""

    serializer = JSONSerializer

    def __init__(self, queues, *args, **kwargs):
        kwargs["job_class"] = GuardedJob
        super().__init__(queues, *args, **kwargs)
        # The registries' cleanup loads jobs with each queue's job_class: set
        # here, so the guard holds however the queues were built.
        for q in self.queues:
            q.job_class = GuardedJob

    def execute_job(self, job, queue):
        try:
            check(job)
        except JobRefused as exc:
            self.log.error("refused job %s: %s", job.id, exc)
            self.handle_job_failure(job, queue, exc_string=f"refused: {exc}")
            return
        super().execute_job(job, queue)


def main():
    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    queue_name = os.environ.get("MARKER_QUEUE", "marker")

    # Warm the models before taking jobs (SimpleWorker runs jobs in-process).
    print(f"[worker] loading marker models …", flush=True)
    tasks.get_models()
    print("[worker] warming up: one OCR'd page with a table (torch, llama-server, tables) …",
          flush=True)
    took = tasks.prewarm()
    if took is not None:
        print(f"[worker] warm in {took:.1f}s", flush=True)
    print(f"[worker] ready — consuming '{queue_name}' from {redis_url}", flush=True)

    conn = tasks.redis_connection(redis_url)
    queue = Queue(queue_name, connection=conn, serializer=JSONSerializer, job_class=GuardedJob)
    GuardedWorker([queue], connection=conn, serializer=JSONSerializer).work()


if __name__ == "__main__":
    main()
