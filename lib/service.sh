# shellcheck shell=bash
# -----------------------------------------------------------------------------
# lib/service.sh — the containerized conversion service (marker workers).
# A Redis queue + FastAPI enqueue API + scalable marker workers, via Docker
# Compose or Kubernetes. This is marker-only (the image builds marker); it has
# no markitdown equivalent. Contract: service_main <cmd> [flags].
# Needs HERE (repo root) exported by protocol-droid.sh.
# -----------------------------------------------------------------------------

COMPOSE="$HERE/docker-compose.yaml"
ENV_FILE="$HERE/.env"           # compose reads it; deploy records the mounts here
K8S="$HERE/k8s"
IMAGE="marker-service:latest"    # matches the k8s manifests' image ref
K8S_NS="marker"                  # the k8s namespace the manifests define

_need_docker() {
    command -v docker &>/dev/null || error_exit "docker not found."
    docker compose version &>/dev/null || error_exit "'docker compose' plugin not available."
}
_need_kubectl() { command -v kubectl &>/dev/null || error_exit "kubectl not found."; }

# Absolute path of a host directory (created if missing), resolved against the
# caller's cwd — compose would otherwise resolve a relative path against $HERE.
_realpath_dir() { mkdir -p "$1" 2>/dev/null || error_exit "cannot create directory: $1"; (cd "$1" && pwd -P); }

# The mounts live in .env next to the compose file: deploy writes them, every
# other compose call loads them, so scale/enqueue/status see the same volumes.
svc_env_save() {
    local input="$1" output="$2" tmp; tmp=$(mktemp) || error_exit "mktemp failed."
    if [[ -f "$ENV_FILE" ]]; then grep -v -E '^MARKER_(INPUT|OUTPUT)=' "$ENV_FILE" > "$tmp" || true; fi
    printf 'MARKER_INPUT=%s\nMARKER_OUTPUT=%s\n' "$input" "$output" >> "$tmp"
    mv "$tmp" "$ENV_FILE"
}
svc_env_load() {
    [[ -f "$ENV_FILE" ]] || return 0
    local line
    while IFS= read -r line || [[ -n "$line" ]]; do case "$line" in
        MARKER_INPUT=*)  export MARKER_INPUT="${line#MARKER_INPUT=}" ;;
        MARKER_OUTPUT=*) export MARKER_OUTPUT="${line#MARKER_OUTPUT=}" ;;
    esac; done < "$ENV_FILE"
}
_compose() { svc_env_load; docker compose -f "$COMPOSE" "$@"; }

svc_build() {
    local target="$1"
    if [[ "$target" == docker ]]; then _need_docker; else command -v docker &>/dev/null || error_exit "docker not found (needed to build the image)."; fi
    info "Building ${IMAGE} (large: installs marker + torch + deps)..."
    docker build -t "$IMAGE" "$HERE" && success "Built ${IMAGE}." || error_exit "Build failed."
    [[ "$target" == k8s ]] && info "For k8s, make it available to the cluster (registry push, or 'kind load docker-image ${IMAGE}')."
    return 0
}

svc_deploy() {
    local target="$1" input="${2:-$HERE/input}" output="${3:-$HERE/output}"
    if [[ "$target" == docker ]]; then
        _need_docker
        input="$(_realpath_dir "$input")"; output="$(_realpath_dir "$output")"
        svc_env_save "$input" "$output"
        info "Mounts: ${input} → /data/input, ${output} → /data/output (recorded in ${ENV_FILE})."
        info "Building image + starting stack (first run downloads models — several GB)..."
        _compose up -d --build \
            && success "Service up — API at http://localhost:8000 (POST /jobs)." || error_exit "docker compose up failed."
    else
        _need_kubectl
        info "Applying manifests to namespace '${K8S_NS}'..."
        kubectl apply -f "${K8S}/namespace.yaml" || error_exit "apply failed."
        kubectl apply -f "${K8S}/pvc.yaml" -f "${K8S}/redis.yaml" -f "${K8S}/api.yaml" -f "${K8S}/worker.yaml" \
            && success "Applied. Make image '${IMAGE}' available to the cluster (registry push or 'kind load')." || error_exit "kubectl apply failed."
        info "Reach the API: kubectl -n ${K8S_NS} port-forward svc/marker-api 8000:8000"
    fi
}

svc_status() {
    local target="$1"
    if [[ "$target" == docker ]]; then _need_docker; _compose ps || warn "Is the stack deployed?"
    else _need_kubectl; kubectl -n "$K8S_NS" get pods,svc,pvc || warn "Is the namespace deployed?"; fi
}

svc_logs() {
    local target="$1"; info "Streaming worker logs — Ctrl-C to stop."
    if [[ "$target" == docker ]]; then _need_docker; _compose logs -f worker
    else _need_kubectl; kubectl -n "$K8S_NS" logs -f deploy/marker-worker; fi
}

svc_scale() {
    local target="$1" replicas="$2"
    [[ "$replicas" =~ ^[0-9]+$ ]] || error_exit "scale: --replicas must be a number."
    if [[ "$target" == docker ]]; then _need_docker
        _compose up -d --scale worker="$replicas" && success "Workers scaled to ${replicas}." || error_exit "Scale failed."
    else _need_kubectl
        kubectl -n "$K8S_NS" scale deploy/marker-worker --replicas="$replicas" && success "Workers scaled to ${replicas}." || error_exit "Scale failed."
    fi
}

svc_enqueue() {
    local target="$1" dir="${2:-/data/input}"
    if [[ "$target" == docker ]]; then _need_docker
        info "Enqueuing every supported file under ${dir} (in-container) ..."
        _compose run --rm api python enqueue_batch.py "$dir" || error_exit "Batch enqueue failed (is the stack up?)."
    else _need_kubectl
        info "Ensure your documents are on the 'marker-input' PVC first (e.g. 'kubectl cp')."
        kubectl -n "$K8S_NS" delete job/marker-batch --ignore-not-found >/dev/null 2>&1 || true
        kubectl apply -f "${K8S}/batch-job.yaml" \
            && success "Batch job started — watch: kubectl -n ${K8S_NS} logs -f job/marker-batch" || error_exit "Failed to start batch job."
    fi
}

svc_teardown() {
    local target="$1" yes="$2"
    [[ "$yes" == true ]] || error_exit "teardown: pass --yes to confirm (stops the stack; data/volumes kept)."
    if [[ "$target" == docker ]]; then _need_docker
        _compose down && success "Stack stopped (named volumes kept)." || error_exit "compose down failed."
    else _need_kubectl
        kubectl delete -f "${K8S}/worker.yaml" -f "${K8S}/api.yaml" -f "${K8S}/redis.yaml" --ignore-not-found
        info "Data kept. To remove everything: kubectl delete ns ${K8S_NS}"
    fi
}

service_main() {
    local cmd="${1:-}"; [[ $# -gt 0 ]] && shift || true
    local target="docker" input="" output="" replicas="" dir="/data/input" yes=false   # empty -> deploy defaults
    while [[ $# -gt 0 ]]; do case "$1" in
        --target) target="$2"; shift 2 ;; --input) input="$2"; shift 2 ;; --output) output="$2"; shift 2 ;;
        --replicas) replicas="$2"; shift 2 ;; --dir) dir="$2"; shift 2 ;; --yes|-y) yes=true; shift ;;
        -h|--help) usage; return 0 ;; *) usage; error_exit "service: unknown flag: $1" ;;
    esac; done
    [[ "$target" == docker || "$target" == k8s ]] || error_exit "--target must be docker or k8s"
    case "$cmd" in
        build) svc_build "$target" ;; deploy) svc_deploy "$target" "$input" "$output" ;;
        status) svc_status "$target" ;; logs) svc_logs "$target" ;; scale) svc_scale "$target" "$replicas" ;;
        enqueue) svc_enqueue "$target" "$dir" ;; teardown) svc_teardown "$target" "$yes" ;;
        ""|-h|--help) usage ;; *) usage; error_exit "unknown service command: $cmd" ;;
    esac
}
