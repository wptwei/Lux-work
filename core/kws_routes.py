"""受控提供浏览器端语音唤醒（KWS）静态资源。

这些是公开的 Apache-2.0 模型文件与 WASM 运行时，不含任何用户数据或密钥。
由本应用自身提供（而非第三方 CDN），并使用明确的 MIME 类型，保证
WebAssembly 的 instantiateStreaming 与 ES Module Worker 能在浏览器中正常加载。
"""
from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.routing import APIRoute

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 路由路径 -> (磁盘文件, media_type)。路径刻意保持与磁盘布局一致，
# 使 kws-worker.js 内的 `new URL("./prebuilt/kws.wasm", import.meta.url)` 能正确解析。
ASSETS = {
    "/lux-kws/main.js": (
        _PROJECT_ROOT / "frontend" / "kws" / "main.js",
        "text/javascript",
    ),
    "/lux-kws/kws-worker.js": (
        _PROJECT_ROOT / "frontend" / "kws" / "kws-worker.js",
        "text/javascript",
    ),
    "/lux-kws/prebuilt/kws.wasm": (
        _PROJECT_ROOT / "frontend" / "kws" / "prebuilt" / "kws.wasm",
        "application/wasm",
    ),
    "/lux-kws/model.data": (
        _PROJECT_ROOT / "models" / "kws" / "preload.data",
        "application/octet-stream",
    ),
    "/lux-kws/model.metadata": (
        _PROJECT_ROOT / "models" / "kws" / "preload.js.metadata",
        "application/json",
    ),
}

# 大型模型/WASM 可长缓存；JS 入口短缓存便于发版更新。
_LONG_CACHE = "public, max-age=86400"
_SHORT_CACHE = "public, max-age=300"


def _handler_factory(path: Path, media_type: str, cache: str):
    async def serve_asset():
        if not path.is_file() or path.is_symlink():
            return _missing()
        return FileResponse(
            path,
            media_type=media_type,
            headers={
                "Cache-Control": cache,
                "X-Content-Type-Options": "nosniff",
            },
        )

    return serve_asset


def _missing():
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "语音唤醒资源未就绪，请先运行 install_kws.ps1。"}, status_code=404)


def kws_routes():
    routes = []
    for url, (path, media_type) in ASSETS.items():
        cache = _LONG_CACHE if (url.endswith(".wasm") or url.endswith(".data")) else _SHORT_CACHE
        routes.append(
            APIRoute(
                url,
                _handler_factory(path, media_type, cache),
                methods=["GET"],
                include_in_schema=False,
            )
        )
    return routes
