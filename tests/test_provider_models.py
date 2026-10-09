"""Providers hold models: two under one provider never collide, and a legacy
one-model-per-provider file migrates without losing anything."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("BASE_URL", "http://localhost:1/v1")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("MODEL", "test-model")

from harness import providers  # noqa: E402


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, user, secret):
        self.store[(service, user)] = secret

    def get_password(self, service, user):
        return self.store.get((service, user))

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


LEGACY = {
    "active_id": "https://a.example/v1",
    "providers": [
        {"id": "https://a.example/v1", "name": "A - Flash", "base_url": "https://a.example/v1",
         "model": "flash-1", "headers": {}, "disabled": False, "reasoning_effort": "high"},
        {"id": "https://b.example/v1", "name": "B - Flash", "base_url": "https://b.example/v1",
         "model": "flash-2", "headers": {"X-Test": "1"}, "disabled": False},
    ],
}


class ModelCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "providers.json"
        mock.patch.object(providers, "PROVIDERS_FILE", self.file).start()
        self.fake = FakeKeyring()
        mock.patch.dict("sys.modules", {"keyring": self.fake}).start()
        self.addCleanup(mock.patch.stopall)

    def write(self, data):
        self.file.write_text(json.dumps(data), encoding="utf-8")
        providers._CACHE.clear()

    def group(self, gid="grp-1", name="NVIDIA", base="https://nvidia.example/v1"):
        return {"id": gid, "name": name, "base_url": base, "headers": {}, "disabled": False}

    def model(self, mid, name, model, gid="grp-1", **extra):
        payload = {"id": mid, "name": name, "model": model, **extra}
        return providers.save_model(payload, gid)

    def test_group_can_hold_several_models(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash")

        models = providers.list_models("grp-1")
        self.assertEqual([m["id"] for m in models], ["mdl-a", "mdl-b"])
        self.assertEqual([m["name"] for m in models], ["GLM 5.3 Flash", "DeepSeek V4.1 Flash"])
        self.assertEqual([m["model"] for m in models], ["glm-5.3-flash", "deepseek-v4.1-flash"])

    def test_models_survive_a_reload(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash")

        providers._CACHE.clear()
        again = providers.list_models("grp-1")
        self.assertEqual([m["model"] for m in again], ["glm-5.3-flash", "deepseek-v4.1-flash"])
        raw = json.loads(self.file.read_text())
        self.assertEqual(len(raw["providers"]), 1)

    def test_edit_one_model_leaves_its_siblings_alone(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash", reasoning_effort="high")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash")

        providers.save_model({"id": "mdl-b", "name": "DeepSeek V4.1 Flash",
                              "model": "deepseek-v4.1-flash:free", "temperature": "0.4"}, "grp-1")

        a, b = providers.list_models("grp-1")
        self.assertEqual(a["model"], "glm-5.3-flash")
        self.assertEqual(a["reasoning_effort"], "high")
        self.assertEqual(b["model"], "deepseek-v4.1-flash:free")
        self.assertEqual(b["temperature"], 0.4)
        self.assertIsNone(b["reasoning_effort"])

    def test_rename_keeps_identity(self):
        providers.save_group(self.group())
        self.model("mdl-a", "Old name", "glm-5.3-flash")
        providers.save_model({"id": "mdl-a", "name": "New name", "model": "glm-5.3-flash"}, "grp-1")

        entry = providers.get_provider("mdl-a")
        self.assertEqual(entry["name"], "New name")
        self.assertEqual(entry["provider_id"], "grp-1")

    def test_delete_one_model_keeps_provider_and_sibling(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash")
        providers.set_active("mdl-a")

        self.assertTrue(providers.delete_model("mdl-a"))

        self.assertEqual(len(providers.list_groups()), 1)
        self.assertEqual([m["id"] for m in providers.list_models("grp-1")], ["mdl-b"])
        # the deleted model was active: the provider's other model takes over
        self.assertEqual(providers.get_active_id(), "mdl-b")

    def test_deleting_last_model_removes_provider_and_key(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash")
        providers.set_key("grp-1", "sk-secret")
        providers.delete_model("mdl-a")
        self.assertEqual(providers.list_groups(), [])
        self.assertEqual(providers._key_for_group("grp-1"), None)

    def test_one_key_serves_every_model_of_a_provider(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash")
        providers.set_key("mdl-b", "sk-shared")

        self.assertEqual(providers.provider_status("mdl-a")["key"], "keyring")
        self.assertEqual(providers.provider_status("mdl-b")["key"], "keyring")
        providers.set_active("mdl-a")
        base, key, model, source = providers.active_config()
        self.assertEqual((base, key, model, source), ("https://nvidia.example/v1", "sk-shared",
                                                      "glm-5.3-flash", "keyring"))
        providers.set_active("mdl-b")
        self.assertEqual(providers.active_config()[2], "deepseek-v4.1-flash")

    def test_each_model_keeps_its_own_settings(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash", reasoning_effort="xhigh",
                   context_window=8_000, max_output_tokens=512, temperature="0.1")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash",
                   supports_tools=False, extra_params={"top_p": 0.9})

        providers.set_active("mdl-a")
        self.assertEqual(providers.active_model_params(),
                         {"reasoning_effort": "xhigh", "temperature": 0.1, "max_tokens": 512})
        self.assertEqual(providers.active_context_window(), 8_000)
        self.assertTrue(providers.active_model_capabilities()["supports_tools"])

        providers.set_active("mdl-b")
        self.assertEqual(providers.active_model_params(), {"top_p": 0.9})
        self.assertFalse(providers.active_model_capabilities()["supports_tools"])
        self.assertEqual(providers.active_context_window(), 128_000)  # unset falls back

    def test_effort_is_per_model(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash")
        providers.set_effort("mdl-a", "low")

        self.assertEqual(providers.get_model("mdl-a")["reasoning_effort"], "low")
        self.assertIsNone(providers.get_model("mdl-b")["reasoning_effort"])

    def test_same_provider_name_or_host_is_not_an_identity(self):
        providers.save_group(self.group())
        providers.save_group({"id": "grp-2", "name": "NVIDIA", "base_url": "https://nvidia.example/v1"})
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash", gid="grp-1")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash", gid="grp-2")

        self.assertEqual(len(providers.list_groups()), 2)
        self.assertEqual(len(providers.list_models()), 2)
        self.assertNotEqual(providers.get_model("mdl-a")["id"], providers.get_model("mdl-b")["id"])

    def test_legacy_file_migrates_without_losing_entries(self):
        self.write(LEGACY)
        models = providers.list_providers()
        self.assertEqual([m["model"] for m in models], ["flash-1", "flash-2"])
        self.assertEqual(len(providers.list_groups()), 2)
        self.assertEqual(providers.get_active_id(), models[0]["id"])
        self.assertEqual(providers.get_provider(models[0]["id"])["reasoning_effort"], "high")
        self.assertEqual(providers.get_provider(models[1]["id"])["headers"], {"X-Test": "1"})
        self.assertEqual(providers.get_active_provider_id(), "https://a.example/v1")

        # the first write keeps the original file next to the new one
        providers.set_active(models[1]["id"])
        backup = self.file.with_name("providers.json.bak")
        self.assertTrue(backup.exists())
        raw = json.loads(self.file.read_text())
        self.assertEqual(raw["version"], providers.FORMAT_VERSION)
        self.assertEqual([m["model"] for m in providers.list_providers()], ["flash-1", "flash-2"])

    def test_legacy_colliding_entries_become_siblings(self):
        data = json.loads(json.dumps(LEGACY))
        data["providers"][1]["id"] = data["providers"][0]["id"]
        data["providers"][1]["model"] = "flash-2"
        self.write(data)

        models = providers.list_providers()
        self.assertEqual([m["model"] for m in models], ["flash-1", "flash-2"])
        self.assertEqual(len(providers.list_groups()), 1)
        self.assertEqual(len(providers.list_groups()[0]["models"]), 2)

    def test_legacy_ids_keep_their_keyring_entry(self):
        self.write(LEGACY)
        self.fake.set_password(providers.SERVICE, "https://b.example/v1", "sk-legacy")
        self.assertEqual(providers.provider_status("https://b.example/v1")["key"], "keyring")
        providers.set_active("https://b.example/v1")
        self.assertEqual(providers.active_config()[1], "sk-legacy")

    def test_migrated_active_id_lands_on_its_model(self):
        self.write(LEGACY)
        self.assertEqual(providers.get_active_id(), providers.get_provider("https://a.example/v1")["id"])

    def test_migration_is_reported(self):
        self.write(LEGACY)
        providers.list_providers()
        self.assertEqual(providers.migration_report(), [])
        providers.set_active(providers.list_providers()[1]["id"])
        report = providers.migration_report()
        self.assertTrue(report)
        self.assertIn("providers.json.bak", report[0])

    def test_flat_save_still_works_like_the_old_format(self):
        providers.save_provider({"id": "p1", "name": "P1", "base_url": "https://x/v1", "model": "m"})
        self.assertEqual(providers.get_active_id(), "p1")
        entry = providers.get_provider("p1")
        self.assertEqual((entry["base_url"], entry["model"]), ("https://x/v1", "m"))
        self.assertEqual(providers.get_active_provider_id(), "p1")

    def test_a_disabled_model_is_never_served(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash")
        providers.set_active("mdl-a")
        providers.save_model({"id": "mdl-a", "disabled": True}, "grp-1")

        self.assertEqual(providers.active_config()[2], "deepseek-v4.1-flash")
        self.assertEqual(providers.active_model_capabilities()["model"], "deepseek-v4.1-flash")

    def test_a_model_without_the_reasoning_knob_never_sends_effort(self):
        providers.save_group(self.group())
        self.model("mdl-a", "Plain model", "plain-1", reasoning_effort="high",
                   supports_reasoning=False)
        providers.set_active("mdl-a")
        self.assertNotIn("reasoning_effort", providers.active_model_params())

    def test_deleting_a_model_keeps_the_other_models_settings(self):
        providers.save_group(self.group())
        self.model("mdl-a", "GLM 5.3 Flash", "glm-5.3-flash", reasoning_effort="high")
        self.model("mdl-b", "DeepSeek V4.1 Flash", "deepseek-v4.1-flash",
                   extra_params={"top_p": 0.9})
        providers.save_model({"id": "mdl-c", "model": "third"}, "grp-1")
        providers.delete_model("mdl-b")

        remaining = {m["model"]: m for m in providers.list_models("grp-1")}
        self.assertEqual(set(remaining), {"glm-5.3-flash", "third"})
        self.assertEqual(remaining["glm-5.3-flash"]["reasoning_effort"], "high")

    def test_extra_params_are_whitelisted(self):
        providers.save_group(self.group())
        providers.save_model({"id": "mdl-a", "model": "m", "extra_params":
                              {"top_p": 0.5, "extra_body": {"x": 1}, "messages": "nope",
                               "ok": True}}, "grp-1")
        model = providers.get_model("mdl-a")
        self.assertEqual(model["extra_params"], {"top_p": 0.5, "ok": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
