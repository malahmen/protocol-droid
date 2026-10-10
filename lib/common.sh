# shellcheck shell=bash
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

# --- exit codes --------------------------------------------------------------
#
# A scripted caller could not tell a failed batch from an empty one: both left
# with 0. Every `convert` path now ends on one of these, and `auto` reports the
# worst of the two backends it ran.
#
#   0  everything asked for was converted
#   1  at least one conversion failed (also every usage error, via error_exit)
#   2  a named input does not exist, or is not a file -- nothing was attempted
#      for it, which is a caller mistake rather than a conversion failure
#   3  nothing to convert: the folder held no file either backend handles
#
# 2 and 3 are deliberately distinct. "You named a file that is not there" and
# "this directory has nothing I convert" need different responses from a cron
# job, and a tool that answers both with silence and 0 teaches you to trust a
# backup that is not happening.
# shellcheck disable=SC2034  # consumed by lib/<backend>.sh and protocol-droid.sh
EX_OK=0
# shellcheck disable=SC2034
EX_FAILED=1
# shellcheck disable=SC2034
EX_NO_INPUT=2
# shellcheck disable=SC2034
EX_NOTHING=3

# shellcheck disable=SC2034  # consumed by lib/<backend>.sh and protocol-droid.sh
DEFAULT_OUTPUT_DIR="./converted"
# shellcheck disable=SC2034
DEFAULT_DEPTH=3
PIPX=""   # resolved by resolve_pipx()

os_family() {
    case "$(uname -s)" in
        Darwin) echo "macos" ;;
        Linux)  echo "linux" ;;
        *)      echo "other" ;;
    esac
}

# Reveal a path in the desktop file manager. PROTOCOL_DROID_NO_OPEN=1 disables
# it (CI, cron, TUIs — and auto mode, which opens the output dir once itself).
open_path() {
    [[ "${PROTOCOL_DROID_NO_OPEN:-}" == 1 ]] && return 0
    local p="$1"
    if command -v xdg-open &>/dev/null; then xdg-open "$p" &>/dev/null &
    elif command -v open &>/dev/null; then open "$p"; fi
}

# A Python 3.10–3.13 with a working venv, preferring the most settled version.
# Never returns 3.14+ (marker/torch don't support it, and onnxruntime — pulled by
# markitdown — lags there too). Echoes the interpreter path; non-zero if none.
PYTHON_VERSIONS=(3.12 3.11 3.13 3.10)
_python_venv_ok() { local tmp rc=1; tmp="$(mktemp -d)" || return 1; "$1" -m venv "${tmp}/v" &>/dev/null && rc=0; rm -rf "$tmp"; return "$rc"; }
find_python() {
    local v path
    for v in "${PYTHON_VERSIONS[@]}"; do
        path="$(command -v "python${v}" 2>/dev/null || true)"
        [[ -n "$path" ]] && _python_venv_ok "$path" && { printf '%s' "$path"; return 0; }
    done
    # An unversioned python3 (pyenv/uv shim, distro default) whose version is in range.
    path="$(command -v python3 2>/dev/null || true)"
    [[ -n "$path" ]] || return 1
    v="$("$path" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
    for _ in "${PYTHON_VERSIONS[@]}"; do
        [[ "$v" == "$_" ]] && _python_venv_ok "$path" && { printf '%s' "$path"; return 0; }
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
    # `|| true`: under pipefail one unreadable subfolder would otherwise fail the
    # whole pipeline and blank the scan; find still lists what it could read.
    found=$({ find "$dir" -maxdepth "$depth" -type f \( "${find_args[@]}" \) \
        ! -path "*/node_modules/*" ! -path "*/.git/*" \
        ! -path "*/converted/*" ! -path "*/marker-output/*" ! -path "*/markitdown-output/*" \
        2>/dev/null || true; } | sed 's|^\./||' | sort)
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

# Output path <dir>/<stem>.<ext>.
#
# OVERWRITES by default. It used to disambiguate a collision with _N, which
# meant a second run of the same corpus produced doc.md AND doc_2.md: the
# downstream ingestion pipeline then indexed the same document two or three
# times over, and no step in between could tell which copy was current. A
# re-conversion replacing its own earlier output is the behaviour that matches
# what the tool is for.
#
# `clobber=false` (markitdown convert --no-clobber) restores the _N behaviour
# for the case where the old output is the thing you want to keep.
out_path() {
    local dir="$1" stem="$2" ext="$3" clobber="${4:-true}" out="${1}/${2}.${3}"
    if [[ "$clobber" != true && -e "$out" ]]; then
        local i=2; while [[ -e "${dir}/${stem}_${i}.${ext}" ]]; do i=$((i+1)); done
        out="${dir}/${stem}_${i}.${ext}"
    fi
    printf '%s' "$out"
}

# --- provenance --------------------------------------------------------------
#
# One sidecar format for every execution mode: this calls the same
# provenance.py the containerized worker imports, rather than writing a second
# JSON builder in bash that would drift from it.
#
# PROTOCOL_DROID_NO_PROVENANCE=1 turns it off.
now_epoch() { printf '%s' "${EPOCHSECONDS:-$(date +%s)}"; }

# provenance_write <out-folder> <source> <backend> <pkg=ver> <started> <finished>
#                  <output-format> <input-root|""> <sidecar-name> <output-file...>
#                  -- <argv that ran...>
#
# <sidecar-name> matters where the output folder is shared between documents:
# markitdown writes one .md per input into one folder, so a single
# provenance.json there would be overwritten by the next document in the same
# run, leaving one sidecar for the batch and no error to show it. It passes
# <stem>.md.provenance.json, and names its own output file so the sidecar does
# not claim every other .md in the folder.
#
# Returns non-zero when the sidecar was not written, and the callers count that
# as a failed file. A conversion whose provenance is missing is not a
# conversion you can make a re-run decision about later, and the hole is
# invisible until the moment that decision is needed -- which is the whole
# reason the sidecar exists.
provenance_write() {
    [[ "${PROTOCOL_DROID_NO_PROVENANCE:-}" == 1 ]] && return 0
    local out="$1" src="$2" backend="$3" ver="$4" started="$5" finished="$6"
    local fmt="$7" root="$8" name="$9"; shift 9
    local outputs=()
    while (( $# )) && [[ "$1" != "--" ]]; do outputs+=("$1"); shift; done
    [[ "${1:-}" == "--" ]] && shift
    local script="${HERE:-}/provenance.py"
    if [[ ! -f "$script" ]]; then
        warn "provenance: ${script} is missing — no sidecar written for ${src}."
        return 1
    fi
    if ! command -v python3 &>/dev/null; then
        warn "provenance: python3 not found — no sidecar written for ${src}."
        return 1
    fi
    local args=(--source "$src" --output-dir "$out" --backend "$backend"
                --started "$started" --finished "$finished" --version "$ver")
    [[ -n "$fmt" ]]  && args+=(--output-format "$fmt")
    [[ -n "$root" ]] && args+=(--input-root "$root")
    [[ -n "$name" ]] && args+=(--name "$name")
    local o; for o in "${outputs[@]}"; do args+=(--output "$o"); done
    # --command is argparse.REMAINDER, so it has to come last.
    (( $# )) && args+=(--command "$@")
    python3 "$script" "${args[@]}" >/dev/null || {
        warn "provenance: could not write a sidecar in ${out}."
        return 1
    }
}
