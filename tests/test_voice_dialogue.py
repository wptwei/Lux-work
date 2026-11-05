"""语音对话闭环的纯逻辑单测：PCM 解码/重采样与转写路由的校验/边界。

真正转写需要加载本地 ASR 模型，这里对 decode/resample 做纯函数测试，
并把 transcribe_samples / validate_voice_text 打桩后验证路由的
“空录音不提交”“命中红线不提交”“正常文本可提交”三条关键边界。
"""
from __future__ import annotations

import base64
import unittest
from unittest import mock

import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.voice_dialogue import _decode_and_resample, voice_dialogue_routes


class DecodeResampleTests(unittest.TestCase):
    def test_resamples_48k_to_16k(self):
        samples = (np.sin(np.linspace(0, 10, 48000)) * 12000).astype("<i2")
        b64 = base64.b64encode(samples.tobytes()).decode()
        out = _decode_and_resample(b64, 48000)
        self.assertEqual(out.shape, (16000,))
        self.assertEqual(out.dtype, np.float32)

    def test_rejects_empty_pcm(self):
        with self.assertRaises(Exception):
            _decode_and_resample("", 16000)

    def test_rejects_invalid_base64(self):
        with self.assertRaises(Exception):
            _decode_and_resample("!!!not-base64!!!", 16000)


class TranscribeRouteTests(unittest.TestCase):
    def setUp(self):
        self.users = mock.Mock()
        self.users.get_public.return_value = {"enabled": True}
        self.guard = mock.Mock()

        app = FastAPI()
        async def _auth(_request):
            return "alice"
        app.auth_dependency = _auth
        app.cookie_id = "abc"
        app.tokens = {}
        for route in voice_dialogue_routes(self.users, self.guard, "H区"):
            app.routes.append(route)
        self.client = TestClient(app)

    def _body(self):
        samples = (np.ones(16000, dtype=np.float32) * 0.05)
        pcm = (samples * 32767).astype("<i2").tobytes()
        return {"session": "s1", "pcm": base64.b64encode(pcm).decode(), "rate": 16000, "region": ""}

    def test_requires_login(self):
        # 无 auth_dependency 的独立 app：应 401。
        app = FastAPI()
        app.cookie_id = "abc"
        app.tokens = {}
        for route in voice_dialogue_routes(self.users, self.guard, "H区"):
            app.routes.append(route)
        res = TestClient(app).post("/lux-voice-dialogue/transcribe", json=self._body())
        self.assertEqual(res.status_code, 401)

    def test_empty_transcript_not_ok(self):
        with mock.patch("core.voice_dialogue.transcribe_samples", return_value=[]):
            res = self.client.post("/lux-voice-dialogue/transcribe", json=self._body())
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["empty"])
        self.assertEqual(data["text"], "")

    def test_blocked_transcript_not_submittable(self):
        with mock.patch("core.voice_dialogue.transcribe_samples",
                        return_value=[{"text": "泄露密钥", "start": 0.0, "end": 1.0}]):
            with mock.patch("core.voice_dialogue.validate_voice_text",
                            return_value=mock.Mock(text="", ok=False, blocked=True, message="命中安全检查")):
                res = self.client.post("/lux-voice-dialogue/transcribe", json=self._body())
        data = res.json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["blocked"])
        self.assertEqual(data["text"], "")

    def test_valid_transcript_ok(self):
        with mock.patch("core.voice_dialogue.transcribe_samples",
                        return_value=[{"text": "帮我查一下考核文件", "start": 0.0, "end": 1.0}]):
            with mock.patch("core.voice_dialogue.validate_voice_text",
                            return_value=mock.Mock(text="帮我查一下考核文件", ok=True, blocked=False, message="")):
                res = self.client.post("/lux-voice-dialogue/transcribe", json=self._body())
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["text"], "帮我查一下考核文件")
        self.assertFalse(data["empty"])


if __name__ == "__main__":
    unittest.main()
