"""The LLM marker talks to when use_llm is set.

The bug this exists for: `use_llm: true` used to mean Google Gemini. marker's
ConfigParser falls back to "marker.services.gemini.GoogleGeminiService" when no
llm_service is given, so a job on this LAN reached for an internet API and a key
nobody had set — and the failure said nothing about why.

Two restrictions are asserted here as behaviour, not left as comments: the
service comes from a fixed map (marker imports that string as a class), and the
endpoint is deployment configuration, so a request cannot name it.
"""
import os
import unittest
from contextlib import contextmanager

import tasks


@contextmanager
def env(**kw):
    """Set/clear environment variables for one test and restore them after."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


CLEAR = dict(LLM_SERVICE=None, LLM_BASE_URL=None, LLM_MODEL=None, LLM_API_KEY=None)


class TestLlmConfig(unittest.TestCase):
    def test_nothing_configured_is_not_an_error(self):
        with env(**CLEAR):
            self.assertEqual(tasks.llm_config(), {})

    def test_ollama_uses_markers_own_option_names(self):
        with env(**{**CLEAR, "LLM_SERVICE": "ollama",
                    "LLM_BASE_URL": "http://corelia:11434", "LLM_MODEL": "gemma3"}):
            cfg = tasks.llm_config()
        self.assertEqual(cfg["llm_service"], "marker.services.ollama.OllamaService")
        self.assertEqual(cfg["ollama_base_url"], "http://corelia:11434")
        self.assertEqual(cfg["ollama_model"], "gemma3")

    def test_openai_compatible_gets_a_key_it_does_not_need(self):
        # llama.cpp / vLLM / LM Studio ignore the key, but the client requires
        # one to be present, so a missing LLM_API_KEY must not break the job.
        with env(**{**CLEAR, "LLM_SERVICE": "openai",
                    "LLM_BASE_URL": "http://corelia:8080/v1"}):
            cfg = tasks.llm_config()
        self.assertEqual(cfg["llm_service"], "marker.services.openai.OpenAIService")
        self.assertEqual(cfg["openai_base_url"], "http://corelia:8080/v1")
        self.assertTrue(cfg["openai_api_key"])

    def test_a_trailing_slash_does_not_double_up(self):
        with env(**{**CLEAR, "LLM_SERVICE": "ollama", "LLM_BASE_URL": "http://x:11434/"}):
            self.assertEqual(tasks.llm_config()["ollama_base_url"], "http://x:11434")

    def test_defaults_to_a_local_endpoint_not_a_cloud_one(self):
        for name in ("ollama", "openai"):
            with env(**{**CLEAR, "LLM_SERVICE": name}):
                url = tasks.llm_config()[f"{name}_base_url"]
            self.assertIn("localhost", url, f"{name} default should be local")

    def test_an_unknown_service_fails_loudly(self):
        with env(**{**CLEAR, "LLM_SERVICE": "gemini"}):
            with self.assertRaises(tasks.InvalidOption) as e:
                tasks.llm_config()
        # The message must name what IS supported, not just what is not.
        self.assertIn("ollama", str(e.exception))
        self.assertIn("openai", str(e.exception))

    def test_case_and_spacing_are_forgiven(self):
        with env(**{**CLEAR, "LLM_SERVICE": "  OLLAMA  "}):
            self.assertEqual(tasks.llm_config()["llm_service"],
                             "marker.services.ollama.OllamaService")


class TestUseLlmThroughValidate(unittest.TestCase):
    def test_use_llm_without_configuration_is_refused(self):
        # The regression: this used to be accepted and became a Gemini call.
        with env(**CLEAR):
            with self.assertRaises(tasks.InvalidOption) as e:
                tasks.validate_options({"use_llm": True})
        msg = str(e.exception)
        self.assertIn("LLM_SERVICE", msg)
        self.assertIn("Gemini", msg, "the message should say what it used to do")

    def test_use_llm_picks_up_the_deployment_config(self):
        with env(**{**CLEAR, "LLM_SERVICE": "ollama", "LLM_BASE_URL": "http://h:11434"}):
            opts = tasks.validate_options({"use_llm": True})
        self.assertEqual(opts["llm_service"], "marker.services.ollama.OllamaService")
        self.assertEqual(opts["ollama_base_url"], "http://h:11434")

    def test_a_request_cannot_name_the_service_or_the_endpoint(self):
        # Arbitrary-class import and request forgery, in one assertion: what a
        # caller sends is overwritten by the deployment's own configuration.
        with env(**{**CLEAR, "LLM_SERVICE": "ollama", "LLM_BASE_URL": "http://trusted:11434"}):
            opts = tasks.validate_options({
                "use_llm": True,
                "llm_service": "os.system",
                "ollama_base_url": "http://attacker.example/",
            })
        self.assertEqual(opts["llm_service"], "marker.services.ollama.OllamaService")
        self.assertEqual(opts["ollama_base_url"], "http://trusted:11434")

    def test_not_asking_for_an_llm_adds_nothing(self):
        with env(**{**CLEAR, "LLM_SERVICE": "ollama"}):
            opts = tasks.validate_options({"output_format": "markdown"})
        self.assertNotIn("llm_service", opts)

    def test_the_callers_dict_is_still_not_mutated(self):
        src = {"use_llm": True}
        with env(**{**CLEAR, "LLM_SERVICE": "ollama"}):
            tasks.validate_options(src)
        self.assertEqual(src, {"use_llm": True})


if __name__ == "__main__":
    unittest.main()
