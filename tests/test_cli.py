"""cli.py — the one-shot entry the container mode runs.

marker is never loaded: tasks.get_models and tasks.convert_document are
replaced (and the fresh-process conversions the bench times), so this checks
what cli.py does with their results - the bench report's arithmetic, and the
exit codes - not marker itself.

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
        self.a = os.path.join(self.tmp, "a.pdf")      # 5 pages, text layer
        self.b = os.path.join(self.tmp, "b.docx")     # no page count, unknown path
        self.c = os.path.join(self.tmp, "c.pdf")      # 22 scanned pages
        for f in (self.a, self.b, self.c):
            with open(f, "wb") as fh:
                fh.write(b"x")
        # A clock that moves only when we say: model load takes 10 s, every
        # conversion 2 s.
        self.now = 0.0
        self.patches = [
            mock.patch.object(cli.time, "perf_counter", lambda: self.now),
            mock.patch.object(tasks, "get_models", self._load),
            mock.patch.object(tasks, "convert_document", self._convert),
            mock.patch.object(tasks, "prewarm", self._prewarm),
            mock.patch.object(cli, "_facts", self._facts),
            mock.patch.object(cli, "_cold_run", self._cold_run),
        ]
        self.calls = []
        # Page 0 in a fresh process, per path; warm in this process every page
        # takes 2s, so the overhead is 28s with OCR and 12s on a text layer.
        self.cold = {"ocr": 30.0, "text": 14.0}
        self.cold_fail = set()
        for p in self.patches:
            p.start()
        self.converted = []
        self.fail_on = set()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _load(self):
        self.calls.append("load")
        self.now += 10.0

    def _prewarm(self):
        self.calls.append("prewarm")
        self.now += 5.0
        return 5.0

    def _facts(self, path, page_range=None, force_ocr=False):
        name = os.path.basename(path)
        facts = {"a.pdf": (5, "text"), "b.docx": (None, None), "c.pdf": (22, "ocr")}[name]
        return (facts[0], "ocr") if force_ocr else facts

    def _cold_run(self, path, page_range, out_dir, force_ocr):
        self.calls.append(("cold", os.path.basename(path), page_range))
        if os.path.basename(path) in self.cold_fail:
            raise RuntimeError("cold convert failed (1)")
        return self.cold[self._facts(path, force_ocr=force_ocr)[1]]

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
    def report(self, *argv):
        rc, out, _ = self.run_cli("bench", *argv)
        return rc, json.loads(out)

    def test_schema_2(self):
        _, r = self.report(self.a)
        self.assertEqual(r["schema"], "protocol-droid/bench/2")

    def test_the_overhead_is_measured_per_path_not_inferred(self):
        # Item 64: v1 took get_models() (here 10s) as the overhead.
        _, r = self.report(self.a, self.c)
        self.assertEqual(r["overhead"]["ocr"]["seconds"], 28.0)       # cold 30 - warm 2
        self.assertEqual(r["overhead"]["ocr"]["from"], "c.pdf")
        self.assertEqual(r["overhead"]["text"]["seconds"], 12.0)      # cold 14 - warm 2
        self.assertEqual(r["overhead"]["text"]["warm_seconds"], 2.0)
        self.assertEqual(r["model_load_seconds"], 10.0)               # reported, not used

    def test_cold_before_anything_here_is_warm_and_warm_after_the_runs(self):
        # Cold: a throwaway run primes the page cache, then two samples - all
        # before this process loads anything. Warm: after the timed runs, and
        # straight after a different OCR'd page (prewarm's), so neither the runs
        # nor the sample is served from llama-server's prompt cache.
        self.report(self.a)
        page0 = ("cold", "a.pdf", "0")
        self.assertEqual(self.calls, [page0, page0, page0, "load", "prewarm", "prewarm"])
        timed, warm_sample = self.converted
        self.assertNotIn("page_range", timed[1])          # the run: the whole document
        self.assertEqual(warm_sample[1]["page_range"], "0")  # then the sample

    def test_samples_that_do_not_measure_the_overhead_give_no_number(self):
        # A fresh process no slower than a warm one is noise, not a saving of
        # zero: the bench says it does not know rather than print a number.
        self.cold["text"] = 1.5
        _, r = self.report(self.a)
        self.assertIsNone(r["overhead"]["text"]["seconds"])
        self.assertIn("do not measure the overhead", r["overhead"]["text"]["why"])
        self.assertEqual(r["overhead"]["text"]["cold_seconds"], [1.5, 1.5])
        self.assertIsNone(r["resident_saving"][0]["speedup"])

    def test_a_path_with_no_pdf_says_so(self):
        _, r = self.report(self.a)
        self.assertIsNone(r["overhead"]["ocr"]["seconds"])
        self.assertIn("no ocr PDF", r["overhead"]["ocr"]["why"])

    def test_a_failed_cold_run_is_reported_not_fatal(self):
        self.cold_fail = {"c.pdf"}
        rc, r = self.report(self.c)
        self.assertEqual(rc, 0)
        self.assertIsNone(r["overhead"]["ocr"]["seconds"])
        self.assertIn("failed", r["overhead"]["ocr"]["why"])

    def test_no_cold_skips_it(self):
        _, r = self.report("--no-cold", self.a, self.c)
        self.assertEqual(r["overhead"], {})
        self.assertFalse(any(isinstance(c, tuple) for c in self.calls))

    def test_timed_runs_start_warm(self):
        _, r = self.report(self.a)
        self.assertEqual(r["warmup_seconds"], 5.0)
        self.assertEqual([d["seconds"] for d in r["documents"]], [2.0])

    def test_warm_is_per_path_never_blended(self):
        # Item 65: v1 averaged 3.41 s/page text with 61.5 s/page OCR.
        _, r = self.report(self.a, self.b, self.c)
        self.assertEqual(set(r["warm"]), {"text", "ocr", "unknown"})
        self.assertEqual(r["warm"]["text"]["pages"], 5)
        self.assertEqual(r["warm"]["ocr"]["pages"], 22)
        self.assertEqual(r["warm"]["ocr"]["seconds_per_page"], round(2.0 / 22, 3))
        self.assertNotIn("seconds_per_page", {k for k in r if k != "warm"})

    def test_the_saving_is_per_document_against_its_own_path(self):
        _, r = self.report(self.a, self.b, self.c)
        by = {d["file"]: d for d in r["resident_saving"]}
        self.assertEqual((by["a.pdf"]["overhead_seconds"], by["a.pdf"]["speedup"]), (12.0, 7.0))
        self.assertEqual((by["c.pdf"]["overhead_seconds"], by["c.pdf"]["speedup"]), (28.0, 15.0))
        self.assertIsNone(by["b.docx"]["speedup"])          # no path, no overhead to set it against
        # A text document's tables may be OCR'd: it may pay up to the OCR overhead.
        self.assertEqual(by["a.pdf"]["speedup_if_its_tables_are_ocrd"], 15.0)
        self.assertNotIn("speedup_if_its_tables_are_ocrd", by["c.pdf"])

    def test_runs_repeat_the_set_and_the_saving_uses_their_mean(self):
        _, r = self.report("--runs", "3", self.a)
        self.assertEqual([d["run"] for d in r["documents"]], [1, 2, 3])
        self.assertEqual(r["resident_saving"][0]["warm_seconds"], 2.0)

    def test_force_ocr_and_page_range_reach_the_conversion(self):
        _, r = self.report("--no-cold", "--force-ocr", "--page-range", "0-3", self.a)
        opts = self.converted[0][1]
        self.assertTrue(opts["force_ocr"])
        self.assertEqual(opts["page_range"], "0-3")
        self.assertEqual(r["documents"][0]["path"], "ocr")
        self.assertEqual(r["options"], {"page_range": "0-3", "force_ocr": True, "runs": 1})

    def test_report_file(self):
        path = os.path.join(self.tmp, "r.json")
        self.run_cli("bench", "--report", path, self.a)
        with open(path) as fh:
            self.assertEqual(json.load(fh)["schema"], "protocol-droid/bench/2")

    def test_a_failed_document_is_reported_and_fails_the_run(self):
        self.fail_on = {"b.docx"}
        rc, r = self.report(self.a, self.b)
        self.assertEqual(rc, cli.EX_FAILED)
        self.assertFalse(r["documents"][1]["ok"])
        self.assertIn("marker exploded", r["documents"][1]["error"])
        self.assertNotIn("unknown", r["warm"])

    def test_nothing_to_measure(self):
        rc, _, _ = self.run_cli("bench", os.path.join(self.tmp, "nope.pdf"))
        self.assertEqual(rc, cli.EX_NO_INPUT)


class NoRecall(Base):
    """llama-server's per-slot prompt cache served a seen page in 3.0s against 15.1s."""

    def test_the_bench_turns_recall_off_before_any_server_starts(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LLAMA_CPP_EXTRA_ARGS", None)
            _, out, _ = self.run_cli("bench", "--no-cold", self.a)
            self.assertEqual(os.environ["LLAMA_CPP_EXTRA_ARGS"], "--no-cache-prompt")
        self.assertEqual(json.loads(out)["llama_server_extra_args"], "--no-cache-prompt")

    def test_other_extra_args_are_kept(self):
        with mock.patch.dict(os.environ, {"LLAMA_CPP_EXTRA_ARGS": "--threads 8"}):
            self.run_cli("bench", "--no-cold", self.a)
            self.assertEqual(os.environ["LLAMA_CPP_EXTRA_ARGS"], "--threads 8 --no-cache-prompt")

    def test_an_explicit_choice_wins(self):
        with mock.patch.dict(os.environ, {"LLAMA_CPP_EXTRA_ARGS": "--cache-prompt"}):
            self.run_cli("bench", "--no-cold", self.a)
            self.assertEqual(os.environ["LLAMA_CPP_EXTRA_ARGS"], "--cache-prompt")


class PageRange(Base):
    """One --page-range for the set, cut to each document: marker refuses a range past the end."""

    def test_clamp(self):
        self.assertEqual(cli._clamp("0-5", 3), "0-2")
        self.assertEqual(cli._clamp("0,2,4-9", 5), "0,2,4")
        self.assertEqual(cli._clamp("1-2,4", 10), "1-2,4")
        self.assertIsNone(cli._clamp("7-9", 3))

    def test_each_document_gets_its_own_cut(self):
        with mock.patch.object(cli, "_pages", lambda p: {"a.pdf": 3, "c.pdf": 22}[os.path.basename(p)]):
            rc, out, _ = self.run_cli("bench", "--no-cold", "--page-range", "0-5", self.a, self.c)
        self.assertEqual(rc, 0)
        self.assertEqual([o["page_range"] for _, o, _ in self.converted], ["0-2", "0-5"])

    def test_a_range_past_the_end_is_that_document_s_failure(self):
        with mock.patch.object(cli, "_pages", lambda p: 3):
            rc, out, _ = self.run_cli("bench", "--no-cold", "--page-range", "7-9", self.a)
        self.assertEqual(rc, cli.EX_FAILED)
        self.assertIn("past the end", json.loads(out)["documents"][0]["error"])
        self.assertEqual(self.converted, [])


class Facts(unittest.TestCase):
    """cli._facts without pypdfium2's answer: what does not need it."""

    def test_an_image_is_one_ocr_d_page(self):
        self.assertEqual(cli._facts("/x/scan.PNG"), (1, "ocr"))

    def test_a_document_marker_reads_whole_has_no_page_count(self):
        self.assertEqual(cli._facts("/x/report.docx"), (None, None))
        self.assertEqual(cli._facts("/x/report.docx", force_ocr=True), (None, "ocr"))


class ColdRun(unittest.TestCase):
    def test_a_fresh_process_running_convert(self):
        seen = []
        with mock.patch.object(cli.subprocess, "run",
                               lambda cmd, **kw: seen.append(cmd) or mock.Mock(returncode=0)):
            cli._cold_run("/x/a.pdf", "0-1", "/tmp/o", True)
        (cmd,) = seen
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("cli.py"))
        self.assertEqual(cmd[2:], ["convert", "--page-range", "0-1", "--output-dir", "/tmp/o",
                                   "--force-ocr", "/x/a.pdf"])

    def test_a_failed_one_raises(self):
        with mock.patch.object(cli.subprocess, "run", lambda cmd, **kw: mock.Mock(returncode=1)):
            with self.assertRaises(RuntimeError):
                cli._cold_run("/x/a.pdf", "0", "/tmp/o", False)


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
