# -----------------------------------------------------------------------------
# lib/markitdown.sh — the "markitdown" backend adapter for protocol-droid.
# Runs Microsoft markitdown (light pip tool) for fast, broad-format conversion —
# incl. audio transcription, YouTube transcripts, ZIP, Outlook .msg. Uses the
# shared helpers from lib/common.sh. Contract: markitdown_main <cmd> [args] with
# setup/scan/convert/status/install-plugin/uninstall.
# -----------------------------------------------------------------------------

MID_PKG="markitdown"
# Optional pip extras markitdown[...] enables per format group.
MID_EXTRAS=(pptx docx xlsx xls pdf outlook az-doc-intel az-content-understanding audio-transcription youtube-transcription)
MID_EXTS=(pdf docx pptx xlsx xls html htm csv json xml epub zip msg png jpg jpeg gif bmp tiff tif webp wav mp3 m4a)

markitdown_installed() { resolve_bin "$MID_PKG" markitdown &>/dev/null; }
markitdown_version()   { pkg_version "$MID_PKG"; }
markitdown_require()   { markitdown_installed || error_exit "markitdown is not installed. Run: protocol-droid.sh local setup --backend markitdown"; }

# Build the "markitdown[...]" spec from an --extras value ("all" or a csv list).
markitdown_spec() {
    local extras="${1:-all}"
    [[ "$extras" == "all" ]] && { printf '%s[all]' "$MID_PKG"; return; }
    printf '%s[%s]' "$MID_PKG" "$extras"
}

markitdown_setup() {
    local extras="all" do_upgrade=false
    while [[ $# -gt 0 ]]; do case "$1" in
        --extras) extras="$2"; shift 2 ;;
        --upgrade) do_upgrade=true; shift ;;
        *) error_exit "markitdown setup: unknown flag: $1" ;;
    esac; done

    local py; py="$(find_python)" || error_exit "No markitdown-compatible Python (3.10+ with a working venv) found.
Install one, e.g.  macOS: brew install python@3.12   Linux: your pkg manager's python3.12 (or 3.11/3.10)."
    success "Using $("$py" --version 2>&1) at ${py} for markitdown's isolated environment."
    ensure_pipx

    if markitdown_installed; then
        if [[ "$do_upgrade" == true ]]; then
            $PIPX upgrade "$MID_PKG" && success "markitdown upgraded to $(markitdown_version)." || error_exit "Upgrade failed."
        else info "markitdown already installed (version: $(markitdown_version)). Pass --upgrade to update."; fi
    else
        local spec; spec="$(markitdown_spec "$extras")"
        info "Installing ${spec} via pipx on $("$py" --version 2>&1)..."
        PIPX_DEFAULT_PYTHON="$py" $PIPX install --python "$py" "$spec" || error_exit "Installation failed."
        markitdown_installed || error_exit "Install reported success but the markitdown CLI wasn't found."
        success "markitdown installed (version: $(markitdown_version))."
        command -v markitdown &>/dev/null || warn "markitdown isn't on PATH yet — run 'pipx ensurepath' and restart your shell (this tool resolves it from the venv regardless)."
        if [[ "$spec" == *audio* || "$spec" == *"[all]"* ]] && ! command -v ffmpeg &>/dev/null; then
            warn "Audio (mp3) transcription also needs ffmpeg on your system — install it separately (brew/apt/dnf install ffmpeg)."
        fi
    fi
}

markitdown_scan() {
    local depth="$DEFAULT_DEPTH" dir=""
    while [[ $# -gt 0 ]]; do case "$1" in
        --depth) depth="$2"; shift 2 ;; -*) error_exit "markitdown scan: unknown flag: $1" ;; *) dir="$1"; shift ;;
    esac; done
    dir="${dir:-.}"; [[ -d "$dir" ]] || error_exit "not a directory: $dir"
    scan_files "$dir" "$depth" "${MID_EXTS[@]}"
}

# markitdown has no batch mode: one .md per input file (loop). Flags:
#   --output-dir DIR   (default ./converted)
#   -- <markitdown args...>   forwarded verbatim (e.g. --use-plugins, -d, -e URL)
markitdown_convert() {
    local out_dir="$DEFAULT_OUTPUT_DIR" paths=() extra=()
    while [[ $# -gt 0 ]]; do case "$1" in
        --output-dir) out_dir="$2"; shift 2 ;;
        --output-format) shift 2 ;;   # accepted+ignored (markitdown only emits Markdown)
        --) shift; extra+=("$@"); break ;;
        -*) error_exit "markitdown convert: unknown flag: $1 (forward markitdown flags after --)" ;;
        *) paths+=("$1"); shift ;;
    esac; done
    (( ${#paths[@]} > 0 )) || error_exit "markitdown convert: no input path given."

    markitdown_require
    local bin; bin=$(resolve_bin "$MID_PKG" markitdown) || error_exit "markitdown not found — run setup."
    mkdir -p "$out_dir"

    # Expand a single directory argument into its convertible files.
    if (( ${#paths[@]} == 1 )) && [[ -d "${paths[0]}" ]]; then
        local listed; listed=$(scan_files "${paths[0]}" "$DEFAULT_DEPTH" "${MID_EXTS[@]}") || { warn "No supported files under ${paths[0]}."; return 0; }
        mapfile -t paths <<< "$listed"
    fi

    local ok=0 fail=0 f base stem out
    for f in "${paths[@]}"; do
        [[ -f "$f" ]] || { warn "Skipping (not a file): $f"; continue; }
        base="$(basename "$f")"; stem="${base%.*}"; out="$(unique_out "$out_dir" "$stem" md)"
        info "Converting ${f} ..."
        if "$bin" "$f" -o "$out" "${extra[@]}"; then success "→ ${out}"; ok=$((ok+1)); else warn "Failed: ${f}"; fail=$((fail+1)); fi
    done
    success "Done — ${ok} converted, ${fail} failed → ${out_dir}/"
    (( ok > 0 )) && open_path "$out_dir"
}

markitdown_status() {
    if markitdown_installed; then success "markitdown: installed — version $(markitdown_version)"
    else warn "markitdown: not installed. Run: protocol-droid.sh local setup --backend markitdown"; return 0; fi
    command -v markitdown &>/dev/null && info "CLI on PATH: yes" || warn "CLI on PATH: no (resolved from pipx venv; run 'pipx ensurepath' to fix)"
    local py; py=$(venv_python "$MID_PKG" 2>/dev/null) && info "Env Python: $("$py" --version 2>&1)"
    command -v ffmpeg &>/dev/null && info "ffmpeg: found (audio transcription OK)" || info "ffmpeg: not found (needed only for mp3 audio transcription)"
    [[ -n "${MARKITDOWN_DOCINTEL_ENDPOINT:-}" ]] && info "Document Intelligence endpoint: set (env)" || info "Document Intelligence endpoint: not set (MARKITDOWN_DOCINTEL_ENDPOINT)"
    local bin; bin=$(resolve_bin "$MID_PKG" markitdown) && { echo >&2; info "Installed plugins (markitdown --list-plugins):"; "$bin" --list-plugins 2>/dev/null || warn "Could not list plugins."; }
}

markitdown_install_plugin() {
    markitdown_require
    local pkg="${1:-}"; [[ -n "$pkg" ]] || error_exit "markitdown install-plugin: give a pip package (e.g. markitdown-sample-plugin)."
    resolve_pipx || error_exit "pipx not found."
    info "Injecting ${pkg} into markitdown's environment..."
    $PIPX inject "$MID_PKG" "$pkg" && success "Installed ${pkg}. Enable it per-conversion with --use-plugins." || warn "Could not install ${pkg}."
}

markitdown_uninstall() {
    [[ "${1:-}" == "--yes" ]] || error_exit "markitdown uninstall: pass --yes to confirm (removes markitdown's pipx env)."
    markitdown_installed || { info "markitdown is not installed."; return 0; }
    resolve_pipx || error_exit "pipx not found."
    $PIPX uninstall "$MID_PKG" && success "markitdown uninstalled."
}

markitdown_main() {
    local cmd="${1:-}"; [[ $# -gt 0 ]] && shift || true
    case "$cmd" in
        setup) markitdown_setup "$@" ;; scan) markitdown_scan "$@" ;; convert) markitdown_convert "$@" ;;
        status) markitdown_status "$@" ;; install-plugin) markitdown_install_plugin "$@" ;; uninstall) markitdown_uninstall "$@" ;;
        *) error_exit "markitdown: unknown command: $cmd (setup|scan|convert|status|install-plugin|uninstall)" ;;
    esac
}
