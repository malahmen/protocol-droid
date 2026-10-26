#!/usr/bin/env bash
# Every input of a run either produces its own output, with a sidecar that
# names it and the command that made it, or fails (Kamino items 126-128):
#   - markitdown: inputs sharing a stem no longer overwrite each other;
#   - local marker: provenance follows the name the file was staged under,
#     ignores an output folder an earlier run left, records the argv that ran
#     (redacted) and the OCR/torch versions, and sees the files marker saw;
#   - auto: a missing input is exit 2 next to a clean conversion.
# and an output folder inside the input folder is never converted again on
# the next run (Kamino item 190).
# Converters are stubs on PATH (tests/stubs, tests/stubs-marker).
set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PD="${PROTOCOL_DROID:-${TEST_DIR}/../protocol-droid.sh}"
[[ -f "$PD" ]] || { echo "protocol-droid.sh not found at $PD" >&2; exit 1; }
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
T="$(cd "$T" && pwd -P)"
export PATH="${TEST_DIR}/stubs:${TEST_DIR}/stubs-marker:$PATH"
export PROTOCOL_DROID_NO_OPEN=1 SURYA_INFERENCE_BACKEND=vllm

FAILS=0
check() { local d="$1"; shift; if "$@"; then echo "   ok   - $d"; else echo "   FAIL - $d"; FAILS=$((FAILS + 1)); fi; }
ERR=""; RC=0
run() { bash "$PD" "$@" >/dev/null 2>"$T/err" </dev/null; RC=$?; ERR="$(cat "$T/err")"; }
in_err() { grep -qE -- "$1" <<<"$ERR"; }
field() { python3 -c 'import json,sys;d=json.load(open(sys.argv[1]));
for k in sys.argv[2].split("."):
    d=d[k]
print(json.dumps(d) if isinstance(d,(list,dict)) else d)' "$1" "$2"; }
field_is() { [[ "$(field "$1" "$2" 2>/dev/null)" == "$3" ]]; }
field_has() { [[ "$(field "$1" "$2" 2>/dev/null)" == *"$3"* ]]; }
field_lacks() { [[ "$(field "$1" "$2" 2>/dev/null)" != *"$3"* ]]; }
mk() { mkdir -p "$(dirname "$T/$1")"; printf 'content of %s\n' "$1" > "$T/$1"; }

echo "== 126: markitdown, inputs that share a stem =="
mk in/a/notes.html; mk in/b/notes.html; mk in/report.docx; mk in/report.pptx; mk in/solo.docx
out="$T/md"
run local convert --backend markitdown --output-dir "$out" "$T/in"
check "exits 0"                                   test "$RC" -eq 0
check "a/notes.html has its own output"           test -f "$out/a/notes_html.md"
check "b/notes.html has its own output"           test -f "$out/b/notes_html.md"
check "report.docx has its own output"            test -f "$out/report_docx.md"
check "report.pptx has its own output"            test -f "$out/report_pptx.md"
check "a stem nobody shares keeps <stem>.md"      test -f "$out/solo.md"
check "nothing was written as notes.md"           test ! -e "$out/notes.md"
check "each sidecar names its own source (a)"     field_is "$out/a/notes_html.md.provenance.json" source.path "$T/in/a/notes.html"
check "each sidecar names its own source (b)"     field_is "$out/b/notes_html.md.provenance.json" source.path "$T/in/b/notes.html"
check "  and its relative path"                   field_is "$out/b/notes_html.md.provenance.json" source.relative_path b/notes.html
check "the pptx sidecar names the pptx"           field_is "$out/report_pptx.md.provenance.json" source.path "$T/in/report.pptx"
before=$(find "$out" -type f | sort | md5sum)
run local convert --backend markitdown --output-dir "$out" "$T/in"
check "a re-run writes the same names, nothing new" test "$(find "$out" -type f | sort | md5sum)" = "$before"
mk in2/x/plan.docx; mk in2/y/plan.docx
out="$T/md2"
run local convert --backend markitdown --output-dir "$out" "$T/in2/x/plan.docx" "$T/in2/y/plan.docx"
check "files named one by one: both kept"         test -f "$out/x/plan_docx.md" -a -f "$out/y/plan_docx.md"

echo
echo "== 127: local marker, one sidecar per document, from this run =="
mk mx/x/a.pdf; mk mx/y/a.pdf
out="$T/mk1"
run local convert --backend marker --output-dir "$out" "$T/mx/x/a.pdf" "$T/mx/y/a.pdf"
check "two a.pdf: exits 0"                        test "$RC" -eq 0
check "  out/a's sidecar names x/a.pdf"           field_is "$out/a/provenance.json" source.path "$T/mx/x/a.pdf"
check "  out/a_2's sidecar names y/a.pdf"         field_is "$out/a_2/provenance.json" source.path "$T/mx/y/a.pdf"
check "  the command is the batch CLI that ran"   field_has "$out/a/provenance.json" conversion.command "stubs-marker/marker\""
check "versions include surya-ocr"                field_has "$out/a/provenance.json" versions surya-ocr
check "  and torch"                               field_has "$out/a/provenance.json" versions torch

mk ms/b.pdf; mk ms/boom.pdf
out="$T/mk2"; mkdir -p "$out/boom"; echo old > "$out/boom/boom.md"; touch -d '1 hour ago' "$out/boom/boom.md"
run local convert --backend marker --output-dir "$out" "$T/ms"
check "a stale folder does not hide a failure: exit 1" test "$RC" -eq 1
check "  saying the folder is from an earlier run"     in_err "earlier run"
check "  and no fresh sidecar claims it"                test ! -e "$out/boom/provenance.json"
check "  the good file still has its sidecar"           test -f "$out/b/provenance.json"
rm -f "$T/ms/boom.pdf"; touch -d '1 hour ago' "$out/b/b.md"; rm -f "$out/b/provenance.json"; mk ms/c.pdf
run local convert --backend marker --output-dir "$out" "$T/ms" -- --skip_existing
check "--skip_existing: a skipped folder is fine"      test "$RC" -eq 0
check "  and says it was skipped"                       in_err "Skipped: .*b.pdf"

mk one/report.pdf
out="$T/mk3"
run local convert --backend marker --output-dir "$out" --page-range 0-2 "$T/one/report.pdf" -- --openai_api_key sk-SECRET --gemini_api_key=g-SECRET
check "a single file: exits 0"                          test "$RC" -eq 0
check "  the command names marker_single"               field_has "$out/report/provenance.json" conversion.command "marker_single"
check "  with the page range that was used"             field_has "$out/report/provenance.json" conversion.command "--page_range"
check "  and the passthrough flags"                     field_has "$out/report/provenance.json" conversion.command "--openai_api_key"
check "  with their secrets redacted"                   field_lacks "$out/report/provenance.json" conversion.command "SECRET"

mk nest/top.pdf; mk nest/sub/deep.pdf
out="$T/mk4"
run local convert --backend marker --output-dir "$out" "$T/nest"
check "a folder: a nested file is converted too"        test -f "$out/deep/deep.md"
check "  with a sidecar relative to the folder"         field_is "$out/deep/provenance.json" source.relative_path sub/deep.pdf
check "  and the run exits 0"                           test "$RC" -eq 0

echo
echo "== 190: an output folder inside the input folder is never an input =="
sources() { find "$1" -name '*provenance.json' -exec python3 -c 'import json,sys
for p in sys.argv[1:]: print(json.load(open(p))["source"]["path"])' {} + | sort; }
mk ri/a.html
for i in 1 2 3; do run local convert --backend markitdown --output-dir "$T/ri/md" "$T/ri"; done
check "markitdown, third run: exits 0"                  test "$RC" -eq 0
check "  only a.md and its sidecar in the output"       test "$(cd "$T/ri/md" && find . -type f | sort | tr '\n' ' ')" = "./a.md ./a.md.provenance.json "
check "  every sidecar names the one input"             test "$(sources "$T/ri/md" | uniq)" = "$T/ri/a.html"
mk rm/a.pdf
run local convert --backend marker --output-dir "$T/rm/md" "$T/rm"
printf 'jpeg' > "$T/rm/md/a/_page_0_Picture_1.jpeg"     # what marker extracts beside a.md
run local convert --backend marker --output-dir "$T/rm/md" "$T/rm"
check "marker, second run: exits 0"                     test "$RC" -eq 0
check "  its extracted image was not converted"         test "$(ls "$T/rm/md")" = "a"
check "  one sidecar, for the input"                    test "$(sources "$T/rm/md")" = "$T/rm/a.pdf"
mk ra/a.pdf; mk ra/b.html
run local convert --backend auto --output-dir "$T/ra/md" "$T/ra"
run local convert --backend auto --output-dir "$T/ra/md" "$T/ra"
check "auto, second run: exits 0"                       test "$RC" -eq 0
check "  no sidecar became a document"                  test -z "$(find "$T/ra/md" -name '*provenance.md' -o -name '*provenance_json*')"
check "  two sidecars, one per input"                   test "$(sources "$T/ra/md" | tr '\n' ' ')" = "$T/ra/a.pdf $T/ra/b.html "

echo
echo "== auto: a missing input next to a clean one is exit 2 =="
mk au/a.docx
run local convert --backend auto --output-dir "$T/au-out" "$T/au/a.docx" "$T/au/ghost.docx"
check "exit 2"                                          test "$RC" -eq 2
check "  and the present file is converted"             test -f "$T/au-out/a.md"
run local convert --backend auto --output-dir "$T/au-out2" "$T/au/ghost.docx"
check "only a missing input: exit 2"                    test "$RC" -eq 2

echo
(( FAILS == 0 )) && echo "test-one-output-per-input: all checks passed" || { echo "test-one-output-per-input: ${FAILS} check(s) failed"; exit 1; }
