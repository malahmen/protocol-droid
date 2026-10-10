"""provenance: the sidecar written beside every conversion.

Run: python3 -m unittest discover -s tests   (stdlib only; marker not needed)

convert_document itself needs marker, so the pieces are exercised directly.
That is the whole reason provenance.py is its own module rather than inline in
convert_document: the half that decides what gets recorded is testable without
a GB of torch, and the bash backends can run it as a script.
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import provenance  # noqa: E402
import tasks  # noqa: E402


class Hash(unittest.TestCase):
    def test_known_digest(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "f")
            with open(p, "wb") as fh:
                fh.write(b"abc")
            # sha256("abc"), so the hash is pinned to a value from outside this
            # code rather than to whatever the code happens to produce.
            self.assertEqual(
                provenance.file_sha256(p),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

    def test_chunking_does_not_change_the_digest(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "f")
            with open(p, "wb") as fh:
                fh.write(b"x" * (3 << 20))      # 3 MiB: several 1 MiB reads
            self.assertEqual(provenance.file_sha256(p), provenance.file_sha256(p, chunk=7))

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "f")
            open(p, "wb").close()
            self.assertEqual(
                provenance.file_sha256(p),
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


class Redaction(unittest.TestCase):
    def test_every_credential_shaped_name_is_replaced(self):
        got = provenance.redact_options({
            "openai_api_key": "sk-real-secret",
            "LLM_API_KEY": "another",
            "auth_token": "t",
            "client_secret": "s",
            "db_password": "p",
            "aws_credential": "c",
            "output_format": "markdown",
        })
        for name in ("openai_api_key", "LLM_API_KEY", "auth_token",
                     "client_secret", "db_password", "aws_credential"):
            self.assertEqual(got[name], provenance.REDACTED, name)
        self.assertEqual(got["output_format"], "markdown")

    def test_no_secret_value_survives_serialisation(self):
        # The assertion that matters: not that the key says "[redacted]", but
        # that the string is nowhere in the JSON that reaches the output dir.
        blob = json.dumps(provenance.redact_options(
            {"openai_api_key": "sk-real-secret", "use_llm": True}))
        self.assertNotIn("sk-real-secret", blob)

    def test_ordinary_options_are_untouched(self):
        opts = {"output_format": "json", "page_range": "0-3", "force_ocr": True}
        self.assertEqual(provenance.redact_options(opts), opts)

    def test_none_is_an_empty_dict(self):
        self.assertEqual(provenance.redact_options(None), {})


class Versions(unittest.TestCase):
    def test_an_absent_package_is_recorded_as_absent(self):
        # Recorded, not omitted: a missing key could not be told apart from an
        # older schema that never collected that package.
        got = provenance.tool_versions(("no-such-package-anywhere",))
        self.assertEqual(got, {"no-such-package-anywhere": "absent"})

    def test_a_present_package_gets_its_version(self):
        # pip is not guaranteed; the interpreter's own stdlib dists are not
        # either. unittest has no dist, so use this project's one certainty:
        # whatever `sys.modules` cannot answer, importlib.metadata can for at
        # least one installed dist. Skip rather than assert a false positive.
        from importlib import metadata
        names = [d.metadata["Name"] for d in metadata.distributions()
                 if d.metadata and d.metadata["Name"]]
        if not names:
            self.skipTest("no installed distributions to read a version from")
        got = provenance.tool_versions((names[0],))
        self.assertNotEqual(got[names[0]], "absent")


class Record(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.t = self._tmp.name
        self.root = os.path.join(self.t, "in")
        os.makedirs(os.path.join(self.root, "a"))
        self.src = os.path.join(self.root, "a", "report.pdf")
        with open(self.src, "wb") as fh:
            fh.write(b"%PDF-1.4 pretend")
        self.out = os.path.join(self.t, "out", "a", "report_pdf")
        os.makedirs(self.out)
        for name in ("report.md", "report_meta.json"):
            open(os.path.join(self.out, name), "w").close()

    def tearDown(self):
        self._tmp.cleanup()

    def record(self, options=None, input_root="__root__"):
        root = self.root if input_root == "__root__" else input_root
        now = time.time()
        return provenance.provenance_record(self.src, self.out, options, now, now + 1.5, root)

    def test_source_identity(self):
        r = self.record()
        self.assertEqual(r["source"]["path"], self.src)
        self.assertEqual(r["source"]["relative_path"], os.path.join("a", "report.pdf"))
        self.assertEqual(r["source"]["sha256"], provenance.file_sha256(self.src))
        self.assertEqual(r["source"]["bytes"], os.path.getsize(self.src))
        self.assertTrue(r["source"]["modified"].endswith("Z"))

    def test_the_hash_is_of_the_source_not_the_output(self):
        # The question provenance answers is "has THIS INPUT already been
        # converted by these versions", so the hash has to be the input's.
        with open(os.path.join(self.out, "report.md"), "w") as fh:
            fh.write("different bytes entirely")
        self.assertEqual(self.record()["source"]["sha256"], provenance.file_sha256(self.src))

    def test_a_changed_source_changes_the_hash(self):
        before = self.record()["source"]["sha256"]
        with open(self.src, "ab") as fh:
            fh.write(b" edited")
        self.assertNotEqual(self.record()["source"]["sha256"], before)

    def test_timing_and_outputs(self):
        r = self.record()
        self.assertEqual(r["conversion"]["seconds"], 1.5)
        self.assertEqual(r["conversion"]["outputs"], ["report.md", "report_meta.json"])
        self.assertEqual(r["conversion"]["output_dir"], self.out)

    def test_the_sidecar_does_not_list_itself(self):
        # Re-recording into a folder that already has one must not make the
        # second record claim provenance.json as a conversion output.
        provenance.write_provenance(self.out, self.record())
        self.assertNotIn(provenance.PROVENANCE_FILE, self.record()["conversion"]["outputs"])

    def test_subdirectories_are_not_listed_as_outputs(self):
        os.makedirs(os.path.join(self.out, "images"))
        self.assertNotIn("images", self.record()["conversion"]["outputs"])

    def test_output_format_defaults_to_markdown(self):
        self.assertEqual(self.record()["conversion"]["output_format"], "markdown")
        self.assertEqual(
            self.record({"output_format": "json"})["conversion"]["output_format"], "json")

    def test_options_are_recorded_redacted(self):
        r = self.record({"use_llm": True, "openai_api_key": "sk-secret",
                         "openai_base_url": "http://192.168.3.46:1234/v1"})
        self.assertEqual(r["options"]["openai_api_key"], provenance.REDACTED)
        self.assertEqual(r["options"]["openai_base_url"], "http://192.168.3.46:1234/v1")
        self.assertNotIn("sk-secret", json.dumps(r))

    def test_no_input_root_means_no_relative_path(self):
        self.assertIsNone(self.record(input_root=None)["source"]["relative_path"])

    def test_a_source_outside_the_input_root_has_no_relative_path(self):
        outside = os.path.join(self.t, "elsewhere.pdf")
        with open(outside, "wb") as fh:
            fh.write(b"x")
        now = time.time()
        r = provenance.provenance_record(outside, self.out, None, now, now, self.root)
        self.assertIsNone(r["source"]["relative_path"])

    def test_schema_and_versions_are_present(self):
        r = self.record()
        self.assertEqual(r["schema"], provenance.PROVENANCE_SCHEMA)
        self.assertEqual(set(r["versions"]), set(provenance.PROVENANCE_PACKAGES))


class Write(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.out = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_it_writes_parseable_json_at_the_expected_name(self):
        path = provenance.write_provenance(self.out, {"schema": "x", "n": 1})
        self.assertEqual(path, os.path.join(self.out, "provenance.json"))
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), {"schema": "x", "n": 1})

    def test_no_temp_file_is_left_behind(self):
        provenance.write_provenance(self.out, {"schema": "x"})
        self.assertEqual(os.listdir(self.out), ["provenance.json"])

    def test_rewriting_replaces_rather_than_appends(self):
        provenance.write_provenance(self.out, {"schema": "x", "n": 1})
        path = provenance.write_provenance(self.out, {"schema": "x", "n": 2})
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["n"], 2)

    def test_it_ends_with_a_newline(self):
        path = provenance.write_provenance(self.out, {"schema": "x"})
        with open(path, encoding="utf-8") as fh:
            self.assertTrue(fh.read().endswith("\n"))


class ReExport(unittest.TestCase):
    def test_tasks_uses_the_same_implementation(self):
        # convert_document calls these by name; if the module split ever leaves
        # tasks.py with its own copy, the three execution modes stop agreeing.
        self.assertIs(tasks.provenance_record, provenance.provenance_record)
        self.assertIs(tasks.write_provenance, provenance.write_provenance)


class ScriptEntry(unittest.TestCase):
    """The interface the bash backends use."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.t = self._tmp.name
        self.src = os.path.join(self.t, "doc.docx")
        with open(self.src, "wb") as fh:
            fh.write(b"PK pretend")
        self.out = os.path.join(self.t, "converted")
        os.makedirs(self.out)
        open(os.path.join(self.out, "doc.md"), "w").close()

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args):
        return provenance.main([str(a) for a in args])

    def read(self):
        with open(os.path.join(self.out, "provenance.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def test_a_markitdown_run(self):
        rc = self.run_cli("--source", self.src, "--output-dir", self.out,
                          "--backend", "markitdown", "--started", 1000, "--finished", 1002,
                          "--version", "markitdown=0.1.3", "--input-root", self.t,
                          "--command", "markitdown", self.src, "-o", "doc.md")
        self.assertEqual(rc, 0)
        r = self.read()
        self.assertEqual(r["conversion"]["backend"], "markitdown")
        self.assertEqual(r["conversion"]["seconds"], 2.0)
        self.assertEqual(r["versions"], {"markitdown": "0.1.3"})
        self.assertEqual(r["conversion"]["command"][0], "markitdown")
        self.assertEqual(r["source"]["relative_path"], "doc.docx")
        self.assertEqual(r["source"]["sha256"], provenance.file_sha256(self.src))

    def test_an_empty_version_is_recorded_as_unknown(self):
        # pkg_version() echoes nothing when pipx cannot answer. "unknown" is a
        # fact; an empty string would read as "version is the empty string".
        self.run_cli("--source", self.src, "--output-dir", self.out,
                     "--backend", "markitdown", "--started", 1, "--finished", 2,
                     "--version", "markitdown=")
        self.assertEqual(self.read()["versions"], {"markitdown": "unknown"})

    def test_no_version_given_falls_back_to_this_interpreter(self):
        self.run_cli("--source", self.src, "--output-dir", self.out,
                     "--backend", "markitdown", "--started", 1, "--finished", 2)
        self.assertEqual(set(self.read()["versions"]), {"markitdown"})

    def test_options_are_redacted_here_too(self):
        self.run_cli("--source", self.src, "--output-dir", self.out,
                     "--backend", "marker", "--started", 1, "--finished", 2,
                     "--option", "openai_api_key=sk-secret", "--option", "force_ocr=true")
        r = self.read()
        self.assertEqual(r["options"]["openai_api_key"], provenance.REDACTED)
        self.assertEqual(r["options"]["force_ocr"], "true")
        self.assertNotIn("sk-secret", json.dumps(r))

    def test_a_value_containing_an_equals_sign_survives(self):
        self.run_cli("--source", self.src, "--output-dir", self.out,
                     "--backend", "marker", "--started", 1, "--finished", 2,
                     "--option", "openai_base_url=http://h/v1?a=b")
        self.assertEqual(self.read()["options"]["openai_base_url"], "http://h/v1?a=b")

    def test_a_missing_source_exits_2_and_writes_nothing(self):
        rc = self.run_cli("--source", os.path.join(self.t, "gone.pdf"),
                          "--output-dir", self.out, "--backend", "marker",
                          "--started", 1, "--finished", 2)
        self.assertEqual(rc, 2)
        self.assertNotIn("provenance.json", os.listdir(self.out))

    def test_a_missing_output_dir_exits_2(self):
        rc = self.run_cli("--source", self.src, "--output-dir",
                          os.path.join(self.t, "nowhere"), "--backend", "marker",
                          "--started", 1, "--finished", 2)
        self.assertEqual(rc, 2)

    def test_a_malformed_pair_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--source", self.src, "--output-dir", self.out,
                         "--backend", "marker", "--started", 1, "--finished", 2,
                         "--option", "nokeyvalue")

    def test_an_unknown_backend_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_cli("--source", self.src, "--output-dir", self.out,
                         "--backend", "pandoc", "--started", 1, "--finished", 2)


if __name__ == "__main__":
    unittest.main()
