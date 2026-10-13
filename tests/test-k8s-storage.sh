#!/usr/bin/env bash
# The k8s storage settings: what `service deploy --target k8s` would apply.
#
# What it protects: claims that BIND on the cluster in front of it. Every claim
# used to request ReadWriteMany with no class, and on a local-path cluster
# (meksha, kind, k3s) an RWX request never binds - the deployment sat Pending
# with nothing saying why. Now the model cache is always RWO; input and output
# follow K8S_ACCESS_MODE (default RWO); K8S_STORAGE_CLASS names the class for
# all three. Rendering only: no cluster is needed (binding was verified by hand
# on kind's local-path provisioner).
set -uo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${TEST_DIR}/.." && pwd)"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT

FAILS=0
check() { local d="$1"; shift; if "$@"; then echo "   ok   - $d"; else echo "   FAIL - $d"; FAILS=$((FAILS + 1)); fi; }

# render [VAR=value ...] — svc_render_pvcs with the given settings; stdout to
# $T/out, stderr to $T/err, exit code to RC. The project .env is replaced by
# one holding $ENV_CONTENT (empty by default), so the developer's own .env never
# leaks in.
render() {
    printf '%s\n' "${ENV_CONTENT:-}" > "$T/env"
    env -u K8S_STORAGE_CLASS -u K8S_ACCESS_MODE "$@" bash -c '
        HERE="$1"; source "$1/lib/service.sh"; ENV_FILE="$2"
        error_exit() { echo "ERR: $*" >&2; exit 1; }
        svc_env_load; svc_render_pvcs' _ "$ROOT" "$T/env" > "$T/out" 2> "$T/err"
    RC=$?
}
# claim <name> — the rendered manifest of one claim.
claim() { awk -v n="$1" 'BEGIN{RS="---"} $0 ~ "name: " n "\n" {print}' "$T/out"; }
mode_of()  { claim "$1" | sed -n 's/.*accessModes: \["\([A-Za-z]*\)"\].*/\1/p'; }
class_of() { claim "$1" | sed -n 's/^  storageClassName: //p'; }

echo "# 1. defaults: everything RWO, the cluster's default class"
render
check "renders" [ "$RC" = 0 ]
for c in marker-models marker-input marker-output; do
    check "$c is ReadWriteOnce" [ "$(mode_of $c)" = ReadWriteOnce ]
    check "$c names no class" [ -z "$(class_of $c)" ]
done
check "no ReadWriteMany anywhere" bash -c '! grep -q "\"ReadWriteMany\"" "$1"' _ "$T/out"

echo "# 2. a second node: RWX input/output on a named class; the model cache stays RWO"
render K8S_STORAGE_CLASS=nfs-qnap K8S_ACCESS_MODE=ReadWriteMany
check "input is ReadWriteMany" [ "$(mode_of marker-input)" = ReadWriteMany ]
check "output is ReadWriteMany" [ "$(mode_of marker-output)" = ReadWriteMany ]
check "model cache stays ReadWriteOnce" [ "$(mode_of marker-models)" = ReadWriteOnce ]
for c in marker-models marker-input marker-output; do
    check "$c uses nfs-qnap" [ "$(class_of $c)" = nfs-qnap ]
done

echo "# 3. the settings come from .env too, and the environment wins"
ENV_CONTENT="K8S_STORAGE_CLASS=from-env-file
K8S_ACCESS_MODE=ReadWriteMany" render
check ".env class used" [ "$(class_of marker-input)" = from-env-file ]
check ".env access mode used" [ "$(mode_of marker-output)" = ReadWriteMany ]
ENV_CONTENT="K8S_STORAGE_CLASS=from-env-file" render K8S_STORAGE_CLASS=from-env
check "the environment wins over .env" [ "$(class_of marker-input)" = from-env ]

echo "# 4. bad values are refused before anything is applied"
render K8S_ACCESS_MODE=RWX
check "bad access mode refused" [ "$RC" != 0 ]
check "and explained" grep -q "must be ReadWriteOnce or ReadWriteMany" "$T/err"
render "K8S_STORAGE_CLASS=Bad Class!"
check "bad class name refused" [ "$RC" != 0 ]

echo "# 5. the file on its own is still a valid, RWO manifest"
check "no RWX in k8s/pvc.yaml" bash -c '! grep -q "\"ReadWriteMany\"" "$1"' _ "$ROOT/k8s/pvc.yaml"
check "three claims" [ "$(grep -c '^kind: PersistentVolumeClaim' "$ROOT/k8s/pvc.yaml")" = 3 ]

echo; if (( FAILS == 0 )); then echo "all passed"; else echo "${FAILS} failed"; exit 1; fi
