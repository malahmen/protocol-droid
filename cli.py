"""One-shot marker runs inside the image: `convert` and `bench`.

`protocol-droid.sh container ...` runs this in the service image, so marker
needs no pipx, no particular Python and nothing installed on the host. Models
load ONCE per run (tasks.get_models), then every file goes through
tasks.convert_document - the same function the queued worker uses, so a
container conversion writes the same folders and the same provenance sidecars.

  python cli.py convert [--output-format F] [--output-dir D] [--page-range R]
                        [--force-ocr] FILE...
  python cli.py bench   [--output-dir D] [--runs N] [--report FILE]
                        [--page-range R] [--force-ocr] [--no-cold] FILE...

`bench` answers what keeping the models resident saves, per document, and
what each kind of page costs. Schema protocol-droid/bench/2 (Kamino items 64
and 65). The first version timed only tasks.get_models() as the overhead -
3.6s, where a per-file CLI really pays ~28s once OCR is involved - and on that
gave a wrong answer to a gating decision; and it averaged text-layer and OCR
pages together, which differ ~18x. So:

  - the per-invocation overhead is MEASURED, not inferred: for each path (text
    layer, OCR), page 0 of one PDF is converted in fresh processes and again
    in this one once it is warm, and the overhead is the difference. That is
    what a per-file CLI pays - imports, models, warm-up, and llama-server once
    anything is OCR'd - short of the container start, which a container per
    file adds on top;
  - the timed runs start warm (tasks.prewarm first, timed on its own), so no
    document carries the process's first-conversion costs;
  - `warm` is reported per path, never blended, and the resident saving per
    document, against its own path's overhead.

It prints one JSON report (and writes it to --report when given).

Exit codes match the bash backends: 0 all converted, 1 any conversion failed,
2 an input was missing.
"""
import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time

import tasks
from provenance import tool_versions

EX_OK, EX_FAILED, EX_NO_INPUT = 0, 1, 2
BENCH_SCHEMA = "protocol-droid/bench/2"


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



IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".tiff", ".tif", ".webp", ".gif", ".bmp")
_TEXT_LAYER_MIN_CHARS = 50     # a scan's stray OCR'd header is not a text layer
_SAMPLE_PAGES = 3


def _range_indices(spec: str) -> list[int]:
    out = []
    for item in spec.split(","):
        lo, _, hi = item.partition("-")
        out.extend(range(int(lo), (int(hi) if hi else int(lo)) + 1))
    return out


def _facts(path: str, page_range: str | None = None, force_ocr: bool = False):
    """(pages that will be converted, path) - path is "ocr", "text" or None (unknown).

    A page is OCR'd when the request forces it, when the file is an image, or
    when the PDF has no text layer (read on its first pages, as protocol's
    cost estimate reads it).
    """
    if path.lower().endswith(IMAGE_EXTS):
        return 1, "ocr"
    if not path.lower().endswith(".pdf"):
        return None, ("ocr" if force_ocr else None)
    try:
        import pypdfium2
        doc = pypdfium2.PdfDocument(path)
    except Exception:  # noqa: BLE001 - facts only label a measurement
        return _pages(path), ("ocr" if force_ocr else None)
    try:
        total = len(doc)
        pages = sorted(i for i in set(_range_indices(page_range)) if i < total) if page_range \
            else list(range(total))
        if force_ocr:
            return len(pages), "ocr"
        chars = 0
        for i in pages[:_SAMPLE_PAGES]:
            page = doc[i]
            try:
                text = page.get_textpage()
                try:
                    chars += len(text.get_text_range().strip())
                finally:
                    text.close()
            finally:
                page.close()
        return len(pages), ("text" if chars >= _TEXT_LAYER_MIN_CHARS else "ocr")
    finally:
        doc.close()


def _clamp(spec: str, total: int) -> str | None:
    """`spec` cut to the pages a document of `total` pages has; None if none are left.

    marker refuses a range past the end ("values must be between 0 and 2"), so
    one --page-range for a whole bench set has to be cut to each document.
    """
    keep = sorted(i for i in set(_range_indices(spec)) if i < total)
    if not keep:
        return None
    runs, lo = [], keep[0]
    for prev, cur in zip(keep, keep[1:] + [None]):
        if cur != prev + 1:
            runs.append(str(lo) if lo == prev else f"{lo}-{prev}")
            lo = cur
    return ",".join(runs)


def _cold_run(path: str, page_range: str, out_dir: str, force_ocr: bool) -> float:
    """Wall seconds for one `convert` in a fresh process: what a per-file CLI pays."""
    cmd = [sys.executable, os.path.abspath(__file__), "convert", "--page-range", page_range,
           "--output-dir", out_dir]
    if force_ocr:
        cmd.append("--force-ocr")
    started = time.perf_counter()
    res = subprocess.run(cmd + [path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    took = time.perf_counter() - started
    if res.returncode != 0:
        raise RuntimeError(f"cold convert of {os.path.basename(path)} failed ({res.returncode})")
    return took


def _cold_samples(path: str, out_dir: str, force_ocr: bool) -> dict:
    """Page 0 converted in fresh processes: what a per-file CLI pays for it.

    A throwaway run primes the page cache first: the first fresh process reads
    the models from disk and later ones find them cached, so whichever ran
    first looked dear (seen: 14.7s for one page, 5.5s for two).
    """
    def run(n):
        return _cold_run(path, "0", os.path.join(out_dir, f"cold-{n}"), force_ocr)
    return {"from": os.path.basename(path), "prime_seconds": round(run(0), 3),
            "cold_seconds": [round(run(1), 3), round(run(2), 3)]}


def _overhead(cold: dict, warm: float) -> dict:
    """What a fresh process pays for a page beyond what a warm one pays for the same page.

    Measured on one page directly, cold against warm, rather than inferred from
    one page against two (2A - B): that assumed two pages cost the same, which
    on light pages is far below the noise. Where cold comes out no dearer than
    warm, the samples are not measuring the overhead: reported as unknown,
    never as a number.

    `warm` is ONE conversion of the page, after the timed runs and after a
    different OCR'd page, with llama-server's prompt cache off (NO_RECALL): a
    repeated page otherwise came back in 3.0s against 15.1s, and averaging that
    in made the overhead read 22.9s.
    """
    out = dict(cold, warm_seconds=round(warm, 3))
    a, w = sum(cold["cold_seconds"]) / len(cold["cold_seconds"]), warm
    out["seconds"] = round(a - w, 3)
    if a <= w:
        out["seconds"] = None
        out["why"] = "a fresh process was no slower than a warm one: these samples do not measure the overhead"
    return out

def _path_summary(docs: list[dict]) -> dict:
    seconds = sum(d["seconds"] for d in docs)
    pages = sum(d["pages"] or 0 for d in docs)
    return {
        "documents": len(docs),
        "pages": pages,
        "seconds": round(seconds, 3),
        "seconds_per_page": round(seconds / pages, 3) if pages else None,
        "pages_per_minute": round(pages * 60 / seconds, 2) if pages and seconds else None,
    }

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


# llama-server caches prompts per slot (--cache-prompt, on by default), so a
# page it has converted comes back from that cache: the same scanned page took
# 3.0s the second time against 15.1s the first, even with other pages in
# between. A bench that repeats pages - --runs, the overhead's warm sample -
# would time recall, not conversion. surya appends LLAMA_CPP_EXTRA_ARGS to the
# server's command line, so the bench passes --no-cache-prompt. Measured on
# corelia: with it, pages not seen before take the same time (14.9s / 12.8s
# against 15.1s / 12.9s) and the repeat takes 15.1s - nothing is lost but the
# recall. (--cache-ram 0, the host-RAM cache, was tried first and changed
# nothing: 3.0s.)
NO_RECALL = "--no-cache-prompt"


def _no_recall() -> str:
    """Set LLAMA_CPP_EXTRA_ARGS so llama-server recalls no earlier prompt; returns it."""
    extra = os.environ.get("LLAMA_CPP_EXTRA_ARGS", "")
    words = extra.split()
    if "--cache-prompt" not in words and "--no-cache-prompt" not in words:
        extra = f"{extra} {NO_RECALL}".strip()
        os.environ["LLAMA_CPP_EXTRA_ARGS"] = extra       # inherited by the cold runs
    return extra


def cmd_bench(args) -> int:
    files, missing = _existing(args.files)
    if not files:
        print("[error] nothing to measure", file=sys.stderr)
        return EX_NO_INPUT
    out_dir = args.output_dir or tempfile.mkdtemp(prefix="pd-bench-")
    llama_args = _no_recall()          # before anything starts a llama-server
    raw = {"output_format": "markdown", "output_dir": out_dir}
    if args.page_range:
        raw["page_range"] = args.page_range
    if args.force_ocr:
        raw["force_ocr"] = True
    opts = tasks.validate_options(raw)
    facts = {f: _facts(f, opts.get("page_range"), args.force_ocr) for f in files}

    # The overhead's cold half first, before this process loads anything (it
    # runs in fresh processes anyway): per path, page 0 of the first PDF.
    overhead, cold, picks = {}, {}, {}
    if not args.no_cold:
        for kind in ("text", "ocr"):
            pick = next((f for f in files if facts[f][1] == kind and f.lower().endswith(".pdf")), None)
            if pick is None:
                overhead[kind] = {"seconds": None, "why": f"no {kind} PDF in the set"}
                continue
            try:
                cold[kind], picks[kind] = _cold_samples(pick, os.path.join(out_dir, f"cold-{kind}"),
                                                        args.force_ocr), pick
            except RuntimeError as exc:
                overhead[kind] = {"seconds": None, "why": str(exc)}

    started = time.perf_counter()
    tasks.get_models()
    load = time.perf_counter() - started
    started = time.perf_counter()
    warmed = tasks.prewarm()
    warmup = time.perf_counter() - started


    # One --page-range for the set, cut to each document's own pages.
    per_doc = {}
    for f in files:
        doc_opts = dict(opts)
        if opts.get("page_range"):
            total = _pages(f)
            if total is not None:
                clamped = _clamp(opts["page_range"], total)
                if clamped is None:
                    doc_opts = None
                else:
                    doc_opts["page_range"] = clamped
        per_doc[f] = doc_opts

    docs, failed = [], 0
    for run in range(1, args.runs + 1):
        for f in files:
            t0 = time.perf_counter()
            try:
                if per_doc[f] is None:
                    raise ValueError(f"--page-range {opts['page_range']} is past the end of this document")
                tasks.convert_document(f, per_doc[f])
                ok, error = True, None
            except Exception as exc:  # noqa: BLE001
                ok, error = False, f"{type(exc).__name__}: {exc}"
                failed += 1
            pages, kind = facts[f]
            docs.append({"file": os.path.basename(f), "run": run, "pages": pages, "path": kind,
                         "seconds": round(time.perf_counter() - t0, 3), "ok": ok, "error": error})

    # The overhead's warm half, after the timed runs so none of them is served
    # a page from llama-server's prompt cache: the same page 0, here, warm,
    # once - straight after a different OCR'd page (prewarm's), so it is not
    # served from that cache either. Cold minus warm is the overhead.
    # Guarded: it comes after runs that may have taken hours, and one failed
    # sample must not lose them - the report says why instead (Kamino item 100).
    for kind, pick in picks.items():
        try:
            tasks.prewarm()
            page0 = dict(opts, page_range="0", output_dir=os.path.join(out_dir, f"warm-{kind}"))
            t0 = time.perf_counter()
            tasks.convert_document(pick, page0)
            overhead[kind] = _overhead(cold[kind], time.perf_counter() - t0)
        except Exception as exc:  # noqa: BLE001
            overhead[kind] = dict(cold[kind], seconds=None,
                                  why=f"the warm sample failed: {type(exc).__name__}: {exc}")

    good = [d for d in docs if d["ok"]]
    warm = {kind: _path_summary([d for d in good if d["path"] == kind])
            for kind in ("text", "ocr", None) if any(d["path"] == kind for d in good)}
    warm = {(k or "unknown"): v for k, v in warm.items()}

    # Per document: its mean warm time against its own path's overhead.
    saving = []
    for name in dict.fromkeys(d["file"] for d in good):
        runs = [d for d in good if d["file"] == name]
        mean = sum(d["seconds"] for d in runs) / len(runs)
        kind = runs[0]["path"]
        over = (overhead.get(kind) or {}).get("seconds")
        entry = {
            "file": name, "path": kind, "warm_seconds": round(mean, 3),
            "overhead_seconds": over,
            "cli_seconds": round(over + mean, 3) if over is not None else None,
            "speedup": round((over + mean) / mean, 2) if over is not None and mean else None,
        }
        # A text-layer document whose tables are OCR'd starts llama-server too,
        # and so pays up to the OCR path's overhead; page 0 cannot show that.
        ocr_over = (overhead.get("ocr") or {}).get("seconds")
        if kind == "text" and ocr_over is not None and mean:
            entry["speedup_if_its_tables_are_ocrd"] = round((ocr_over + mean) / mean, 2)
        saving.append(entry)

    report = {
        "schema": BENCH_SCHEMA,
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": _host(),
        "versions": tool_versions(),
        "options": {"page_range": opts.get("page_range"), "force_ocr": bool(args.force_ocr),
                    "runs": args.runs},
        "llama_server_extra_args": llama_args,
        # What a per-file CLI pays on every file, measured; a resident worker pays it once.
        "overhead": overhead,
        # Inside this process, for reference - NOT the overhead: get_models()
        # loads the layout model only, and prewarm() includes one page of work.
        "model_load_seconds": round(load, 3),
        "warmup_seconds": round(warmup, 3) if warmed is not None else None,
        "documents": docs,
        "warm": warm,
        "resident_saving": saving,
        "notes": [
            "overhead = page 0 in a fresh process (after one priming run) minus the same page in this "
            "warm one; a container per file adds its own start on top",
            "overhead[text] is for pages needing no OCR at all; a text-layer document whose tables are "
            "OCR'd starts llama-server and pays up to overhead[ocr] - hence speedup_if_its_tables_are_ocrd",
            "llama-server ran with " + (llama_args or "no extra arguments") + ": pages are converted, "
            "never recalled from its prompt cache",
        ],
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

    bench = sub.add_parser("bench", help="measure the per-file overhead and each conversion")
    bench.add_argument("--output-dir", help="where conversions go (default: a temp folder)")
    bench.add_argument("--runs", type=int, default=1, help="convert the set this many times")
    bench.add_argument("--report", help="also write the JSON report here")
    bench.add_argument("--page-range", help="convert only these pages of every document")
    bench.add_argument("--force-ocr", action="store_true", help="OCR every page (the OCR path, on purpose)")
    bench.add_argument("--no-cold", action="store_true",
                       help="skip measuring the per-file overhead (two fresh-process conversions per path)")
    bench.add_argument("files", nargs="+")
    bench.set_defaults(func=cmd_bench)

    args = parser.parse_args(argv)
    if getattr(args, "runs", 1) < 1:
        parser.error("--runs must be at least 1")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
