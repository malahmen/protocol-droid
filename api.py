"""protocol-droid conversion API — thin HTTP front door that enqueues jobs.

A caller (e.g. a downstream RAG pipeline) POSTs a document reference (a path
reachable by the workers, i.e. on the shared input volume/PVC) and polls for
status. Workers run marker to convert it to Markdown/JSON — preparing it for
ingestion; no chunking/embedding/indexing happens here. Heavy work is on the
workers, not in this process.

Env:
  REDIS_URL             default redis://localhost:6379/0
  MARKER_QUEUE          default "marker"
  INPUT_DIR             default /data/input  — `path` must live under it
  OUTPUT_DIR            default /data/output — `output_dir` must live under it
  JOB_TIMEOUT           default 3600 (seconds)
  RESULT_TTL            default 86400 (seconds) — how long results/failures stay in Redis
  PROTOCOL_DROID_TOKEN  when set, every request needs `Authorization: Bearer <token>`
                        (unset = open API: keep it bound to localhost, see docker-compose.yaml)
"""
import hmac
import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from redis import Redis
from redis.exceptions import RedisError
from rq import Queue
from rq.exceptions import NoSuchJobError
from rq.job import Job

import tasks

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
QUEUE_NAME = os.environ.get("MARKER_QUEUE", "marker")
INPUT_DIR = os.path.realpath(os.environ.get("INPUT_DIR", "/data/input"))
OUTPUT_DIR = os.path.realpath(os.environ.get("OUTPUT_DIR", "/data/output"))
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "3600"))
RESULT_TTL = int(os.environ.get("RESULT_TTL", "86400"))
API_TOKEN = os.environ.get("PROTOCOL_DROID_TOKEN", "")


def require_token(authorization: Optional[str] = Header(default=None)):
    """Shared-secret check; a no-op when PROTOCOL_DROID_TOKEN is unset."""
    if not API_TOKEN:
        return
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), API_TOKEN):
        raise HTTPException(401, "missing or invalid bearer token",
                            headers={"WWW-Authenticate": "Bearer"})


def confine(path: str, root: str, what: str) -> str:
    """Resolve `path` and require it to be `root` or inside it (no `..`/symlink escapes)."""
    real = os.path.realpath(path)
    if real != root and not real.startswith(root + os.sep):
        raise HTTPException(400, f"{what} must be under {root}")
    return real


app = FastAPI(title="protocol-droid conversion API", dependencies=[Depends(require_token)])
_conn = Redis.from_url(REDIS_URL)
_queue = Queue(QUEUE_NAME, connection=_conn, default_timeout=JOB_TIMEOUT)


class JobIn(BaseModel):
    path: str                                  # reachable by the workers (shared volume)
    output_format: str = "markdown"            # markdown | json | html | chunks
    output_dir: Optional[str] = None
    use_llm: bool = False
    force_ocr: bool = False
    page_range: Optional[str] = None


@app.get("/healthz")
def healthz():
    try:
        _conn.ping()
        return {"ok": True}
    except RedisError:
        raise HTTPException(503, "redis unavailable")


@app.post("/jobs")
def enqueue(body: JobIn):
    path = confine(body.path, INPUT_DIR, "path")
    output_dir = confine(body.output_dir or OUTPUT_DIR, OUTPUT_DIR, "output_dir")
    opts = {
        "output_format": body.output_format,
        "output_dir": output_dir,
    }
    if body.use_llm:
        opts["use_llm"] = True
    if body.force_ocr:
        opts["force_ocr"] = True
    if body.page_range:
        opts["page_range"] = body.page_range

    try:
        job = _queue.enqueue(tasks.convert_document, path, opts,
                             result_ttl=RESULT_TTL, failure_ttl=RESULT_TTL)
    except RedisError:
        raise HTTPException(503, "redis unavailable")
    return {"job_id": job.id, "status": job.get_status()}


@app.get("/jobs/{job_id}")
def job_status(job_id: str):
    try:
        job = Job.fetch(job_id, connection=_conn)
    except NoSuchJobError:
        raise HTTPException(404, "job not found")
    except RedisError:
        raise HTTPException(503, "redis unavailable")
    return {
        "job_id": job.id,
        "status": job.get_status(),
        "result": job.result,
        # Never echo the traceback (paths, internals) to callers — it is in the worker logs.
        "error": "conversion failed — see worker logs" if job.is_failed else None,
    }
