"""The engine's own logic, pinned to the bash engine it replaced (v1.0.0).

Until the swap these were side-by-side comparisons with lib/*.sh. That code
is gone, so its answers are frozen here instead: the folder scanner's output
for a fixed tree at every depth, output naming, exit-code severity. A change
in any of them is a change in behaviour, not a refactor.

The rest of the engine is covered end to end by the bash suites
(test-exit-codes.sh, test-container.sh, test-k8s-storage.sh).

Run: python3 -m unittest discover -s tests   (standard library only)
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pdroid import common, ui  # noqa: E402
from pdroid import markitdown as mid  # noqa: E402
from pdroid import marker  # noqa: E402

TREE = ("a.pdf", "B.PDF", "c.Docx", "notes.txt", "img.jpeg", "one/d.pdf", "one/two/e.pdf",
        "one/two/three/f.pdf", "one/two/three/four/g.pdf", "node_modules/x.pdf", ".git/y.pdf",
        "converted/z.pdf", "one/marker-output/w.pdf", "one/markitdown-output/v.docx",
        "space dir/h i.pdf", "one/song.MP3")

# v1.0.0's lib/common.sh scan_files, run from inside the tree as `scan_files .`
# with LC_ALL=C - recorded on 2026-10-15 before lib/ was removed.
TOP = ["B.PDF", "a.pdf", "c.Docx", "img.jpeg"]
V1_MARKER = {
    1: TOP,
    2: TOP + ["one/d.pdf", "space dir/h i.pdf"],
    3: TOP + ["one/d.pdf", "one/two/e.pdf", "space dir/h i.pdf"],
    4: TOP + ["one/d.pdf", "one/two/e.pdf", "one/two/three/f.pdf", "space dir/h i.pdf"],
    5: TOP + ["one/d.pdf", "one/two/e.pdf", "one/two/three/f.pdf", "one/two/three/four/g.pdf",
              "space dir/h i.pdf"],
}
V1_MARKITDOWN_3 = TOP + ["one/d.pdf", "one/song.MP3", "one/two/e.pdf", "space dir/h i.pdf"]


class ScanFiles(unittest.TestCase):
    """Depth, case-insensitive extensions, skipped folders, spaces - as v1.0.0."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        for rel in TREE:
            path = os.path.join(cls.tmp, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("x")
        cls.old = os.getcwd()
        os.chdir(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.old)
        shutil.rmtree(cls.tmp)

    def test_marker_types_at_every_depth(self):
        for depth, expected in V1_MARKER.items():
            self.assertEqual(common.scan_files(".", depth, marker.EXTS), expected, f"depth {depth}")

    def test_markitdown_types(self):
        self.assertEqual(common.scan_files(".", 3, mid.EXTS), V1_MARKITDOWN_3)

    def test_an_absolute_folder_keeps_its_prefix(self):
        self.assertEqual(common.scan_files(self.tmp, 1, marker.EXTS), [os.path.join(self.tmp, f) for f in TOP])

    def test_nothing_found(self):
        self.assertEqual(common.scan_files(".", 3, ("xyz",)), [])


class OutPath(unittest.TestCase):
    """Overwrite by default; --no-clobber takes the first free _N."""

    def test_clobber_and_no_clobber(self):
        tmp = tempfile.mkdtemp()
        try:
            doc = os.path.join(tmp, "doc.md")
            self.assertEqual(common.out_path(tmp, "doc", "md", True), doc)
            self.assertEqual(common.out_path(tmp, "doc", "md", False), doc)
            open(doc, "w").close()
            self.assertEqual(common.out_path(tmp, "doc", "md", True), doc)
            self.assertEqual(common.out_path(tmp, "doc", "md", False), os.path.join(tmp, "doc_2.md"))
            open(os.path.join(tmp, "doc_2.md"), "w").close()
            self.assertEqual(common.out_path(tmp, "doc", "md", False), os.path.join(tmp, "doc_3.md"))
        finally:
            shutil.rmtree(tmp)


class Worse(unittest.TestCase):
    """Severity, not number: failure > missing input > nothing to convert > ok."""

    def test_severity(self):
        cases = {(0, 3): 3, (3, 2): 2, (2, 1): 1, (1, 2): 1, (1, 3): 1, (2, 3): 2, (0, 0): 0, (3, 0): 3}
        for (a, b), want in cases.items():
            self.assertEqual(ui.worse(a, b), want, (a, b))


class Cli(unittest.TestCase):
    def run_engine(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "protocol_droid.py"), *args],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_help_and_unknown_mode(self):
        res = self.run_engine("--help")
        self.assertEqual(res.returncode, 0)
        self.assertIn("protocol-droid.sh local", res.stderr)
        res = self.run_engine("nonsense")
        self.assertEqual(res.returncode, 1)
        self.assertIn("unknown mode: nonsense", res.stderr)

    def test_unknown_backend_and_command(self):
        self.assertIn("unknown --backend", self.run_engine("local", "convert", "--backend", "x", "a").stderr)
        self.assertIn("unknown command", self.run_engine("local", "frobnicate", "--backend", "markitdown").stderr)

    def test_the_launcher_runs_the_same_engine(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash not found")
        via_launcher = subprocess.run([bash, os.path.join(ROOT, "protocol-droid.sh"), "--help"],
                                      capture_output=True, text=True).stderr
        self.assertEqual(via_launcher, self.run_engine("--help").stderr)


if __name__ == "__main__":
    unittest.main()
