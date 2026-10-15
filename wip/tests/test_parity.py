"""The Python engine against the bash one, function by function.

The bash suites in tests/ already run end to end against both engines (see
wip/README.md). These cover what they cannot reach: the same input handed to
the bash function and to its Python port, and the outputs compared exactly.

Run: python3 -m unittest discover -s wip/tests   (needs bash; nothing installed)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

WIP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(WIP)
sys.path.insert(0, WIP)

from pdroid import common, service, ui  # noqa: E402
from pdroid import markitdown as mid  # noqa: E402
from pdroid import marker  # noqa: E402

BASH = shutil.which("bash")


def bash(script: str, *args, env=None) -> subprocess.CompletedProcess:
    """Run a snippet with lib/common.sh (and lib/service.sh) sourced."""
    prelude = (f'HERE="{ROOT}"; source "{ROOT}/lib/common.sh"; source "{ROOT}/lib/service.sh"; '
               f'source "{ROOT}/lib/marker.sh"; source "{ROOT}/lib/markitdown.sh"; ')
    full_env = dict(os.environ, LC_ALL="C")
    if env is not None:
        full_env = dict(env, LC_ALL="C")
    return subprocess.run([BASH, "-c", prelude + script, "_", *args],
                          capture_output=True, text=True, env=full_env)


@unittest.skipUnless(BASH, "bash not found")
class ScanFiles(unittest.TestCase):
    """The folder scanner: depth, case-insensitive extensions, skipped folders."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        tree = [
            "a.pdf", "B.PDF", "c.Docx", "notes.txt", "img.jpeg",
            "one/d.pdf", "one/two/e.pdf", "one/two/three/f.pdf", "one/two/three/four/g.pdf",
            "node_modules/x.pdf", ".git/y.pdf", "converted/z.pdf", "one/marker-output/w.pdf",
            "one/markitdown-output/v.docx", "space dir/h i.pdf", "one/song.MP3",
        ]
        for rel in tree:
            path = os.path.join(cls.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("x")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def compare(self, depth, exts, cwd=None, directory=None):
        directory = directory or self.tmp
        res = bash('scan_files "$1" "$2" "${@:3}"', directory, str(depth), *exts)
        expected = res.stdout.splitlines()
        if cwd:
            old = os.getcwd(); os.chdir(cwd)
            try:
                got = common.scan_files(directory, depth, exts)
            finally:
                os.chdir(old)
        else:
            got = common.scan_files(directory, depth, exts)
        self.assertEqual(got, expected, f"depth {depth}")

    def test_marker_types_at_every_depth(self):
        for depth in (1, 2, 3, 4, 5):
            self.compare(depth, marker.EXTS)

    def test_markitdown_types(self):
        self.compare(3, mid.EXTS)

    def test_relative_dot(self):
        res = bash('cd "$1" && scan_files . 3 pdf', self.tmp)
        old = os.getcwd(); os.chdir(self.tmp)
        try:
            got = common.scan_files(".", 3, ("pdf",))
        finally:
            os.chdir(old)
        self.assertEqual(got, res.stdout.splitlines())

    def test_nothing_found(self):
        self.assertEqual(common.scan_files(self.tmp, 3, ("xyz",)), [])
        self.assertNotEqual(bash('scan_files "$1" 3 xyz', self.tmp).returncode, 0)


@unittest.skipUnless(BASH, "bash not found")
class RenderPvcs(unittest.TestCase):
    """k8s/pvc.yaml with the storage settings applied - byte for byte."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env_file = os.path.join(self.tmp, "env")
        self.saved = {k: os.environ.pop(k, None) for k in ("K8S_STORAGE_CLASS", "K8S_ACCESS_MODE")}
        os.environ["PROTOCOL_DROID_ENV_FILE"] = self.env_file

    def tearDown(self):
        shutil.rmtree(self.tmp)
        os.environ.pop("PROTOCOL_DROID_ENV_FILE", None)
        for k, v in self.saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    def compare(self, env_content="", **settings):
        with open(self.env_file, "w") as fh:
            fh.write(env_content)
        base = {k: v for k, v in os.environ.items() if k not in ("K8S_STORAGE_CLASS", "K8S_ACCESS_MODE")}
        res = bash('ENV_FILE="$1"; error_exit() { echo "ERR: $*" >&2; exit 1; }; svc_env_load; svc_render_pvcs',
                   self.env_file, env=dict(base, **settings))
        for k in ("K8S_STORAGE_CLASS", "K8S_ACCESS_MODE"):
            os.environ.pop(k, None)
        os.environ.update(settings)
        try:
            service.env_load()
            got, failed = service.render_pvcs(), False
        except ui.Fatal:
            got, failed = "", True
        self.assertEqual(failed, res.returncode != 0, res.stderr)
        if not failed:
            self.assertEqual(got, res.stdout)

    def test_defaults(self):
        self.compare()

    def test_class_and_rwx(self):
        self.compare(K8S_STORAGE_CLASS="nfs-taris", K8S_ACCESS_MODE="ReadWriteMany")

    def test_from_env_file(self):
        self.compare("K8S_STORAGE_CLASS=from-env-file\nK8S_ACCESS_MODE=ReadWriteMany\n")

    def test_environment_wins_over_env_file(self):
        self.compare("K8S_STORAGE_CLASS=from-env-file\n", K8S_STORAGE_CLASS="from-env")

    def test_bad_mode_and_class_are_refused_by_both(self):
        self.compare(K8S_ACCESS_MODE="ReadOnlyMany")
        self.compare(K8S_STORAGE_CLASS="Not_Valid")


@unittest.skipUnless(BASH, "bash not found")
class OutPath(unittest.TestCase):
    def test_clobber_and_no_clobber(self):
        tmp = tempfile.mkdtemp()
        try:
            for existing in ([], ["doc.md"], ["doc.md", "doc_2.md"]):
                for name in existing:
                    open(os.path.join(tmp, name), "w").close()
                for clobber in (True, False):
                    res = bash('out_path "$1" doc md "$2"', tmp, "true" if clobber else "false")
                    self.assertEqual(common.out_path(tmp, "doc", "md", clobber), res.stdout)
        finally:
            shutil.rmtree(tmp)


class Worse(unittest.TestCase):
    """The same table as protocol-droid.sh's _worse: severity, not number."""

    def test_severity(self):
        cases = {(0, 3): 3, (3, 2): 2, (2, 1): 1, (1, 2): 1, (1, 3): 1, (2, 3): 2, (0, 0): 0, (3, 0): 3}
        for (a, b), want in cases.items():
            self.assertEqual(ui.worse(a, b), want, (a, b))


@unittest.skipUnless(BASH, "bash not found")
class MissingProvenance(unittest.TestCase):
    """The case tests/test-exit-codes.sh can only run against the bash copy:
    a sidecar that cannot be written fails the file."""

    def test_counted_as_without_provenance(self):
        tmp = tempfile.mkdtemp()
        try:
            src = os.path.join(tmp, "a.docx")
            with open(src, "w") as fh:
                fh.write("content")
            out = os.path.join(tmp, "out")
            env = dict(os.environ, PROTOCOL_DROID_ROOT=tmp, PROTOCOL_DROID_NO_OPEN="1",
                       PATH=os.path.join(ROOT, "tests", "stubs") + os.pathsep + os.environ["PATH"])
            res = subprocess.run([sys.executable, os.path.join(WIP, "protocol_droid.py"), "local", "convert",
                                  "--backend", "markitdown", "--output-dir", out, src],
                                 capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
            self.assertEqual(res.returncode, 1, res.stderr)
            self.assertTrue(os.path.isfile(os.path.join(out, "a.md")))
            self.assertIn("provenance", res.stderr)
            self.assertIn("without provenance", res.stderr)
        finally:
            shutil.rmtree(tmp)


class Cli(unittest.TestCase):
    def run_engine(self, *args):
        return subprocess.run([sys.executable, os.path.join(WIP, "protocol_droid.py"), *args],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_help_and_unknown_mode(self):
        self.assertEqual(self.run_engine("--help").returncode, 0)
        res = self.run_engine("nonsense")
        self.assertEqual(res.returncode, 1)
        self.assertIn("unknown mode: nonsense", res.stderr)

    def test_unknown_backend_and_command(self):
        self.assertIn("unknown --backend", self.run_engine("local", "convert", "--backend", "x", "a").stderr)
        self.assertIn("unknown command", self.run_engine("local", "frobnicate", "--backend", "markitdown").stderr)

    def test_usage_text_matches_the_bash_engine(self):
        bash_usage = subprocess.run([BASH, os.path.join(ROOT, "protocol-droid.sh"), "--help"],
                                    capture_output=True, text=True).stderr
        self.assertEqual(self.run_engine("--help").stderr, bash_usage)


if __name__ == "__main__":
    unittest.main()
