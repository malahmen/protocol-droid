#!/usr/bin/env bash
# container mode — marker from the service image, run once on local files.
#
# Nothing is installed on the host: no pipx, no particular Python, no torch.
# The image is the service's own (Dockerfile, tag marker-service:latest), so a
# container conversion and a queued one run the same marker, the same
# convert_document and write the same provenance sidecar.
#
# Mounts are made at the SAME path inside the container, so no path needs
# rewriting: each input's folder read-only, the output folder read-write. The
# models live in a named volume, so they download once, not per run.
#
# Sourced by protocol-droid.sh; expects lib/common.sh (and lib/marker.sh for
# MARKER_EXTS) to be loaded already.

CTR_IMAGE="marker-service:latest"                         # the service image (lib/service.sh, compose, k8s)
CTR_MODELS_VOLUME="${PROTOCOL_DROID_MODELS_VOLUME:-protocol-droid-models}"
CTR_RUNTIME="${PROTOCOL_DROID_RUNTIME:-}"                # podman | docker (empty: podman if present, else docker)
CTR_APP_UID=1000                                         # the image's `app` user

ctr_runtime() {
    local rt="$CTR_RUNTIME"
    if [[ -z "$rt" ]]; then
        if command -v podman &>/dev/null; then rt=podman
        elif command -v docker &>/dev/null; then rt=docker
        else error_exit "container mode needs podman or docker on PATH."; fi
    fi
    case "$rt" in podman|docker) ;; *) error_exit "--runtime must be podman or docker, got '${rt}'." ;; esac
    command -v "$rt" &>/dev/null || error_exit "${rt} not found on PATH."
    printf '%s' "$rt"
}

_ctr_abs_dir() { (cd "$1" 2>/dev/null && pwd -P); }
_ctr_abs_file() { printf '%s/%s' "$(_ctr_abs_dir "$(dirname "$1")")" "$(basename "$1")"; }

# The run options every command shares: no leftovers, the model cache, and the
# image's own uid mapped to the caller where the runtime can do that.
ctr_base_args() {
    local rt="$1"
    CTR_ARGS=(run --rm -i -v "${CTR_MODELS_VOLUME}:/models"
        # Bind mounts of home folders on an SELinux host (Fedora, Bazzite)
        # would otherwise need relabelling, which is not ours to do to them.
        --security-opt label=disable
        -e "BENCH_HOST=$(hostname -s 2>/dev/null || hostname)")
    if [[ "$rt" == podman ]]; then
        # Rootless podman: the caller becomes the image's app user, so the
        # model volume (owned by app) is writable and outputs belong to the caller.
        CTR_ARGS+=(--userns="keep-id:uid=${CTR_APP_UID},gid=${CTR_APP_UID}")
    elif [[ "$(uname -s)" == Linux && "$(id -u)" != "$CTR_APP_UID" ]]; then
        warn "docker runs the image as uid ${CTR_APP_UID}; outputs will belong to that uid, not to you ($(id -u))."
    fi
}

ctr_require_image() {
    local rt="$1"
    "$rt" image inspect "$CTR_IMAGE" &>/dev/null \
        || error_exit "image ${CTR_IMAGE} is not built — run: protocol-droid.sh container build"
}

# ctr_collect <path>... — fill CTR_FILES with the files to convert, absolute:
# a file stands for itself, a folder for its convertible files (marker's
# types). CTR_MISSING counts the paths that were neither. Fills globals rather
# than printing, so the count survives (a $(...) would lose it in a subshell).
ctr_collect() {
    local p listed f
    CTR_FILES=(); CTR_MISSING=0
    for p in "$@"; do
        if [[ -d "$p" ]]; then
            listed="$(scan_files "$p" "$DEFAULT_DEPTH" "${MARKER_EXTS[@]}")" || { warn "No convertible files under ${p}."; continue; }
            while IFS= read -r f; do CTR_FILES+=("$(_ctr_abs_file "$f")"); done <<< "$listed"
        elif [[ -f "$p" ]]; then
            CTR_FILES+=("$(_ctr_abs_file "$p")")
        else
            warn "Not a file: ${p}"
            CTR_MISSING=$((CTR_MISSING + 1))
        fi
    done
}

# Mount each input folder read-only and the output folder read-write, each at
# its own path. A folder that is both is mounted once, writable.
ctr_mount_args() {
    local out="$1"; shift
    local -a dirs=() ; local f d
    for f in "$@"; do dirs+=("$(dirname "$f")"); done
    while IFS= read -r d; do
        [[ -z "$d" ]] && continue
        [[ "$d" == / ]] && error_exit "refusing to mount / into the container."
        if [[ -n "$out" && "$d" == "$out" ]]; then continue; fi
        CTR_ARGS+=(-v "${d}:${d}:ro")
    done < <(printf '%s\n' ${dirs[@]+"${dirs[@]}"} | sort -u)
    [[ -n "$out" ]] && CTR_ARGS+=(-v "${out}:${out}")
    return 0
}

ctr_build() {
    local rt; rt="$(ctr_runtime)"
    info "Building ${CTR_IMAGE} with ${rt} (large: marker + torch + llama-server)..."
    "$rt" build -t "$CTR_IMAGE" "$HERE" || error_exit "Build failed."
    success "Built ${CTR_IMAGE}."
}

ctr_convert() {
    local out_dir="$DEFAULT_OUTPUT_DIR" fmt="markdown" page_range="" force_ocr=false paths=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --output-dir)    out_dir="$2"; shift 2 ;;
            --output-format) fmt="$2"; shift 2 ;;
            --page-range)    page_range="$2"; shift 2 ;;
            --force-ocr)     force_ocr=true; shift ;;
            -*)              error_exit "container convert: unknown flag $1" ;;
            *)               paths+=("$1"); shift ;;
        esac
    done
    (( ${#paths[@]} )) || error_exit "container convert: give one or more files or folders."
    local rt files=() rc=0; rt="$(ctr_runtime)"
    ctr_require_image "$rt"
    ctr_collect "${paths[@]}"
    files=(${CTR_FILES[@]+"${CTR_FILES[@]}"})
    if (( ${#files[@]} == 0 )); then
        (( CTR_MISSING > 0 )) && return "$EX_NO_INPUT"
        return "$EX_NOTHING"
    fi
    mkdir -p "$out_dir" || error_exit "cannot create ${out_dir}"
    out_dir="$(_ctr_abs_dir "$out_dir")"

    ctr_base_args "$rt"
    ctr_mount_args "$out_dir" "${files[@]}"
    local -a cli=(python cli.py convert --output-format "$fmt" --output-dir "$out_dir")
    [[ -n "$page_range" ]] && cli+=(--page-range "$page_range")
    [[ "$force_ocr" == true ]] && cli+=(--force-ocr)
    info "container: ${#files[@]} file(s) → marker (${rt}, ${CTR_IMAGE})"
    "$rt" "${CTR_ARGS[@]}" "$CTR_IMAGE" "${cli[@]}" "${files[@]}" || rc=$?
    (( rc == 0 )) && [[ -d "$out_dir" ]] && open_path "$out_dir"
    (( rc == 0 && CTR_MISSING > 0 )) && rc="$EX_NO_INPUT"
    return "$rc"
}

ctr_bench() {
    local runs=1 report="" paths=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --runs)   runs="$2"; shift 2 ;;
            --report) report="$2"; shift 2 ;;
            -*)       error_exit "container bench: unknown flag $1" ;;
            *)        paths+=("$1"); shift ;;
        esac
    done
    (( ${#paths[@]} )) || error_exit "container bench: give the documents to measure (files or folders)."
    [[ "$runs" =~ ^[1-9][0-9]*$ ]] || error_exit "--runs must be a positive number."
    local rt files=() rc=0; rt="$(ctr_runtime)"
    ctr_require_image "$rt"
    ctr_collect "${paths[@]}"
    files=(${CTR_FILES[@]+"${CTR_FILES[@]}"})
    (( ${#files[@]} )) || error_exit "nothing to measure."
    local host; host="$(hostname -s 2>/dev/null || hostname)"
    [[ -n "$report" ]] || report="./marker-bench-${host}-$(date -u +%Y%m%dT%H%M%SZ).json"

    ctr_base_args "$rt"
    ctr_mount_args "" "${files[@]}"
    info "bench: ${#files[@]} document(s) × ${runs} run(s) on ${host} (${rt}) - the first run also downloads the models"
    # The report is the container's stdout, written here on the host: no
    # folder of the caller's has to be writable from inside.
    "$rt" "${CTR_ARGS[@]}" "$CTR_IMAGE" python cli.py bench --runs "$runs" "${files[@]}" > "$report" || rc=$?
    if [[ -s "$report" ]]; then success "Report: ${report}"; else warn "No report written."; fi
    return "$rc"
}

ctr_status() {
    local rt; rt="$(ctr_runtime)"
    info "runtime: ${rt}"
    if "$rt" image inspect "$CTR_IMAGE" &>/dev/null; then success "image: ${CTR_IMAGE}"; else warn "image: ${CTR_IMAGE} not built (container build)"; fi
    if "$rt" volume inspect "$CTR_MODELS_VOLUME" &>/dev/null; then
        success "model cache: volume ${CTR_MODELS_VOLUME}"
    else
        info "model cache: volume ${CTR_MODELS_VOLUME} not created yet (the first conversion downloads the models)"
    fi
}

container_main() {
    local rest=() seen_dd=false
    while [[ $# -gt 0 ]]; do
        if [[ "$seen_dd" == false && "$1" == "--" ]]; then seen_dd=true; rest+=("$1"); shift; continue; fi
        if [[ "$seen_dd" == false && "$1" == "--runtime" ]]; then CTR_RUNTIME="$2"; shift 2; continue; fi
        rest+=("$1"); shift
    done
    local cmd="${rest[0]:-}"; rest=("${rest[@]:1}")
    case "$cmd" in
        build)   ctr_build ;;
        convert) ctr_convert ${rest[@]+"${rest[@]}"} ;;
        bench)   ctr_bench ${rest[@]+"${rest[@]}"} ;;
        status)  ctr_status ;;
        ""|-h|--help) usage ;;
        *)       error_exit "unknown container command: ${cmd} (build|convert|bench|status)" ;;
    esac
}
