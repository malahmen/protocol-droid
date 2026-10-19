"""The containerized conversion service - the Python of lib/service.sh.

A Redis queue + FastAPI enqueue API + scalable marker workers, via Docker
Compose or Kubernetes. Marker only. Commands: build, deploy, status, logs,
scale, enqueue, teardown; each takes --target docker|k8s (default docker).
"""
import os
import re
import secrets
import shutil
import subprocess
import tempfile

from . import common as c
from .ui import EX_OK, error_exit, info, success, warn

IMAGE = "marker-service:latest"     # matches the k8s manifests' image ref
K8S_NS = "marker"                   # the namespace the manifests define


def compose_file() -> str:
    return os.path.join(c.ROOT, "docker-compose.yaml")


def env_file() -> str:
    return os.environ.get("PROTOCOL_DROID_ENV_FILE") or os.path.join(c.ROOT, ".env")


def k8s_dir() -> str:
    return os.path.join(c.ROOT, "k8s")


def _quiet(cmd) -> bool:
    return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0


def need_docker():
    if not shutil.which("docker"):
        error_exit("docker not found.")
    if not _quiet(["docker", "compose", "version"]):
        error_exit("'docker compose' plugin not available.")


def need_kubectl():
    if not shutil.which("kubectl"):
        error_exit("kubectl not found.")


def realpath_dir(path: str) -> str:
    """Absolute host directory (created if missing), resolved against the
    caller's cwd - compose would otherwise resolve it against the repo."""
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        error_exit(f"cannot create directory: {path}")
    return os.path.realpath(path)


def gen_secret() -> str:
    return secrets.token_hex(24)


def _env_lines() -> list:
    try:
        with open(env_file(), encoding="utf-8") as fh:
            return fh.read().splitlines()
    except FileNotFoundError:
        return []


def _env_write(lines, mode=None):
    path = env_file()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
    if mode is not None:
        os.chmod(path, mode)


def redis_password_docker():
    """Redis AUTH for compose: generated once and then kept, so a redeploy does
    not lock the workers out."""
    lines = _env_lines()
    if any(re.match(r"^REDIS_PASSWORD=.", ln) for ln in lines):
        return
    lines = [ln for ln in lines if not ln.startswith("REDIS_PASSWORD=")]
    lines.append(f"REDIS_PASSWORD={gen_secret()}")
    _env_write(lines, 0o600)
    info(f"Generated a Redis password (REDIS_PASSWORD in {env_file()}).")


def redis_secret_k8s():
    """Redis AUTH for k8s: the pods refuse to start without the Secret, so a
    missing one never silently means "no auth"."""
    if _quiet(["kubectl", "-n", K8S_NS, "get", "secret", "marker-redis"]):
        return
    if subprocess.call(["kubectl", "-n", K8S_NS, "create", "secret", "generic", "marker-redis",
                        f"--from-literal=password={gen_secret()}"], stdout=subprocess.DEVNULL) != 0:
        error_exit("could not create the marker-redis Secret.")
    info("Created Secret marker-redis (Redis password).")


def env_save(input_dir: str, output_dir: str):
    lines = [ln for ln in _env_lines() if not re.match(r"^MARKER_(INPUT|OUTPUT)=", ln)]
    lines += [f"MARKER_INPUT={input_dir}", f"MARKER_OUTPUT={output_dir}"]
    _env_write(lines)


def env_load():
    """The mounts and k8s storage settings recorded in .env; the environment
    wins over .env for the storage settings."""
    for line in _env_lines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key in ("MARKER_INPUT", "MARKER_OUTPUT"):
            os.environ[key] = value
        elif key in ("K8S_STORAGE_CLASS", "K8S_ACCESS_MODE") and key not in os.environ:
            os.environ[key] = value


_CLASS_RE = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?\Z")


def render_pvcs() -> str:
    """k8s/pvc.yaml with the storage settings applied.

    The file stays a valid manifest (RWO, the cluster's default class); the
    settings fill in its marked lines:
      K8S_STORAGE_CLASS  every "# storageClassName: (K8S_STORAGE_CLASS)" line
      K8S_ACCESS_MODE    every 'accessModes: ["ReadWriteOnce"]   # (K8S_ACCESS_MODE)' line
    """
    cls = os.environ.get("K8S_STORAGE_CLASS", "")
    mode = os.environ.get("K8S_ACCESS_MODE") or "ReadWriteOnce"
    if mode not in ("ReadWriteOnce", "ReadWriteMany"):
        error_exit(f"K8S_ACCESS_MODE must be ReadWriteOnce or ReadWriteMany (got '{mode}').")
    if cls and not _CLASS_RE.match(cls):
        error_exit(f"K8S_STORAGE_CLASS '{cls}' is not a valid StorageClass name.")
    with open(os.path.join(k8s_dir(), "pvc.yaml"), encoding="utf-8") as fh:
        text = fh.read()
    text = text.replace('accessModes: ["ReadWriteOnce"]   # (K8S_ACCESS_MODE)', f'accessModes: ["{mode}"]')
    if cls:
        text = re.sub(r"(?m)^  # storageClassName: \(K8S_STORAGE_CLASS\)$", f"  storageClassName: {cls}", text)
    return text


def compose(*args) -> int:
    env_load()
    return c.run(["docker", "compose", "-f", compose_file(), *args])


def verify_image_ocr() -> bool:
    """The image should ship llama-server (surya's CPU OCR backend). One quick
    probe after the build turns a silent gap into an immediate, named failure."""
    res = subprocess.run(["docker", "run", "--rm", "--entrypoint", "llama-server", IMAGE, "--version"],
                         capture_output=True, text=True)
    if res.returncode == 0:
        first = (res.stdout + res.stderr).strip().splitlines()[:1]
        success(f"OCR backend present in {IMAGE}: {first[0] if first else ''}")
        return True
    warn(f"llama-server is NOT runnable in {IMAGE} — text-layer PDFs will convert,")
    warn("but scanned pages and --force_ocr will fail. Rebuild with 'service build',")
    warn("or set SURYA_INFERENCE_URL to an external inference server.")
    return False


# --- the image k8s runs -------------------------------------------------------
#
# The manifests name marker-service:latest with imagePullPolicy IfNotPresent,
# and the image reaches the cluster by hand (kind load, or a push). So after a
# rebuild nothing in the pod spec changed: `kubectl apply` rolled nothing out,
# running pods kept the old image, and a node that already had a :latest never
# looked again (Kamino item 55). `Always` is no fix -
# there is no registry to pull from.
#
# So every build is also tagged with its image ID, and deploy renders the
# manifests with that tag: a new build is a new spec, the pods roll, and
# IfNotPresent becomes exactly right - an immutable tag never needs pulling
# twice. Without docker here the ID cannot be read, and :latest is applied with
# a warning saying how to roll by hand.
_LATEST_LINE = f"image: {IMAGE}"


def image_tag() -> str | None:
    """marker-service:<first 12 hex of the image ID>, or None if it cannot be read."""
    if not shutil.which("docker"):
        return None
    res = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", IMAGE],
                         capture_output=True, text=True)
    image_id = res.stdout.strip().split(":")[-1]
    if res.returncode != 0 or not re.fullmatch(r"[0-9a-f]{12,}", image_id):
        return None
    return f"{IMAGE.rsplit(':', 1)[0]}:{image_id[:12]}"


def tag_build() -> str | None:
    """Tag the current :latest with its ID; the tag, or None."""
    tag = image_tag()
    if tag and c.run(["docker", "tag", IMAGE, tag]) != 0:
        return None
    return tag


def deployed_image() -> str | None:
    """The image the marker-worker Deployment runs, as the cluster has it, or None."""
    res = subprocess.run(["kubectl", "-n", K8S_NS, "get", "deploy/marker-worker", "-o",
                          "jsonpath={.spec.template.spec.containers[0].image}"],
                         capture_output=True, text=True)
    image = (res.stdout or "").strip()
    if res.returncode != 0 or not image.startswith(IMAGE.rsplit(":", 1)[0] + ":"):
        return None
    return image


def render_manifest(path: str, tag: str | None) -> str:
    """The manifest with every `image: marker-service:latest` pinned to `tag`."""
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if tag:
        if _LATEST_LINE not in text:
            error_exit(f"{os.path.basename(path)} has no '{_LATEST_LINE}' line to pin.")
        text = text.replace(_LATEST_LINE, f"image: {tag}")
    return text


def _apply_text(text: str) -> bool:
    return subprocess.run(["kubectl", "apply", "-f", "-"], input=text, text=True).returncode == 0


def _make_available(tag: str | None) -> str:
    ref = tag or IMAGE
    return f"Make image '{ref}' available to the cluster (registry push, or 'kind load docker-image {ref}')."


def build(target: str) -> int:
    if target == "docker":
        need_docker()
    elif not shutil.which("docker"):
        error_exit("docker not found (needed to build the image).")
    info(f"Building {IMAGE} (large: installs marker + torch + deps)...")
    if c.run(["docker", "build", "-t", IMAGE, c.ROOT]) != 0:
        error_exit("Build failed.")
    tag = tag_build()
    success(f"Built {IMAGE}" + (f", also tagged {tag}." if tag else "."))
    verify_image_ocr()   # reported, not fatal
    if target == "k8s":
        info("For k8s: " + _make_available(tag))
    return EX_OK


def deploy(target: str, input_dir: str, output_dir: str) -> int:
    if target == "docker":
        need_docker()
        input_dir = realpath_dir(input_dir or os.path.join(c.ROOT, "input"))
        output_dir = realpath_dir(output_dir or os.path.join(c.ROOT, "output"))
        env_save(input_dir, output_dir)
        redis_password_docker()
        info(f"Mounts: {input_dir} → /data/input, {output_dir} → /data/output (recorded in {env_file()}).")
        info("Building image + starting stack (first run downloads models — several GB)...")
        if compose("up", "-d", "--build") != 0:
            error_exit("docker compose up failed.")
        success("Service up — API at http://localhost:8000 (POST /jobs).")
        verify_image_ocr()
        return EX_OK
    need_kubectl()
    # The image has to exist before manifests that would sit in ImagePullBackOff.
    if shutil.which("docker") and not _quiet(["docker", "image", "inspect", IMAGE]):
        warn(f"Image {IMAGE} not present locally — building it first.")
        build("k8s")
    info(f"Applying manifests to namespace '{K8S_NS}'...")
    if c.run(["kubectl", "apply", "-f", os.path.join(k8s_dir(), "namespace.yaml")]) != 0:
        error_exit("apply failed.")
    redis_secret_k8s()
    env_load()
    info(f"Storage: class {os.environ.get('K8S_STORAGE_CLASS') or '<cluster default>'}, "
         f"input/output {os.environ.get('K8S_ACCESS_MODE') or 'ReadWriteOnce'}, model cache ReadWriteOnce.")
    if subprocess.run(["kubectl", "apply", "-f", "-"], input=render_pvcs(), text=True).returncode != 0:
        error_exit("kubectl apply (volume claims) failed. An existing claim cannot change class or access mode: delete it first (a Pending one holds no data).")
    files = [os.path.join(k8s_dir(), f) for f in ("redis.yaml", "networkpolicy.yaml")]
    if c.run(["kubectl", "apply"] + [x for f in files for x in ("-f", f)]) != 0:
        error_exit("kubectl apply failed.")
    tag = tag_build()
    if not tag:
        warn(f"Cannot read {IMAGE}'s ID here (no docker, or not built), so it is applied as :latest - "
             f"after loading a new build, roll it by hand: kubectl -n {K8S_NS} rollout restart "
             "deploy/marker-api deploy/marker-worker")
    for f in ("api.yaml", "worker.yaml"):
        if not _apply_text(render_manifest(os.path.join(k8s_dir(), f), tag)):
            error_exit(f"kubectl apply ({f}) failed.")
    success("Applied. " + _make_available(tag))
    info(f"Reach the API: kubectl -n {K8S_NS} port-forward svc/marker-api 8000:8000")
    return EX_OK


def status(target: str) -> int:
    if target == "docker":
        need_docker()
        if compose("ps") != 0:
            warn("Is the stack deployed?")
        if _quiet(["docker", "image", "inspect", IMAGE]):
            verify_image_ocr()
        else:
            info(f"Image {IMAGE} not built yet.")
    else:
        need_kubectl()
        if c.run(["kubectl", "-n", K8S_NS, "get", "pods,svc,pvc"]) != 0:
            warn("Is the namespace deployed?")
    return EX_OK


def logs(target: str) -> int:
    info("Streaming worker logs — Ctrl-C to stop.")
    if target == "docker":
        need_docker()
        return compose("logs", "-f", "worker")
    need_kubectl()
    return c.run(["kubectl", "-n", K8S_NS, "logs", "-f", "deploy/marker-worker"])


def scale(target: str, replicas: str) -> int:
    if not replicas.isdigit():
        error_exit("scale: --replicas must be a number.")
    if target == "docker":
        need_docker()
        rc = compose("up", "-d", "--scale", f"worker={replicas}")
    else:
        need_kubectl()
        rc = c.run(["kubectl", "-n", K8S_NS, "scale", "deploy/marker-worker", f"--replicas={replicas}"])
    if rc != 0:
        error_exit("Scale failed.")
    success(f"Workers scaled to {replicas}.")
    return EX_OK


def enqueue(target: str, directory: str) -> int:
    if target == "docker":
        need_docker()
        info(f"Enqueuing every supported file under {directory} (in-container) ...")
        if compose("run", "--rm", "api", "python", "enqueue_batch.py", directory) != 0:
            error_exit("Batch enqueue failed (is the stack up?).")
        return EX_OK
    need_kubectl()
    info("Ensure your documents are on the 'marker-input' PVC first (e.g. 'kubectl cp').")
    subprocess.call(["kubectl", "-n", K8S_NS, "delete", "job/marker-batch", "--ignore-not-found"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # The image the workers run, not this machine's newest build: a rebuild not
    # yet deployed has a tag the cluster may not hold (ErrImagePull), and the
    # batch must enqueue with the code the workers will run (Kamino item 100).
    tag = deployed_image()
    if not tag:
        tag = image_tag()
        warn(f"No marker-worker deployment to take the image from: the batch job uses "
             f"{tag or IMAGE}, this machine's build.")
    if not _apply_text(render_manifest(os.path.join(k8s_dir(), "batch-job.yaml"), tag)):
        error_exit("Failed to start batch job.")
    success(f"Batch job started — watch: kubectl -n {K8S_NS} logs -f job/marker-batch")
    return EX_OK


def teardown(target: str, yes: bool) -> int:
    if not yes:
        error_exit("teardown: pass --yes to confirm (stops the stack; data/volumes kept).")
    if target == "docker":
        need_docker()
        if compose("down") != 0:
            error_exit("compose down failed.")
        success("Stack stopped (named volumes kept).")
    else:
        need_kubectl()
        files = [os.path.join(k8s_dir(), f) for f in ("worker.yaml", "api.yaml", "redis.yaml", "networkpolicy.yaml")]
        c.run(["kubectl", "delete"] + [x for f in files for x in ("-f", f)] + ["--ignore-not-found"])
        info(f"Data kept. To remove everything: kubectl delete ns {K8S_NS}")
    return EX_OK


def main(args, usage) -> int:
    cmd, rest = (args[0], args[1:]) if args else ("", [])
    target, input_dir, output_dir, replicas, directory, yes = "docker", "", "", "", "/data/input", False
    i = 0
    while i < len(rest):
        a = rest[i]
        val = rest[i + 1] if i + 1 < len(rest) else ""
        if a == "--target":
            target = val; i += 2
        elif a == "--input":
            input_dir = val; i += 2
        elif a == "--output":
            output_dir = val; i += 2
        elif a == "--replicas":
            replicas = val; i += 2
        elif a == "--dir":
            directory = val; i += 2
        elif a in ("--yes", "-y"):
            yes = True; i += 1
        elif a in ("-h", "--help"):
            usage()
            return EX_OK
        else:
            usage()
            error_exit(f"service: unknown flag: {a}")
    if target not in ("docker", "k8s"):
        error_exit("--target must be docker or k8s")
    if cmd in ("", "-h", "--help"):
        usage()
        return EX_OK
    commands = {
        "build": lambda: build(target), "deploy": lambda: deploy(target, input_dir, output_dir),
        "status": lambda: status(target), "logs": lambda: logs(target),
        "scale": lambda: scale(target, replicas), "enqueue": lambda: enqueue(target, directory),
        "teardown": lambda: teardown(target, yes),
    }
    if cmd not in commands:
        usage()
        error_exit(f"unknown service command: {cmd}")
    return commands[cmd]()
