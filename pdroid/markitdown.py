"""The markitdown backend - the Python of lib/markitdown.sh.

Runs Microsoft markitdown (a light pip tool) from its own pipx environment:
fast, broad formats, audio transcription, YouTube, ZIP, Outlook .msg.
Commands: setup, scan, convert, status, install-plugin, uninstall.
"""
import os
import shutil
import sys

from . import common as c
from .ui import EX_FAILED, EX_NO_INPUT, EX_NOTHING, EX_OK, error_exit, info, success, warn

PKG = "markitdown"
EXTRAS = ("pptx", "docx", "xlsx", "xls", "pdf", "outlook", "az-doc-intel",
          "az-content-understanding", "audio-transcription", "youtube-transcription")
EXTS = ("pdf", "docx", "pptx", "xlsx", "xls", "html", "htm", "csv", "json", "xml", "epub",
        "zip", "msg", "png", "jpg", "jpeg", "gif", "bmp", "tiff", "tif", "webp", "wav", "mp3", "m4a")


def installed() -> bool:
    return c.resolve_bin(PKG, "markitdown") is not None


def version() -> str:
    return c.pkg_version(PKG)


def require():
    if not installed():
        error_exit("markitdown is not installed. Run: protocol-droid.sh local setup --backend markitdown")


def check_extras(extras: str):
    """--extras must be "all" or a csv subset; pip would only warn about an
    unknown extra and silently install a markitdown without it."""
    if extras == "all":
        return
    if not extras:
        error_exit(f"markitdown setup: --extras needs a value (all or a csv of: {' '.join(EXTRAS)}).")
    for e in extras.split(","):
        if e not in EXTRAS:
            error_exit(f"markitdown setup: unknown extra '{e}' (valid: all, {' '.join(EXTRAS)}).")


def spec(extras: str = "all") -> str:
    return f"{PKG}[all]" if extras == "all" else f"{PKG}[{extras}]"


def setup(args) -> int:
    extras, upgrade = "all", False
    i = 0
    while i < len(args):
        if args[i] == "--extras":
            extras = args[i + 1] if i + 1 < len(args) else ""; i += 2
        elif args[i] == "--upgrade":
            upgrade = True; i += 1
        else:
            error_exit(f"markitdown setup: unknown flag: {args[i]}")
    check_extras(extras)
    py = c.find_python()
    if not py:
        error_exit("No markitdown-compatible Python (3.10+ with a working venv) found.\n"
                   "Install one, e.g.  macOS: brew install python@3.12   Linux: your pkg manager's python3.12 (or 3.11/3.10).")
    success(f"Using {c.python_label(py)} at {py} for markitdown's isolated environment.")
    pipx = c.ensure_pipx()
    if installed():
        if upgrade:
            if c.run(pipx + ["upgrade", PKG]) == 0:
                success(f"markitdown upgraded to {version()}.")
            else:
                error_exit("Upgrade failed.")
        else:
            info(f"markitdown already installed (version: {version()}). Pass --upgrade to update.")
        return EX_OK
    s = spec(extras)
    info(f"Installing {s} via pipx on {c.python_label(py)}...")
    if c.run(pipx + ["install", "--python", py, s], env=dict(os.environ, PIPX_DEFAULT_PYTHON=py)) != 0:
        error_exit("Installation failed.")
    if not installed():
        error_exit("Install reported success but the markitdown CLI wasn't found.")
    success(f"markitdown installed (version: {version()}).")
    if not shutil.which("markitdown"):
        warn("markitdown isn't on PATH yet — run 'pipx ensurepath' and restart your shell (this tool resolves it from the venv regardless).")
    if ("audio" in s or "[all]" in s) and not shutil.which("ffmpeg"):
        warn("Audio (mp3) transcription also needs ffmpeg on your system — install it separately (brew/apt/dnf install ffmpeg).")
    return EX_OK


def scan(args) -> int:
    depth, directory = c.DEFAULT_DEPTH, ""
    it = iter(args)
    for a in it:
        if a == "--depth":
            depth = int(next(it, c.DEFAULT_DEPTH))
        elif a.startswith("-"):
            error_exit(f"markitdown scan: unknown flag: {a}")
        else:
            directory = a
    directory = directory or "."
    if not os.path.isdir(directory):
        error_exit(f"not a directory: {directory}")
    found = c.scan_files(directory, depth, EXTS)
    if not found:
        return EX_FAILED
    print("\n".join(found))
    return EX_OK


def output_names(files, input_root=""):
    """{file: output path relative to the output folder}, one per input:
    common.output_stems plus ".md" (Kamino items 126, 204)."""
    return {f: n + ".md" for f, n in c.output_stems(files, input_root).items()}


def convert(args, open_output=True) -> int:
    """One .md per input (markitdown has no batch mode).

    --output-dir DIR, --no-clobber (keep doc.md, write doc_2.md), and anything
    after `--` forwarded verbatim (e.g. --use-plugins, -d, -e URL).
    """
    out_dir, paths, extra, clobber, input_root = c.DEFAULT_OUTPUT_DIR, [], [], True, ""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--output-dir":
            out_dir = args[i + 1]; i += 2
        elif a == "--output-format":
            i += 2        # accepted and ignored: markitdown only emits Markdown
        elif a == "--no-clobber":
            clobber = False; i += 1
        elif a == "--":
            extra += args[i + 1:]; break
        elif a.startswith("-"):
            error_exit(f"markitdown convert: unknown flag: {a} (forward markitdown flags after --)")
        else:
            paths.append(a); i += 1
    if not paths:
        error_exit("markitdown convert: no input path given.")

    require()
    binary = c.resolve_bin(PKG, "markitdown") or error_exit("markitdown not found — run setup.")
    os.makedirs(out_dir, exist_ok=True)

    # A single directory stands for its convertible files, and is the
    # provenance input root.
    if len(paths) == 1 and os.path.isdir(paths[0]):
        input_root = paths[0]
        paths = c.scan_files(paths[0], c.DEFAULT_DEPTH, EXTS, skip=(out_dir,))
        if not paths:
            warn(f"No supported files under {input_root} (markitdown handles: {' '.join(EXTS)}).")
            return EX_NOTHING

    ok = fail = missing = noprov = 0
    ver = version()
    names = output_names([f for f in paths if os.path.isfile(f)], input_root)
    written = set()     # this run's outputs: a second input never replaces one
    for f in paths:
        # Counted, not just warned about: a path the caller named and this tool
        # never opened is the case that used to exit 0.
        if not os.path.isfile(f):
            warn(f"Not a file: {f}")
            missing += 1
            continue
        rel = names[f]
        folder = os.path.join(out_dir, os.path.dirname(rel))
        os.makedirs(folder, exist_ok=True)
        out = c.out_path(folder, os.path.splitext(os.path.basename(rel))[0], "md", clobber)
        if os.path.normcase(os.path.abspath(out)).casefold() in written:
            warn(f"Failed: {f} — its output {out} was already written by another input of this run")
            fail += 1
            continue
        written.add(os.path.normcase(os.path.abspath(out)).casefold())
        info(f"Converting {f} ...")
        started = c.now_epoch()
        if c.run([binary, f, "-o", out, *extra]) == 0:
            success(f"→ {out}")
            ok += 1
            # Named after the output: every input in this run writes into the
            # SAME folder, so one shared provenance.json would let the last
            # document own the whole batch's provenance.
            name = os.path.basename(out)
            if not c.provenance_write(folder, f, "markitdown", f"markitdown={ver}", started, c.now_epoch(),
                                      "markdown", input_root, f"{name}.provenance.json", [name],
                                      [binary, f, "-o", out, *extra]):
                noprov += 1
        else:
            warn(f"Failed: {f}")
            fail += 1

    summary = f"{ok} converted"
    if fail:
        summary += f", {fail} failed"
    if missing:
        summary += f", {missing} missing"
    if noprov:
        summary += f", {noprov} without provenance"
    (warn if fail + missing + noprov else success)(f"Done — {summary} → {out_dir}/")
    if ok and open_output:
        c.open_path(out_dir)

    # A missing input wins only when nothing actually failed.
    if fail or noprov:
        return EX_FAILED
    if missing:
        return EX_NO_INPUT
    if ok == 0:
        return EX_NOTHING
    return EX_OK


def status(_args) -> int:
    if installed():
        success(f"markitdown: installed — version {version()}")
    else:
        warn("markitdown: not installed. Run: protocol-droid.sh local setup --backend markitdown")
        return EX_OK
    if shutil.which("markitdown"):
        info("CLI on PATH: yes")
    else:
        warn("CLI on PATH: no (resolved from pipx venv; run 'pipx ensurepath' to fix)")
    py = c.venv_python(PKG)
    if py:
        info(f"Env Python: {c.python_label(py)}")
    info("ffmpeg: found (audio transcription OK)" if shutil.which("ffmpeg")
         else "ffmpeg: not found (needed only for mp3 audio transcription)")
    info("Document Intelligence endpoint: set (env)" if os.environ.get("MARKITDOWN_DOCINTEL_ENDPOINT")
         else "Document Intelligence endpoint: not set (MARKITDOWN_DOCINTEL_ENDPOINT)")
    binary = c.resolve_bin(PKG, "markitdown")
    if binary:
        print(file=sys.stderr)
        info("Installed plugins (markitdown --list-plugins):")
        if c.run([binary, "--list-plugins"]) != 0:
            warn("Could not list plugins.")
    return EX_OK


def install_plugin(args) -> int:
    require()
    pkg = args[0] if args else ""
    if not pkg:
        error_exit("markitdown install-plugin: give a pip package (e.g. markitdown-sample-plugin).")
    pipx = c.resolve_pipx() or error_exit("pipx not found.")
    info(f"Injecting {pkg} into markitdown's environment...")
    if c.run(pipx + ["inject", PKG, pkg]) == 0:
        success(f"Installed {pkg}. Enable it per-conversion with --use-plugins.")
    else:
        warn(f"Could not install {pkg}.")
    return EX_OK


def uninstall(args) -> int:
    if args[:1] != ["--yes"]:
        error_exit("markitdown uninstall: pass --yes to confirm (removes markitdown's pipx env).")
    if not installed():
        info("markitdown is not installed.")
        return EX_OK
    pipx = c.resolve_pipx() or error_exit("pipx not found.")
    if c.run(pipx + ["uninstall", PKG]) == 0:
        success("markitdown uninstalled.")
    return EX_OK


COMMANDS = {"setup": setup, "scan": scan, "convert": convert, "status": status,
            "install-plugin": install_plugin, "uninstall": uninstall}


def main(cmd: str, args) -> int:
    if cmd not in COMMANDS:
        error_exit(f"markitdown: unknown command: {cmd} (setup|scan|convert|status|install-plugin|uninstall)")
    return COMMANDS[cmd](args)
