"""tasks.job_timeout and tasks.prewarm - the two things the 2026-10-16
throughput measurement on corelia showed to be wrong.

The timeout was one fixed 3600s. OCR measured 54.7 s/page, so a scanned PDF
over ~66 pages was killed part-way, every time. The worker's warm-up loaded the
layout model only, so its first job still paid torch's warm-up and the
llama-server start: 25.5s for one scanned page, against 15.2s once warmed.

Run: python3 -m unittest discover -s tests   (stdlib only; marker not needed)
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tasks  # noqa: E402

OCR_SECONDS_PER_PAGE = 54.73   # measured on corelia, idle, 2026-10-16


def pages(n):
    return mock.patch.object(tasks, "page_count", lambda _path: n)


class JobTimeout(unittest.TestCase):
    def setUp(self):
        # An operator's JOB_TIMEOUT in the test environment must not decide these.
        patcher = mock.patch.object(tasks, "JOB_TIMEOUT", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_book_that_was_killed_now_fits(self):
        # davis-women-race-class.pdf: 166 scanned pages, 2.52 h at the measured
        # rate - past the old 3600s, and it must be well inside the new one.
        with pages(166):
            limit = tasks.job_timeout("/in/book.pdf")
        self.assertGreater(limit, 3600)
        self.assertGreater(limit, 2 * 166 * OCR_SECONDS_PER_PAGE)

    def test_every_scan_gets_room_for_its_pages(self):
        for n in (1, 3, 22, 66, 67, 300, 2000):
            with pages(n):
                self.assertGreater(tasks.job_timeout("/in/x.pdf"), n * OCR_SECONDS_PER_PAGE,
                                   msg=f"{n} pages")

    def test_a_small_job_no_longer_gets_an_hour_to_hang(self):
        with pages(3):
            self.assertLess(tasks.job_timeout("/in/x.pdf"), 3600)

    def test_it_is_base_plus_pages(self):
        with pages(10):
            self.assertEqual(tasks.job_timeout("/in/x.pdf"),
                             tasks.JOB_TIMEOUT_BASE + 10 * tasks.JOB_TIMEOUT_PER_PAGE)

    def test_page_range_narrows_it(self):
        with pages(166):
            self.assertEqual(tasks.job_pages("/in/x.pdf", {"page_range": "0-9"}), 10)
            self.assertEqual(tasks.job_pages("/in/x.pdf", {"page_range": "0,5-7,20"}), 5)

    def test_a_range_past_the_end_counts_only_real_pages(self):
        with pages(10):
            self.assertEqual(tasks.job_pages("/in/x.pdf", {"page_range": "0-99"}), 10)

    def test_unknown_count_gets_the_unknown_timeout(self):
        with pages(None):
            self.assertEqual(tasks.job_timeout("/in/report.docx"), tasks.JOB_TIMEOUT_UNKNOWN)

    def test_unknown_count_with_a_range_uses_the_range(self):
        with pages(None):
            self.assertEqual(tasks.job_pages("/in/report.docx", {"page_range": "0-4"}), 5)

    def test_a_bad_range_is_refused_not_guessed_at(self):
        with pages(10), self.assertRaises(tasks.InvalidOption):
            tasks.job_timeout("/in/x.pdf", {"page_range": "0-999999999"})

    def test_an_operator_s_fixed_timeout_still_wins(self):
        with pages(166), mock.patch.object(tasks, "JOB_TIMEOUT", 600):
            self.assertEqual(tasks.job_timeout("/in/x.pdf"), 600)


class PageCount(unittest.TestCase):
    def test_an_image_is_one_page(self):
        for name in ("scan.png", "scan.JPG", "scan.tiff", "scan.webp"):
            self.assertEqual(tasks.page_count(name), 1, msg=name)

    def test_office_documents_are_unknown(self):
        for name in ("report.docx", "deck.pptx", "book.epub", "page.html"):
            self.assertIsNone(tasks.page_count(name), msg=name)

    def test_an_unreadable_pdf_is_unknown_not_an_error(self):
        # With pypdfium2 this fails to parse; without it the import fails. Both
        # must come back None: a count only sizes a timeout.
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(b"not a pdf")
            f.flush()
            self.assertIsNone(tasks.page_count(f.name))


class Prewarm(unittest.TestCase):
    def setUp(self):
        self.calls = []
        patchers = [
            mock.patch.object(tasks, "_text_page_pdf", lambda path: open(path, "wb").close()),
            mock.patch.object(tasks, "convert_document",
                              lambda path, opts, **kw: self.calls.append((path, opts))),
            mock.patch.dict(os.environ, {"WORKER_PREWARM": "1"}),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

    def test_it_converts_one_page_with_ocr_forced(self):
        took = tasks.prewarm()
        self.assertIsInstance(took, float)
        self.assertEqual(len(self.calls), 1)
        path, opts = self.calls[0]
        # force_ocr is the point: a page with a text layer would not start
        # llama-server, and starting it is most of what is being prepaid.
        self.assertTrue(opts.get("force_ocr"))
        self.assertTrue(path.endswith(".pdf"))
        self.assertTrue(opts["output_dir"].startswith(tempfile.gettempdir()))

    def test_it_cleans_up_after_itself(self):
        tasks.prewarm()
        path, opts = self.calls[0]
        self.assertFalse(os.path.exists(path))
        self.assertFalse(os.path.exists(opts["output_dir"]))

    def test_a_failure_is_reported_not_raised(self):
        def boom(path, opts, **kw):
            raise RuntimeError("llama-server would not start")
        err = io.StringIO()
        with mock.patch.object(tasks, "convert_document", boom), redirect_stderr(err):
            self.assertIsNone(tasks.prewarm())
        self.assertIn("warm-up failed", err.getvalue())
        self.assertIn("llama-server would not start", err.getvalue())

    def test_a_missing_marker_is_a_failed_warm_up_not_a_dead_worker(self):
        def no_marker(path):
            raise ModuleNotFoundError("No module named 'PIL'")
        with mock.patch.object(tasks, "_text_page_pdf", no_marker), redirect_stderr(io.StringIO()):
            self.assertIsNone(tasks.prewarm())
        self.assertEqual(self.calls, [])

    def test_it_can_be_switched_off(self):
        for off in ("0", "false", "no", "off", " OFF "):
            with mock.patch.dict(os.environ, {"WORKER_PREWARM": off}):
                self.assertIsNone(tasks.prewarm(), msg=off)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
