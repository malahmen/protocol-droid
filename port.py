"""What every converter shares at protocol's port: bytes in, files out.

protocol (the conversion API) calls each converter with the document as bytes
and its original name. Both images - marker's (tasks.py) and markitdown's
(markitdown_tasks.py) - need the same two guarantees around that, so they live
here once rather than twice:

  - the bytes are written under a name that keeps its extension (converters
    pick a reader by it) but cannot choose where the file goes: a path in the
    name is dropped, and an empty or dotted one becomes "document";
  - all of it happens in a folder of its own that is removed afterwards, so
    nothing is left on the worker and two calls never share a folder.

Standard library only: it is copied into both images and imported by the CI
suite without either converter installed.
"""
import os
import shutil
import tempfile
from contextlib import contextmanager


def safe_name(original_name) -> str:
    """The file name to convert under: the last path element, never empty or dotted."""
    name = os.path.basename((original_name or "").replace("\\", "/")).strip()
    return "document" if name in ("", ".", "..") else name


@contextmanager
def scratch(data: bytes, original_name, prefix: str = "pd-convert-"):
    """Yield (folder, source path) with `data` written there; remove it all on exit."""
    tmp = tempfile.mkdtemp(prefix=prefix)
    try:
        src_dir = os.path.join(tmp, "in")
        os.makedirs(src_dir)
        src = os.path.join(src_dir, safe_name(original_name))
        with open(src, "wb") as fh:
            fh.write(data)
        yield tmp, src
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
