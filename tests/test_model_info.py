"""Auto-configuration from a provider's /models endpoint: normalization of
heterogeneous payloads, fill-only-empty semantics, and the save backstop."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import paths, providers


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


class ProviderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        paths.init(
            project_root=Path(self.tmp.name) / "proj",
            app_data_dir=Path(self.tmp.name) / "app",
        )
        self.addCleanup(paths.reset)
        self.fake = FakeKeyring()
        mock.patch.dict("sys.modules", {"keyring": self.fake}).start()
        self.addCleanup(mock.patch.stopall)

    def entry(self, pid="p1", model="m"):
        return {
            "id": pid,
            "name": "P1",
            "base_url": "https://x/v1",
            "model": model,
            "headers": {},
            "disabled": False,
        }


class NormalizeTests(unittest.TestCase):
    def test_openrouter_shape(self):
        raw = {
            "id": "vendor/model",
            "name": "Vendor Model",
            "context_length": 131072,
            "top_provider": {"max_completion_tokens": 32768},
            "architecture": {"input_modalities": ["text", "file"]},
            "supported_parameters": ["temperature", "top_p"],
        }
        info = providers._normalize_models_entry(raw)
        self.assertEqual(info["context_window"], 131072)
        self.assertEqual(info["max_output_tokens"], 32768)
        self.assertEqual(info["name"], "Vendor Model")
        self.assertFalse(info["supports_images"])
        self.assertFalse(info["supports_tools"])
        self.assertFalse(info["supports_reasoning"])

    def test_groq_shape(self):
        info = providers._normalize_models_entry(
            {"id": "llama-3", "context_window": 8192}
        )
        self.assertEqual(info, {"context_window": 8192})

    def test_vllm_shape(self):
        info = providers._normalize_models_entry({"id": "qwen", "max_model_len": 40960})
        self.assertEqual(info, {"context_window": 40960})

    def test_plain_openai_entry_yields_nothing(self):
        raw = {"id": "gpt-x", "object": "model", "created": 1, "owned_by": "org"}
        self.assertEqual(providers._normalize_models_entry(raw), {})

    def test_string_modalities(self):
        info = providers._normalize_models_entry(
            {"id": "m", "modalities": "text->text"}
        )
        self.assertFalse(info["supports_images"])
        self.assertNotIn(
            "supports_images",
            providers._normalize_models_entry(
                {"id": "m", "modalities": "text+image->text"}
            ),
        )

    def test_name_matching_id_is_skipped(self):
        info = providers._normalize_models_entry({"id": "m", "name": "m"})
        self.assertNotIn("name", info)

    def test_bogus_payload_is_ignored(self):
        self.assertEqual(providers._normalize_models_entry(None), {})
        self.assertEqual(providers._normalize_models_entry("x"), {})
        self.assertEqual(providers._normalize_models_entry({"top_provider": 5}), {})

    def test_capable_model_flips_nothing(self):
        raw = {
            "id": "m",
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "reasoning"],
        }
        info = providers._normalize_models_entry(raw)
        self.assertEqual(info, {})


class MatchTests(unittest.TestCase):
    def test_exact_then_case_insensitive(self):
        entries = [{"id": "Model-X"}, {"id": "other"}]
        self.assertEqual(
            providers._match_models_entry(entries, "Model-X"), {"id": "Model-X"}
        )
        self.assertEqual(
            providers._match_models_entry(entries, "model-x"), {"id": "Model-X"}
        )
        self.assertIsNone(providers._match_models_entry(entries, "missing"))
        self.assertIsNone(providers._match_models_entry(entries, ""))
        self.assertIsNone(providers._match_models_entry([], "m"))


class ApplyTests(unittest.TestCase):
    def test_fills_only_empty_fields(self):
        payload = {"context_window": None, "max_output_tokens": None, "name": ""}
        applied = providers.apply_model_info(
            payload,
            {
                "context_window": 1000,
                "max_output_tokens": 500,
                "name": "Pretty",
            },
        )
        self.assertEqual(
            payload,
            {"context_window": 1000, "max_output_tokens": 500, "name": "Pretty"},
        )
        self.assertEqual(len(applied), 3)

    def test_never_overwrites_set_fields(self):
        payload = {"context_window": 7, "max_output_tokens": 8, "name": "Mine"}
        applied = providers.apply_model_info(
            payload,
            {"context_window": 1000, "max_output_tokens": 500, "name": "Pretty"},
        )
        self.assertEqual(applied, [])
        self.assertEqual(
            payload, {"context_window": 7, "max_output_tokens": 8, "name": "Mine"}
        )

    def test_bools_never_move_on_the_save_path(self):
        payload = {"supports_images": True, "supports_tools": True}
        info = {"supports_images": False, "supports_tools": False}
        applied = providers.apply_model_info(payload, info)
        self.assertEqual(applied, [])
        self.assertEqual(payload, {"supports_images": True, "supports_tools": True})

    def test_bools_move_only_downward_when_asked(self):
        payload = {"supports_images": True, "supports_tools": False}
        info = {"supports_images": False, "supports_tools": False}
        applied = providers.apply_model_info(payload, info, include_bools=True)
        self.assertEqual(payload, {"supports_images": False, "supports_tools": False})
        self.assertEqual(len(applied), 1)

    def test_empty_info_is_a_noop(self):
        payload = {"context_window": None}
        self.assertEqual(providers.apply_model_info(payload, {}), [])
        self.assertEqual(providers.apply_model_info(payload, None), [])
        self.assertIsNone(payload["context_window"])


class FetchAndBackstopTests(ProviderCase):
    def test_fetch_model_config_by_base_url(self):
        with mock.patch.object(
            providers,
            "_models_entries",
            return_value=[{"id": "m", "context_length": 4096}],
        ):
            result = providers.fetch_model_config(
                "m", base_url="https://x/v1", key="sk"
            )
        self.assertTrue(result["found"])
        self.assertEqual(result["info"], {"context_window": 4096})
        self.assertEqual(result["note"], "")

    def test_fetch_reports_a_miss(self):
        with mock.patch.object(
            providers, "_models_entries", return_value=[{"id": "other"}]
        ):
            result = providers.fetch_model_config("m", base_url="https://x/v1")
        self.assertFalse(result["found"])
        self.assertIn("not in the /models list", result["note"])

    def test_fetch_survives_transport_errors(self):
        with mock.patch.object(
            providers, "_models_entries", side_effect=RuntimeError("boom")
        ):
            result = providers.fetch_model_config("m", base_url="https://x/v1")
        self.assertFalse(result["found"])
        self.assertTrue(result["note"])

    def test_fetch_needs_a_model_and_a_base(self):
        self.assertFalse(providers.fetch_model_config("")["found"])
        self.assertFalse(providers.fetch_model_config("m")["found"])

    def test_fetch_resolves_existing_provider(self):
        providers.save_provider(self.entry())
        seen = {}

        def fake_entries(base_url, api_key, timeout):
            seen["base_url"] = base_url
            seen["api_key"] = api_key
            return [{"id": "m", "context_window": 2048}]

        with mock.patch.object(providers, "_models_entries", side_effect=fake_entries):
            result = providers.fetch_model_config("m", provider_id="p1")
        self.assertTrue(result["found"])
        self.assertEqual(seen["base_url"], "https://x/v1")
        self.assertEqual(result["info"], {"context_window": 2048})

    def test_backstop_fills_empty_numerics_only(self):
        stored = providers.save_provider(self.entry())
        with mock.patch.object(
            providers,
            "_models_entries",
            return_value=[
                {
                    "id": "m",
                    "context_length": 4096,
                    "top_provider": {"max_completion_tokens": 256},
                    "architecture": {"input_modalities": ["text"]},
                }
            ],
        ):
            result = providers.autoconfigure_from_models(stored["id"])
        self.assertTrue(result["found"])
        self.assertIn("context window 4096", result["applied"])
        self.assertIn("max output 256", result["applied"])
        row = providers.get_provider(stored["id"])
        self.assertEqual(row["context_window"], 4096)
        self.assertEqual(row["max_output_tokens"], 256)

        self.assertTrue(row["supports_images"])

    def test_backstop_keeps_user_values(self):
        entry = self.entry()
        entry["context_window"] = 123
        stored = providers.save_provider(entry)
        with mock.patch.object(
            providers,
            "_models_entries",
            return_value=[{"id": "m", "context_length": 4096}],
        ):
            result = providers.autoconfigure_from_models(stored["id"])
        self.assertEqual(result["applied"], [])
        self.assertEqual(providers.get_provider(stored["id"])["context_window"], 123)

    def test_backstop_survives_endpoint_failures(self):
        stored = providers.save_provider(self.entry())
        with mock.patch.object(
            providers, "_models_entries", side_effect=RuntimeError("down")
        ):
            result = providers.autoconfigure_from_models(stored["id"])
        self.assertFalse(result["found"])
        self.assertEqual(result["applied"], [])
        self.assertTrue(result["note"])

    def test_backstop_handles_a_missing_model_entry(self):
        stored = providers.save_provider(self.entry())
        with mock.patch.object(
            providers, "_models_entries", return_value=[{"id": "other"}]
        ):
            result = providers.autoconfigure_from_models(stored["id"])
        self.assertFalse(result["found"])
        self.assertIn("no /models metadata", result["note"])

    def test_backstop_explains_a_bare_models_entry(self):
        stored = providers.save_provider(self.entry())
        with mock.patch.object(
            providers,
            "_models_entries",
            return_value=[{"id": "m", "object": "model", "owned_by": "river"}],
        ):
            result = providers.autoconfigure_from_models(stored["id"])
        self.assertTrue(result["found"])
        self.assertEqual(result["applied"], [])
        self.assertIn("publishes no settings", result["note"])

    def test_fetch_explains_a_bare_models_entry(self):
        with mock.patch.object(
            providers,
            "_models_entries",
            return_value=[{"id": "m", "object": "model", "owned_by": "river"}],
        ):
            result = providers.fetch_model_config("m", base_url="https://x/v1")
        self.assertTrue(result["found"])
        self.assertIsNone(result["info"])
        self.assertIn("publishes no settings", result["note"])

    def test_row_now_carries_supports_images(self):
        entry = self.entry()
        entry["supports_images"] = False
        stored = providers.save_provider(entry)
        self.assertFalse(providers.get_provider(stored["id"])["supports_images"])


if __name__ == "__main__":
    unittest.main()
