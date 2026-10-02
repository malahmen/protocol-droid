"""marker conversion task (RQ job function).

Grounded on marker's own `convert_single_cli`: load the model dict ONCE per
worker process and reuse it across every job (the CLI reloads it per file — the
whole point of a long-lived worker is to avoid that). Run with an RQ
*SimpleWorker* (no fork) so the loaded models — and any GPU/CUDA context — are
reused safely across jobs.
"""
import os

# Match marker's CLI environment (quiet gRPC/glog, MPS fallback for Macs).
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")
os.environ.setdefault("GLOG_minloglevel", "2")
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")


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
    config_parser = ConfigParser(options or {})

    converter_cls = config_parser.get_converter_cls()
    converter = converter_cls(
        config=config_parser.generate_config_dict(),
        artifact_dict=models,
        processor_list=config_parser.get_processors(),
        renderer=config_parser.get_renderer(),
        llm_service=config_parser.get_llm_service(),
    )
    rendered = converter(fpath)
    out_dir = (options or {}).get("output_dir") or os.environ.get("OUTPUT_DIR", "/data/output")
    out_folder = output_folder(fpath, out_dir, input_root)
    save_output(rendered, out_folder, config_parser.get_base_filename(fpath))
    return out_folder
