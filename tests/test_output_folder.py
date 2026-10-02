"""tasks.output_folder: every input document gets its own output folder.

Run: python3 -m unittest discover -s tests   (stdlib only; marker not needed)
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tasks  # noqa: E402  (marker is imported lazily inside convert_document)


class OutputFolder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = self.t = self._tmp.name
        for d in ("in/a", "in/b", "elsewhere"):
            os.makedirs(os.path.join(t, d))
        for f in ("in/a/report.pdf", "in/b/report.pdf", "in/report.pdf", "in/report.docx",
                  "in/README", "elsewhere/x.pdf"):
            open(os.path.join(t, f), "w").close()
        os.symlink(os.path.join(t, "elsewhere/x.pdf"), os.path.join(t, "in/a/link.pdf"))
        os.symlink(os.path.join(t, "in"), os.path.join(t, "in-link"))
        self.out = os.path.join(t, "out")

    def tearDown(self):
        self._tmp.cleanup()

    def rel(self, f, root="in"):
        root = os.path.join(self.t, root) if root else None
        got = tasks.output_folder(os.path.join(self.t, f), self.out, root)
        self.assertTrue(os.path.isdir(got))
        return os.path.relpath(got, self.out)

    def test_same_name_in_different_folders(self):
        self.assertEqual(self.rel("in/a/report.pdf"), "a/report_pdf")
        self.assertEqual(self.rel("in/b/report.pdf"), "b/report_pdf")

    def test_same_stem_different_extension(self):
        self.assertEqual(self.rel("in/report.pdf"), "report_pdf")
        self.assertEqual(self.rel("in/report.docx"), "report_docx")

    def test_collision_set_is_distinct(self):
        got = {self.rel(f) for f in ("in/a/report.pdf", "in/b/report.pdf", "in/report.pdf", "in/report.docx")}
        self.assertEqual(len(got), 4)

    def test_symlinked_file_keeps_its_name(self):
        self.assertEqual(self.rel("in/a/link.pdf"), "a/link_pdf")

    def test_symlinked_root_either_side(self):
        self.assertEqual(self.rel("in-link/a/report.pdf", "in"), "a/report_pdf")
        self.assertEqual(self.rel("in/a/report.pdf", "in-link"), "a/report_pdf")

    def test_outside_root_or_no_root_uses_the_name(self):
        self.assertEqual(self.rel("elsewhere/x.pdf"), "x_pdf")
        self.assertEqual(self.rel("in/a/report.pdf", None), "report_pdf")

    def test_no_extension(self):
        self.assertEqual(self.rel("in/README"), "README")


if __name__ == "__main__":
    unittest.main()
