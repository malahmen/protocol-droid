# -----------------------------------------------------------------------------
# lib/common.sh — shared helpers for the protocol-droid backends.
# Sourced by protocol-droid.sh (which owns `set -euo pipefail`). Defines logging,
# OS detection, pipx/Python resolution, a folder scanner, and batch staging that
# every local backend (marker, markitdown) reuses. Backend-specific logic lives
# in lib/<backend>.sh; the containerized service lives in lib/service.sh.
# -----------------------------------------------------------------------------

if [[ -t 2 ]]; then C_G=$'\033[0;32m'; C_Y=$'\033[0;33m'; C_R=$'\033[0;31m'; C_C=$'\033[0;36m'; C_N=$'\033[0m'
else C_G=""; C_Y=""; C_R=""; C_C=""; C_N=""; fi
info()       { printf '%s[info]%s  %s\n' "$C_C" "$C_N" "$*" >&2; }
success()    { printf '%s[ok]%s    %s\n' "$C_G" "$C_N" "$*" >&2; }
warn()       { printf '%s[warn]%s  %s\n' "$C_Y" "$C_N" "$*" >&2; }
error_exit() { printf '%s[error]%s %s\n' "$C_R" "$C_N" "$*" >&2; exit 1; }

DEFAULT_OUTPUT_DIR="./converted"
DEFAULT_DEPTH=3
PIPX=""   # resolved by resolve_pipx()

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

# A Python 3.10–3.13 with a working venv, preferring the most settled version.
# Never returns 3.14+ (marker/torch don't support it, and onnxruntime — pulled by
# markitdown — lags there too). Echoes the interpreter path; non-zero if none.
find_python() {
    local v path tmp
    for v in 3.12 3.11 3.13 3.10; do
        path="$(command -v "python${v}" 2>/dev/null || true)"
        [[ -n "$path" ]] || continue
        tmp="$(mktemp -d)"
        if "$path" -m venv "${tmp}/v" &>/dev/null; then rm -rf "$tmp"; printf '%s' "$path"; return 0; fi
        rm -rf "$tmp"
    done
    return 1
}

resolve_pipx() {
    if command -v pipx &>/dev/null; then PIPX="pipx"; return 0; fi
    if command -v python3 &>/dev/null && python3 -m pipx --version &>/dev/null; then PIPX="python3 -m pipx"; return 0; fi
    PIPX=""; return 1
}

# Install pipx as part of a backend's `setup` (never as a silent side effect).
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

# The directory holding pipx app venvs (falls back to the default location).
pipx_venvs_dir() { resolve_pipx || return 1; $PIPX environment --value PIPX_LOCAL_VENVS 2>/dev/null || echo "${HOME}/.local/pipx/venvs"; }

# resolve_bin <pkg> <bin> — a runnable path for a CLI: PATH first, else the
# pkg's pipx venv bin (covers a not-yet-sourced PATH right after install).
resolve_bin() {
    local pkg="$1" bin="$2"
    command -v "$bin" &>/dev/null && { echo "$bin"; return 0; }
    local cand; cand="$(pipx_venvs_dir)/${pkg}/bin/${bin}"
    [[ -x "$cand" ]] && { echo "$cand"; return 0; }
    return 1
}

venv_python()  { local pkg="$1" c; c="$(pipx_venvs_dir)/${pkg}/bin/python"; [[ -x "$c" ]] && { echo "$c"; return 0; }; return 1; }
venv_bin_dir() { local pkg="$1"; printf '%s' "$(pipx_venvs_dir)/${pkg}/bin"; }
pkg_version()  { resolve_pipx || { echo "unknown"; return; }; $PIPX list --short 2>/dev/null | awk -v p="$1" '$1==p{print $2}' | head -1; }

# scan_files <dir> <depth> <ext...> — echo convertible files, one per line.
scan_files() {
    local dir="$1" depth="$2"; shift 2
    local exts=("$@") find_args=() ext found
    for ext in "${exts[@]}"; do find_args+=(-iname "*.${ext}" -o); done
    unset 'find_args[${#find_args[@]}-1]'   # drop trailing -o
    found=$(find "$dir" -maxdepth "$depth" -type f \( "${find_args[@]}" \) \
        ! -path "*/node_modules/*" ! -path "*/.git/*" \
        ! -path "*/converted/*" ! -path "*/marker-output/*" ! -path "*/markitdown-output/*" \
        2>/dev/null | sed 's|^\./||' | sort)
    [[ -n "$found" ]] || return 1
    printf '%s\n' "$found"
}

# Symlink files into a fresh temp dir (collision-safe names) and echo the dir,
# so a batch CLI can process an arbitrary selection loading models once.
link_into_tmp() {
    local tmp; tmp=$(mktemp -d "${TMPDIR:-/tmp}/pd-sel.XXXXXX") || return 1
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

# Unique output path <dir>/<stem>.<ext>, disambiguating collisions with _N.
unique_out() {
    local dir="$1" stem="$2" ext="$3" out="${1}/${2}.${3}"
    if [[ -e "$out" ]]; then local i=2; while [[ -e "${dir}/${stem}_${i}.${ext}" ]]; do i=$((i+1)); done; out="${dir}/${stem}_${i}.${ext}"; fi
    printf '%s' "$out"
}
