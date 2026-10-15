#!/usr/bin/env bash
# The Python engine behind the bash engine's name and calling convention, so
# anything that runs `bash protocol-droid.sh ...` - the test suites, scomp-link -
# can be pointed at it unchanged:
#
#   PROTOCOL_DROID=wip/protocol-droid.sh tests/test-exit-codes.sh
#
# PROTOCOL_DROID_PYTHON picks the interpreter (default: python3).
exec "${PROTOCOL_DROID_PYTHON:-python3}" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/protocol_droid.py" "$@"
