"""tasks.validate_* — what the API refuses to pass on to marker.

The case that matters: marker expands page_range eagerly, so an unbounded
range is accepted and then builds the whole list in memory. The worker was
OOM-killed (137), and SimpleWorker runs jobs in-process, so the worker died
with it.

Run: python3 -m unittest discover -s tests   (stdlib only; marker not needed)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import tasks  # noqa: E402


class PageRange(unittest.TestCase):
    def good(self, spec, expect):
        self.assertEqual(tasks.validate_page_range(spec), expect)

    def bad(self, spec, because):
        with self.assertRaises(tasks.InvalidOption, msg=f"accepted {spec!r}") as cm:
            tasks.validate_page_range(spec)
        self.assertIn(because, str(cm.exception))

    def test_accepts_what_marker_documents(self):
        self.good("0", "0")
        self.good("0,5-10", "0,5-10")
        self.good(" 2 , 7-9 ", "2,7-9")      # whitespace normalised
        self.good("4-4", "4-4")
        self.good("3-3,8", "3-3,8")

    def test_the_range_that_killed_the_worker(self):
        self.bad("0-999999999", "past the maximum page index")

    def test_caps_the_total_even_in_small_pieces(self):
        # Each element is individually harmless; together they are not.
        spec = ",".join(f"{i}-{i + 99}" for i in range(0, 100000, 1000))
        self.bad(spec, "covers more than")

    def test_rejects_nonsense(self):
        self.bad("", "empty")
        self.bad("   ", "empty")
        self.bad("1,,2", "empty element")
        self.bad("abc", "not a page")
        self.bad("1-", "not a page")
        self.bad("-5", "not a page")
        self.bad("1-2-3", "not a page")
        self.bad("10-2", "counts backwards")
        self.bad("1.5", "not a page")
        self.bad("1 2", "not a page")

    def test_rejects_unicode_digits_without_crashing(self):
        # str.isdigit() is true for these and int() then refuses them, so the
        # naive check turned a bad request into an unhandled ValueError.
        self.bad("²", "not a page")       # superscript two
        self.bad("٣", "not a page")       # Arabic-Indic three

    def test_the_cap_is_configurable(self):
        self.assertGreater(tasks.PAGE_RANGE_MAX_PAGES, 0)
        self.assertGreater(tasks.PAGE_RANGE_MAX_INDEX, tasks.PAGE_RANGE_MAX_PAGES)

    def test_a_range_at_the_cap_is_allowed(self):
        n = tasks.PAGE_RANGE_MAX_PAGES
        self.good(f"0-{n - 1}", f"0-{n - 1}")
        self.bad(f"0-{n}", "covers more than")


class OutputFormat(unittest.TestCase):
    def test_accepts_the_documented_set(self):
        for fmt in ("markdown", "json", "html", "chunks"):
            self.assertEqual(tasks.validate_output_format(fmt), fmt)

    def test_rejects_anything_else(self):
        for fmt in ("", "MARKDOWN", "pdf", "md", "markdown "):
            with self.assertRaises(tasks.InvalidOption, msg=f"accepted {fmt!r}"):
                tasks.validate_output_format(fmt)


class Options(unittest.TestCase):
    def test_normalises_and_leaves_the_rest_alone(self):
        # `use_llm` used to be the unrelated key here. It is validated now (see
        # test_llm_config.py: it meant an unconfigured Google Gemini call), so
        # the passthrough is asserted with a key that really is unrelated.
        got = tasks.validate_options(
            {"output_format": "json", "page_range": " 1 , 4-6 ", "force_ocr": True})
        self.assertEqual(got, {"output_format": "json", "page_range": "1,4-6", "force_ocr": True})

    def test_does_not_mutate_the_caller_s_dict(self):
        src = {"page_range": " 1 "}
        tasks.validate_options(src)
        self.assertEqual(src, {"page_range": " 1 "})

    def test_absent_keys_are_fine(self):
        self.assertEqual(tasks.validate_options(None), {})
        self.assertEqual(tasks.validate_options({"page_range": None}), {"page_range": None})
