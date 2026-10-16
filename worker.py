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

Env:
  REDIS_URL   default redis://localhost:6379/0
  REDIS_PASSWORD  Redis AUTH password (unset = no AUTH)
  MARKER_QUEUE default "marker"
  WORKER_PREWARM  default 1; 0 skips the OCR warm-up page (the first job pays it)
"""
import os

from rq import Queue, SimpleWorker
from rq.serializers import JSONSerializer

import tasks


class JobRefused(Exception):
    """A job this service did not enqueue: run nothing, fail it."""


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


class GuardedWorker(SimpleWorker):
    """SimpleWorker - no fork, so the models load once - running only tasks.JOB_FUNC."""

    serializer = JSONSerializer

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
    queue = Queue(queue_name, connection=conn, serializer=JSONSerializer)
    GuardedWorker([queue], connection=conn, serializer=JSONSerializer).work()


if __name__ == "__main__":
    main()
