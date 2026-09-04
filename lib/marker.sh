# -----------------------------------------------------------------------------
# lib/marker.sh — the "marker" backend adapter for protocol-droid.
# Runs datalab-to/marker (heavy: PyTorch + models + surya OCR) for high-fidelity
# PDF/OCR/layout conversion. Uses the shared helpers from lib/common.sh.
# Contract: marker_main <cmd> [args] with setup/scan/convert/status/gui/server/
# clear-cache/uninstall.
# -----------------------------------------------------------------------------

MARKER_PKG="marker-pdf"
MARKER_SPEC="marker-pdf[full]"     # [full] = non-PDF inputs (docx/pptx/xlsx/epub/html)
MARKER_EXTS=(pdf docx pptx xlsx html epub png jpg jpeg tiff tif webp gif bmp)

marker_installed() { resolve_bin "$MARKER_PKG" marker_single &>/dev/null; }
marker_version()   { pkg_version "$MARKER_PKG"; }
marker_require()   { marker_installed || error_exit "marker is not installed. Run: protocol-droid.sh local setup --backend marker"; }

# Ensure a Python import is satisfiable in marker's venv, injecting if not.
marker_ensure_injected() {
    local import_stmt="$1"; shift
    local py; py=$(venv_python "$MARKER_PKG" 2>/dev/null) || py=""
    [[ -n "$py" ]] && "$py" -c "import ${import_stmt}" 2>/dev/null && return 0
    resolve_pipx || return 1
    info "Adding $* to marker's environment (one-time)..."
    $PIPX inject "$MARKER_PKG" "$@" >/dev/null 2>&1 || { warn "Could not inject: $*"; return 1; }
}
marker_ensure_deps() { marker_ensure_injected psutil psutil; }   # batch/chunk needs psutil

# --- Surya OCR backend (llama.cpp / vLLM) -----------------------------------
marker_surya_backend() {
    [[ -n "${SURYA_INFERENCE_BACKEND:-}" ]] && { echo "${SURYA_INFERENCE_BACKEND}"; return; }
    if [[ -e /dev/nvidia0 ]] || command -v nvidia-smi &>/dev/null; then echo "vllm"; else echo "llamacpp"; fi
}
marker_llama_present() {
    command -v llama-server &>/dev/null && return 0
    [[ -n "${LLAMA_CPP_BINARY:-}" && -x "${LLAMA_CPP_BINARY}" ]]
}
marker_install_llama() {
    if marker_llama_present; then info "llama-server already available."; return 0; fi
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
    marker_llama_present && { success "llama-server is available."; return 0; }
    warn "llama-server still not found after install — check the output above."; return 1
}
marker_check_surya() {
    [[ "$(marker_surya_backend)" == "llamacpp" ]] || return 0
    marker_llama_present && return 0
    warn "marker's OCR engine (surya) needs 'llama-server' (llamacpp backend) — not found."
    warn "  OCR-dependent conversions will fail. Install it: protocol-droid.sh local setup --backend marker"
}

# --- Hugging Face model cache / offline mode --------------------------------
MARKER_HF_CACHE="${HF_HOME:-$HOME/.cache/huggingface}/hub"
marker_hf_cached() { [[ -d "$MARKER_HF_CACHE" ]] && compgen -G "${MARKER_HF_CACHE}/models--datalab-to--*" >/dev/null 2>&1; }
marker_hf_mode() {
    if [[ -n "${HF_HUB_OFFLINE:-}" ]]; then echo "offline (HF_HUB_OFFLINE=${HF_HUB_OFFLINE} in env)"
    elif [[ "${MARKER_HF_ONLINE:-}" == "1" ]]; then echo "online (MARKER_HF_ONLINE=1 forces Hub update checks)"
    elif marker_hf_cached; then echo "offline (auto — models cached)"
    else echo "online (models not cached yet — first run downloads them)"; fi
}
marker_apply_hf_offline() {
    [[ -n "${HF_HUB_OFFLINE:-}" ]] && return 0
    [[ "${MARKER_HF_ONLINE:-}" == "1" ]] && return 0
    if marker_hf_cached; then export HF_HUB_OFFLINE=1; info "HF offline mode (models cached) — skipping Hub checks; set MARKER_HF_ONLINE=1 to re-enable."; fi
}

# --- commands ---------------------------------------------------------------

marker_setup() {
    local do_upgrade=false want_llama=true
    while [[ $# -gt 0 ]]; do case "$1" in
        --upgrade) do_upgrade=true; shift ;;
        --no-llama) want_llama=false; shift ;;
        *) error_exit "marker setup: unknown flag: $1" ;;
    esac; done

    local py; py="$(find_python)" || error_exit "No marker-compatible Python (3.10–3.13 with a working venv) found.
Install one, e.g.  macOS: brew install python@3.12   Linux: your pkg manager's python3.12 (or 3.11/3.13)."
    success "Using $("$py" --version 2>&1) at ${py} for marker's isolated environment."
    ensure_pipx

    if marker_installed; then
        if [[ "$do_upgrade" == true ]]; then
            info "Upgrading ${MARKER_PKG} (pulls model/torch updates — may take a while)..."
            $PIPX upgrade "$MARKER_PKG" || error_exit "Upgrade failed."
            marker_ensure_deps; success "marker upgraded to $(marker_version)."
        else info "marker already installed (version: $(marker_version)). Pass --upgrade to update."; fi
    else
        info "Installing ${MARKER_SPEC} via pipx on $("$py" --version 2>&1) (several minutes; downloads torch + GBs of models on first run)..."
        PIPX_DEFAULT_PYTHON="$py" $PIPX install --python "$py" "$MARKER_SPEC" || error_exit "Installation failed."
        marker_ensure_deps
        marker_installed || error_exit "Install reported success but marker_single wasn't found."
        success "marker installed (version: $(marker_version))."
        command -v marker_single &>/dev/null || warn "marker CLIs aren't on PATH yet — run 'pipx ensurepath' and restart your shell (this tool resolves them from the venv regardless)."
    fi

    if [[ "$want_llama" == true && "$(marker_surya_backend)" == "llamacpp" ]]; then
        marker_llama_present || { info "Setting up the OCR backend (llama-server)..."; marker_install_llama || warn "OCR backend not installed; conversions needing OCR will fail until it is."; }
    fi
}

marker_scan() {
    local depth="$DEFAULT_DEPTH" dir=""
    while [[ $# -gt 0 ]]; do case "$1" in
        --depth) depth="$2"; shift 2 ;; -*) error_exit "marker scan: unknown flag: $1" ;; *) dir="$1"; shift ;;
    esac; done
    dir="${dir:-.}"; [[ -d "$dir" ]] || error_exit "not a directory: $dir"
    scan_files "$dir" "$depth" "${MARKER_EXTS[@]}"
}

marker_convert() {
    local out_fmt="markdown" out_dir="$DEFAULT_OUTPUT_DIR" workers="" page_range=""
    local paths=() extra=()
    while [[ $# -gt 0 ]]; do case "$1" in
        --output-format) out_fmt="$2"; shift 2 ;;
        --output-dir)    out_dir="$2"; shift 2 ;;
        --workers)       workers="$2"; shift 2 ;;
        --page-range)    page_range="$2"; shift 2 ;;
        --) shift; extra+=("$@"); break ;;
        -*) error_exit "marker convert: unknown flag: $1 (forward marker flags after --)" ;;
        *) paths+=("$1"); shift ;;
    esac; done
    (( ${#paths[@]} > 0 )) || error_exit "marker convert: no input path given."

    marker_require; marker_check_surya; marker_apply_hf_offline; mkdir -p "$out_dir"

    local mode in_dir label tmp="" bin
    if (( ${#paths[@]} == 1 )) && [[ -f "${paths[0]}" ]]; then mode=single
    elif (( ${#paths[@]} == 1 )) && [[ -d "${paths[0]}" ]]; then mode=batch; in_dir="${paths[0]}"; label="${paths[0]}"
    else mode=batch; tmp=$(link_into_tmp "${paths[@]}") || error_exit "Could not stage the selection for batch."; in_dir="$tmp"; label="${#paths[@]} selected files"; fi

    if [[ "$mode" == single ]]; then
        [[ -n "$page_range" ]] && extra+=(--page_range "$page_range")
        bin=$(resolve_bin "$MARKER_PKG" marker_single) || error_exit "marker_single not found — run setup."
        info "Converting ${paths[0]} → ${out_fmt} in ${out_dir}/"
        if "$bin" "${paths[0]}" --output_format "$out_fmt" --output_dir "$out_dir" "${extra[@]}"; then success "Done → ${out_dir}/"; open_path "$out_dir"
        else warn "Conversion failed for: ${paths[0]}"; fi
    else
        if [[ -n "$workers" ]]; then
            [[ "$workers" =~ ^[0-9]+$ ]] && (( workers >= 1 )) && extra+=(--workers "$workers") || warn "Invalid worker count '${workers}', letting marker decide."
        fi
        marker_ensure_deps
        bin=$(resolve_bin "$MARKER_PKG" marker) || { [[ -n "$tmp" ]] && rm -rf "$tmp"; error_exit "marker (batch CLI) not found — run setup."; }
        info "Converting ${label} → ${out_fmt} in ${out_dir}/"
        if "$bin" "$in_dir" --output_format "$out_fmt" --output_dir "$out_dir" "${extra[@]}"; then success "Done → ${out_dir}/"; open_path "$out_dir"
        else warn "Batch conversion reported errors."; fi
        [[ -n "$tmp" ]] && rm -rf "$tmp"
    fi
}

marker_status() {
    if marker_installed; then success "marker: installed — version $(marker_version)"
    else warn "marker: not installed. Run: protocol-droid.sh local setup --backend marker"; return 0; fi
    command -v marker_single &>/dev/null && info "CLIs on PATH: yes" || warn "CLIs on PATH: no (resolved from pipx venv; run 'pipx ensurepath' to fix)"

    local py device
    if py=$(venv_python "$MARKER_PKG"); then
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
    local be; be="$(marker_surya_backend)"
    info "OCR backend (surya): ${be}${SURYA_INFERENCE_BACKEND:+ (SURYA_INFERENCE_BACKEND=$SURYA_INFERENCE_BACKEND)}"
    if [[ "$be" == "llamacpp" ]]; then
        marker_llama_present && info "llama-server: found ($(command -v llama-server 2>/dev/null || echo "$LLAMA_CPP_BINARY"))" \
            || warn "llama-server: MISSING — OCR will fail. Run setup."
    fi
    local c; for c in "${HOME}/.cache/datalab" "${HOME}/.cache/huggingface/hub"; do
        [[ -d "$c" ]] && info "Cache ${c}: $(du -sh "$c" 2>/dev/null | awk '{print $1}')"
    done
    marker_hf_cached && info "HF models: cached (reused indefinitely — no TTL)" || warn "HF models: not cached — first conversion downloads them."
    info "HF mode (next conversion): $(marker_hf_mode)"
    [[ -z "${HF_TOKEN:-}" ]] && info "  Tip: set HF_TOKEN for higher Hub rate limits (no 'unauthenticated' warning) when online." || info "  HF_TOKEN: set"
}

marker_clear_cache() {
    [[ "${1:-}" == "--yes" ]] || error_exit "marker clear-cache: pass --yes to confirm (deletes ~/.cache/datalab; models re-download)."
    [[ -d "${HOME}/.cache/datalab" ]] || { info "No ~/.cache/datalab to clear."; return 0; }
    rm -rf "${HOME}/.cache/datalab" && success "Cache cleared (~/.cache/datalab)."
}

marker_gui() {
    marker_require; marker_check_surya; marker_apply_hf_offline
    marker_ensure_injected streamlit streamlit || error_exit "streamlit unavailable — cannot launch the GUI."
    local bin vbin; bin=$(resolve_bin "$MARKER_PKG" marker_gui) || error_exit "marker_gui not found — run setup."
    vbin=$(venv_bin_dir "$MARKER_PKG" 2>/dev/null) || vbin=""
    info "Launching the Streamlit GUI — open http://localhost:8501, Ctrl-C to stop."
    PATH="${vbin:+$vbin:}$PATH" "$bin"
}

marker_server() {
    marker_require; marker_check_surya; marker_apply_hf_offline
    marker_ensure_injected "fastapi, uvicorn, multipart" fastapi uvicorn python-multipart || error_exit "server deps unavailable — cannot launch the API server."
    local bin vbin; bin=$(resolve_bin "$MARKER_PKG" marker_server) || error_exit "marker_server not found — run setup."
    vbin=$(venv_bin_dir "$MARKER_PKG" 2>/dev/null) || vbin=""
    info "Launching marker's FastAPI server — Ctrl-C to stop."
    PATH="${vbin:+$vbin:}$PATH" "$bin"
}

marker_uninstall() {
    [[ "${1:-}" == "--yes" ]] || error_exit "marker uninstall: pass --yes to confirm (removes marker's pipx env; model caches kept)."
    marker_installed || { info "marker is not installed."; return 0; }
    resolve_pipx || error_exit "pipx not found."
    $PIPX uninstall "$MARKER_PKG" && success "marker uninstalled."
}

marker_main() {
    local cmd="${1:-}"; [[ $# -gt 0 ]] && shift || true
    case "$cmd" in
        setup) marker_setup "$@" ;; scan) marker_scan "$@" ;; convert) marker_convert "$@" ;;
        status) marker_status "$@" ;; gui) marker_gui "$@" ;; server) marker_server "$@" ;;
        clear-cache) marker_clear_cache "$@" ;; uninstall) marker_uninstall "$@" ;;
        *) error_exit "marker: unknown command: $cmd (setup|scan|convert|status|gui|server|clear-cache|uninstall)" ;;
    esac
}
