"""TTS 纯逻辑单测：文本清理、音频归属隔离与 TTL 清理。

真正的合成需要下载模型，这里用干净的纯函数与 AudioRegistry 覆盖
“不朗读排版符号 / 越权取不到别人音频 / 过期被回收”三条关键边界。
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from core.tts import (
    AudioRegistry,
    clean_spoken_text,
    last_answer,
    record_last_answer,
)


class CleanSpokenTextTests(unittest.TestCase):
    def test_strips_markdown_code_links_and_urls(self):
        raw = "**加粗** `code` [链接](https://a.com/x) 见 https://example.com/path"
        got = clean_spoken_text(raw)
        self.assertIn("加粗", got)
        self.assertIn("code", got)
        self.assertIn("链接", got)
        self.assertNotIn("http", got)
        self.assertNotIn("**", got)
        self.assertNotIn("`", got)

    def test_strips_fenced_code_block(self):
        got = clean_spoken_text("```python\nprint(1)\n```\n结论如下。")
        self.assertIn("结论如下。", got)
        self.assertNotIn("print", got)

    def test_strips_lux_notice_prefixes(self):
        got = clean_spoken_text("🔒 _已脱敏 3 处_\n\n---\n\n答案正文。")
        self.assertIn("答案正文。", got)
        self.assertNotIn("脱敏", got)

    def test_empty_and_whitespace(self):
        self.assertEqual(clean_spoken_text(""), "")
        self.assertEqual(clean_spoken_text("   \n\t "), "")


class AudioRegistryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.registry = AudioRegistry(self.root, ttl=3600)

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, name: str) -> Path:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"RIFF")
        return p

    def test_own_audio_resolves_other_user_cannot(self):
        path = self._write("a.wav")
        audio_id = self.registry.register("alice", "s1", path, 100)
        self.assertEqual(self.registry.resolve("alice", audio_id), path)
        self.assertIsNone(self.registry.resolve("bob", audio_id))

    def test_unknown_id_returns_none(self):
        self.assertIsNone(self.registry.resolve("alice", "nope"))

    def test_expired_audio_is_recycled(self):
        reg = AudioRegistry(self.root, ttl=3600)
        path = self._write("b.wav")
        audio_id = reg.register("alice", "s1", path, 100)
        # 把创建时间改到 2 小时前，触发过期回收。
        with reg._lock:
            reg._items[audio_id]["created"] = time.time() - 7200
        self.assertIsNone(reg.resolve("alice", audio_id))
        reg._sweep_once()
        self.assertFalse(path.exists())

    def test_audio_is_isolated_between_browser_sessions(self):
        # 同一账号的手机与电脑：另一台设备取不到本设备合成的音频。
        path = self._write("c.wav")
        audio_id = self.registry.register("alice", "phone", path, 100)
        self.assertEqual(self.registry.resolve("alice", audio_id, session="phone"), path)
        self.assertIsNone(self.registry.resolve("alice", audio_id, session="desktop"))
        # 未提供会话时保持原有用户级行为（兼容旧调用）。
        self.assertEqual(self.registry.resolve("alice", audio_id), path)


class LastAnswerSessionTests(unittest.TestCase):
    def tearDown(self):
        record_last_answer("alice", "", "")

    def test_answer_is_scoped_to_browser_session(self):
        record_last_answer("alice", "手机上的回答", "phone")
        self.assertEqual(last_answer("alice", "phone"), "手机上的回答")
        # 电脑端读不到手机的回答，因此不会串台朗读。
        self.assertEqual(last_answer("alice", "desktop"), "")
        record_last_answer("alice", "电脑上的回答", "desktop")
        self.assertEqual(last_answer("alice", "phone"), "手机上的回答")
        self.assertEqual(last_answer("alice", "desktop"), "电脑上的回答")

    def test_clearing_one_session_keeps_the_other(self):
        record_last_answer("alice", "手机上的回答", "phone")
        record_last_answer("alice", "电脑上的回答", "desktop")
        record_last_answer("alice", "", "desktop")   # 电脑开始新一轮，只清自己的
        self.assertEqual(last_answer("alice", "phone"), "手机上的回答")
        self.assertEqual(last_answer("alice", "desktop"), "")


if __name__ == "__main__":
    unittest.main()
