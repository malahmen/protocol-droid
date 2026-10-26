"""Shared helpers - the Python of lib/common.sh.

OS detection, pipx and Python resolution, the folder scanner, batch staging,
output naming, and provenance. Standard library only: this engine runs on
whatever python3 the host has (3.14 on a current Mac), while marker and
markitdown live in their own pipx environments on 3.10-3.13. Neither is ever
imported here.
"""
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
import io

from .ui import Fatal, error_exit, info, success, warn

# The repository root: provenance.py, the Dockerfile, k8s/ and the compose file
# live there, next to pdroid/. PROTOCOL_DROID_ROOT overrides it (tests).
ROOT = os.environ.get("PROTOCOL_DROID_ROOT") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))

DEFAULT_OUTPUT_DIR = "./converted"
DEFAULT_DEPTH = 3
PYTHON_VERSIONS = ("3.12", "3.11", "3.13", "3.10")

# Folders a scan never descends into: generated output and tool clutter.
_SKIP_PARTS = ("node_modules", ".git", "converted", "marker-output", "markitdown-output")


def os_family() -> str:
    return {"Darwin": "macos", "Linux": "linux"}.get(platform.system(), "other")


def run(cmd, **kw) -> int:
    """Run a command with the terminal attached; its exit status."""
    try:
        return subprocess.call(cmd, **kw)
    except FileNotFoundError:
        return 127


def output_of(cmd, **kw):
    """stdout of a command, stripped; None if it failed or is missing."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, **kw)
    except (FileNotFoundError, PermissionError):
        return None
    return res.stdout.strip() if res.returncode == 0 else None


def open_path(path: str) -> None:
    """Reveal a path in the file manager. PROTOCOL_DROID_NO_OPEN=1 disables it."""
    if os.environ.get("PROTOCOL_DROID_NO_OPEN") == "1":
        return
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif shutil.which("open"):
        subprocess.call(["open", path])


# --- Python and pipx ----------------------------------------------------------

def _python_venv_ok(py: str) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        return subprocess.call([py, "-m", "venv", os.path.join(tmp, "v")],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def find_python():
    """A Python 3.10-3.13 with a working venv, preferring the most settled version.

    Never 3.14+: marker/torch do not support it, and onnxruntime (markitdown)
    lags there too. The interpreter's path, or None.
    """
    for v in PYTHON_VERSIONS:
        path = shutil.which(f"python{v}")
        if path and _python_venv_ok(path):
            return path
    path = shutil.which("python3")
    if not path:
        return None
    v = output_of([path, "-c", 'import sys; print("%d.%d" % sys.version_info[:2])'])
    if v in PYTHON_VERSIONS and _python_venv_ok(path):
        return path
    return None


def python_label(py: str) -> str:
    return (output_of([py, "--version"]) or "python").strip()


def resolve_pipx():
    """The pipx command as a list, or None."""
    if shutil.which("pipx"):
        return ["pipx"]
    if shutil.which("python3") and output_of(["python3", "-m", "pipx", "--version"]) is not None:
        return ["python3", "-m", "pipx"]
    return None


def ensure_pipx():
    """Install pipx as part of a backend's `setup` (never as a silent side effect)."""
    pipx = resolve_pipx()
    if pipx:
        return pipx
    warn("pipx is not installed — installing it (part of setup).")
    fam = os_family()
    user_pip = ["python3", "-m", "pip", "install", "--user", "pipx"]
    if fam == "macos" and shutil.which("brew"):
        if run(["brew", "install", "pipx"]) == 0:
            run(["pipx", "ensurepath"])
    elif fam == "linux" and shutil.which("apt-get") and run(["sudo", "-n", "true"]) == 0:
        run(["sudo", "apt-get", "install", "-y", "pipx"]) == 0 or run(user_pip)
    elif fam == "linux" and shutil.which("dnf") and run(["sudo", "-n", "true"]) == 0:
        run(["sudo", "dnf", "install", "-y", "pipx"]) == 0 or run(user_pip)
    else:
        run(user_pip)
    if not shutil.which("pipx"):
        run(["python3", "-m", "pipx", "ensurepath"], stderr=subprocess.DEVNULL)
    pipx = resolve_pipx()
    if not pipx:
        error_exit("pipx installation failed. Install it manually, then re-run.")
    success(f"pipx ready ({' '.join(pipx)}).")
    return pipx


def pipx_venvs_dir():
    pipx = resolve_pipx()
    if not pipx:
        return None
    return output_of(pipx + ["environment", "--value", "PIPX_LOCAL_VENVS"]) \
        or os.path.join(os.path.expanduser("~"), ".local", "pipx", "venvs")


def resolve_bin(pkg: str, binary: str):
    """A runnable path for a CLI: PATH first, else the package's pipx venv."""
    found = shutil.which(binary)
    if found:
        return found
    venvs = pipx_venvs_dir()
    if venvs:
        cand = os.path.join(venvs, pkg, "bin", binary)
        if os.access(cand, os.X_OK):
            return cand
    return None


def venv_python(pkg: str):
    venvs = pipx_venvs_dir()
    if venvs:
        cand = os.path.join(venvs, pkg, "bin", "python")
        if os.access(cand, os.X_OK):
            return cand
    return None


def venv_bin_dir(pkg: str):
    venvs = pipx_venvs_dir()
    return os.path.join(venvs, pkg, "bin") if venvs else None


def pkg_version(pkg: str) -> str:
    pipx = resolve_pipx()
    if not pipx:
        return "unknown"
    listing = output_of(pipx + ["list", "--short"]) or ""
    for line in listing.splitlines():
        parts = line.split()
        if parts and parts[0] == pkg:
            return parts[1] if len(parts) > 1 else ""
    return ""


# --- files --------------------------------------------------------------------

def _is_sidecar(name: str) -> bool:
    """One of our provenance sidecars (provenance.py's is_provenance)."""
    return name == "provenance.json" or name.endswith(".provenance.json")


def scan_files(directory: str, depth: int, exts, skip=()) -> list:
    """Convertible files under `directory`, `depth` levels deep, sorted.

    Matches extensions case-insensitively and skips generated output and tool
    clutter, as `find -maxdepth` did. An unreadable subfolder is skipped, not
    fatal. Paths are returned as `directory/...`, without a leading "./".

    `skip` names folders never to descend into, compared by real path: the
    run's own output folder. Inside the input folder it was converted again
    on the next run - markitdown's sidecars as documents, marker's extracted
    images too (Kamino item 190). Provenance sidecars are never inputs.
    """
    wanted = {e.lower() for e in exts}
    skip_real = {os.path.realpath(d) for d in skip if d}
    found = []
    base_depth = directory.rstrip(os.sep).count(os.sep)

    def on_error(_):
        return None

    for root, dirs, files in os.walk(directory, onerror=on_error):
        # find -maxdepth N: the folder itself is depth 0, its files depth 1,
        # so files are listed down to level N-1 and nothing below is walked.
        level = root.rstrip(os.sep).count(os.sep) - base_depth
        dirs[:] = [] if level >= depth - 1 else [
            d for d in dirs
            if d not in _SKIP_PARTS and os.path.realpath(os.path.join(root, d)) not in skip_real]
        for name in files:
            if _is_sidecar(name):
                continue
            ext = os.path.splitext(name)[1].lower().lstrip(".")
            if ext in wanted:
                path = os.path.join(root, name)
                found.append(path[2:] if path.startswith("./") else path)
    return sorted(found)


def output_stems(files, input_root="", flat=False):
    """{file: output stem}, one per input, decided by the inputs alone.

    `<stem>`, as always - except where several inputs share a stem: in/a/notes.html
    and in/b/notes.html, or report.docx and report.pptx (Kamino item 126).
    Every input of such a group gets `<dir>/<stem>_<ext>`, the directory taken
    relative to the input folder (or to the group's common parent); `flat`
    joins it with "_" instead, for marker's one-level output folder. Stems are
    compared case-insensitively: on macOS notes.md and Notes.md are one file.

    A plain name can still meet another input's `<stem>_<ext>` name
    (report_docx.html next to report.docx and report.pptx); that input takes
    the `<stem>_<ext>` form as well (Kamino item 204). The rule never looks at
    the order of the inputs, so a re-run writes the same names. A clash that
    survives this (Notes.HTML and notes.html on a case-sensitive disk) is left
    for the caller to refuse.
    """
    def split(f):
        return os.path.splitext(os.path.basename(f))

    def suffixed(f, rel):
        stem, ext = split(f)
        name = f"{stem}_{ext.lstrip('.')}" if ext else stem
        if not rel:
            return name
        return "_".join(rel.split(os.sep) + [name]) if flat else os.path.join(rel, name)

    groups = {}
    for f in files:
        groups.setdefault(split(f)[0].casefold(), []).append(f)
    names, plain = {}, []
    for group in groups.values():
        if len(group) == 1:
            names[group[0]] = split(group[0])[0]
            plain.append(group[0])
            continue
        dirs = [os.path.dirname(os.path.abspath(f)) for f in group]
        base = os.path.abspath(input_root) if input_root else os.path.commonpath(dirs)
        for f, d in zip(group, dirs):
            rel = os.path.relpath(d, base)
            names[f] = suffixed(f, "" if rel == os.curdir or rel.startswith(os.pardir) else rel)
    taken = {}
    for f, n in names.items():
        taken.setdefault(n.casefold(), []).append(f)
    for f in plain:
        if len(taken[names[f].casefold()]) > 1 and split(f)[1]:
            names[f] = suffixed(f, "")
    return names


def name_clashes(names) -> set:
    """The inputs whose output name another input of the run also has."""
    seen = {}
    for f, n in names.items():
        seen.setdefault(n.casefold(), []).append(f)
    return {f for group in seen.values() if len(group) > 1 for f in group}


def link_into_tmp(files, names):
    """Symlink files into a fresh temp dir, each as <names[file]><its ext>.

    Returns (dir, [(file, staged name)]). marker names a document's output
    folder after the name it was given, so the staged name IS the output
    folder: it comes from output_stems, which depends on the inputs and not on
    their order. Order-based a_2, a_3 moved from one input to the next as
    inputs came and went (Kamino items 127, 204).
    """
    tmp = tempfile.mkdtemp(prefix="pd-sel.", dir=os.environ.get("TMPDIR") or None)
    staged = []
    for f in files:
        if not os.path.isfile(f):
            continue
        name = names[f] + os.path.splitext(f)[1]
        os.symlink(os.path.abspath(f), os.path.join(tmp, name))
        staged.append((f, name))
    return tmp, staged


def out_path(directory: str, stem: str, ext: str, clobber: bool = True) -> str:
    """<dir>/<stem>.<ext>; with clobber off, the first free <stem>_N.<ext>.

    Overwrites by default: a second run used to leave doc.md AND doc_2.md, and
    the ingestion pipeline then indexed the same document twice.
    """
    out = os.path.join(directory, f"{stem}.{ext}")
    if not clobber and os.path.exists(out):
        i = 2
        while os.path.exists(os.path.join(directory, f"{stem}_{i}.{ext}")):
            i += 1
        out = os.path.join(directory, f"{stem}_{i}.{ext}")
    return out


def now_epoch() -> int:
    return int(time.time())


# --- provenance ------------------------------------------------------------------

def _load_provenance():
    path = os.path.join(ROOT, "provenance.py")
    if not os.path.isfile(path):
        return None, path
    import importlib.util
    spec = importlib.util.spec_from_file_location("pd_provenance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, path


def provenance_write(out_folder, src, backend, version, started, finished, fmt,
                     root, name, outputs, command) -> bool:
    """Write the sidecar with the repository's provenance.py; True if written.

    The same module the containerized worker imports, called in-process rather
    than as a script. A sidecar that cannot be written fails the file: the hole
    is invisible until the day a re-conversion decision needs it.
    PROTOCOL_DROID_NO_PROVENANCE=1 turns it off.
    """
    if os.environ.get("PROTOCOL_DROID_NO_PROVENANCE") == "1":
        return True
    versions = [version] if isinstance(version, str) else list(version)
    module, path = _load_provenance()
    if module is None:
        warn(f"provenance: {path} is missing — no sidecar written for {src}.")
        return False
    args = ["--source", src, "--output-dir", out_folder, "--backend", backend,
            "--started", str(started), "--finished", str(finished)]
    for v in versions:
        args += ["--version", v]
    if fmt:
        args += ["--output-format", fmt]
    if root:
        args += ["--input-root", root]
    if name:
        args += ["--name", name]
    for o in outputs or []:
        args += ["--output", o]
    if command:
        args += ["--command"] + list(command)   # argparse REMAINDER: last
    try:
        with redirect_stdout(io.StringIO()):
            rc = module.main(args)
    except (SystemExit, Exception) as exc:   # noqa: BLE001 - reported, then counted
        rc = getattr(exc, "code", 1) or 1
    if rc != 0:
        warn(f"provenance: could not write a sidecar in {out_folder}.")
        return False
    return True


__all__ = ["Fatal"]
