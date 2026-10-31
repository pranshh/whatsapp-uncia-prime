import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from .chat import ChatService
from .config import Settings
from .db import Database
from .meta import MetaClient, MetaError
from .util import normalize_mobile, normalize_role, parse_webhook, verify_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("whatsapp.api")

PREFIX = "/api/whatsapp"


class StartRequest(BaseModel):
    mobile: str | None = None
    role: str | None = None


def create_app(settings: Settings | None = None, store=None, meta=None) -> FastAPI:
    """Real Postgres and Meta clients unless fakes are passed in (the tests do)."""
    settings = settings or Settings.from_env()
    state = {"owns_store": store is None, "owns_http": meta is None}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = None
        if state["owns_http"]:
            http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
            app.state.meta = MetaClient(settings, http)
        if state["owns_store"]:
            app.state.store = Database(settings.database_url)
            await app.state.store.open()
        app.state.chat = ChatService(app.state.store, app.state.meta, settings)
        app.state.queue = asyncio.Queue()
        worker = asyncio.create_task(_worker(app))
        yield
        worker.cancel()
        if state["owns_store"]:
            await app.state.store.close()
        if http:
            await http.aclose()

    app = FastAPI(title="WhatsApp intake bot", lifespan=lifespan)
    if store is not None:
        app.state.store = store
    if meta is not None:
        app.state.meta = meta

    if settings.allowed_origin:
        app.add_middleware(CORSMiddleware, allow_origins=[settings.allowed_origin], allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        started = time.perf_counter()
        ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "-")).split(",")[0].strip()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            log.info("%s %s -> %s in %d ms from %s", request.method, request.url.path, status, (time.perf_counter() - started) * 1000, ip)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, ex: Exception):
        log.exception("Unhandled error for %s %s", request.method, request.url.path)
        return JSONResponse({"ok": False, "error": "internal error"}, status_code=500)

    @app.get(f"{PREFIX}/health")
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get(f"{PREFIX}/health/ready")
    @app.get("/health/ready")
    async def ready(request: Request):
        checks: dict[str, str] = {}
        try:
            await asyncio.wait_for(request.app.state.store.ping(), timeout=10)
            checks["database"] = "ok"
        except Exception as ex:
            checks["database"] = f"error: {type(ex).__name__}"
        missing = settings.missing()
        checks["config"] = "ok" if not missing else "missing: " + ", ".join(missing)
        meta_error = await request.app.state.meta.check()
        checks["metaApi"] = "ok" if meta_error is None else f"error: {meta_error}"
        healthy = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if healthy else "degraded", "checks": checks}, status_code=200 if healthy else 503)

    @app.post(f"{PREFIX}/start")
    async def start(body: StartRequest, request: Request):
        mobile = normalize_mobile(body.mobile, settings.default_country_code)
        if mobile is None:
            return JSONResponse({"ok": False, "error": "mobile is not a valid phone number."}, status_code=400)
        role = normalize_role(body.role)
        if not role:
            return JSONResponse({"ok": False, "error": "role must be A or CA."}, status_code=400)
        if not settings.start_template_name:
            return JSONResponse({"ok": False, "error": "START_TEMPLATE_NAME is not configured."}, status_code=500)
        await request.app.state.store.start_session(mobile, role)
        try:
            await request.app.state.meta.send_template(mobile, settings.start_template_name, settings.start_template_language)
        except MetaError as ex:
            return JSONResponse({"ok": False, "error": str(ex)}, status_code=502)
        return {"ok": True}

    @app.get(f"{PREFIX}/webhook")
    async def verify(mode: str | None = Query(None, alias="hub.mode"), token: str | None = Query(None, alias="hub.verify_token"), challenge: str | None = Query(None, alias="hub.challenge")):
        valid = mode == "subscribe" and bool(settings.verify_token) and token == settings.verify_token
        log.info("Webhook verification %s", "accepted" if valid else "REJECTED")
        return PlainTextResponse(challenge or "") if valid else PlainTextResponse("Forbidden", status_code=403)

    @app.post(f"{PREFIX}/webhook")
    async def receive(request: Request):
        body = await request.body()
        if not verify_signature(body, request.headers.get("x-hub-signature-256"), settings.app_secret):
            log.warning("Webhook signature check failed (bad or missing X-Hub-Signature-256, or WHATSAPP_APP_SECRET not set)")
            return PlainTextResponse("Unauthorized", status_code=401)
        try:
            messages = parse_webhook(json.loads(body))
        except (ValueError, AttributeError):
            log.warning("Webhook body could not be parsed")
            return {"ok": True}
        for message in messages:
            request.app.state.queue.put_nowait(message)
        return {"ok": True}

    @app.get(f"{PREFIX}/session")
    async def session(request: Request, mobile: str | None = None, role: str | None = None):
        m, r = normalize_mobile(mobile, settings.default_country_code), normalize_role(role)
        found = await request.app.state.store.get_session(m, r) if m and r else None
        if found is None:
            return {"ok": False}
        return {
            "ok": True,
            "status": found.status,
            "name": found.name,
            "email": found.email,
            "loan_amount": float(found.loan_amount) if found.loan_amount is not None else None,
            "tenure_months": found.tenure_months,
            "zip_url": _zip_url(settings, found.mobile, found.role) if found.has_zip else None,
            "file_count": found.file_count,
        }

    @app.get(f"{PREFIX}/zip")
    async def zip_file(request: Request, mobile: str | None = None, role: str | None = None):
        m, r = normalize_mobile(mobile, settings.default_country_code), normalize_role(role)
        zipped = await request.app.state.store.get_zip(m, r) if m and r else None
        if zipped is None:
            return JSONResponse({"ok": False, "error": "No zip for this session yet."}, status_code=404)
        name, content = zipped
        return Response(content, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


def _zip_url(settings: Settings, mobile: str, role: str) -> str:
    from urllib.parse import quote
    return f"{settings.public_base_url}{PREFIX}/zip?mobile={quote(mobile, safe='')}&role={role}"


async def _worker(app: FastAPI) -> None:
    """One message at a time, in arrival order; a failure is logged and never stops the worker."""
    while True:
        message = await app.state.queue.get()
        try:
            await app.state.chat.handle(message)
        except Exception:
            log.exception("Message %s from %s failed", message.message_id, message.sender)
        finally:
            app.state.queue.task_done()


app = create_app()
