"""Authenticated downloads; never expose the library through Gradio allowed_paths."""
import inspect

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute

from core.pdf_library import LibraryError


def library_routes(store, users):
    async def download_pdf(version_id: str, request: Request):
        app = request.app
        dependency = getattr(app, 'auth_dependency', None)
        if dependency:
            username = dependency(request)
            if inspect.isawaitable(username):
                username = await username
        else:
            cookie_id = getattr(app, 'cookie_id', '')
            token = request.cookies.get(f'access-token-{cookie_id}') or request.cookies.get(f'access-token-unsecure-{cookie_id}')
            username = getattr(app, 'tokens', {}).get(token)
        user = users.get_public(username) if username else None
        if not user or not user['enabled']:
            raise HTTPException(401, '请先登录 LUX。')
        try:
            path, filename = store.download(version_id, user)
        except (LibraryError, PermissionError):
            raise HTTPException(404, '文件不存在或无权访问。') from None
        return FileResponse(path, filename=filename, media_type='application/pdf',
                            headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'})
    return [APIRoute('/library-files/{version_id}', download_pdf, methods=['GET'], include_in_schema=False)]
