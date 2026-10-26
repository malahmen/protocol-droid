"""The marker backend - the Python of lib/marker.sh.

Runs datalab-to/marker (heavy: PyTorch + models + surya OCR) from its own
pipx environment. Commands: setup, scan, convert, status, gui, server,
clear-cache, uninstall.
"""
import glob
import os
import shutil
import subprocess
import tempfile

from . import common as c
from .ui import EX_FAILED, EX_NO_INPUT, EX_NOTHING, EX_OK, error_exit, info, success, warn

PKG = "marker-pdf"
SPEC = "marker-pdf[full]"      # [full] = non-PDF inputs (docx/pptx/xlsx/epub/html)
EXTS = ("pdf", "docx", "pptx", "xlsx", "html", "epub", "png", "jpg", "jpeg",
        "tiff", "tif", "webp", "gif", "bmp")

_TORCH_DEVICE = """
try:
    import torch
    if torch.cuda.is_available(): print("cuda")
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available(): print("mps")
    else: print("cpu")
except Exception: print("unknown")
"""


def installed() -> bool:
    return c.resolve_bin(PKG, "marker_single") is not None


def version() -> str:
    return c.pkg_version(PKG)


def require():
    if not installed():
        error_exit("marker is not installed. Run: protocol-droid.sh local setup --backend marker")


def ensure_injected(import_stmt: str, *packages) -> bool:
    """Make an import work in marker's venv, injecting packages if it does not."""
    py = c.venv_python(PKG)
    if py and c.run([py, "-c", f"import {import_stmt}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        return True
    pipx = c.resolve_pipx()
    if not pipx:
        return False
    info(f"Adding {' '.join(packages)} to marker's environment (one-time)...")
    if c.run(pipx + ["inject", PKG, *packages], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        warn(f"Could not inject: {' '.join(packages)}")
        return False
    return True


def ensure_deps():
    # batch/chunk needs psutil; a failed inject is only a warning (marker still runs).
    ensure_injected("psutil", "psutil")


# --- surya's OCR backend (llama.cpp / vLLM) ----------------------------------

def surya_backend() -> str:
    if os.environ.get("SURYA_INFERENCE_BACKEND"):
        return os.environ["SURYA_INFERENCE_BACKEND"]
    return "vllm" if os.path.exists("/dev/nvidia0") or shutil.which("nvidia-smi") else "llamacpp"


def llama_present() -> bool:
    if shutil.which("llama-server"):
        return True
    binary = os.environ.get("LLAMA_CPP_BINARY", "")
    return bool(binary) and os.access(binary, os.X_OK)


def install_llama() -> bool:
    if llama_present():
        info("llama-server already available.")
        return True
    fam = c.os_family()
    if fam == "macos":
        if not shutil.which("brew"):
            warn("Homebrew not found — install it, then 'brew install llama.cpp' (or set LLAMA_CPP_BINARY).")
            return False
        info("Installing llama.cpp via Homebrew (provides llama-server)...")
        c.run(["brew", "install", "llama.cpp"])
    elif fam == "linux":
        if shutil.which("brew"):
            info("Installing llama.cpp via Homebrew...")
            c.run(["brew", "install", "llama.cpp"])
        else:
            warn("No automatic installer for this Linux host. Either:")
            warn("  • download a llama-server build: https://github.com/ggml-org/llama.cpp/releases, then export LLAMA_CPP_BINARY=/path/to/llama-server")
            warn("  • or install Homebrew and 'brew install llama.cpp'")
            return False
    else:
        warn("Unsupported OS for automatic install — set LLAMA_CPP_BINARY to a llama-server path.")
        return False
    if llama_present():
        success("llama-server is available.")
        return True
    warn("llama-server still not found after install — check the output above.")
    return False


def check_surya():
    if surya_backend() != "llamacpp" or llama_present():
        return
    warn("marker's OCR engine (surya) needs 'llama-server' (llamacpp backend) — not found.")
    warn("  OCR-dependent conversions will fail. Install it: protocol-droid.sh local setup --backend marker")


# --- caches ---------------------------------------------------------------------
#
# Two caches, not one. marker's layout/table models come from the Hugging Face
# Hub (HF_HOME); surya's OCR models come from models.datalab.to over plain
# HTTP into a directory of its own - which is why HF_HUB_OFFLINE has no effect
# on them. surya computes that directory with platformdirs, so it moves with
# the OS and XDG_CACHE_HOME, and MODEL_CACHE_DIR overrides it outright.

def cache_root() -> str:
    home = os.path.expanduser("~")
    if c.os_family() == "macos":
        return os.path.join(home, "Library", "Caches", "datalab")
    return os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(home, ".cache"), "datalab")


def model_cache() -> str:
    return os.environ.get("MODEL_CACHE_DIR") or os.path.join(cache_root(), "models")


def hf_cache() -> str:
    return os.path.join(os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface"), "hub")


def hf_cached() -> bool:
    return os.path.isdir(hf_cache()) and bool(glob.glob(os.path.join(hf_cache(), "models--datalab-to--*")))


def hf_mode() -> str:
    if os.environ.get("HF_HUB_OFFLINE"):
        return f"offline (HF_HUB_OFFLINE={os.environ['HF_HUB_OFFLINE']} in env)"
    if os.environ.get("MARKER_HF_ONLINE") == "1":
        return "online (MARKER_HF_ONLINE=1 forces Hub update checks)"
    if hf_cached():
        return "offline (auto — models cached)"
    return "online (models not cached yet — first run downloads them)"


def apply_hf_offline():
    if os.environ.get("HF_HUB_OFFLINE") or os.environ.get("MARKER_HF_ONLINE") == "1":
        return
    if hf_cached():
        os.environ["HF_HUB_OFFLINE"] = "1"
        info("HF offline mode (models cached) — skipping Hub checks; set MARKER_HF_ONLINE=1 to re-enable.")


def _du(path: str) -> str:
    """A human size for a folder, like `du -sh`."""
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    size = float(total)
    for unit in ("B", "K", "M", "G", "T"):
        if size < 1024 or unit == "T":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{total}B"


# --- commands ---------------------------------------------------------------------

def setup(args) -> int:
    upgrade, want_llama = False, True
    for a in args:
        if a == "--upgrade":
            upgrade = True
        elif a == "--no-llama":
            want_llama = False
        else:
            error_exit(f"marker setup: unknown flag: {a}")
    py = c.find_python()
    if not py:
        error_exit("No marker-compatible Python (3.10–3.13 with a working venv) found.\n"
                   "Install one, e.g.  macOS: brew install python@3.12   Linux: your pkg manager's python3.12 (or 3.11/3.13).")
    success(f"Using {c.python_label(py)} at {py} for marker's isolated environment.")
    pipx = c.ensure_pipx()
    if installed():
        if upgrade:
            info(f"Upgrading {PKG} (pulls model/torch updates — may take a while)...")
            if c.run(pipx + ["upgrade", PKG]) != 0:
                error_exit("Upgrade failed.")
            ensure_deps()
            success(f"marker upgraded to {version()}.")
        else:
            info(f"marker already installed (version: {version()}). Pass --upgrade to update.")
    else:
        info(f"Installing {SPEC} via pipx on {c.python_label(py)} (several minutes; downloads torch + GBs of models on first run)...")
        env = dict(os.environ, PIPX_DEFAULT_PYTHON=py)
        if c.run(pipx + ["install", "--python", py, SPEC], env=env) != 0:
            error_exit("Installation failed.")
        ensure_deps()
        if not installed():
            error_exit("Install reported success but marker_single wasn't found.")
        success(f"marker installed (version: {version()}).")
        if not shutil.which("marker_single"):
            warn("marker CLIs aren't on PATH yet — run 'pipx ensurepath' and restart your shell (this tool resolves them from the venv regardless).")
    if want_llama and surya_backend() == "llamacpp" and not llama_present():
        info("Setting up the OCR backend (llama-server)...")
        if not install_llama():
            warn("OCR backend not installed; conversions needing OCR will fail until it is.")
    return EX_OK


def scan(args) -> int:
    depth, directory = c.DEFAULT_DEPTH, ""
    it = iter(args)
    for a in it:
        if a == "--depth":
            depth = int(next(it, c.DEFAULT_DEPTH))
        elif a.startswith("-"):
            error_exit(f"marker scan: unknown flag: {a}")
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


_VERSIONS = """
from importlib import metadata
for p in ("marker-pdf", "surya-ocr", "torch"):
    try: print(p + "=" + metadata.version(p))
    except Exception: print(p + "=")
"""


def _versions(ver: str) -> list:
    """PKG=VER for marker and what its output depends on, read in marker's venv.

    marker-pdf alone said nothing about the OCR (surya) or torch that made the
    output (Kamino item 127); an empty VER is recorded as unknown.
    """
    py = c.venv_python(PKG)
    got = (c.output_of([py, "-c", _VERSIONS]) or "") if py else ""
    found = dict(line.split("=", 1) for line in got.splitlines() if "=" in line)
    found["marker-pdf"] = found.get("marker-pdf") or ver
    return [f"{k}={found.get(k, '')}" for k in ("marker-pdf", "surya-ocr", "torch")]


def _fs_clock(directory: str, fallback: int) -> int:
    """The time by the clock of the filesystem `directory` is on.

    _fresh compares output mtimes, which a network share stamps with the
    server's clock, so the start of the run is read the same way: the mtime of
    a file created there now. A client clock ahead of the server's would
    otherwise call every fresh folder "from an earlier run" (Kamino item 204).
    """
    try:
        fd, path = tempfile.mkstemp(prefix=".pd-clock.", dir=directory)
        try:
            os.close(fd)
            return int(os.path.getmtime(path))
        finally:
            os.unlink(path)
    except OSError:
        return fallback


def _fresh(folder: str, started: int) -> bool:
    """True if this run wrote anything in `folder` (`started` by the
    filesystem's clock, see _fs_clock)."""
    for root, _dirs, files in os.walk(folder):
        for name in files:
            try:
                if os.path.getmtime(os.path.join(root, name)) >= started:
                    return True
            except OSError:
                pass
    return False


def _provenance(out_dir, root, versions, fmt, started, staged, command, skip_existing=False,
                fresh_since=None) -> int:
    """One sidecar per document, in the folder marker made for it; the number
    of documents that have no output from this run.

    `staged` pairs each source with the name marker saw: <out>/<stem of that
    name>/ is marker's layout, and a source staged as a_2.pdf is in a_2/, not
    in the a/ another a.pdf made (Kamino item 127). A folder left by an
    earlier run is not this run's output: marker's batch CLI carries on past
    a file it cannot convert, and a fresh sidecar on the old folder made the
    failure invisible. With --skip_existing that folder is marker skipping on
    purpose, and its own sidecar stays.

    Once a document has its folder, any other folder whose sidecar names the
    same source is an earlier run's copy - a leftover of the order-based a_2
    naming, or of a name that changed as inputs came and went - and is
    removed, so the document is not ingested twice (Kamino item 204).
    """
    bad, finished = 0, c.now_epoch()
    fresh_since = started if fresh_since is None else fresh_since
    own = {}
    for f, name in staged:
        folder = os.path.join(out_dir, os.path.splitext(name)[0])
        if not os.path.isdir(folder):
            warn(f"Failed: {f} — marker wrote no output folder ({folder}).")
            bad += 1
            continue
        if not _fresh(folder, fresh_since):
            if skip_existing:
                info(f"Skipped: {f} — already converted in {folder} (--skip_existing).")
                own[os.path.abspath(f)] = folder
                continue
            warn(f"Failed: {f} — {folder} is from an earlier run; marker wrote nothing there this time.")
            bad += 1
            continue
        if not c.provenance_write(folder, f, "marker", versions, started, finished, fmt, root, "",
                                  None, command):
            bad += 1
            continue
        own[os.path.abspath(f)] = folder
    _drop_duplicates(out_dir, own)
    return bad


def _drop_duplicates(out_dir: str, own: dict) -> None:
    """Remove the folders in `out_dir` whose marker sidecar names a source of
    `own` ({source: the folder this run gave it}) other than its own folder."""
    import json
    keep = {os.path.realpath(d) for d in own.values()}
    try:
        entries = sorted(os.listdir(out_dir))
    except OSError:
        return
    for entry in entries:
        folder = os.path.join(out_dir, entry)
        sidecar = os.path.join(folder, "provenance.json")
        if os.path.realpath(folder) in keep or not os.path.isfile(sidecar):
            continue
        try:
            with open(sidecar, encoding="utf-8") as fh:
                record = json.load(fh)
            src = record["source"]["path"]
            backend = record["conversion"]["backend"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if backend == "marker" and src in own:
            info(f"Removed {folder}: an earlier copy of {src}, which is now in {own[src]}.")
            shutil.rmtree(folder, ignore_errors=True)


def _inputs(paths, out_dir=""):
    """The files a batch stands for: a folder for its convertible files, as
    deep as scan goes, and a file for itself. marker's batch CLI lists only
    the top of the folder it is given (os.listdir, marker-pdf 2.0.0) while
    provenance walked three levels (Kamino item 128), so the batch is staged
    from this list and both see the same files. The output folder is never
    an input (Kamino item 190)."""
    files = []
    for p in paths:
        files += c.scan_files(p, c.DEFAULT_DEPTH, EXTS, skip=(out_dir,)) if os.path.isdir(p) else [p]
    return files


def convert(args, open_output=True) -> int:
    out_fmt, out_dir, workers, page_range = "markdown", c.DEFAULT_OUTPUT_DIR, "", ""
    paths, extra = [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--output-format":
            out_fmt = args[i + 1]; i += 2
        elif a == "--output-dir":
            out_dir = args[i + 1]; i += 2
        elif a == "--workers":
            workers = args[i + 1]; i += 2
        elif a == "--page-range":
            page_range = args[i + 1]; i += 2
        elif a == "--":
            extra += args[i + 1:]; break
        elif a.startswith("-"):
            error_exit(f"marker convert: unknown flag: {a} (forward marker flags after --)")
        else:
            paths.append(a); i += 1
    if not paths:
        error_exit("marker convert: no input path given.")

    require()
    check_surya()
    apply_hf_offline()
    os.makedirs(out_dir, exist_ok=True)

    # Inputs that are not there are reported and dropped BEFORE anything runs:
    # staged into an empty batch they used to make marker exit 0.
    present, missing = [], 0
    for p in paths:
        if os.path.exists(p):
            present.append(p)
        else:
            warn(f"No such input: {p}")
            missing += 1
    if not present:
        warn(f"Nothing to convert — none of the {len(paths)} given path(s) exist.")
        return EX_NO_INPUT
    paths = present

    input_root = paths[0] if len(paths) == 1 and os.path.isdir(paths[0]) else ""
    files = _inputs(paths, out_dir)
    if not files:
        warn(f"No supported files in {' '.join(paths)} (marker handles: {' '.join(EXTS)}).")
        return EX_NO_INPUT if missing else EX_NOTHING
    # Output folder names, from the inputs alone (Kamino item 204). One that
    # two inputs would share is refused for both, loudly, before anything runs.
    names = c.output_stems(files, input_root, flat=True)
    clashes = c.name_clashes(names)
    for f in sorted(clashes):
        warn(f"Failed: {f} — its output folder {names[f]}/ would also be another input's; rename one of them.")
    files = [f for f in files if f not in clashes]
    if not files:
        return EX_FAILED

    versions, started, rc, bad = _versions(version()), c.now_epoch(), 0, len(clashes)
    fresh_since = _fs_clock(out_dir, started)
    skip_existing = "--skip_existing" in extra
    tmp = ""
    try:
        if len(files) == 1 and not input_root:
            if page_range:
                extra += ["--page_range", page_range]
            binary = c.resolve_bin(PKG, "marker_single") or error_exit("marker_single not found — run setup.")
            # What ran is what is recorded - this argv, not a rebuilt one.
            cmd = [binary, files[0], "--output_format", out_fmt, "--output_dir", out_dir, *extra]
            staged = [(files[0], os.path.basename(files[0]))]
            info(f"Converting {files[0]} → {out_fmt} in {out_dir}/")
        else:
            if page_range:
                warn("--page-range applies to a single file; marker's batch CLI has no such option - ignored.")
            if workers:
                if workers.isdigit() and int(workers) >= 1:
                    extra += ["--workers", workers]
                else:
                    warn(f"Invalid worker count '{workers}', letting marker decide.")
            ensure_deps()
            binary = c.resolve_bin(PKG, "marker") or error_exit("marker (batch CLI) not found — run setup.")
            tmp, staged = c.link_into_tmp(files, names)
            cmd = [binary, tmp, "--output_format", out_fmt, "--output_dir", out_dir, *extra]
            info(f"Converting {input_root or f'{len(files)} selected files'} ({len(files)} file(s)) → {out_fmt} in {out_dir}/")
        if c.run(cmd) == 0:
            bad += _provenance(out_dir, input_root, versions, out_fmt, started, staged, cmd,
                               skip_existing, fresh_since)
            if bad:
                warn(f"Done with {bad} of {len(staged) + len(clashes)} document(s) failed → {out_dir}/")
            else:
                success(f"Done → {out_dir}/")
            if open_output:
                c.open_path(out_dir)
        else:
            warn("marker reported errors.")
            rc = 1
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    if rc != 0 or bad > 0:
        return EX_FAILED
    if missing > 0:
        return EX_NO_INPUT
    return EX_OK


def status(_args) -> int:
    if installed():
        success(f"marker: installed — version {version()}")
    else:
        warn("marker: not installed. Run: protocol-droid.sh local setup --backend marker")
        return EX_OK
    if shutil.which("marker_single"):
        info("CLIs on PATH: yes")
    else:
        warn("CLIs on PATH: no (resolved from pipx venv; run 'pipx ensurepath' to fix)")
    py = c.venv_python(PKG)
    if py:
        device = c.output_of([py, "-c", _TORCH_DEVICE]) or "unknown"
        override = f" (TORCH_DEVICE={os.environ['TORCH_DEVICE']} overrides)" if os.environ.get("TORCH_DEVICE") else ""
        info(f"Torch device: {device}{override}")
    be = surya_backend()
    env_be = os.environ.get("SURYA_INFERENCE_BACKEND")
    info(f"OCR backend (surya): {be}" + (f" (SURYA_INFERENCE_BACKEND={env_be})" if env_be else ""))
    if be == "llamacpp":
        if llama_present():
            info(f"llama-server: found ({shutil.which('llama-server') or os.environ.get('LLAMA_CPP_BINARY')})")
        else:
            warn("llama-server: MISSING — OCR will fail. Run setup.")
    # The resolved paths, printed whether or not they exist: "that directory is
    # empty" and "the cache is somewhere else" are different answers.
    for cache in (model_cache(), hf_cache()):
        if os.path.isdir(cache):
            info(f"Cache {cache}: {_du(cache)}")
        else:
            info(f"Cache {cache}: not present yet")
    if os.environ.get("MODEL_CACHE_DIR"):
        info("  (surya's cache is set by MODEL_CACHE_DIR)")
    if hf_cached():
        info("HF models: cached (reused indefinitely — no TTL)")
    else:
        warn("HF models: not cached — first conversion downloads them.")
    info(f"HF mode (next conversion): {hf_mode()}")
    if os.environ.get("HF_TOKEN"):
        info("  HF_TOKEN: set")
    else:
        info("  Tip: set HF_TOKEN for higher Hub rate limits (no 'unauthenticated' warning) when online.")
    return EX_OK


def clear_cache(args) -> int:
    directory = model_cache()
    if args[:1] != ["--yes"]:
        error_exit(f"marker clear-cache: pass --yes to confirm (deletes {directory}; models re-download).")
    if not os.path.isdir(directory):
        info(f"Nothing to clear at {directory}.")
        return EX_OK
    shutil.rmtree(directory)
    success(f"Cache cleared ({directory}).")
    return EX_OK


def _launch(binary_name, what, deps_import, *deps) -> int:
    require()
    check_surya()
    apply_hf_offline()
    if not ensure_injected(deps_import, *deps):
        error_exit(what[1])
    binary = c.resolve_bin(PKG, binary_name) or error_exit(f"{binary_name} not found — run setup.")
    vbin = c.venv_bin_dir(PKG)
    env = dict(os.environ)
    if vbin:
        env["PATH"] = vbin + os.pathsep + env.get("PATH", "")
    info(what[0])
    return c.run([binary], env=env)


def gui(_args) -> int:
    return _launch("marker_gui", ("Launching the Streamlit GUI — open http://localhost:8501, Ctrl-C to stop.",
                                  "streamlit unavailable — cannot launch the GUI."), "streamlit", "streamlit")


def server(_args) -> int:
    return _launch("marker_server", ("Launching marker's FastAPI server — Ctrl-C to stop.",
                                     "server deps unavailable — cannot launch the API server."),
                   "fastapi, uvicorn, multipart", "fastapi", "uvicorn", "python-multipart")


def uninstall(args) -> int:
    if args[:1] != ["--yes"]:
        error_exit("marker uninstall: pass --yes to confirm (removes marker's pipx env; model caches kept).")
    if not installed():
        info("marker is not installed.")
        return EX_OK
    pipx = c.resolve_pipx() or error_exit("pipx not found.")
    if c.run(pipx + ["uninstall", PKG]) == 0:
        success("marker uninstalled.")
    return EX_OK


COMMANDS = {"setup": setup, "scan": scan, "convert": convert, "status": status, "gui": gui,
            "server": server, "clear-cache": clear_cache, "uninstall": uninstall}


def main(cmd: str, args) -> int:
    if cmd not in COMMANDS:
        error_exit(f"marker: unknown command: {cmd} (setup|scan|convert|status|gui|server|clear-cache|uninstall)")
    return COMMANDS[cmd](args)
