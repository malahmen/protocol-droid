"""container mode - the Python of lib/container.sh.

marker from the service image, run once on local files: nothing installed on
the host. Each input's folder is mounted read-only and the output folder
read-write, each at its own path; the models live in a named volume.
Commands: build, convert, bench, status.
"""
import os
import platform
import shutil
import socket
import subprocess
import time

from . import common as c
from . import marker
from .ui import EX_NO_INPUT, EX_NOTHING, EX_OK, error_exit, info, success, warn

IMAGE = "marker-service:latest"          # the service image (service.py, compose, k8s)
APP_UID = 1000                           # the image's `app` user


def models_volume() -> str:
    return os.environ.get("PROTOCOL_DROID_MODELS_VOLUME") or "protocol-droid-models"


def runtime(requested: str = "") -> str:
    rt = requested or os.environ.get("PROTOCOL_DROID_RUNTIME", "")
    if not rt:
        if shutil.which("podman"):
            rt = "podman"
        elif shutil.which("docker"):
            rt = "docker"
        else:
            error_exit("container mode needs podman or docker on PATH.")
    if rt not in ("podman", "docker"):
        error_exit(f"--runtime must be podman or docker, got '{rt}'.")
    if not shutil.which(rt):
        error_exit(f"{rt} not found on PATH.")
    return rt


def _host() -> str:
    return socket.gethostname().split(".")[0]


def _abs_dir(path: str) -> str:
    return os.path.realpath(path)


def base_args(rt: str) -> list:
    """Run options every command shares: no leftovers, the model cache, and the
    image's uid mapped to the caller where the runtime can."""
    args = ["run", "--rm", "-i", "-v", f"{models_volume()}:/models",
            # Home folders on an SELinux host would otherwise need relabelling,
            # which is not ours to do to them.
            "--security-opt", "label=disable",
            "-e", f"BENCH_HOST={_host()}"]
    if rt == "podman":
        args.append(f"--userns=keep-id:uid={APP_UID},gid={APP_UID}")
    elif platform.system() == "Linux" and os.getuid() != APP_UID:
        warn(f"docker runs the image as uid {APP_UID}; outputs will belong to that uid, not to you ({os.getuid()}).")
    return args


def require_image(rt: str):
    if subprocess.call([rt, "image", "inspect", IMAGE], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) != 0:
        error_exit(f"image {IMAGE} is not built — run: protocol-droid.sh container build")


def collect(paths):
    """(files, missing): a file stands for itself, a folder for its convertible
    files (marker's types), all absolute."""
    files, missing = [], 0
    for p in paths:
        if os.path.isdir(p):
            listed = c.scan_files(p, c.DEFAULT_DEPTH, marker.EXTS)
            if not listed:
                warn(f"No convertible files under {p}.")
            files += [os.path.join(_abs_dir(os.path.dirname(f) or "."), os.path.basename(f)) for f in listed]
        elif os.path.isfile(p):
            files.append(os.path.join(_abs_dir(os.path.dirname(p) or "."), os.path.basename(p)))
        else:
            warn(f"Not a file: {p}")
            missing += 1
    return files, missing


def mount_args(out: str, files) -> list:
    """Each input folder read-only and the output read-write, at their own
    paths. A folder that is both is mounted once, writable."""
    args = []
    for d in sorted({os.path.dirname(f) for f in files}):
        if d == "/":
            error_exit("refusing to mount / into the container.")
        if out and d == out:
            continue
        args += ["-v", f"{d}:{d}:ro"]
    if out:
        args += ["-v", f"{out}:{out}"]
    return args


def build(_args, rt="") -> int:
    rt = runtime(rt)
    info(f"Building {IMAGE} with {rt} (large: marker + torch + llama-server)...")
    if c.run([rt, "build", "-t", IMAGE, c.ROOT]) != 0:
        error_exit("Build failed.")
    success(f"Built {IMAGE}.")
    return EX_OK


def convert(args, rt="") -> int:
    out_dir, fmt, page_range, force_ocr, paths = c.DEFAULT_OUTPUT_DIR, "markdown", "", False, []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--output-dir":
            out_dir = args[i + 1]; i += 2
        elif a == "--output-format":
            fmt = args[i + 1]; i += 2
        elif a == "--page-range":
            page_range = args[i + 1]; i += 2
        elif a == "--force-ocr":
            force_ocr = True; i += 1
        elif a.startswith("-"):
            error_exit(f"container convert: unknown flag {a}")
        else:
            paths.append(a); i += 1
    if not paths:
        error_exit("container convert: give one or more files or folders.")
    rt = runtime(rt)
    require_image(rt)
    files, missing = collect(paths)
    if not files:
        return EX_NO_INPUT if missing else EX_NOTHING
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError:
        error_exit(f"cannot create {out_dir}")
    out_dir = _abs_dir(out_dir)

    cmd = [rt, *base_args(rt), *mount_args(out_dir, files), IMAGE,
           "python", "cli.py", "convert", "--output-format", fmt, "--output-dir", out_dir]
    if page_range:
        cmd += ["--page-range", page_range]
    if force_ocr:
        cmd.append("--force-ocr")
    info(f"container: {len(files)} file(s) → marker ({rt}, {IMAGE})")
    rc = c.run(cmd + files)
    if rc == 0 and os.path.isdir(out_dir):
        c.open_path(out_dir)
    if rc == 0 and missing:
        rc = EX_NO_INPUT
    return rc


def bench(args, rt="") -> int:
    runs, report, paths, extra = "1", "", [], []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--runs":
            runs = args[i + 1]; i += 2
        elif a == "--report":
            report = args[i + 1]; i += 2
        elif a == "--page-range":
            extra += ["--page-range", args[i + 1]]; i += 2
        elif a in ("--force-ocr", "--no-cold"):
            extra.append(a); i += 1
        elif a.startswith("-"):
            error_exit(f"container bench: unknown flag {a}")
        else:
            paths.append(a); i += 1
    if not paths:
        error_exit("container bench: give the documents to measure (files or folders).")
    if not (runs.isdigit() and int(runs) >= 1):
        error_exit("--runs must be a positive number.")
    rt = runtime(rt)
    require_image(rt)
    files, _missing = collect(paths)
    if not files:
        error_exit("nothing to measure.")
    host = _host()
    report = report or f"./marker-bench-{host}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    info(f"bench: {len(files)} document(s) × {runs} run(s) on {host} ({rt}) - the per-file overhead is "
         "measured first, in fresh processes (--no-cold skips it); a first-ever run also downloads the models")
    # The report is the container's stdout, written here on the host: no
    # folder of the caller's has to be writable from inside.
    with open(report, "w", encoding="utf-8") as fh:
        rc = c.run([rt, *base_args(rt), *mount_args("", files), IMAGE,
                    "python", "cli.py", "bench", "--runs", runs, *extra, *files], stdout=fh)
    if os.path.getsize(report) > 0:
        success(f"Report: {report}")
    else:
        warn("No report written.")
    return rc


def status(_args, rt="") -> int:
    rt = runtime(rt)
    info(f"runtime: {rt}")
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if subprocess.call([rt, "image", "inspect", IMAGE], **quiet) == 0:
        success(f"image: {IMAGE}")
    else:
        warn(f"image: {IMAGE} not built (container build)")
    if subprocess.call([rt, "volume", "inspect", models_volume()], **quiet) == 0:
        success(f"model cache: volume {models_volume()}")
    else:
        info(f"model cache: volume {models_volume()} not created yet (the first conversion downloads the models)")
    return EX_OK


def main(args, usage) -> int:
    rest, rt, seen_dd = [], "", False
    i = 0
    while i < len(args):
        a = args[i]
        if not seen_dd and a == "--":
            seen_dd = True; rest.append(a); i += 1
        elif not seen_dd and a == "--runtime":
            rt = args[i + 1] if i + 1 < len(args) else ""; i += 2
        else:
            rest.append(a); i += 1
    cmd, rest = (rest[0], rest[1:]) if rest else ("", [])
    commands = {"build": build, "convert": convert, "bench": bench, "status": status}
    if cmd in ("", "-h", "--help"):
        usage()
        return EX_OK
    if cmd not in commands:
        error_exit(f"unknown container command: {cmd} (build|convert|bench|status)")
    return commands[cmd](rest, rt)
