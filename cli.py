"""One-shot marker runs inside the image: `convert` and `bench`.

`protocol-droid.sh container ...` runs this in the service image, so marker
needs no pipx, no particular Python and nothing installed on the host. Models
load ONCE per run (tasks.get_models), then every file goes through
tasks.convert_document - the same function the queued worker uses, so a
container conversion writes the same folders and the same provenance sidecars.

  python cli.py convert [--output-format F] [--output-dir D] [--page-range R]
                        [--force-ocr] FILE...
  python cli.py bench   [--output-dir D] [--runs N] [--report FILE] FILE...

`bench` answers the question the converter-services report could not settle
by reasoning: what does keeping the models resident save? It times the model
load separately from each conversion, so the two costs a per-file CLI pays and
a resident worker does not can be read off directly. It prints one JSON report
(and writes it to --report when given).

Exit codes match the bash backends: 0 all converted, 1 any conversion failed,
2 an input was missing.
"""
import argparse
import json
import os
import platform
import sys
import tempfile
import time

import tasks
from provenance import tool_versions

EX_OK, EX_FAILED, EX_NO_INPUT = 0, 1, 2
BENCH_SCHEMA = "protocol-droid/bench/1"


def _options(args) -> dict:
    opts = {"output_format": args.output_format, "output_dir": args.output_dir}
    if args.page_range:
        opts["page_range"] = args.page_range
    if args.force_ocr:
        opts["force_ocr"] = True
    return tasks.validate_options(opts)


def _existing(files: list[str]) -> tuple[list[str], int]:
    present = [f for f in files if os.path.isfile(f)]
    for f in files:
        if f not in present:
            print(f"[warn]  not a file: {f}", file=sys.stderr)
    return present, len(files) - len(present)


def _pages(path: str):
    """Page count of a PDF, or None for anything else (or if it cannot be read)."""
    if not path.lower().endswith(".pdf"):
        return None
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(path)
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:  # noqa: BLE001 - a page count is a nicety, never a failure
        return None


def cmd_convert(args) -> int:
    try:
        opts = _options(args)
    except tasks.InvalidOption as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return EX_FAILED
    files, missing = _existing(args.files)
    failed = 0
    for f in files:
        try:
            out = tasks.convert_document(f, opts, input_root=args.input_root)
            print(f"[ok]    {f} -> {out}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the batch
            failed += 1
            print(f"[error] {f}: {type(exc).__name__}: {exc}", file=sys.stderr)
    if failed:
        return EX_FAILED
    return EX_NO_INPUT if missing else EX_OK


def _host() -> dict:
    info = {
        "machine": platform.machine(),
        "system": platform.system(),
        "cpus_visible": os.cpu_count(),
        "python": platform.python_version(),
    }
    try:
        import torch
        info["torch_threads"] = torch.get_num_threads()
        info["cuda"] = bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        pass
    try:
        from marker.settings import settings
        info["torch_device"] = str(settings.TORCH_DEVICE_MODEL)
    except Exception:  # noqa: BLE001
        pass
    # The host label is the caller's (protocol-droid.sh passes the real host
    # name in): inside a container, platform.node() is the container's ID.
    if os.environ.get("BENCH_HOST"):
        info["host"] = os.environ["BENCH_HOST"]
    return info


def cmd_bench(args) -> int:
    files, missing = _existing(args.files)
    if not files:
        print("[error] nothing to measure", file=sys.stderr)
        return EX_NO_INPUT
    out_dir = args.output_dir or tempfile.mkdtemp(prefix="pd-bench-")
    opts = tasks.validate_options({"output_format": "markdown", "output_dir": out_dir})

    started = time.perf_counter()
    tasks.get_models()
    load = time.perf_counter() - started

    docs, failed = [], 0
    for run in range(1, args.runs + 1):
        for f in files:
            t0 = time.perf_counter()
            try:
                tasks.convert_document(f, opts)
                ok, error = True, None
            except Exception as exc:  # noqa: BLE001
                ok, error = False, f"{type(exc).__name__}: {exc}"
                failed += 1
            docs.append({"file": os.path.basename(f), "run": run, "pages": _pages(f),
                         "seconds": round(time.perf_counter() - t0, 3), "ok": ok, "error": error})

    good = [d for d in docs if d["ok"]]
    seconds = sum(d["seconds"] for d in good)
    pages = sum(d["pages"] or 0 for d in good)
    per_doc = seconds / len(good) if good else None
    report = {
        "schema": BENCH_SCHEMA,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": _host(),
        "versions": tool_versions(),
        "model_load_seconds": round(load, 3),
        "documents": docs,
        "warm": {
            "documents": len(good),
            "pages": pages,
            "seconds": round(seconds, 3),
            "seconds_per_document": round(per_doc, 3) if per_doc else None,
            "seconds_per_page": round(seconds / pages, 3) if pages else None,
            "pages_per_minute": round(pages * 60 / seconds, 2) if pages and seconds else None,
        },
        # What a per-file CLI pays on every file and a resident worker pays once.
        "resident_saving": {
            "per_document_seconds": round(load, 3),
            "cli_per_document_seconds": round(load + per_doc, 3) if per_doc else None,
            "speedup": round((load + per_doc) / per_doc, 2) if per_doc else None,
        },
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    if failed:
        return EX_FAILED
    return EX_NO_INPUT if missing else EX_OK


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="cli.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    conv = sub.add_parser("convert", help="convert files with marker, models loaded once")
    conv.add_argument("--output-format", default="markdown")
    conv.add_argument("--output-dir", default=os.environ.get("OUTPUT_DIR", "/data/output"))
    conv.add_argument("--page-range")
    conv.add_argument("--force-ocr", action="store_true")
    conv.add_argument("--input-root", help="mirror the input tree under this folder")
    conv.add_argument("files", nargs="+")
    conv.set_defaults(func=cmd_convert)

    bench = sub.add_parser("bench", help="time the model load and each conversion")
    bench.add_argument("--output-dir", help="where conversions go (default: a temp folder)")
    bench.add_argument("--runs", type=int, default=1, help="convert the set this many times")
    bench.add_argument("--report", help="also write the JSON report here")
    bench.add_argument("files", nargs="+")
    bench.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    if getattr(args, "runs", 1) < 1:
        parser.error("--runs must be at least 1")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
