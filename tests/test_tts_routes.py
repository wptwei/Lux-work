"""朗读路由的会话隔离：同一账号在手机和电脑上，各读各的回答。

合成的真实模型不在这里加载；用一个假的合成函数验证路由是否按
`lux-voice-sid` cookie 取“本设备”的答案，并把该会话登记到音频上。
"""
from __future__ import annotations

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import tts as tts_module
from core.tts_routes import tts_routes


def _make_app(users, auth="alice"):
    app = FastAPI()

    async def _auth(_request):
        return auth

    app.auth_dependency = _auth
    app.cookie_id = "cookie"
    app.tokens = {}
    for route in tts_routes(users):
        app.routes.append(route)
    return TestClient(app)


class SynthesizeSessionTests(unittest.TestCase):
    def setUp(self):
        self.users = mock.Mock()
        self.users.get_public.return_value = {"enabled": True, "username": "alice"}
        self.client = _make_app(self.users)

    def test_cookie_session_selects_own_answer(self):
        seen = {}

        def fake_last(username, session=""):
            seen["session"] = session
            return "这是手机提问的回答"

        def fake_synth(username, text, session=""):
            seen["synth_session"] = session
            return {"audio_id": "a" * 32, "duration_ms": 1200, "sample_rate": 24000}

        with mock.patch("core.tts_routes.settings", mock.Mock(tts_enabled=True)), \
             mock.patch.object(tts_module, "last_answer", side_effect=fake_last), \
             mock.patch.object(tts_module, "synthesize_for_user", side_effect=fake_synth):
            res = self.client.post(
                "/lux-tts/synthesize", json={"session": ""},
                cookies={"lux-voice-sid": "phone"},
            )

        self.assertEqual(res.status_code, 200)
        self.assertEqual(seen["session"], "phone")
        self.assertEqual(seen["synth_session"], "phone")

    def test_no_answer_for_this_device_is_not_read(self):
        # 另一台设备没有登记答案时返回 404，不会读到别人的回答。
        with mock.patch("core.tts_routes.settings", mock.Mock(tts_enabled=True)), \
             mock.patch.object(tts_module, "last_answer", return_value=""):
            res = self.client.post(
                "/lux-tts/synthesize", json={"session": ""},
                cookies={"lux-voice-sid": "desktop"},
            )
        self.assertEqual(res.status_code, 404)
        self.assertIn("还没有可朗读的回答", res.json()["detail"])

    def test_audio_is_not_served_to_another_session(self):
        with mock.patch.object(
            tts_module.registry, "resolve", return_value=None
        ) as resolve:
            res = self.client.get("/lux-tts/audio/" + "b" * 32, cookies={"lux-voice-sid": "desktop"})
        self.assertEqual(res.status_code, 404)
        # 路由把本设备的会话传给归属校验，而不是只按用户名。
        self.assertEqual(resolve.call_args.kwargs.get("session"), "desktop")


if __name__ == "__main__":
    unittest.main()
