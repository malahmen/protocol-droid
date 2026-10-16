"""marker conversion task (RQ job function).

Grounded on marker's own `convert_single_cli`: load the model dict ONCE per
worker process and reuse it across every job (the CLI reloads it per file — the
whole point of a long-lived worker is to avoid that). Run with an RQ
*SimpleWorker* (no fork) so the loaded models — and any GPU/CUDA context — are
reused safely across jobs.
"""
import os
import re
import time

# Provenance lives in its own module because the bash backends run it as a
# script (`python3 provenance.py`), so the local, container and service
# execution modes all emit the SAME sidecar from the same code.
from provenance import PROVENANCE_FILE, provenance_record, write_provenance

# Match marker's CLI environment (quiet gRPC/glog, MPS fallback for Macs).
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


# --- option validation -------------------------------------------------------
#
# Checked here rather than only in api.py so that every route into the queue is
# covered: the API, enqueue_batch.py, and anything that enqueues
# convert_document directly. The API turns these into 400s.
#
# page_range is CAPPED, not just parsed. marker expands the range eagerly, so
# "0-999999999" parses fine and then builds a list of a billion page numbers:
# the worker was OOM-killed (137). SimpleWorker runs jobs in-process, which is
# what makes that fatal — the worker dies WITH the job instead of failing it,
# taking every queued job's processing with it. A rejected request costs one
# 400; an accepted one costs the service.
PAGE_RANGE_MAX_PAGES = int(os.environ.get("PAGE_RANGE_MAX_PAGES", "2000"))
PAGE_RANGE_MAX_INDEX = int(os.environ.get("PAGE_RANGE_MAX_INDEX", "100000"))
OUTPUT_FORMATS = ("markdown", "json", "html", "chunks")

# [0-9]+ and not str.isdigit(): isdigit() is true for '²' and other Unicode
# digit characters that int() then refuses, which would turn a bad request into
# an unhandled ValueError.
_NUM_RE = re.compile(r"[0-9]+\Z")


class InvalidOption(ValueError):
    """An option this service will not pass on to marker."""


def validate_output_format(fmt: str) -> str:
    if fmt not in OUTPUT_FORMATS:
        raise InvalidOption("output_format must be one of " + ", ".join(OUTPUT_FORMATS))
    return fmt


def validate_page_range(spec: str) -> str:
    """Normalise and cap a marker page_range such as "0,5-10".

    0-indexed and inclusive, matching marker. Returns the normalised spec.
    """
    text = (spec or "").strip()
    if not text:
        raise InvalidOption("page_range is empty")
    total = 0
    parts = []
    for raw in text.split(","):
        item = raw.strip()
        if not item:
            raise InvalidOption("page_range has an empty element")
        lo_s, sep, hi_s = item.partition("-")
        if not _NUM_RE.match(lo_s) or (sep and not _NUM_RE.match(hi_s)):
            raise InvalidOption(f"page_range element {item!r} is not a page or a page range")
        lo = int(lo_s)
        hi = int(hi_s) if sep else lo
        if hi < lo:
            raise InvalidOption(f"page_range element {item!r} counts backwards")
        if hi > PAGE_RANGE_MAX_INDEX:
            raise InvalidOption(
                f"page_range element {item!r} is past the maximum page index "
                f"{PAGE_RANGE_MAX_INDEX}")
        total += hi - lo + 1
        if total > PAGE_RANGE_MAX_PAGES:
            raise InvalidOption(
                f"page_range covers more than {PAGE_RANGE_MAX_PAGES} pages")
        parts.append(f"{lo}-{hi}" if sep else str(lo))
    return ",".join(parts)


# --- the LLM marker talks to, when use_llm is set ---------------------------
#
# Without this, `use_llm` meant Google Gemini: marker's ConfigParser falls back
# to "marker.services.gemini.GoogleGeminiService" when no llm_service is given,
# so a job on this LAN reached for an internet API and a key nobody had set.
# The point of the option here is to use OUR model.
#
# Two deliberate restrictions.
#
# The service is chosen from a FIXED MAP, never from a caller-supplied string.
# marker's `llm_service` option is a dotted class path that it imports, so
# passing one through from a request would be an arbitrary-class import.
#
# And it is DEPLOYMENT configuration — environment, read by the worker — not a
# per-request field. A request may only say use_llm true or false. A caller who
# could name the base URL could point the worker at any host it can reach,
# which is a request-forgery primitive, and the endpoint is a property of where
# the service runs rather than of one document.
#
# Only the two services that can address a LAN endpoint are wired up, with
# their option names taken from marker 2.0.0 itself rather than guessed:
#   ollama -> ollama_base_url (default http://localhost:11434), ollama_model
#   openai -> openai_base_url (default https://api.openai.com/v1), openai_model,
#             openai_api_key  — "openai" here means OpenAI-COMPATIBLE, which is
#             what llama.cpp's server, vLLM, LM Studio and LocalAI all speak.
# The others marker ships (gemini, claude, azure_openai, vertex, openrouter)
# are reachable only by naming them, and are left out until their option names
# are verified the same way.
LLM_SERVICES = {
    "ollama": "marker.services.ollama.OllamaService",
    "openai": "marker.services.openai.OpenAIService",
}
LLM_DEFAULT_BASE_URL = {
    "ollama": "http://localhost:11434",
    "openai": "http://localhost:8080/v1",
}


def llm_config() -> dict:
    """marker options for the configured LLM, or {} when none is configured.

    Raises InvalidOption when LLM_SERVICE names something unsupported, so a
    typo fails the job with a message instead of quietly falling back to an
    internet API.
    """
    name = (os.environ.get("LLM_SERVICE") or "").strip().lower()
    if not name:
        return {}
    if name not in LLM_SERVICES:
        raise InvalidOption(
            f"LLM_SERVICE={name!r} is not supported; use one of: "
            + ", ".join(sorted(LLM_SERVICES))
        )
    base_url = (os.environ.get("LLM_BASE_URL") or LLM_DEFAULT_BASE_URL[name]).rstrip("/")
    model = (os.environ.get("LLM_MODEL") or "").strip()
    cfg = {"llm_service": LLM_SERVICES[name], f"{name}_base_url": base_url}
    if model:
        cfg[f"{name}_model"] = model
    if name == "openai":
        # An OpenAI-compatible server usually ignores the key but the client
        # still requires one to be present.
        cfg["openai_api_key"] = os.environ.get("LLM_API_KEY") or "not-needed"
    return cfg


def llm_config_or_die() -> dict:
    """llm_config(), but an unconfigured LLM is an error rather than {}.

    Separate from llm_config so the API can reject a use_llm request up front
    while the worker keeps its own check — a job that reaches the queue by
    another route must not bypass it either.
    """
    cfg = llm_config()
    if not cfg:
        raise InvalidOption(
            "use_llm was requested but no LLM is configured: set LLM_SERVICE "
            "(" + ", ".join(sorted(LLM_SERVICES)) + ") and LLM_BASE_URL. "
            "Without it marker would reach for Google Gemini and an API key "
            "this deployment does not have."
        )
    return cfg


def validate_options(options: dict | None) -> dict:
    """A copy of `options` with the two caller-supplied knobs checked."""
    opts = dict(options or {})
    if "output_format" in opts:
        validate_output_format(opts["output_format"])
    if opts.get("page_range"):
        opts["page_range"] = validate_page_range(opts["page_range"])
    if opts.get("use_llm"):
        # Deployment config wins: a request cannot name the service, the model
        # or the endpoint.
        opts.update(llm_config_or_die())
    return opts



# --- how long one job may run ------------------------------------------------
#
# RQ kills a job at its timeout. With SimpleWorker that timeout is two things
# at once: the cap on a wedged job, and the ceiling on the largest document the
# service can convert at all. One fixed number cannot be both. It was 3600s,
# and on corelia (32 threads, no GPU; measured 2026-10-16) OCR runs at
# 54.7 s/page - so every scanned PDF over ~66 pages was killed part-way through,
# while a hung three-page job still held its worker for the full hour.
#
# So each job is sized from what it will actually convert: a base for the work
# a job can owe once (a cold model cache downloads the OCR weights inside the
# first job, which took minutes; a fresh process costs ~28s before its first
# page even with the cache warm) plus a budget per page, ~3x the measured OCR
# rate, for slower nodes and a busy host (a game running beside the
# measurement cost 1.12x). Pages are the PDF's page count, cut
# to the page_range when one is given; an image is one page. When the count
# cannot be known (an office document, a file the API cannot read) the job
# gets JOB_TIMEOUT_UNKNOWN. use_llm adds LLM calls per page that were never
# measured; JOB_TIMEOUT_PER_PAGE is the knob if they turn out to matter.
#
# JOB_TIMEOUT, when set, still fixes every job's timeout: an operator who chose
# a number keeps it.
_FIXED = os.environ.get("JOB_TIMEOUT", "").strip()
JOB_TIMEOUT = int(_FIXED) if _FIXED else None
JOB_TIMEOUT_BASE = int(os.environ.get("JOB_TIMEOUT_BASE", "1800"))
JOB_TIMEOUT_PER_PAGE = int(os.environ.get("JOB_TIMEOUT_PER_PAGE", "180"))
JOB_TIMEOUT_UNKNOWN = int(os.environ.get("JOB_TIMEOUT_UNKNOWN", "21600"))
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp", ".gif", ".bmp"}


def page_count(fpath: str) -> int | None:
    """Pages in a PDF, 1 for an image, None when it cannot be known.

    pypdfium2 is a marker dependency, so this counts in every role that runs
    the service image; anywhere it is missing the answer is simply None.
    """
    ext = os.path.splitext(fpath)[1].lower()
    if ext in IMAGE_EXTS:
        return 1
    if ext != ".pdf":
        return None
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(fpath)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - a count only sizes a timeout; never fail a request over it
        return None


def _page_range_size(spec: str) -> int:
    """Pages covered by a page_range already through validate_page_range."""
    total = 0
    for item in spec.split(","):
        lo, _, hi = item.partition("-")
        total += (int(hi) if hi else int(lo)) - int(lo) + 1
    return total


def job_pages(fpath: str, options: dict | None = None) -> int | None:
    """How many pages this job will convert, or None if that cannot be known."""
    pages = page_count(fpath)
    spec = (options or {}).get("page_range")
    if spec:
        in_range = _page_range_size(validate_page_range(spec))
        pages = in_range if pages is None else min(pages, in_range)
    return pages


def job_timeout(fpath: str, options: dict | None = None) -> int:
    """Seconds RQ lets this job run (see the comment above)."""
    if JOB_TIMEOUT is not None:
        return JOB_TIMEOUT
    pages = job_pages(fpath, options)
    if pages is None:
        return JOB_TIMEOUT_UNKNOWN
    return JOB_TIMEOUT_BASE + pages * JOB_TIMEOUT_PER_PAGE

def redis_connection(url: str | None = None):
    """The Redis connection every role uses (api, worker, batch enqueuer).

    The password comes from REDIS_PASSWORD rather than the URL, so it needs no
    URL-encoding and never shows up in a logged REDIS_URL. Unset means no AUTH.
    """
    from redis import Redis
    return Redis.from_url(url or os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
                          password=os.environ.get("REDIS_PASSWORD") or None)

_models = None


def get_models():
    """Load and cache marker's model dict once per process."""
    global _models
    if _models is None:
        from marker.models import create_model_dict
        _models = create_model_dict()
    return _models



# --- warming a worker up -----------------------------------------------------
#
# get_models() loads the layout model and nothing else (3.6s on corelia). The
# first conversion in a process then pays torch's warm-up, the first one that
# OCRs anything starts surya's llama-server and loads the OCR weights, and the
# first table has a first-use cost of its own. Measured 2026-10-16: one scanned
# page took 25.5s as a fresh worker's first job and 15.2s after a warm-up page
# (12.8s for the next page either way); a 30-page manual with tables took
# 111.4s first after a warm-up page of prose, against ~97s on repeats, and
# 92.3s once the warm-up page carried a table. The worker called get_models()
# alone and said the first job would not be slow; it was.
#
# prewarm() converts one generated page - prose and a ruled table, no text
# layer - with OCR forced, so all of it is paid at startup. On a cold model
# cache it is also what downloads the OCR weights, which then no longer happens
# inside a caller's first job and against that job's timeout. A failure is reported, never raised: a worker that could
# not warm up still takes jobs, it is only slow on the first one.
# WORKER_PREWARM=0 skips it.
_PREWARM_TEXT = (
    "protocol-droid worker warm-up",
    "One page of generated text, converted with OCR forced,",
    "so the first real job does not start llama-server.",
)


def _text_page_pdf(path: str) -> None:
    """Write a one-page, image-only PDF of a few lines of text (no text layer)."""
    from PIL import Image, ImageDraw, ImageFont
    try:
        from marker.settings import settings
        font = ImageFont.truetype(settings.FONT_PATH, 40)
    except Exception:  # noqa: BLE001 - any legible font will do
        font = ImageFont.load_default()
    page = Image.new("RGB", (1240, 1754), "white")  # A4 at 150 dpi
    draw = ImageDraw.Draw(page)
    for i, line in enumerate(_PREWARM_TEXT):
        draw.text((100, 150 + 80 * i), line, fill="black", font=font)
    # A ruled table too: the first table marker meets in a process has its own
    # first-use cost, which a page of prose leaves for the first real job.
    left, top, cols, rows, w, h = 100, 500, 4, 5, 260, 90
    for r in range(rows + 1):
        draw.line((left, top + r * h, left + cols * w, top + r * h), fill="black", width=3)
    for c in range(cols + 1):
        draw.line((left + c * w, top, left + c * w, top + rows * h), fill="black", width=3)
    for r in range(rows):
        for c in range(cols):
            cell = ("Item", "Pages", "Seconds", "Rate")[c] if r == 0 else f"{r * 7 + c * 3}"
            draw.text((left + c * w + 20, top + r * h + 22), cell, fill="black", font=font)
    page.save(path, "PDF", resolution=150)


def prewarm() -> float | None:
    """Pay the first-conversion costs now. Seconds taken, or None if skipped or failed."""
    if os.environ.get("WORKER_PREWARM", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    import shutil
    import sys
    import tempfile
    tmp = tempfile.mkdtemp(prefix="pd-prewarm-")
    try:
        pdf = os.path.join(tmp, "prewarm.pdf")
        _text_page_pdf(pdf)
        started = time.perf_counter()
        convert_document(pdf, {"output_format": "markdown", "output_dir": tmp, "force_ocr": True})
        return time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001 - see above: report, keep serving
        print(f"[worker] warm-up failed ({type(exc).__name__}: {exc}); "
              "the first job will pay it instead", file=sys.stderr, flush=True)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

def output_folder(fpath: str, output_dir: str, input_root: str | None = None) -> str:
    """Where one document's output goes: <output_dir>/<relative dir>/<stem>_<ext>/.

    marker's own get_output_folder uses <output_dir>/<stem>/, so a/report.pdf,
    b/report.pdf and report.docx all land in one folder and silently overwrite
    each other in a recursive batch. Mirroring the input tree (relative to
    `input_root`) and keeping the extension in the folder name gives every input
    its own folder. Without `input_root`, or for a path outside it, only the
    file name is used.
    """
    name = os.path.basename(fpath)
    rel_dir = ""
    if input_root:
        # Resolve the directory only, so a symlinked file keeps its own name.
        root = os.path.realpath(input_root)
        real_dir = os.path.realpath(os.path.dirname(os.path.abspath(fpath)))
        if os.path.commonpath([root, real_dir]) == root:
            rel_dir = os.path.relpath(real_dir, root)
            if rel_dir == os.curdir:
                rel_dir = ""
    stem, ext = os.path.splitext(name)
    folder = f"{stem}_{ext[1:].lower()}" if ext else stem
    out = os.path.join(output_dir, rel_dir, folder)
    os.makedirs(out, exist_ok=True)
    return out


def convert_document(fpath: str, options: dict | None = None, input_root: str | None = None) -> str:
    """Convert one document with marker; returns the output folder.

    `options` mirrors marker_single's flags, e.g.:
      {"output_format": "markdown", "output_dir": "/data/output",
       "use_llm": True, "force_ocr": True, "page_range": "0,5-10"}
    `input_root` is the folder the job's path is relative to (the API's
    INPUT_DIR, or the folder enqueue_batch walked); the output mirrors the path
    under it. Jobs queued without it still work and use the file name alone.
    """
    from marker.config.parser import ConfigParser
    from marker.output import save_output

    if not os.path.exists(fpath):
        raise FileNotFoundError(fpath)

    models = get_models()
    # Re-checked in the worker, not only at the API: a job that reaches the
    # queue by another route must not be able to OOM this process either.
    # Kept in a local, not read back off the parser afterwards: marker's
    # ConfigParser stores it as `cli_options`, and reaching for an attribute
    # name that belongs to another project is how a provenance record quietly
    # becomes an AttributeError on the next marker bump.
    validated = validate_options(options)
    config_parser = ConfigParser(validated)

    converter_cls = config_parser.get_converter_cls()
    converter = converter_cls(
        config=config_parser.generate_config_dict(),
        artifact_dict=models,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )
    # Taken before the conversion, so `seconds` measures the conversion and not
    # the sidecar's own hashing.
    started = time.time()
    rendered = converter(fpath)
    out_dir = (options or {}).get("output_dir") or os.environ.get("OUTPUT_DIR", "/data/output")
    out_folder = output_folder(fpath, out_dir, input_root)
    save_output(rendered, out_folder, config_parser.get_base_filename(fpath))

    # The options RECORDED are the validated ones -- what actually ran, not what
    # was asked for. validate_options folds the deployment's LLM config in, and
    # caps page_range.
    #
    # An error here is deliberately not swallowed: the sidecar is what makes a
    # future re-conversion a diff instead of a corpus-wide re-run, and a
    # conversion with no sidecar is only discovered when that question is
    # finally asked. The rendered output is already on disk, so a failed job
    # here loses a retry, not the work.
    write_provenance(out_folder, provenance_record(
        fpath, out_folder, validated, started, time.time(), input_root))
    return out_folder


# --- the conversion API's converter port -------------------------------------
#
# protocol (the conversion API) runs this image as its marker worker and calls
# converters through one port: convert(data, original_name, options) ->
# (files, tool_versions) - bytes in, every output file as bytes out. It owns
# the stores, so a path in or a folder out means nothing to it.
#
# Each call converts in a folder of its own and removes it, under a name a path
# cannot steer (port.scratch, shared with markitdown's image). The caller's
# output_dir is ignored: where the files end up is the caller's store.
#
# The sidecar is NOT among the files. protocol writes its own provenance.json
# under the same key after the converter's files, so passing this one through
# would have it silently replaced. Its versions come back as tool_versions
# instead, read from the record written for this very conversion rather than
# looked up again.
def convert_bytes(data: bytes, original_name: str, options: dict | None = None):
    """Convert one document given as bytes: (files {relative name: bytes}, tool_versions)."""
    import json

    from port import scratch

    with scratch(data, original_name) as (tmp, src):
        opts = dict(options or {})
        opts["output_dir"] = os.path.join(tmp, "out")
        out_folder = convert_document(src, opts)

        files, versions = {}, {}
        for root, _dirs, names in os.walk(out_folder):
            for fname in sorted(names):
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, out_folder).replace(os.sep, "/")
                if rel == PROVENANCE_FILE:
                    with open(full, encoding="utf-8") as fh:
                        versions = dict(json.load(fh).get("versions") or {})
                    continue
                with open(full, "rb") as fh:
                    files[rel] = fh.read()
        return files, {k: str(v) for k, v in versions.items()}
