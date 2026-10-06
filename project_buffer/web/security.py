"""Security response headers."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; manifest-src 'self'; form-action 'self'; base-uri 'none'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_static = scope["path"].startswith("/static/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {name.lower() for name, _ in headers}

                def add(name: bytes, value: str) -> None:
                    if name not in present:
                        headers.append((name, value.encode()))

                add(b"content-security-policy", CSP)
                add(b"x-content-type-options", "nosniff")
                add(b"x-frame-options", "DENY")
                add(b"referrer-policy", "no-referrer")
                add(b"cross-origin-opener-policy", "same-origin")
                add(b"cross-origin-resource-policy", "same-origin")
                add(b"permissions-policy", "camera=(), geolocation=(), microphone=(), payment=()")
                add(b"x-robots-tag", "noindex, nofollow")
                if not is_static:
                    # Private content must never be stored by browsers or proxies.
                    add(b"cache-control", "no-store")
                if self.hsts:
                    add(b"strict-transport-security", "max-age=31536000; includeSubDomains")
            await send(message)

        await self.app(scope, receive, send_with_headers)
