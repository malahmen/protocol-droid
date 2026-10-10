r"""Provenance sidecars: what was converted, by which versions, with which options.

Written beside every conversion as `provenance.json`, by all three execution
modes — the two local bash backends run this module as a script, the
containerized worker imports it.

The point is re-conversion. Without it, a marker bump is a corpus-wide re-run,
because nothing on disk says which documents were converted by the old version
— and the same goes for a changed option, or a source file that has been
edited since. With it the question is a diff: this source hash and these
versions against the sidecar.

It is cheap to add now and expensive to backfill: a sidecar cannot be
reconstructed for a document that was already converted, so every conversion
that happens before this exists is permanently unattributable.

As a script:

  python3 provenance.py --source FILE --output-dir DIR --backend NAME \
      --started EPOCH --finished EPOCH [--output-format F] [--input-root DIR] \
      [--version PKG=VER]... [--option KEY=VALUE]... [--command ARG]...

Versions are passed in by the bash backends rather than looked up, because
marker and markitdown live in pipx venvs that this interpreter cannot see; the
worker, which runs in the same environment as marker, lets them be read here.
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import sys

PROVENANCE_FILE = "provenance.json"
PROVENANCE_SUFFIX = ".provenance.json"
PROVENANCE_SCHEMA = "protocol-droid/provenance/1"


def is_provenance(name: str) -> bool:
    """Is this file name one of ours?

    Both shapes have to be recognised, because the two layouts differ: marker
    gives each document its own folder, so the sidecar is just
    provenance.json; markitdown writes one .md per input into a SHARED folder,
    where a single provenance.json would be overwritten by the next document in
    the same run -- leaving one sidecar for the whole batch and no error. There
    the sidecar is named after its output: doc.md.provenance.json.
    """
    return name == PROVENANCE_FILE or name.endswith(PROVENANCE_SUFFIX)

# Versions worth pinning a conversion to, per backend. surya-ocr is listed
# separately from marker-pdf on purpose: the OCR models and their output change
# with surya, which marker's own version does not move in step with.
BACKEND_PACKAGES = {
    "marker": ("marker-pdf", "surya-ocr", "torch"),
    "markitdown": ("markitdown",),
}
PROVENANCE_PACKAGES = BACKEND_PACKAGES["marker"]

# Anything whose NAME looks like a credential is replaced rather than written.
# llm_config() puts openai_api_key into the resolved options, so without this
# the sidecar would carry the deployment's key into every output folder — and
# output is the directory meant to be handed to the next pipeline stage.
_SECRET_NAME_RE = re.compile(r"key|token|secret|password|credential", re.I)
REDACTED = "[redacted]"


def _utc(ts: float) -> str:
    """An ISO-8601 UTC timestamp from a POSIX time, to the second."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def file_sha256(path: str, chunk: int = 1 << 20) -> str:
    """The source file's content hash, read in chunks (documents can be large)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def redact_options(options: dict | None) -> dict:
    """`options` with every credential-looking value replaced.

    Matched on the key name, not on the value: a value-based filter has to
    recognise a secret to redact it, and the one thing a secret looks like is
    an ordinary string.
    """
    out = {}
    for k, v in (options or {}).items():
        out[k] = REDACTED if _SECRET_NAME_RE.search(str(k)) else v
    return out


def tool_versions(packages=PROVENANCE_PACKAGES) -> dict:
    """Installed versions of the packages a conversion depends on.

    importlib.metadata reads the dist metadata, so this does not import marker
    or torch. A package that is absent is recorded as "absent" rather than
    omitted: a missing key would be indistinguishable from an older sidecar
    schema that never collected it.
    """
    from importlib import metadata
    out = {}
    for pkg in packages:
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = "absent"
    return out


def _relative_to(fpath: str, input_root: str | None) -> str | None:
    if not input_root:
        return None
    root = os.path.realpath(input_root)
    real = os.path.realpath(os.path.abspath(fpath))
    try:
        if os.path.commonpath([root, os.path.dirname(real)]) != root:
            return None
    except ValueError:                      # different drives/roots
        return None
    return os.path.relpath(real, root)


def provenance_record(fpath: str, out_folder: str, options: dict | None,
                      started: float, finished: float,
                      input_root: str | None = None, backend: str = "marker",
                      versions: dict | None = None,
                      command: list | None = None,
                      output_format: str | None = None,
                      outputs: list | None = None) -> dict:
    """The sidecar's contents for one conversion.

    `versions` overrides the importlib lookup, for a caller that knows better
    than this interpreter does — the bash backends, whose converter lives in a
    pipx venv. `command` is the argv actually run, which for those backends IS
    the set of options used.

    `outputs` names this document's output files. Without it the folder is
    scanned, which is right only where the folder belongs to one document
    (marker). A shared folder has to say which files are its own, or every
    sidecar would claim every other document's output as well.
    """
    st = os.stat(fpath)
    if outputs is None:
        outputs = sorted(
            name for name in os.listdir(out_folder)
            if not is_provenance(name) and os.path.isfile(os.path.join(out_folder, name))
        )
    else:
        outputs = sorted(outputs)
    if versions is None:
        versions = tool_versions(BACKEND_PACKAGES.get(backend, ()))
    # Both keys are always present, empty where they do not apply: a consumer
    # should not have to tell "this backend records no options" apart from "an
    # older schema did not collect them".
    return {
        "schema": PROVENANCE_SCHEMA,
        "source": {
            "path": os.path.abspath(fpath),
            "relative_path": _relative_to(fpath, input_root),
            "sha256": file_sha256(fpath),
            "bytes": st.st_size,
            "modified": _utc(st.st_mtime),
        },
        "conversion": {
            "backend": backend,
            "started": _utc(started),
            "finished": _utc(finished),
            "seconds": round(finished - started, 3),
            "output_format": output_format or (options or {}).get("output_format", "markdown"),
            "command": list(command or []),
            "output_dir": os.path.abspath(out_folder),
            "outputs": outputs,
        },
        "versions": versions,
        "options": redact_options(options),
    }


def write_provenance(out_folder: str, record: dict, name: str = PROVENANCE_FILE) -> str:
    """Write the sidecar atomically; returns its path.

    Atomically because a half-written provenance.json is worse than none: the
    next run would parse it, fail, and have to decide what that means. os.replace
    is atomic within a filesystem, and the temp file is in the same directory.
    """
    if os.path.basename(name) != name:
        raise ValueError(f"provenance name must be a bare file name, got {name!r}")
    path = os.path.join(out_folder, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return path


# --- script entry point ------------------------------------------------------


def _pairs(values, what):
    """["a=1", "b=2"] -> {"a": "1", "b": "2"}."""
    out = {}
    for item in values or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"provenance: --{what} wants KEY=VALUE, got {item!r}")
        out[key] = value
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="provenance", description=__doc__.splitlines()[0])
    ap.add_argument("--source", required=True, help="the file that was converted")
    ap.add_argument("--output-dir", required=True, help="the folder its output went to")
    ap.add_argument("--backend", required=True, choices=sorted(BACKEND_PACKAGES))
    ap.add_argument("--started", type=float, required=True, help="POSIX time")
    ap.add_argument("--finished", type=float, required=True, help="POSIX time")
    ap.add_argument("--output-format", default=None)
    ap.add_argument("--input-root", default=None)
    ap.add_argument("--version", action="append", metavar="PKG=VER",
                    help="repeatable; an empty VER is recorded as unknown")
    ap.add_argument("--option", action="append", metavar="KEY=VALUE", help="repeatable")
    ap.add_argument("--name", default=PROVENANCE_FILE,
                    help=f"sidecar file name (default {PROVENANCE_FILE}); use "
                         f"<output>{PROVENANCE_SUFFIX} in a folder shared by "
                         "several documents")
    ap.add_argument("--output", action="append", metavar="NAME",
                    help="repeatable; this document's own output files. Omit to "
                         "scan the output folder, which is only correct when the "
                         "folder holds one document")
    ap.add_argument("--command", nargs=argparse.REMAINDER, default=[],
                    help="the argv that ran; must come last")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.source):
        print(f"provenance: not a file: {args.source}", file=sys.stderr)
        return 2
    if not os.path.isdir(args.output_dir):
        print(f"provenance: not a directory: {args.output_dir}", file=sys.stderr)
        return 2

    versions = {k: (v or "unknown") for k, v in _pairs(args.version, "version").items()}
    record = provenance_record(
        args.source, args.output_dir, _pairs(args.option, "option"),
        args.started, args.finished, args.input_root, args.backend,
        versions or None, args.command, args.output_format, args.output)
    try:
        print(write_provenance(args.output_dir, record, args.name))
    except ValueError as exc:
        print(f"provenance: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
