"""Messages and exit codes - the same words and codes as lib/common.sh.

Every message goes to stderr, prefixed as the bash engine prefixes it, so a
caller (scomp-link, cron, the test suites) cannot tell the two engines apart
by their output.
"""
import sys

# Exit codes (lib/common.sh):
#   0  everything asked for was converted
#   1  at least one conversion failed (also every usage error)
#   2  a named input does not exist, or is not a file
#   3  nothing to convert: the folder held no file the backend handles
EX_OK, EX_FAILED, EX_NO_INPUT, EX_NOTHING = 0, 1, 2, 3

_TTY = sys.stderr.isatty()
_G, _Y, _R, _C, _N = (("\033[0;32m", "\033[0;33m", "\033[0;31m", "\033[0;36m", "\033[0m")
                      if _TTY else ("", "", "", "", ""))


class Fatal(Exception):
    """error_exit: print the message, exit 1."""


def info(msg: str) -> None:
    print(f"{_C}[info]{_N}  {msg}", file=sys.stderr, flush=True)


def success(msg: str) -> None:
    print(f"{_G}[ok]{_N}    {msg}", file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    print(f"{_Y}[warn]{_N}  {msg}", file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(f"{_R}[error]{_N} {msg}", file=sys.stderr, flush=True)


def error_exit(msg: str):
    raise Fatal(msg)


# Severity order, not numeric order: a failure (1) outranks a missing input (2),
# which outranks nothing-to-convert (3). Taking the larger number would let an
# empty second backend hide the first one's failure.
_SEVERITY = {EX_OK: 0, EX_NOTHING: 1, EX_NO_INPUT: 2, EX_FAILED: 3}


def worse(a: int, b: int) -> int:
    """The exit code that should win when two runs disagree."""
    return b if _SEVERITY.get(b, 3) > _SEVERITY.get(a, 3) else a
