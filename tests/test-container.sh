#!/usr/bin/env bash
# container mode, against a stub runtime that records what it was asked.
#
# Nothing is built or run: `docker`/`podman` are recording stubs. This checks
# the decisions protocol-droid.sh makes - which files, which mounts, as whom,
# which arguments reach cli.py - not marker in the image (cli.py has its own
# tests, and a real run was verified by hand).
set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PD="${PROTOCOL_DROID:-${TEST_DIR}/../protocol-droid.sh}"
[[ -f "$PD" ]] || { echo "protocol-droid.sh not found at $PD" >&2; exit 1; }

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
T="$(cd "$T" && pwd -P)"
export PROTOCOL_DROID_NO_OPEN=1
FAILS=0
check() { local d="$1"; shift; if "$@"; then echo "   ok   - $d"; else echo "   FAIL - $d"; FAILS=$((FAILS + 1)); fi; }
has()    { grep -qF -- "$1" "$2"; }
lacks()  { ! grep -qF -- "$1" "$2"; }
no_arg() { ! grep -qxF -- "$1" "$2"; }   # no line is exactly $1 (one argument per line)

mkdir -p "$T/bin"
for rt in docker podman; do
    cat > "$T/bin/$rt" <<'EOF'
#!/usr/bin/env bash
{ echo "=== $(basename "$0")"; printf '%s\n' "$@"; } >> "$STUB_LOG"
case "${1:-} ${2:-}" in
    "image inspect")  [[ -f "$STUB_BUILT" ]] ;;
    "volume inspect") exit 1 ;;
    build*)           : > "$STUB_BUILT" ;;
    "run "*)          # bench prints its report on stdout
                      for a in "$@"; do [[ "$a" == bench ]] && echo '{"schema": "protocol-droid/bench/1"}'; done
                      exit "${STUB_RUN_RC:-0}" ;;
    *)                exit 0 ;;
esac
EOF
    chmod +x "$T/bin/$rt"
done
export PATH="$T/bin:$PATH" STUB_LOG="$T/log" STUB_BUILT="$T/built"
unset PROTOCOL_DROID_RUNTIME

mkdir -p "$T/docs/sub" "$T/work"
: > "$T/docs/a.pdf"; : > "$T/docs/sub/b.png"; : > "$T/docs/notes.txt"
RC=0
run() { : > "$STUB_LOG"; (cd "$T/work" && bash "$PD" "$@") > "$T/out" 2>&1; RC=$?; }
args() { sed -n '/^run$/,$p' "$STUB_LOG" > "$T/args"; }

echo "## image"
run container convert --runtime docker "$T/docs/a.pdf"
check "no image: refused, naming the fix" has "container build" "$T/out"
check "  and nothing is run" no_arg "run" "$STUB_LOG"
run container build --runtime docker
check "build tags the service image" has "marker-service:latest" "$STUB_LOG"

echo "## convert"
run container convert --runtime docker --output-dir "$T/out-dir" --output-format json --page-range 0-2 "$T/docs"
args
check "a folder stands for its convertible files" has "$T/docs/a.pdf" "$T/args"
check "  including subfolders" has "$T/docs/sub/b.png" "$T/args"
check "  but not files marker cannot read" lacks "notes.txt" "$T/args"
check "inputs are mounted read-only, at their own path" has "$T/docs:$T/docs:ro" "$T/args"
check "the output folder is created and mounted read-write" has "$T/out-dir:$T/out-dir" "$T/args"
check "  (it exists)" test -d "$T/out-dir"
check "the models live in a named volume" has "protocol-droid-models:/models" "$T/args"
check "cli.py convert gets the options" has "--page-range" "$T/args"
check "  and the format" has "json" "$T/args"
check "--runtime is not passed on" no_arg "--runtime" "$T/args"

run container convert --runtime docker --output-dir "$T/docs" "$T/docs/a.pdf"
args
check "a folder that is input and output is mounted once, writable" has "$T/docs:$T/docs" "$T/args"
check "  not also read-only" lacks "$T/docs:$T/docs:ro" "$T/args"

run container convert --runtime docker --output-dir "$T/o" "$T/docs/a.pdf" "$T/docs/missing.pdf"
check "a missing input is exit 2, after converting the rest" test "$RC" = 2
STUB_RUN_RC=1 run container convert --runtime docker --output-dir "$T/o" "$T/docs/a.pdf"
check "a failed conversion is exit 1" test "$RC" = 1

echo "## podman"
run container convert --runtime podman --output-dir "$T/o" "$T/docs/a.pdf"
args
check "podman maps the caller onto the image's app user" has "--userns=keep-id:uid=1000,gid=1000" "$T/args"

echo "## bench"
run container bench --runtime docker --runs 2 --report "$T/report.json" "$T/docs/a.pdf"
args
check "bench runs cli.py bench with the runs" has "bench" "$T/args"
check "  --runs 2" has "2" "$T/args"
check "the report is written on the host" has "protocol-droid/bench/1" "$T/report.json"
check "the host's name goes in, not the container's" has "BENCH_HOST=" "$T/args"
run container bench --runtime docker --runs 0 "$T/docs/a.pdf"
check "--runs must be positive" has "--runs must be a positive number" "$T/out"

echo
if (( FAILS )); then echo "${FAILS} check(s) failed"; exit 1; fi
echo "all checks passed"
