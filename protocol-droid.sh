#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# protocol-droid.sh — manage document→Markdown conversion, gum-free & flag-driven.
# "I am fluent in over six million forms of communication."
#
# A multi-backend conversion engine that PREPARES documents for a downstream
# RAG/LLM ingestion pipeline (it converts; it does NOT chunk/embed/index). Two
# local backends, plus a containerized service:
#
#   local --backend marker      datalab-to/marker — heavy, high-fidelity PDF/OCR/layout.
#   local --backend markitdown  Microsoft markitdown — light, broad formats
#                               (audio transcription, YouTube, ZIP, Outlook .msg).
#   local --backend auto        route each file by type (PDF/images -> marker,
#                               everything else -> markitdown).
#   service                     containerized marker workers (Redis + API), Docker/K8s.
#
# Gum-free and non-interactive: every choice is a flag, so it fits a Makefile,
# CI, or cron. scomp-link ships a thin gum TUI that builds these flags. Backend
# logic lives in lib/<backend>.sh; shared helpers in lib/common.sh. --help below.
# -----------------------------------------------------------------------------

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${HERE}/lib/common.sh"

usage() {
    cat >&2 <<'EOF'
protocol-droid — multi-backend document→Markdown conversion

USAGE
  protocol-droid.sh local   <command> [--backend marker|markitdown|auto] [flags]
  protocol-droid.sh service <command> [--target docker|k8s] [flags]

LOCAL — run a converter on this machine (isolated in a pipx environment).
  --backend selects the tool (default: marker). Commands vary by backend:

  marker (default) — datalab-to/marker; high-fidelity PDF/OCR/layout (heavy):
    setup [--upgrade] [--no-llama]   install/upgrade marker + OCR backend
    scan [--depth N] DIR             list convertible files
    convert [--output-format F] [--output-dir D] [--page-range R] [--workers N]
            PATH... [-- <marker args…>]      one file, several files, or a folder
    status | gui | server | clear-cache --yes | uninstall --yes

  markitdown — Microsoft markitdown; fast, broad formats (light):
    setup [--extras all|pdf,docx,…] [--upgrade]
    scan [--depth N] DIR
    convert [--output-dir D] PATH... [-- <markitdown args…>]   (e.g. --use-plugins, -d, -e URL)
    status | install-plugin <pip-pkg> | uninstall --yes

  auto — convert only; routes PDF/images -> marker, everything else -> markitdown:
    convert [--output-dir D] PATH...

  convert PATH rules: one file -> that file; a folder -> its convertible files;
  several files -> batch. marker/markitdown feature flags go after '--'.

SERVICE — containerized marker workers (Redis queue + FastAPI enqueue API).
  build | deploy [--input DIR] [--output DIR] | status | logs |
  scale --replicas N | enqueue [--dir PATH] | teardown --yes
  Add --target docker|k8s to any service command (default: docker).

Prepares a corpus for a downstream RAG pipeline — it does not chunk/embed/index.
The k8s objects live in the 'marker' namespace since the service runs marker.
EOF
}

# Convert with backend=auto: partition inputs by extension and hand each group
# to the backend that fits (PDF/images -> marker; the rest -> markitdown).
AUTO_MARKER_EXTS=(pdf png jpg jpeg tiff tif webp gif bmp)
_ext_lower() { local b="${1##*/}"; local e="${b##*.}"; printf '%s' "${e,,}"; }
_is_marker_ext() { local e="$1" m; for m in "${AUTO_MARKER_EXTS[@]}"; do [[ "$e" == "$m" ]] && return 0; done; return 1; }

auto_convert() {
    local out_dir="$DEFAULT_OUTPUT_DIR" paths=()
    while [[ $# -gt 0 ]]; do case "$1" in
        --output-dir) out_dir="$2"; shift 2 ;;
        --) shift ;;   # no per-tool passthrough in auto (backends differ)
        -*) error_exit "auto convert: unsupported flag in auto mode: $1 (pick --backend marker|markitdown for tool flags)" ;;
        *) paths+=("$1"); shift ;;
    esac; done
    (( ${#paths[@]} > 0 )) || error_exit "auto convert: no input path given."

    # Expand a single directory into the union of both backends' file types.
    if (( ${#paths[@]} == 1 )) && [[ -d "${paths[0]}" ]]; then
        local union=("${MARKER_EXTS[@]}" "${MID_EXTS[@]}") listed
        listed=$(scan_files "${paths[0]}" "$DEFAULT_DEPTH" "${union[@]}") || error_exit "No supported files under ${paths[0]}."
        mapfile -t paths <<< "$listed"
    fi

    local m_files=() d_files=() f e
    for f in "${paths[@]}"; do
        [[ -f "$f" ]] || { warn "Skipping (not a file): $f"; continue; }
        e="$(_ext_lower "$f")"
        if _is_marker_ext "$e"; then m_files+=("$f"); else d_files+=("$f"); fi
    done

    (( ${#m_files[@]} > 0 )) && { info "auto: ${#m_files[@]} file(s) → marker"; marker_convert --output-dir "$out_dir" "${m_files[@]}"; }
    (( ${#d_files[@]} > 0 )) && { info "auto: ${#d_files[@]} file(s) → markitdown"; markitdown_convert --output-dir "$out_dir" "${d_files[@]}"; }
    (( ${#m_files[@]} == 0 && ${#d_files[@]} == 0 )) && warn "auto: nothing to convert."
    return 0
}

local_main() {
    source "${HERE}/lib/marker.sh"
    source "${HERE}/lib/markitdown.sh"

    # Extract --backend (before any '--' passthrough); keep the rest verbatim.
    local backend="marker" rest=() seen_dd=false
    while [[ $# -gt 0 ]]; do
        if [[ "$seen_dd" == false && "$1" == "--" ]]; then seen_dd=true; rest+=("$1"); shift; continue; fi
        if [[ "$seen_dd" == false && "$1" == "--backend" ]]; then backend="$2"; shift 2; continue; fi
        rest+=("$1"); shift
    done

    local cmd="${rest[0]:-}"; rest=("${rest[@]:1}")
    [[ -z "$cmd" ]] && { usage; return 0; }
    case "$backend" in
        marker)     marker_main "$cmd" "${rest[@]}" ;;
        markitdown) markitdown_main "$cmd" "${rest[@]}" ;;
        auto)       [[ "$cmd" == convert ]] || error_exit "--backend auto only supports 'convert'."; auto_convert "${rest[@]}" ;;
        *)          error_exit "unknown --backend: $backend (marker|markitdown|auto)" ;;
    esac
}

main() {
    local mode="${1:-}"; [[ $# -gt 0 ]] && shift || true
    case "$mode" in
        local)   local_main "$@" ;;
        service) source "${HERE}/lib/service.sh"; service_main "$@" ;;
        ""|-h|--help) usage ;;
        *) usage; error_exit "unknown mode: $mode (expected 'local' or 'service')" ;;
    esac
}

main "$@"
