# wip/ — protocol-droid's engine in Python

**Work in progress. Nothing uses it by default:** `protocol-droid.sh` at the
repository root is still the engine, and scomp-link still calls it.

This is the same engine - the same modes, commands, flags, messages and exit
codes - written in Python (standard library only) instead of bash. The aim is
portability and tests, not a smaller footprint: the weight is marker's, and
stays where it is.

- **Runs on the host's own python3**, whatever version (3.14 on a current Mac).
  marker and markitdown stay in their own pipx environments on 3.10–3.13, and
  are never imported here.
- **The macOS/Linux tool differences go away** - GNU vs BSD `stat`, `wc`,
  `find`, `sed`, bash 3.2 vs 5 - which caused five bugs found on 2026-10-14/15.
- **`provenance.py` is called in-process**, the same module the worker uses.

## Trying it

```sh
wip/protocol-droid.sh local status --backend marker      # any command, as with the bash engine
PROTOCOL_DROID_ENGINE=python scomp-link                  # scomp-link's menus, on the Python engine
```

`wip/protocol-droid.sh` is a launcher with the bash engine's name and calling
convention, so anything that runs `bash protocol-droid.sh ...` can be pointed
at it unchanged. `PROTOCOL_DROID_PYTHON` picks the interpreter.

## How it is checked

The parity check is the existing suite, run against both engines:

```sh
PROTOCOL_DROID="$PWD/wip/protocol-droid.sh" tests/test-exit-codes.sh   # 50 checks
PROTOCOL_DROID="$PWD/wip/protocol-droid.sh" tests/test-container.sh    # 23 checks
python3 -m unittest discover -s wip/tests                              # 15 tests
```

`wip/tests/test_parity.py` hands the same input to a bash function and to its
Python port and compares the outputs exactly: the folder scanner at depths 1–5
(case, skipped folders, spaces), the Kubernetes volume claims rendered from
`k8s/pvc.yaml`, output naming, the exit-code severity, and the usage text. It
also covers the one case `test-exit-codes.sh` can only run against the bash
copy: a sidecar that cannot be written fails the file. CI runs all of it.

Beyond the suites, 18 real commands were run through both engines on a Mac
(status, scan, setup and convert errors, container and service refusals): the
same output and the same exit code for each.

## Layout

| Bash | Python |
| --- | --- |
| `protocol-droid.sh` | `pdroid/cli.py` (usage, modes, `auto`) |
| `lib/common.sh` | `pdroid/common.py`, `pdroid/ui.py` |
| `lib/marker.sh` | `pdroid/marker.py` |
| `lib/markitdown.sh` | `pdroid/markitdown.py` |
| `lib/container.sh` | `pdroid/container.py` |
| `lib/service.sh` | `pdroid/service.py` |

## What is left before it replaces the bash engine

- Use it for real: `local convert` with marker and markitdown installed, and
  `container` / `service` against a live runtime and cluster. The suites stub
  the converters and the runtimes.
- Then swap: `protocol-droid.sh` becomes the launcher, `lib/` goes, and `pdroid/`
  becomes the library `protocol`'s marker worker imports (Kamino TODO 25 → 23).
