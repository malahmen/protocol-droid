"""markitdown at protocol's converter port - the light converter's image.

protocol (the conversion API) sends every file that is not a PDF, an image or
audio - DOCX, PPTX, XLSX, XLS, Outlook .msg, HTML, EPUB, CSV and the rest - to
its markitdown queue, and calls the converter through one port: bytes in,
every output file as bytes out, plus tool versions. This is that port for
markitdown, in an image of its own (Dockerfile.markitdown; Kamino item 76):
marker's image carries torch and llama-server, and markitdown needs neither.

Three things are deliberately narrow:

  - convert_local, never convert: given a string, convert() also accepts a URL
    and fetches it. The worker only ever converts the bytes it was handed.
  - plugins off: a plugin is third-party code that can claim any file type.
  - no options: protocol allowlists none for markitdown, so anything that does
    arrive here came by another route and is refused rather than ignored.

`python -m markitdown_tasks` prints what is installed (the image's default
command); `python -m markitdown_tasks FILE...` converts files to stdout, which
is how the image is checked by hand.
"""
import os
import sys

from port import scratch
from provenance import BACKEND_PACKAGES, tool_versions


class InvalidOption(ValueError):
    """An option markitdown is not given (protocol allowlists none)."""


def convert_bytes(data: bytes, original_name: str, options: dict | None = None):
    """Convert one document given as bytes: (files {relative name: bytes}, tool_versions)."""
    if options:
        raise InvalidOption("markitdown takes no options, got: " + ", ".join(sorted(options)))
    from markitdown import MarkItDown

    with scratch(data, original_name, prefix="pd-markitdown-") as (_tmp, src):
        result = MarkItDown(enable_plugins=False).convert_local(src)
        stem = os.path.splitext(os.path.basename(src))[0] or "document"
        files = {f"{stem}.md": result.markdown.encode("utf-8")}
    return files, tool_versions(BACKEND_PACKAGES["markitdown"])


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        for pkg, version in tool_versions(BACKEND_PACKAGES["markitdown"]).items():
            print(f"{pkg} {version}")
        return 0
    failed = 0
    for path in args:
        try:
            with open(path, "rb") as fh:
                files, _ = convert_bytes(fh.read(), os.path.basename(path))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            print(f"[error] {path}: {type(exc).__name__}: {exc}", file=sys.stderr)
            failed += 1
            continue
        for name, body in files.items():
            print(f"===== {path} -> {name} ({len(body)} bytes)", file=sys.stderr)
            sys.stdout.write(body.decode("utf-8"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
