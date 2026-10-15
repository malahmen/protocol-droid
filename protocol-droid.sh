#!/usr/bin/env bash
# protocol-droid - document -> Markdown/JSON conversion.
#
# The engine is Python (pdroid/, standard library only); this launcher keeps
# the name and the calling convention every caller already uses - scomp-link,
# cron, CI: `protocol-droid.sh local|container|service <command> [flags]`.
# Run protocol_droid.py directly if you prefer. --help lists everything.
#
# PROTOCOL_DROID_PYTHON picks the interpreter (default: python3, any 3.9+).
# The bash engine this replaced is release v1.0.0.
exec "${PROTOCOL_DROID_PYTHON:-python3}" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/protocol_droid.py" "$@"
