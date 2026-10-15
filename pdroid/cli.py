"""protocol-droid's command line - the Python of protocol-droid.sh.

The same modes, commands, flags, messages and exit codes as the bash engine,
so scomp-link's front-end and any script calling it cannot tell them apart.
"""
import os
import sys

from . import common as c
from . import container, marker, markitdown, service
from .ui import EX_FAILED, EX_NO_INPUT, EX_NOTHING, EX_OK, Fatal, error, error_exit, info, warn, worse

USAGE = """protocol-droid — multi-backend document→Markdown conversion

USAGE
  protocol-droid.sh local   <command> [--backend marker|markitdown|auto] [flags]
  protocol-droid.sh container <command> [--runtime podman|docker] [flags]
  protocol-droid.sh service <command> [--target docker|k8s] [flags]

LOCAL — run a converter on this machine (isolated in a pipx environment).
  --backend selects the tool (default: marker). Commands vary by backend:

  marker (default) — datalab-to/marker; high-fidelity PDF/OCR/layout (heavy):
    setup [--upgrade] [--no-llama]   install/upgrade marker + OCR backend
    scan [--depth N] DIR             list convertible files
    convert [--output-format F] [--output-dir D] [--page-range R] [--workers N]
            PATH... [-- <marker args…>]      one file, several files, or a folder
    status | gui | server | clear-cache --yes | uninstall --yes

  markitdown — Microsoft markitdown; fast, broad formats (light):
    setup [--extras all|pdf,docx,…] [--upgrade]
    scan [--depth N] DIR
    convert [--output-dir D] PATH... [-- <markitdown args…>]   (e.g. --use-plugins, -d, -e URL)
    status | install-plugin <pip-pkg> | uninstall --yes

  auto — convert only; routes PDF/images -> marker, everything else -> markitdown:
    convert [--output-dir D] PATH...

  convert PATH rules: one file -> that file; a folder -> its convertible files;
  several files -> batch. marker/markitdown feature flags go after '--'.

CONTAINER — marker from the service image, run once on local files. Nothing is
  installed on this host; the models download once into a named volume.
  build                                       build the image (same as service build)
  convert [--output-format F] [--output-dir D] [--page-range R] [--force-ocr] PATH...
  bench [--runs N] [--report FILE] PATH...    time the model load vs each document;
                                              writes a JSON report (default ./marker-bench-<host>-<time>.json)
  status                                      image and model cache
  --runtime podman|docker (default: podman if present, else docker; or PROTOCOL_DROID_RUNTIME)

SERVICE — containerized marker workers (Redis queue + FastAPI enqueue API).
  build | deploy [--input DIR] [--output DIR] | status | logs |
  scale --replicas N | enqueue [--dir PATH] | teardown --yes
  Add --target docker|k8s to any service command (default: docker).

Prepares a corpus for a downstream RAG pipeline — it does not chunk/embed/index.
The k8s objects live in the 'marker' namespace since the service runs marker.
"""

# auto: PDF and images go to marker, everything else to markitdown.
AUTO_MARKER_EXTS = ("pdf", "png", "jpg", "jpeg", "tiff", "tif", "webp", "gif", "bmp")


def usage():
    sys.stderr.write(USAGE)


def _ext(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[1].lower().lstrip(".")


def auto_convert(args) -> int:
    out_dir, paths = c.DEFAULT_OUTPUT_DIR, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--output-dir":
            out_dir = args[i + 1]; i += 2
        elif a == "--":
            i += 1        # no per-tool passthrough in auto (backends differ)
        elif a.startswith("-"):
            error_exit(f"auto convert: unsupported flag in auto mode: {a} (pick --backend marker|markitdown for tool flags)")
        else:
            paths.append(a); i += 1
    if not paths:
        error_exit("auto convert: no input path given.")

    # A single folder stands for the union of both backends' types; an empty
    # one is "nothing to convert" (3), not a failure (1).
    if len(paths) == 1 and os.path.isdir(paths[0]):
        listed = c.scan_files(paths[0], c.DEFAULT_DEPTH, tuple(marker.EXTS) + tuple(markitdown.EXTS))
        if not listed:
            warn(f"No supported files under {paths[0]}.")
            return EX_NOTHING
        paths = listed

    m_files, d_files, missing = [], [], 0
    for f in paths:
        if not os.path.isfile(f):
            warn(f"Not a file: {f}")
            missing += 1
            continue
        (m_files if _ext(f) in AUTO_MARKER_EXTS else d_files).append(f)

    # Both backends share one output folder, so it is revealed once, here. A
    # failure in one must not skip the other, and the worst code wins.
    rc = EX_OK
    if m_files:
        info(f"auto: {len(m_files)} file(s) → marker")
        rc = worse(rc, _run(marker.convert, ["--output-dir", out_dir, *m_files]))
    if d_files:
        info(f"auto: {len(d_files)} file(s) → markitdown")
        rc = worse(rc, _run(markitdown.convert, ["--output-dir", out_dir, *d_files]))
    if not m_files and not d_files:
        warn("auto: nothing to convert.")
        return EX_NO_INPUT if missing else EX_NOTHING
    if os.path.isdir(out_dir):
        c.open_path(out_dir)
    if missing:
        rc = worse(rc, EX_NO_INPUT)
    return rc


def _run(convert, args) -> int:
    """A backend's convert, with its own folder reveal off and its error_exit
    turned into a failed half rather than the end of the run."""
    try:
        return convert(args, open_output=False)
    except Fatal as exc:
        error(str(exc))
        return EX_FAILED


def local_main(args) -> int:
    backend, rest, seen_dd = "marker", [], False
    i = 0
    while i < len(args):
        a = args[i]
        if not seen_dd and a == "--":
            seen_dd = True; rest.append(a); i += 1
        elif not seen_dd and a == "--backend":
            backend = args[i + 1] if i + 1 < len(args) else ""; i += 2
        else:
            rest.append(a); i += 1
    if not rest:
        usage()
        return EX_OK
    cmd, rest = rest[0], rest[1:]
    if backend == "marker":
        return marker.main(cmd, rest)
    if backend == "markitdown":
        return markitdown.main(cmd, rest)
    if backend == "auto":
        if cmd != "convert":
            error_exit("--backend auto only supports 'convert'.")
        return auto_convert(rest)
    error_exit(f"unknown --backend: {backend} (marker|markitdown|auto)")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    mode, args = (args[0], args[1:]) if args else ("", [])
    try:
        if mode == "local":
            return local_main(args)
        if mode == "container":
            return container.main(args, usage)
        if mode == "service":
            return service.main(args, usage)
        if mode in ("", "-h", "--help"):
            usage()
            return EX_OK
        usage()
        error_exit(f"unknown mode: {mode} (expected 'local', 'container' or 'service')")
    except Fatal as exc:
        error(str(exc))
        return EX_FAILED
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    return EX_OK
