"""marker RQ worker launcher.

Uses RQ's SimpleWorker (no per-job fork) so marker's models load once and are
reused across jobs — and so a GPU/CUDA context isn't corrupted by forking.
Warms up at startup so the first job isn't slow: the models, then one OCR'd
page with a table, which pays torch's warm-up, starts llama-server and runs the
table path once (tasks.prewarm).

Env:
  REDIS_URL   default redis://localhost:6379/0
  REDIS_PASSWORD  Redis AUTH password (unset = no AUTH)
  MARKER_QUEUE default "marker"
  WORKER_PREWARM  default 1; 0 skips the OCR warm-up page (the first job pays it)
"""
import os

from rq import Queue, SimpleWorker

import tasks


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
    queue = Queue(queue_name, connection=conn)
    SimpleWorker([queue], connection=conn).work()


if __name__ == "__main__":
    main()
