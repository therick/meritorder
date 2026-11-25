"""HTTP guards: request body size limits, and security headers for the console.

Body limits are enforced before a handler reads the body.

LLM requests can be large (long agent histories, base64 images), so the API default is generous:
32 MB, Anthropic's own request limit (``MERITORDER_MAX_BODY_MB``). Endpoints that take small
bodies, including the unauthenticated sign-in exchange, get much smaller limits; batch uploads get the
providers' own (512 MB a file, 256 MB a message batch), and documents for OCR Mistral's (a 50 MB file, sent as base64).
"""

from __future__ import annotations

import json
import os

SMALL = (("/auth/", 64 * 1024), ("/scim/", 1024 * 1024))
# Batches carry many requests in one body, up to the providers' own limits: OpenAI's Files API takes 512 MB a file, and
# Anthropic's Message Batches 256 MB a batch.
# A document for OCR may be 50 MB (Mistral's limit), a third more as a base64 data URI.
LARGE = (("/v1/files", 512 * 1024 * 1024), ("/v1/messages/batches", 256 * 1024 * 1024), ("/v1/ocr", 72 * 1024 * 1024))


def max_body_bytes() -> int:
    """The API's default body limit, which a WebSocket message (the Responses API's) gets too."""
    return int(float(os.environ.get("MERITORDER_MAX_BODY_MB", "32")) * 1024 * 1024)


class _TooLarge(Exception):
    pass


def _limit(path: str, default: int) -> int:
    return next((n for prefix, n in SMALL + LARGE if path.startswith(prefix)), default)


async def _reject(send, limit: int) -> None:
    body = json.dumps({"type": "error", "error": {"type": "request_too_large",
                                                   "message": f"request body is over the {limit:,}-byte limit"}}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    """ASGI middleware: 413 for a declared Content-Length over the limit, or once a streamed
    (chunked) body passes it."""

    def __init__(self, app, max_bytes: int | None = None):
        self.app = app
        self.default = max_bytes if max_bytes is not None else max_body_bytes()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = _limit(scope.get("path", ""), self.default)
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    if int(value) > limit:
                        return await _reject(send, limit)
                except ValueError:
                    return await _reject(send, limit)
        seen = 0
        started = False

        async def counted():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body") or b"")
                if seen > limit:
                    raise _TooLarge
            return msg

        async def tracked(msg):
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, counted, tracked)
        except _TooLarge:
            if not started:
                await _reject(send, limit)


# The console is a static single-page app that only talks to its own origin. Mantine injects <style>
# tags, hence 'unsafe-inline' for styles (scripts stay 'self' only).
CONSOLE_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
               "font-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'")
_CONSOLE_HEADERS = [(b"content-security-policy", CONSOLE_CSP.encode()), (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer")]


class SecurityHeaders:
    """ASGI middleware: nosniff everywhere; on the console (/ui), a strict CSP and no framing, so another
    site can't load it in a frame and trick an admin into clicking "Stop all GPUs"."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        console = scope.get("path", "").startswith("/ui")

        async def with_headers(msg):
            if msg["type"] == "http.response.start":
                headers = list(msg.get("headers") or [])
                headers.append((b"x-content-type-options", b"nosniff"))
                if console:
                    headers.extend(_CONSOLE_HEADERS)
                msg = {**msg, "headers": headers}
            await send(msg)

        await self.app(scope, receive, with_headers)
