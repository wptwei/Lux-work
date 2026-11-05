"""KWS 静态资源路由：MIME 类型正确，浏览器才能加载 WASM 与 ES Module Worker。"""
from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.kws_routes import ASSETS, kws_routes


class KwsRoutesTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(FastAPI(routes=kws_routes()))

    def test_all_assets_served_with_expected_mime(self):
        expected = {
            "/lux-kws/main.js": "text/javascript",
            "/lux-kws/kws-worker.js": "text/javascript",
            "/lux-kws/prebuilt/kws.wasm": "application/wasm",
            "/lux-kws/model.data": "application/octet-stream",
            "/lux-kws/model.metadata": "application/json",
        }
        self.assertEqual(set(ASSETS), set(expected))
        for url, media_type in expected.items():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertEqual(response.headers["content-type"].split(";")[0], media_type, url)
            self.assertEqual(response.headers["x-content-type-options"], "nosniff", url)

    def test_wasm_magic_bytes_preserved(self):
        # instantiateStreaming 依赖真实 WASM 魔数，且文件不能被当成文本破坏。
        response = self.client.get("/lux-kws/prebuilt/kws.wasm")
        self.assertEqual(response.content[:4], b"\x00asm")

    def test_cache_headers_differ_by_asset_kind(self):
        self.assertIn("max-age=86400", self.client.get("/lux-kws/model.data").headers["cache-control"])
        self.assertIn("max-age=300", self.client.get("/lux-kws/main.js").headers["cache-control"])


if __name__ == "__main__":
    unittest.main()
