"""cli.py — the one-shot entry the container mode runs.

marker is never loaded: tasks.get_models and tasks.convert_document are
replaced, so this checks what cli.py does with their results - the bench
report's arithmetic, and the exit codes - not marker itself.

Run: python3 -m unittest discover -s tests   (stdlib only; marker not needed)
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import cli  # noqa: E402
import tasks  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.a = os.path.join(self.tmp, "a.pdf")
        self.b = os.path.join(self.tmp, "b.docx")
        for f in (self.a, self.b):
            with open(f, "wb") as fh:
                fh.write(b"x")
        # A clock that moves only when we say: model load takes 10 s, every
        # conversion 2 s.
        self.now = 0.0
        self.patches = [
            mock.patch.object(cli.time, "perf_counter", lambda: self.now),
            mock.patch.object(tasks, "get_models", self._load),
            mock.patch.object(tasks, "convert_document", self._convert),
            mock.patch.object(cli, "_pages", lambda p: 5 if p.endswith(".pdf") else None),
        ]
        for p in self.patches:
            p.start()
        self.converted = []
        self.fail_on = set()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _load(self):
        self.now += 10.0

    def _convert(self, f, opts, input_root=None):
        self.now += 2.0
        self.converted.append((f, dict(opts), input_root))
        if os.path.basename(f) in self.fail_on:
            raise RuntimeError("marker exploded")
        return os.path.join(opts["output_dir"], "out")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class Bench(Base):
    def test_load_is_timed_apart_from_each_document(self):
        rc, out, _ = self.run_cli("bench", self.a, self.b)
        self.assertEqual(rc, 0)
        r = json.loads(out)
        self.assertEqual(r["schema"], "protocol-droid/bench/1")
        self.assertEqual(r["model_load_seconds"], 10.0)
        self.assertEqual([d["seconds"] for d in r["documents"]], [2.0, 2.0])
        self.assertEqual(r["warm"]["seconds_per_document"], 2.0)

    def test_what_a_resident_worker_saves(self):
        _, out, _ = self.run_cli("bench", self.a)
        saving = json.loads(out)["resident_saving"]
        self.assertEqual(saving["per_document_seconds"], 10.0)
        self.assertEqual(saving["cli_per_document_seconds"], 12.0)
        self.assertEqual(saving["speedup"], 6.0)

    def test_pages_only_count_pdfs(self):
        _, out, _ = self.run_cli("bench", self.a, self.b)
        warm = json.loads(out)["warm"]
        self.assertEqual(warm["pages"], 5)
        self.assertEqual(warm["pages_per_minute"], 75.0)   # 5 pages in 4 s

    def test_runs_repeat_the_set(self):
        _, out, _ = self.run_cli("bench", "--runs", "3", self.a)
        self.assertEqual([d["run"] for d in json.loads(out)["documents"]], [1, 2, 3])

    def test_report_file(self):
        path = os.path.join(self.tmp, "r.json")
        self.run_cli("bench", "--report", path, self.a)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["model_load_seconds"], 10.0)

    def test_a_failed_document_is_reported_and_fails_the_run(self):
        self.fail_on = {"b.docx"}
        rc, out, _ = self.run_cli("bench", self.a, self.b)
        self.assertEqual(rc, cli.EX_FAILED)
        docs = json.loads(out)["documents"]
        self.assertFalse(docs[1]["ok"])
        self.assertIn("marker exploded", docs[1]["error"])
        self.assertEqual(json.loads(out)["warm"]["documents"], 1)

    def test_nothing_to_measure(self):
        rc, _, _ = self.run_cli("bench", os.path.join(self.tmp, "nope.pdf"))
        self.assertEqual(rc, cli.EX_NO_INPUT)


class Convert(Base):
    def test_every_file_once_with_the_options(self):
        rc, _, _ = self.run_cli("convert", "--output-dir", self.tmp, "--output-format", "json",
                                "--page-range", "0-2", self.a, self.b)
        self.assertEqual(rc, 0)
        self.assertEqual([os.path.basename(f) for f, _, _ in self.converted], ["a.pdf", "b.docx"])
        opts = self.converted[0][1]
        self.assertEqual((opts["output_format"], opts["page_range"], opts["output_dir"]),
                         ("json", "0-2", self.tmp))

    def test_one_failure_does_not_stop_the_batch(self):
        self.fail_on = {"a.pdf"}
        rc, _, err = self.run_cli("convert", "--output-dir", self.tmp, self.a, self.b)
        self.assertEqual(rc, cli.EX_FAILED)
        self.assertEqual(len(self.converted), 2)
        self.assertIn("marker exploded", err)

    def test_a_missing_input(self):
        rc, _, _ = self.run_cli("convert", "--output-dir", self.tmp, self.a, os.path.join(self.tmp, "nope.pdf"))
        self.assertEqual(rc, cli.EX_NO_INPUT)

    def test_a_bad_option_is_refused_before_converting(self):
        rc, _, err = self.run_cli("convert", "--output-dir", self.tmp, "--page-range", "0-999999999", self.a)
        self.assertEqual(rc, cli.EX_FAILED)
        self.assertEqual(self.converted, [])


if __name__ == "__main__":
    unittest.main()
