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
from provenance import provenance_record, write_provenance

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
