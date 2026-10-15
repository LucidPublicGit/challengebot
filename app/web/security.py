"""Authenticate and bound admin bodies before Starlette's form parser runs."""
from tempfile import SpooledTemporaryFile

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse


# File allowance plus multipart fields/headers. Other forms get a smaller budget.
FORM_LIMIT = 256 * 1024
BODY_LIMITS = {
    "/achievements": 4 * 1024 * 1024 + FORM_LIMIT,
    "/achievements/catalog/import": 512 * 1024 + FORM_LIMIT,
    "/announcements": 10 * 1024 * 1024 + FORM_LIMIT,
}


class AdminRequestGuard:
    def __init__(self, app, authenticate, security):
        self.app = app
        self.authenticate = authenticate
        self.security = security

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in {
            "/health", "/docs", "/redoc", "/openapi.json"
        }:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            # Disabled-admin status takes precedence even over malformed Basic auth.
            try:
                self.authenticate(None)
            except HTTPException as exc:
                if exc.status_code != 401:
                    raise
            self.authenticate(await self.security(request))
        except HTTPException as exc:
            await PlainTextResponse(str(exc.detail), status_code=exc.status_code,
                                    headers=exc.headers)(scope, receive, send)
            return
        path = scope["path"].rstrip("/")
        limit = BODY_LIMITS.get(path, FORM_LIMIT)
        if path.startswith("/achievements/") and len(path.split("/")) == 3:
            limit = BODY_LIMITS["/achievements"]
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                length = int(declared)
                if length < 0:
                    raise ValueError
            except ValueError:
                await PlainTextResponse("Invalid Content-Length", 400)(scope, receive, send)
                return
            if length > limit:
                await PlainTextResponse("Request body too large", 413)(scope, receive, send)
                return
        # Buffer only within the bounded budget, before dispatch. This also avoids
        # multipart parser error handling turning a streaming-limit failure into 400.
        with SpooledTemporaryFile(max_size=FORM_LIMIT) as body:
            total = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                total += len(chunk)
                if total > limit:
                    await PlainTextResponse("Request body too large", 413)(scope, receive, send)
                    return
                body.write(chunk)
                if not message.get("more_body", False):
                    break
            body.seek(0)
            remaining = total
            replayed = False

            async def bounded_receive():
                nonlocal remaining, replayed
                if replayed:
                    return await receive()
                chunk = body.read(min(64 * 1024, remaining))
                remaining -= len(chunk)
                replayed = remaining == 0
                return {"type": "http.request", "body": chunk, "more_body": remaining > 0}

            await self.app(scope, bounded_receive, send)
