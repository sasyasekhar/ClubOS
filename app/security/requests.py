import re

from starlette.datastructures import Headers, MutableHeaders, URL
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config.settings import settings
from app.services.attachment_policy import MAX_FILE_SIZE


MAX_REQUEST_BODY = 128 * 1024
MAX_ATTACHMENT_REQUEST_SIZE = MAX_FILE_SIZE + 64 * 1024
ATTACHMENT_UPLOAD = re.compile(r"/clubs/[^/]+/activities/[^/]+/revisions/[^/]+/attachments/?")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class RequestSecurityMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "no-referrer")
            await send(message)

        if scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, secure_send)
            return

        headers = Headers(scope=scope)
        origins = headers.getlist("origin")
        url = URL(scope=scope)
        expected = settings.PUBLIC_ORIGIN or f"{url.scheme}://{url.netloc}"
        if headers.get("sec-fetch-site") in {"cross-site", "same-site"} or (origins and origins != [expected]):
            await JSONResponse({"detail": "Cross-origin mutation denied."}, 403)(scope, receive, secure_send)
            return

        is_upload = scope["method"] == "POST" and ATTACHMENT_UPLOAD.fullmatch(scope["path"])
        limit = MAX_ATTACHMENT_REQUEST_SIZE if is_upload else MAX_REQUEST_BODY
        declared = headers.getlist("content-length")
        if declared and (len(declared) != 1 or not re.fullmatch(r"[0-9]{1,12}", declared[0])):
            await JSONResponse({"detail": "Invalid request length."}, 400)(scope, receive, secure_send)
            return
        if declared and int(declared[0]) > limit:
            await JSONResponse({"detail": "Request body is too large."}, 413)(scope, receive, secure_send)
            return
        if is_upload:
            # The attachment parser enforces this same limit while streaming.
            await self.app(scope, receive, secure_send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > limit:
                await JSONResponse({"detail": "Request body is too large."}, 413)(scope, receive, secure_send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, bounded_receive, secure_send)
