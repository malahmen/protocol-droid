#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# protocol-droid.sh — manage marker document conversion, gum-free & flag-driven.
# "I am fluent in over six million forms of communication."
#
# protocol-droid is the engine that drives marker (github.com/datalab-to/marker)
# to turn documents (PDF/DOCX/PPTX/XLSX/HTML/EPUB/images) into Markdown/JSON —
# the preparation step for a downstream RAG/LLM ingestion pipeline. It converts a
# corpus; it does NOT chunk/embed/index (that's downstream). Two modes:
#
#   local    run marker on THIS machine, isolated in a pipx environment.
#   service  deploy a containerized service (Redis queue + enqueue API +
#            scalable marker workers) via Docker Compose or Kubernetes.
#
# Gum-free and non-interactive: every choice is a flag, so it fits a Makefile,
# CI, or cron. scomp-link ships a thin gum TUI that builds these flags. Run
# --help for the full command list.
# -----------------------------------------------------------------------------

set -euo pipefail

if [[ -t 2 ]]; then C_G=$'\033[0;32m'; C_Y=$'\033[0;33m'; C_R=$'\033[0;31m'; C_C=$'\033[0;36m'; C_N=$'\033[0m'
else C_G=""; C_Y=""; C_R=""; C_C=""; C_N=""; fi
info()       { printf '%s[info]%s  %s\n' "$C_C" "$C_N" "$*" >&2; }
success()    { printf '%s[ok]%s    %s\n' "$C_G" "$C_N" "$*" >&2; }
warn()       { printf '%s[warn]%s  %s\n' "$C_Y" "$C_N" "$*" >&2; }
error_exit() { printf '%s[error]%s %s\n' "$C_R" "$C_N" "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- marker (local pipx) constants ------------------------------------------
MARKER_PKG="marker-pdf"          # the pip package name
MARKER_SPEC="marker-pdf[full]"   # [full] = non-PDF inputs (docx/pptx/xlsx/epub/html)
DEFAULT_OUTPUT_DIR="./marker-output"
DEFAULT_DEPTH=3
INPUT_EXTS=(pdf docx pptx xlsx html epub png jpg jpeg tiff tif webp gif bmp)
PIPX=""                          # resolved to "pipx" or "python3 -m pipx"

# --- service (container) constants ------------------------------------------
COMPOSE="$HERE/docker-compose.yaml"
K8S="$HERE/k8s"
IMAGE="marker-service:latest"    # matches the k8s manifests' image ref
K8S_NS="marker"                  # the k8s namespace the manifests define

usage() {
    cat >&2 <<'EOF'
protocol-droid — manage marker document conversion (runs datalab-to/marker)

USAGE
  protocol-droid.sh <mode> <command> [flags]

MODE: local
  Run marker on this machine, isolated in a pipx environment.
    setup [--upgrade] [--no-llama]   install/upgrade marker (+ OCR backend); large
    scan  [--depth N] DIR            list convertible files under DIR (one per line)
    convert [conv-flags] PATH...     convert file(s)/folder (see below)
    status                           installed version, torch device, OCR backend, caches
    gui                              launch marker's Streamlit GUI (foreground)
    server                           launch marker's FastAPI server (foreground)
    clear-cache --yes                delete ~/.cache/datalab (models re-download)
    uninstall --yes                  remove marker's pipx env (model caches kept)

  convert flags:
    --output-format markdown|json|html|chunks   (default markdown)
    --output-dir DIR                            (default ./marker-output)
    --page-range R        e.g. 0,5-10,20 (single-file only)
    --workers N           parallel workers for batch (~3.5GB RAM/VRAM each)
    -- <marker args...>   forwarded verbatim to marker (e.g. --force_ocr,
                          --use_llm, --llm_service …, --gemini_api_key …)
  One file -> marker_single. A folder, or several files, -> marker's batch CLI
  (models load once); several files are symlinked into a temp dir first.

MODE: service
  Deploy the containerized conversion service (Redis + API + marker workers).
    build                            build the marker-service image (large)
    deploy [--input DIR] [--output DIR]   start/update the stack
    status                           stack/namespace status
    logs                             stream worker logs (Ctrl-C to stop)
    scale --replicas N               set the worker count
    enqueue [--dir PATH]             queue every supported file under a folder
    teardown --yes                   stop the stack (data/volumes kept)
  Add --target docker|k8s to any service command (default: docker).

Converts documents to Markdown/JSON ready for a downstream RAG pipeline — it
prepares the corpus; it does not chunk/embed/index. The k8s objects live in the
'marker' namespace (marker-api / marker-worker) since they run marker.
EOF
}

# =============================================================================
# Shared helpers
# =============================================================================

os_family() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)  echo "linux" ;;
        *)      echo "other" ;;
    esac
}

open_path() {
    local p="$1"
    if command -v xdg-open &>/dev/null; then xdg-open "$p" &>/dev/null &
    elif command -v open &>/dev/null; then open "$p"; fi
}

# =============================================================================
# LOCAL — marker in an isolated pipx environment
# =============================================================================

# A marker-compatible Python (3.10–3.13 with a working venv); the system default
# may be too new (e.g. 3.14, whose venv is also broken on some setups). Echoes
# the interpreter path; non-zero if none is usable.
find_marker_python() {
    local v path tmp
    for v in 3.12 3.13 3.11 3.10; do
        path="$(command -v "python${v}" 2>/dev/null || true)"
        [[ -n "$path" ]] || continue
        tmp="$(mktemp -d)"
        if "$path" -m venv "${tmp}/v" &>/dev/null; then
            rm -rf "$tmp"; printf '%s' "$path"; return 0
        fi
        rm -rf "$tmp"
    done
    return 1
}

# Resolve how to invoke pipx (sets PIPX); non-zero if pipx is unavailable.
resolve_pipx() {
    if command -v pipx &>/dev/null; then PIPX="pipx"; return 0; fi
    if command -v python3 &>/dev/null && python3 -m pipx --version &>/dev/null; then
        PIPX="python3 -m pipx"; return 0
    fi
    PIPX=""; return 1
}

# Resolve a marker CLI to a runnable path: prefer PATH, else the pipx venv bin.
marker_cli() {
    local name="$1"
    command -v "$name" &>/dev/null && { echo "$name"; return 0; }
    resolve_pipx || return 1
    local venvs; venvs=$($PIPX environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "${HOME}/.local/pipx/venvs")
    local cand="${venvs}/${MARKER_PKG}/bin/${name}"
    [[ -x "$cand" ]] && { echo "$cand"; return 0; }
    return 1
}

marker_python() {
    resolve_pipx || return 1
    local venvs; venvs=$($PIPX environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "${HOME}/.local/pipx/venvs")
    local cand="${venvs}/${MARKER_PKG}/bin/python"
    [[ -x "$cand" ]] && { echo "$cand"; return 0; }
    return 1
}

marker_venv_bin() {
    resolve_pipx || return 1
    local venvs; venvs=$($PIPX environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "${HOME}/.local/pipx/venvs")
    printf '%s' "${venvs}/${MARKER_PKG}/bin"
}

marker_installed() { marker_cli marker_single &>/dev/null; }
marker_version()   { resolve_pipx || { echo "unknown"; return; }; $PIPX list --short 2>/dev/null | awk -v p="$MARKER_PKG" '$1==p{print $2}' | head -1; }
require_marker()   { marker_installed || error_exit "marker is not installed. Run: protocol-droid.sh local setup"; }

# Install pipx as part of `setup` (never as a silent side effect of other cmds).
ensure_pipx() {
    resolve_pipx && return 0
    warn "pipx is not installed — installing it (part of setup)."
    case "$(os_family)" in
        macos)
            if command -v brew &>/dev/null; then brew install pipx && pipx ensurepath || true
            else python3 -m pip install --user pipx && python3 -m pipx ensurepath || true; fi ;;
        linux)
            if command -v apt-get &>/dev/null && sudo -n true 2>/dev/null; then sudo apt-get install -y pipx || python3 -m pip install --user pipx
            elif command -v dnf &>/dev/null && sudo -n true 2>/dev/null; then sudo dnf install -y pipx || python3 -m pip install --user pipx
            else python3 -m pip install --user pipx; fi
            python3 -m pipx ensurepath 2>/dev/null || true ;;
        *) python3 -m pip install --user pipx && python3 -m pipx ensurepath || true ;;
    esac
    resolve_pipx || error_exit "pipx installation failed. Install it manually, then re-run."
    success "pipx ready ($PIPX)."
}

# Ensure a Python import is satisfiable in marker's venv, injecting if not.
#   $1 = import statement (e.g. "psutil" or "fastapi, uvicorn"); $2… = pip pkgs
ensure_injected() {
    local import_stmt="$1"; shift
    local py; py=$(marker_python 2>/dev/null) || py=""
    [[ -n "$py" ]] && "$py" -c "import ${import_stmt}" 2>/dev/null && return 0
    resolve_pipx || return 1
    info "Adding $* to marker's environment (one-time)..."
    $PIPX inject "$MARKER_PKG" "$@" >/dev/null 2>&1 || { warn "Could not inject: $*"; return 1; }
}
ensure_marker_deps() { ensure_injected psutil psutil; }   # batch/chunk entrypoints need psutil

# --- Surya OCR backend (llama.cpp / vLLM) -----------------------------------
surya_backend() {
    [[ -n "${SURYA_INFERENCE_BACKEND:-}" ]] && { echo "${SURYA_INFERENCE_BACKEND}"; return; }
    if [[ -e /dev/nvidia0 ]] || command -v nvidia-smi &>/dev/null; then echo "vllm"; else echo "llamacpp"; fi
}
llama_server_present() {
    command -v llama-server &>/dev/null && return 0
    [[ -n "${LLAMA_CPP_BINARY:-}" && -x "${LLAMA_CPP_BINARY}" ]]
}
install_llama_server() {
    if llama_server_present; then info "llama-server already available."; return 0; fi
    case "$(os_family)" in
        macos)
            command -v brew &>/dev/null || { warn "Homebrew not found — install it, then 'brew install llama.cpp' (or set LLAMA_CPP_BINARY)."; return 1; }
            info "Installing llama.cpp via Homebrew (provides llama-server)..."; brew install llama.cpp || true ;;
        linux)
            if command -v brew &>/dev/null; then info "Installing llama.cpp via Homebrew..."; brew install llama.cpp || true
            else
                warn "No automatic installer for this Linux host. Either:"
                warn "  • download a llama-server build: https://github.com/ggml-org/llama.cpp/releases, then export LLAMA_CPP_BINARY=/path/to/llama-server"
                warn "  • or install Homebrew and 'brew install llama.cpp'"; return 1
            fi ;;
        *) warn "Unsupported OS for automatic install — set LLAMA_CPP_BINARY to a llama-server path."; return 1 ;;
    esac
    llama_server_present && { success "llama-server is available."; return 0; }
    warn "llama-server still not found after install — check the output above."; return 1
}
# Non-interactive convert-time check: warn if the OCR backend can't run.
check_surya_backend() {
    [[ "$(surya_backend)" == "llamacpp" ]] || return 0
    llama_server_present && return 0
    warn "marker's OCR engine (surya) needs 'llama-server' (llamacpp backend) — not found."
    warn "  OCR-dependent conversions will fail. Install it: protocol-droid.sh local setup"
}

# --- Hugging Face model cache / offline mode --------------------------------
HF_HUB_CACHE_DIR="${HF_HOME:-$HOME/.cache/huggingface}/hub"
hf_models_cached() { [[ -d "$HF_HUB_CACHE_DIR" ]] && compgen -G "${HF_HUB_CACHE_DIR}/models--datalab-to--*" >/dev/null 2>&1; }
hf_mode() {
    if [[ -n "${HF_HUB_OFFLINE:-}" ]]; then echo "offline (HF_HUB_OFFLINE=${HF_HUB_OFFLINE} in env)"
    elif [[ "${MARKER_HF_ONLINE:-}" == "1" ]]; then echo "online (MARKER_HF_ONLINE=1 forces Hub update checks)"
    elif hf_models_cached; then echo "offline (auto — models cached)"
    else echo "online (models not cached yet — first run downloads them)"; fi
}
apply_hf_offline() {
    [[ -n "${HF_HUB_OFFLINE:-}" ]] && return 0
    [[ "${MARKER_HF_ONLINE:-}" == "1" ]] && return 0
    if hf_models_cached; then
        export HF_HUB_OFFLINE=1
        info "HF offline mode (models cached) — skipping Hub checks; set MARKER_HF_ONLINE=1 to re-enable."
    fi
}

# --- local commands ---------------------------------------------------------

local_setup() {
    local do_upgrade=false want_llama=true
    while [[ $# -gt 0 ]]; do case "$1" in
        --upgrade) do_upgrade=true; shift ;;
        --no-llama) want_llama=false; shift ;;
        *) error_exit "local setup: unknown flag: $1" ;;
    esac; done

    local marker_py
    marker_py="$(find_marker_python)" || error_exit "No marker-compatible Python (3.10–3.13 with a working venv) found.
Install one, e.g.  macOS: brew install python@3.12   Linux: your pkg manager's python3.12 (or 3.11/3.13)."
    success "Using $("$marker_py" --version 2>&1) at ${marker_py} for marker's isolated environment."
    ensure_pipx

    if marker_installed; then
        if [[ "$do_upgrade" == true ]]; then
            info "Upgrading ${MARKER_PKG} (pulls model/torch updates — may take a while)..."
            $PIPX upgrade "$MARKER_PKG" || error_exit "Upgrade failed."
            ensure_marker_deps; success "marker upgraded to $(marker_version)."
        else
            info "marker already installed (version: $(marker_version)). Pass --upgrade to update."
        fi
    else
        info "Installing ${MARKER_SPEC} via pipx on $("$marker_py" --version 2>&1) (several minutes; downloads torch + GBs of models on first run)..."
        PIPX_DEFAULT_PYTHON="$marker_py" $PIPX install --python "$marker_py" "$MARKER_SPEC" || error_exit "Installation failed."
        ensure_marker_deps
        marker_installed || error_exit "Install reported success but marker_single wasn't found."
        success "marker installed (version: $(marker_version))."
        command -v marker_single &>/dev/null || warn "marker CLIs aren't on PATH yet — run 'pipx ensurepath' and restart your shell (this tool resolves them from the venv regardless)."
    fi

    if [[ "$want_llama" == true && "$(surya_backend)" == "llamacpp" ]]; then
        llama_server_present || { info "Setting up the OCR backend (llama-server)..."; install_llama_server || warn "OCR backend not installed; conversions needing OCR will fail until it is."; }
    fi
}

# Echo convertible files under DIR (recursive, depth-limited), one per line.
local_scan() {
    local depth="$DEFAULT_DEPTH" dir=""
    while [[ $# -gt 0 ]]; do case "$1" in
        --depth) depth="$2"; shift 2 ;;
        -*) error_exit "local scan: unknown flag: $1" ;;
        *) dir="$1"; shift ;;
    esac; done
    dir="${dir:-.}"
    [[ -d "$dir" ]] || error_exit "not a directory: $dir"
    local find_args=() ext
    for ext in "${INPUT_EXTS[@]}"; do find_args+=(-iname "*.${ext}" -o); done
    unset 'find_args[${#find_args[@]}-1]'   # drop trailing -o
    local found
    found=$(find "$dir" -maxdepth "$depth" -type f \( "${find_args[@]}" \) \
        ! -path "*/node_modules/*" ! -path "*/.git/*" \
        ! -path "*/${DEFAULT_OUTPUT_DIR#./}/*" ! -path "*/marker-output/*" \
        2>/dev/null | sed 's|^\./||' | sort)
    [[ -n "$found" ]] || return 1
    printf '%s\n' "$found"
}

# Symlink files into a fresh temp dir (collision-safe) and echo the dir, so
# marker's batch CLI can process an arbitrary selection loading models once.
_link_into_tmp() {
    local tmp; tmp=$(mktemp -d "${TMPDIR:-/tmp}/marker-sel.XXXXXX") || return 1
    local f abs name stem ext i
    for f in "$@"; do
        [[ -f "$f" ]] || continue
        abs="$(cd "$(dirname "$f")" && pwd)/$(basename "$f")"; name="$(basename "$f")"
        if [[ -e "${tmp}/${name}" ]]; then
            stem="${name%.*}"; ext="${name##*.}"; i=2
            while [[ -e "${tmp}/${stem}_${i}.${ext}" ]]; do i=$(( i + 1 )); done
            name="${stem}_${i}.${ext}"
        fi
        ln -s "$abs" "${tmp}/${name}"
    done
    printf '%s' "$tmp"
}

local_convert() {
    local out_fmt="markdown" out_dir="$DEFAULT_OUTPUT_DIR" workers="" page_range=""
    local paths=() extra=()
    while [[ $# -gt 0 ]]; do case "$1" in
        --output-format) out_fmt="$2"; shift 2 ;;
        --output-dir)    out_dir="$2"; shift 2 ;;
        --workers)       workers="$2"; shift 2 ;;
        --page-range)    page_range="$2"; shift 2 ;;
        --) shift; extra+=("$@"); break ;;
        -*) error_exit "local convert: unknown flag: $1 (forward marker flags after --)" ;;
        *) paths+=("$1"); shift ;;
    esac; done
    (( ${#paths[@]} > 0 )) || error_exit "local convert: no input path given."

    require_marker
    check_surya_backend
    apply_hf_offline
    mkdir -p "$out_dir"

    # Decide single vs batch.
    local mode in_dir label tmp="" bin
    if (( ${#paths[@]} == 1 )) && [[ -f "${paths[0]}" ]]; then
        mode=single
    elif (( ${#paths[@]} == 1 )) && [[ -d "${paths[0]}" ]]; then
        mode=batch; in_dir="${paths[0]}"; label="${paths[0]}"
    else
        mode=batch; tmp=$(_link_into_tmp "${paths[@]}") || error_exit "Could not stage the selection for batch."
        in_dir="$tmp"; label="${#paths[@]} selected files"
    fi

    if [[ "$mode" == single ]]; then
        [[ -n "$page_range" ]] && extra+=(--page_range "$page_range")
        bin=$(marker_cli marker_single) || error_exit "marker_single not found — run: protocol-droid.sh local setup"
        info "Converting ${paths[0]} → ${out_fmt} in ${out_dir}/"
        if "$bin" "${paths[0]}" --output_format "$out_fmt" --output_dir "$out_dir" "${extra[@]}"; then
            success "Done → ${out_dir}/"; open_path "$out_dir"
        else warn "Conversion failed for: ${paths[0]}"; fi
    else
        if [[ -n "$workers" ]]; then
            [[ "$workers" =~ ^[0-9]+$ ]] && (( workers >= 1 )) && extra+=(--workers "$workers") || warn "Invalid worker count '${workers}', letting marker decide."
        fi
        ensure_marker_deps   # batch CLI needs psutil
        bin=$(marker_cli marker) || { [[ -n "$tmp" ]] && rm -rf "$tmp"; error_exit "marker (batch CLI) not found — run: protocol-droid.sh local setup"; }
        info "Converting ${label} → ${out_fmt} in ${out_dir}/"
        if "$bin" "$in_dir" --output_format "$out_fmt" --output_dir "$out_dir" "${extra[@]}"; then
            success "Done → ${out_dir}/"; open_path "$out_dir"
        else warn "Batch conversion reported errors."; fi
        [[ -n "$tmp" ]] && rm -rf "$tmp"
    fi
}

local_status() {
    if marker_installed; then success "Installed — version $(marker_version)"
    else warn "Not installed. Run: protocol-droid.sh local setup"; return 0; fi

    command -v marker_single &>/dev/null && info "CLIs on PATH: yes" \
        || warn "CLIs on PATH: no (resolved from pipx venv; run 'pipx ensurepath' to fix)"

    local py device
    if py=$(marker_python); then
        device=$("$py" - <<'PY' 2>/dev/null || echo "unknown"
try:
    import torch
    if torch.cuda.is_available(): print("cuda")
    elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available(): print("mps")
    else: print("cpu")
except Exception: print("unknown")
PY
)
        info "Torch device: ${device}${TORCH_DEVICE:+ (TORCH_DEVICE=$TORCH_DEVICE overrides)}"
    fi

    local be; be="$(surya_backend)"
    info "OCR backend (surya): ${be}${SURYA_INFERENCE_BACKEND:+ (SURYA_INFERENCE_BACKEND=$SURYA_INFERENCE_BACKEND)}"
    if [[ "$be" == "llamacpp" ]]; then
        if llama_server_present; then info "llama-server: found ($(command -v llama-server 2>/dev/null || echo "$LLAMA_CPP_BINARY"))"
        else warn "llama-server: MISSING — OCR will fail. Run: protocol-droid.sh local setup"; fi
    fi

    local c
    for c in "${HOME}/.cache/datalab" "${HOME}/.cache/huggingface/hub"; do
        [[ -d "$c" ]] && info "Cache ${c}: $(du -sh "$c" 2>/dev/null | awk '{print $1}')"
    done
    if hf_models_cached; then info "HF models: cached (reused indefinitely — no TTL)"
    else warn "HF models: not cached — the first conversion downloads them from the Hub."; fi
    info "HF mode (next conversion): $(hf_mode)"
    [[ -z "${HF_TOKEN:-}" ]] && info "  Tip: set HF_TOKEN for higher Hub rate limits (and no 'unauthenticated' warning) when online." \
        || info "  HF_TOKEN: set"
}

local_clear_cache() {
    [[ "${1:-}" == "--yes" ]] || error_exit "local clear-cache: pass --yes to confirm (deletes ~/.cache/datalab; models re-download next run)."
    [[ -d "${HOME}/.cache/datalab" ]] || { info "No ~/.cache/datalab to clear."; return 0; }
    rm -rf "${HOME}/.cache/datalab" && success "Cache cleared (~/.cache/datalab)."
}

local_gui() {
    require_marker; check_surya_backend; apply_hf_offline
    ensure_injected streamlit streamlit || error_exit "streamlit unavailable — cannot launch the GUI."
    local bin vbin; bin=$(marker_cli marker_gui) || error_exit "marker_gui not found — run setup first."
    vbin=$(marker_venv_bin 2>/dev/null) || vbin=""
    info "Launching the Streamlit GUI — open http://localhost:8501, Ctrl-C to stop."
    PATH="${vbin:+$vbin:}$PATH" "$bin"
}

local_server() {
    require_marker; check_surya_backend; apply_hf_offline
    ensure_injected "fastapi, uvicorn, multipart" fastapi uvicorn python-multipart \
        || error_exit "server deps unavailable — cannot launch the API server."
    local bin vbin; bin=$(marker_cli marker_server) || error_exit "marker_server not found — run setup first."
    vbin=$(marker_venv_bin 2>/dev/null) || vbin=""
    info "Launching marker's FastAPI server — Ctrl-C to stop."
    PATH="${vbin:+$vbin:}$PATH" "$bin"
}

local_uninstall() {
    [[ "${1:-}" == "--yes" ]] || error_exit "local uninstall: pass --yes to confirm (removes marker's pipx env; model caches kept)."
    marker_installed || { info "marker is not installed."; return 0; }
    resolve_pipx || error_exit "pipx not found."
    $PIPX uninstall "$MARKER_PKG" && success "marker uninstalled."
}

local_main() {
    local cmd="${1:-}"; [[ $# -gt 0 ]] && shift || true
    case "$cmd" in
        setup)       local_setup "$@" ;;
        scan)        local_scan "$@" ;;
        convert)     local_convert "$@" ;;
        status)      local_status "$@" ;;
        gui)         local_gui "$@" ;;
        server)      local_server "$@" ;;
        clear-cache) local_clear_cache "$@" ;;
        uninstall)   local_uninstall "$@" ;;
        ""|-h|--help) usage ;;
        *) usage; error_exit "unknown local command: $cmd" ;;
    esac
}

# =============================================================================
# SERVICE — containerized conversion service (Docker Compose / Kubernetes)
# =============================================================================

_need_docker() {
    command -v docker &>/dev/null || error_exit "docker not found."
    docker compose version &>/dev/null || error_exit "'docker compose' plugin not available."
}
_need_kubectl() { command -v kubectl &>/dev/null || error_exit "kubectl not found."; }

svc_build() {
    local target="$1"
    if [[ "$target" == docker ]]; then _need_docker; else command -v docker &>/dev/null || error_exit "docker not found (needed to build the image)."; fi
    info "Building ${IMAGE} (large: installs marker + torch + deps)..."
    docker build -t "$IMAGE" "$HERE" && success "Built ${IMAGE}." || error_exit "Build failed."
    [[ "$target" == k8s ]] && info "For k8s, make it available to the cluster (registry push, or 'kind load docker-image ${IMAGE}')."
    return 0
}

svc_deploy() {
    local target="$1" input="${2:-./input}" output="${3:-./output}"
    if [[ "$target" == docker ]]; then
        _need_docker
        info "Building image + starting stack (first run downloads models — several GB)..."
        MARKER_INPUT="$input" MARKER_OUTPUT="$output" docker compose -f "$COMPOSE" up -d --build \
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
    if [[ "$target" == docker ]]; then _need_docker; docker compose -f "$COMPOSE" ps || warn "Is the stack deployed?"
    else _need_kubectl; kubectl -n "$K8S_NS" get pods,svc,pvc || warn "Is the namespace deployed?"; fi
}

svc_logs() {
    local target="$1"; info "Streaming worker logs — Ctrl-C to stop."
    if [[ "$target" == docker ]]; then _need_docker; docker compose -f "$COMPOSE" logs -f worker
    else _need_kubectl; kubectl -n "$K8S_NS" logs -f deploy/marker-worker; fi
}

svc_scale() {
    local target="$1" replicas="$2"
    [[ "$replicas" =~ ^[0-9]+$ ]] || error_exit "scale: --replicas must be a number."
    if [[ "$target" == docker ]]; then _need_docker
        docker compose -f "$COMPOSE" up -d --scale worker="$replicas" && success "Workers scaled to ${replicas}." || error_exit "Scale failed."
    else _need_kubectl
        kubectl -n "$K8S_NS" scale deploy/marker-worker --replicas="$replicas" && success "Workers scaled to ${replicas}." || error_exit "Scale failed."
    fi
}

svc_enqueue() {
    local target="$1" dir="${2:-/data/input}"
    if [[ "$target" == docker ]]; then _need_docker
        info "Enqueuing every supported file under ${dir} (in-container) ..."
        docker compose -f "$COMPOSE" run --rm api python enqueue_batch.py "$dir" || error_exit "Batch enqueue failed (is the stack up?)."
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
        docker compose -f "$COMPOSE" down && success "Stack stopped (named volumes kept)." || error_exit "compose down failed."
    else _need_kubectl
        kubectl delete -f "${K8S}/worker.yaml" -f "${K8S}/api.yaml" -f "${K8S}/redis.yaml" --ignore-not-found
        info "Data kept. To remove everything: kubectl delete ns ${K8S_NS}"
    fi
}

service_main() {
    local cmd="${1:-}"; [[ $# -gt 0 ]] && shift || true
    local target="docker" input="./input" output="./output" replicas="" dir="/data/input" yes=false
    while [[ $# -gt 0 ]]; do case "$1" in
        --target)   target="$2"; shift 2 ;;
        --input)    input="$2"; shift 2 ;;
        --output)   output="$2"; shift 2 ;;
        --replicas) replicas="$2"; shift 2 ;;
        --dir)      dir="$2"; shift 2 ;;
        --yes|-y)   yes=true; shift ;;
        -h|--help)  usage; return 0 ;;
        *) usage; error_exit "service: unknown flag: $1" ;;
    esac; done
    [[ "$target" == docker || "$target" == k8s ]] || error_exit "--target must be docker or k8s"
    case "$cmd" in
        build)    svc_build "$target" ;;
        deploy)   svc_deploy "$target" "$input" "$output" ;;
        status)   svc_status "$target" ;;
        logs)     svc_logs "$target" ;;
        scale)    svc_scale "$target" "$replicas" ;;
        enqueue)  svc_enqueue "$target" "$dir" ;;
        teardown) svc_teardown "$target" "$yes" ;;
        ""|-h|--help) usage ;;
        *) usage; error_exit "unknown service command: $cmd" ;;
    esac
}

# =============================================================================
# Entry point
# =============================================================================
main() {
    local mode="${1:-}"; [[ $# -gt 0 ]] && shift || true
    case "$mode" in
        local)   local_main "$@" ;;
        service) service_main "$@" ;;
        ""|-h|--help) usage ;;
        *) usage; error_exit "unknown mode: $mode (expected 'local' or 'service')" ;;
    esac
}

main "$@"
