from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import providers


class VisionProviderTests(unittest.TestCase):
    def test_image_is_sent_only_to_configured_local_vision_model(self):
        fake_settings = SimpleNamespace(
            vision_enabled=True,
            vision_model="qwen2.5vl:3b",
            vision_keep_alive="0",
            ollama_url="http://127.0.0.1:11434/api/chat",
            ollama_num_ctx=4096,
            request_timeout=10,
        )
        captured = {}

        def fake_post(url, body, headers, timeout):
            captured.update({"url": url, "body": body, "headers": headers, "timeout": timeout})
            return {"message": {"content": "画面中有一处未关闭的安全门"}}

        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "scene.jpg"
            image.write_bytes(b"local-test-image")
            with patch.object(providers, "settings", fake_settings), patch.object(
                providers, "_post_json", side_effect=fake_post
            ):
                result = providers.ollama_vision(str(image), "描述图片")

        self.assertIn("安全门", result)
        self.assertEqual("qwen2.5vl:3b", captured["body"]["model"])
        self.assertEqual("0", captured["body"]["keep_alive"])
        self.assertTrue(captured["body"]["messages"][0]["images"][0])
        self.assertTrue(captured["url"].startswith("http://127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
