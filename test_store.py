import json
import shutil
import unittest
from unittest import mock

import store
from test_support import make_data_dir


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = make_data_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_missing_config_returns_defaults(self):
        config = store.load_config(self.dir)

        self.assertEqual(config["default_vendor"], "deepseek")
        self.assertEqual(config["model_cloudflare"], "@cf/meta/llama-3.3-70b-instruct-fp8-fast")
        self.assertEqual(config["system_prompt"], "用中文简短回答，200 字以内")

    def test_roundtrip_custom_values(self):
        store.save_config(self.dir, {"default_vendor": "kimi", "model_kimi": "kimi-k2"})

        config = store.load_config(self.dir)

        self.assertEqual(config["default_vendor"], "kimi")
        self.assertEqual(config["model_kimi"], "kimi-k2")
        self.assertEqual(config["system_prompt"], "用中文简短回答，200 字以内")

    def test_corrupt_json_falls_back_to_defaults(self):
        (self.dir / "config.json").write_text("{not json", encoding="utf-8")

        self.assertEqual(store.load_config(self.dir)["default_vendor"], "deepseek")

    def test_non_string_values_are_ignored(self):
        (self.dir / "config.json").write_text(json.dumps({"default_vendor": 5, "model_kimi": "m"}), encoding="utf-8")

        config = store.load_config(self.dir)

        self.assertEqual(config["default_vendor"], "deepseek")
        self.assertEqual(config["model_kimi"], "m")


class CredentialTests(unittest.TestCase):
    def test_target_names(self):
        for vendor in ("cloudflare", "deepseek", "qwen", "kimi", "glm", "openai", "custom"):
            self.assertEqual(store.credential_target(vendor), "WinShun/me.ai-ask/key_" + vendor)

    def test_load_settings_merges_config_and_secrets(self):
        directory = make_data_dir()
        self.addCleanup(shutil.rmtree, directory, True)
        store.save_config(directory, {"default_vendor": "qwen", "model_qwen": "qwen-turbo"})

        with mock.patch("store.read_secret", return_value="sk-qwen"):
            settings = store.load_settings(directory)

        self.assertEqual(settings["default_vendor"], "qwen")
        self.assertEqual(settings["model_qwen"], "qwen-turbo")
        self.assertEqual(settings["key_qwen"], "sk-qwen")
        self.assertIn("key_deepseek", settings)
        self.assertEqual(settings["key_deepseek"], "sk-qwen")


if __name__ == "__main__":
    unittest.main()
