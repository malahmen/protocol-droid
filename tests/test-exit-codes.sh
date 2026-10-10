#!/usr/bin/env bash
# Exit codes, overwrite behaviour and provenance sidecars for the local
# backends, driven through protocol-droid.sh with a stub converter on PATH.
#
# The point of the suite is the thing the old code got wrong in four places:
# reporting success while doing nothing. A missing input, an empty folder and
# a failed conversion all left with 0, so a cron job could not tell them apart
# — or from a run that worked.
set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PD="${PROTOCOL_DROID:-${TEST_DIR}/../protocol-droid.sh}"
[[ -f "$PD" ]] || { echo "protocol-droid.sh not found at $PD" >&2; exit 1; }

T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
T="$(cd "$T" && pwd)"
# The stub converter shadows anything real that may be installed.
export PATH="${TEST_DIR}/stubs:$PATH"
export PROTOCOL_DROID_NO_OPEN=1

FAILS=0
check() { local d="$1"; shift; if "$@"; then echo "   ok   - $d"; else echo "   FAIL - $d"; FAILS=$((FAILS + 1)); fi; }
ERR=""; RC=0
# stdout is discarded: every assertion here is about the exit code, stderr or
# the files on disk. stdin is closed so a regression that starts prompting
# fails rather than waiting for a terminal that is not there.
run() { bash "$PD" "$@" >/dev/null 2>"$T/err" </dev/null; RC=$?; ERR="$(cat "$T/err")"; }
in_err()  { grep -qE -- "$1" <<<"$ERR"; }
no_err()  { ! grep -qE -- "$1" <<<"$ERR"; }
# jq is not a dependency of this repo, so the sidecar is read with python3 —
# which provenance.py needs anyway.
field() { python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));
for k in sys.argv[2].split("."):
    d=d[k]
print(json.dumps(d) if isinstance(d,(list,dict)) else d)' "$1" "$2"; }
field_is() { [[ "$(field "$1" "$2")" == "$3" ]]; }

mkcorpus() {
    local d="$T/$1"; rm -rf "$d"; mkdir -p "$d"
    shift; local f; for f in "$@"; do printf 'content of %s\n' "$f" > "$d/$f"; done
    printf '%s' "$d"
}

echo "== a clean run =="
src=$(mkcorpus in1 a.docx b.html)
out="$T/out1"
run local convert --backend markitdown --output-dir "$out" "$src"
check "exits 0"            test "$RC" -eq 0
check "a.md was written"   test -f "$out/a.md"
check "b.md was written"   test -f "$out/b.md"
check "says 2 converted"   in_err '2 converted'

echo
echo "== provenance, one sidecar per output =="
check "a sidecar for a.md"      test -f "$out/a.md.provenance.json"
check "a sidecar for b.md"      test -f "$out/b.md.provenance.json"
check "the schema is recorded"  field_is "$out/a.md.provenance.json" schema protocol-droid/provenance/1
check "the backend is recorded" field_is "$out/a.md.provenance.json" conversion.backend markitdown
check "the source path"         field_is "$out/a.md.provenance.json" source.path "$src/a.docx"
check "relative to the folder"  field_is "$out/a.md.provenance.json" source.relative_path a.docx
check "the source hash"         field_is "$out/a.md.provenance.json" source.sha256 "$(sha256sum "$src/a.docx" | cut -d' ' -f1)"
check "the source size"         field_is "$out/a.md.provenance.json" source.bytes "$(stat -c %s "$src/a.docx")"
# The bug a shared provenance.json would have hidden: each sidecar must claim
# only its own output, not every .md in the folder.
check "it claims only its own output" field_is "$out/a.md.provenance.json" conversion.outputs '["a.md"]'
check "and so does the other"         field_is "$out/b.md.provenance.json" conversion.outputs '["b.md"]'
check "the command is recorded"       bash -c 'python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
sys.exit(0 if d[\"conversion\"][\"command\"] and \"markitdown\" in d[\"conversion\"][\"command\"][0] else 1)" "$1"' _ "$out/a.md.provenance.json"
check "no shared provenance.json"     test ! -e "$out/provenance.json"

echo
echo "== overwrite by default =="
# A second run used to leave a.md AND a_2.md, so the ingestion pipeline indexed
# the same document twice with nothing to say which was current.
run local convert --backend markitdown --output-dir "$out" "$src"
check "exits 0"              test "$RC" -eq 0
check "no a_2.md"            test ! -e "$out/a_2.md"
check "no b_2.md"            test ! -e "$out/b_2.md"
check "two outputs, two sidecars" bash -c 'test "$(find "$1" -maxdepth 1 -name "*.md" | wc -l)" = 2' _ "$out"

echo
echo "== --no-clobber keeps the old output =="
run local convert --backend markitdown --no-clobber --output-dir "$out" "$src"
check "exits 0"      test "$RC" -eq 0
check "a_2.md exists" test -f "$out/a_2.md"
check "with its own sidecar" test -f "$out/a_2.md.provenance.json"

echo
echo "== a failed conversion is exit 1 =="
src=$(mkcorpus in2 fine.docx boom.docx)
out="$T/out2"
run local convert --backend markitdown --output-dir "$out" "$src"
check "exits 1"                   test "$RC" -eq 1
check "the good one converted"    test -f "$out/fine.md"
check "the bad one did not"       test ! -e "$out/boom.md"
check "reports 1 converted"       in_err '1 converted'
check "and 1 failed"              in_err '1 failed'
check "no sidecar for the failure" test ! -e "$out/boom.md.provenance.json"

echo
echo "== a missing input is exit 2, not 0 =="
src=$(mkcorpus in3 real.docx)
out="$T/out3"
run local convert --backend markitdown --output-dir "$out" "$src/real.docx" "$src/ghost.docx"
check "exits 2"                 test "$RC" -eq 2
check "the real one converted"  test -f "$out/real.md"
check "the missing one is named" in_err 'Not a file.*ghost\.docx'
check "and counted"             in_err '1 missing'

echo
echo "== only missing inputs is still exit 2 =="
out="$T/out3b"
run local convert --backend markitdown --output-dir "$out" "$T/in3/ghost.docx"
check "exits 2" test "$RC" -eq 2

echo
echo "== a failure outranks a missing input =="
out="$T/out3c"
run local convert --backend markitdown --output-dir "$out" "$T/in2/boom.docx" "$T/in3/ghost.docx"
check "exits 1, not 2" test "$RC" -eq 1

echo
echo "== an empty folder is exit 3 =="
src=$(mkcorpus in4 notes.txt)     # .txt is not in MID_EXTS
out="$T/out4"
run local convert --backend markitdown --output-dir "$out" "$src"
check "exits 3"                  test "$RC" -eq 3
check "it says what it handles"  in_err 'No supported files'

echo
echo "== auto: the worst code of the two backends wins =="
# marker is not installed in this environment, so auto's marker half will
# error out; the cases below use only markitdown extensions so the routing
# sends everything to the stub.
src=$(mkcorpus in5 a.docx b.html)
out="$T/out5"
run local convert --backend auto --output-dir "$out" "$src"
check "a clean auto run exits 0" test "$RC" -eq 0
check "both were converted"      test -f "$out/a.md" -a -f "$out/b.md"
out="$T/out5b"
run local convert --backend auto --output-dir "$out" "$T/in2/boom.docx" "$T/in3/ghost.docx"
check "a failure plus a missing input exits 1" test "$RC" -eq 1
out="$T/out5c"
run local convert --backend auto --output-dir "$out" "$T/in4"
check "an empty folder exits 3"  test "$RC" -eq 3

echo
echo "== provenance can be turned off =="
src=$(mkcorpus in6 a.docx)
out="$T/out6"
PROTOCOL_DROID_NO_PROVENANCE=1 run local convert --backend markitdown --output-dir "$out" "$src"
check "exits 0"           test "$RC" -eq 0
check "a.md was written"  test -f "$out/a.md"
check "no sidecar"        test ! -e "$out/a.md.provenance.json"
check "nothing was warned about" no_err 'provenance'

echo
echo "== a sidecar that cannot be written fails the file =="
# The hole provenance is meant to close is invisible later, so it is reported
# now rather than silently skipped.
out="$T/out7"
run local convert --backend markitdown --output-dir "$out" "$T/in6/a.docx"
check "a baseline run exits 0" test "$RC" -eq 0
PROTOCOL_DROID="$T/copy/protocol-droid.sh"
mkdir -p "$T/copy/lib"
cp "${TEST_DIR}/../protocol-droid.sh" "$T/copy/"
cp "${TEST_DIR}/../lib/"*.sh "$T/copy/lib/"
# ... with provenance.py deliberately absent from the copy.
out="$T/out8"
bash "$T/copy/protocol-droid.sh" local convert --backend markitdown \
    --output-dir "$out" "$T/in6/a.docx" >/dev/null 2>"$T/err" </dev/null; RC=$?; ERR="$(cat "$T/err")"
check "exits 1"                      test "$RC" -eq 1
check "the output is still written"  test -f "$out/a.md"
check "and it says provenance is why" in_err 'provenance'
check "counted as without provenance" in_err 'without provenance'

echo
if (( FAILS )); then echo "exit-codes: ${FAILS} check(s) failed"; exit 1; fi
echo "exit-codes: all checks passed"
