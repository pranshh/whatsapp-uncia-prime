import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from .fakes import FakeMeta, FakeStore

ORIGIN = "https://unciaprime-devcore.uncia.ai"
SETTINGS = Settings(
    phone_number_id="123", access_token="tok", verify_token="VT", app_secret="SECRET", start_template_name="hello_world", start_template_language="en_US",
    public_base_url="https://bot.example.com", database_url="postgresql://x", allowed_origin=ORIGIN, max_file_mb=25,
)


@pytest.fixture
def api():
    store, meta = FakeStore(), FakeMeta({"M1": (b"pdf-one", "application/pdf")})
    with TestClient(create_app(SETTINGS, store, meta)) as client:
        yield client, store, meta


def signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    return body, {"X-Hub-Signature-256": "sha256=" + hmac.new(b"SECRET", body, hashlib.sha256).hexdigest(), "Content-Type": "application/json"}


def message(mid, **kw):
    return {"entry": [{"changes": [{"value": {"messages": [{"id": mid, "from": "919163476539", "type": kw.pop("type", "text"), **kw}]}}]}]}


def wait_for(meta, count, seconds=3):
    end = time.time() + seconds
    while len(meta.sent) < count and time.time() < end:
        time.sleep(0.02)
    assert len(meta.sent) >= count, meta.sent


def test_health(api):
    client, _, meta = api
    assert client.get("/api/whatsapp/health").json() == {"status": "ok"}
    assert client.get("/health").status_code == 200
    ready = client.get("/api/whatsapp/health/ready")
    assert ready.status_code == 200 and ready.json() == {"status": "ok", "checks": {"database": "ok", "config": "ok", "metaApi": "ok"}}
    meta.check_result = "Meta answered 401: expired"
    degraded = client.get("/api/whatsapp/health/ready")
    assert degraded.status_code == 503 and degraded.json()["checks"]["metaApi"].startswith("error: Meta answered 401")


def test_ready_reports_missing_config_without_values():
    with TestClient(create_app(Settings(phone_number_id="1"), FakeStore(), FakeMeta())) as client:
        body = client.get("/api/whatsapp/health/ready").json()
    assert body["status"] == "degraded" and "WHATSAPP_ACCESS_TOKEN" in body["checks"]["config"]


def test_webhook_verification(api):
    client, _, _ = api
    ok = client.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "VT", "hub.challenge": "12345"})
    assert ok.status_code == 200 and ok.text == "12345" and ok.headers["content-type"].startswith("text/plain")
    assert client.get("/api/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "1"}).status_code == 403
    assert client.get("/api/whatsapp/webhook").status_code == 403


def test_start_validation_and_template(api):
    client, store, meta = api
    assert client.post("/api/whatsapp/start", json={"mobile": "12"}).status_code == 400
    assert client.post("/api/whatsapp/start", json={"mobile": "9163476539", "role": "X"}).json() == {"ok": False, "error": "role must be A or CA."}
    assert client.post("/api/whatsapp/start", json={"mobile": "9163476539"}).json() == {"ok": True}
    assert meta.sent[-1] == ("+919163476539", "template:hello_world:en_US")
    assert client.post("/api/whatsapp/start", json={"mobile": "+91 91634-76539", "role": "ca"}).json() == {"ok": True}
    assert ("+919163476539", "CA") in store.sessions
    assert store.sessions[("+919163476539", "CA")]["step"] == "CONSENT"
    meta.fail_send = True
    failed = client.post("/api/whatsapp/start", json={"mobile": "9163476539"})
    assert failed.status_code == 502 and failed.json()["ok"] is False


def test_cors_preflight_and_header(api):
    client, _, _ = api
    pre = client.options("/api/whatsapp/start", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    assert pre.status_code == 200 and pre.headers["access-control-allow-origin"] == ORIGIN
    assert client.get("/api/whatsapp/health", headers={"Origin": ORIGIN}).headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-origin" not in client.get("/api/whatsapp/health", headers={"Origin": "https://evil.example"}).headers


def test_webhook_signature_and_full_chat_to_zip(api):
    client, store, meta = api
    body, _ = signed(message("w0", text={"body": "hi"}))
    assert client.post("/api/whatsapp/webhook", content=body, headers={"X-Hub-Signature-256": "sha256=" + "0" * 64}).status_code == 401
    assert client.post("/api/whatsapp/webhook", content=body).status_code == 401
    assert not meta.sent

    steps = [
        ("w1", {"text": {"body": "hi"}}), ("w1b", {"type": "button", "button": {"text": "Yes", "payload": "Yes"}}), ("w2", {"text": {"body": "Ravi Kumar"}}), ("w3", {"text": {"body": "ravi@example.com"}}),
        ("w4", {"text": {"body": "500000"}}), ("w5", {"text": {"body": "36"}}),
        ("w6", {"type": "document", "document": {"id": "M1", "filename": "report.pdf", "mime_type": "application/pdf"}}),
        ("w7", {"text": {"body": "DONE"}}),
    ]
    for i, (mid, kw) in enumerate(steps, 1):
        body, headers = signed(message(mid, **kw))
        assert client.post("/api/whatsapp/webhook", content=body, headers=headers).status_code == 200
        wait_for(meta, i)
    body, headers = signed(message("w6", type="document", document={"id": "M1", "filename": "report.pdf"}))
    client.post("/api/whatsapp/webhook", content=body, headers=headers)
    time.sleep(0.3)
    assert len(meta.sent) == 8

    data = client.get("/api/whatsapp/session", params={"mobile": "+919163476539", "role": "A"}).json()
    assert data == {"ok": True, "status": "DONE", "name": "Ravi Kumar", "email": "ravi@example.com", "loan_amount": 500000.0, "tenure_months": 36,
                    "zip_url": "https://bot.example.com/api/whatsapp/zip?mobile=%2B919163476539&role=A", "file_count": 1}
    spaced = client.get("/api/whatsapp/session?mobile= 919163476539&role=A").json()
    assert spaced["ok"] is True
    zipped = client.get("/api/whatsapp/zip", params={"mobile": "919163476539", "role": "A"})
    assert zipped.status_code == 200 and zipped.headers["content-type"] == "application/zip"
    assert "919163476539_A_documents.zip" in zipped.headers["content-disposition"]
    assert zipped.content[:2] == b"PK"


def test_consent_no_via_interactive_button_then_restart_via_start(api):
    client, store, meta = api
    assert client.post("/api/whatsapp/start", json={"mobile": "9163476539"}).json() == {"ok": True}
    body, headers = signed(message("n1", type="interactive", interactive={"type": "button_reply", "button_reply": {"id": "no-id", "title": "No"}}))
    assert client.post("/api/whatsapp/webhook", content=body, headers=headers).status_code == 200
    wait_for(meta, 2)
    assert meta.last == "No problem, you won't hear from us again about this offer. Thank you!"
    out = client.get("/api/whatsapp/session", params={"mobile": "9163476539"}).json()
    assert out == {"ok": True, "status": "OPTED_OUT", "name": None, "email": None, "loan_amount": None, "tenure_months": None, "zip_url": None, "file_count": 0}
    body, headers = signed(message("n2", text={"body": "hi"}))
    client.post("/api/whatsapp/webhook", content=body, headers=headers)
    wait_for(meta, 3)
    assert meta.last == "You have opted out. Contact us if you change your mind."
    assert client.post("/api/whatsapp/start", json={"mobile": "9163476539"}).json() == {"ok": True}
    assert meta.sent[-1] == ("+919163476539", "template:hello_world:en_US")
    assert client.get("/api/whatsapp/session", params={"mobile": "9163476539"}).json()["status"] == "COLLECTING"


def test_session_and_zip_when_missing(api):
    client, store, _ = api
    assert client.get("/api/whatsapp/session", params={"mobile": "9000000000", "role": "A"}).json() == {"ok": False}
    assert client.get("/api/whatsapp/session", params={"mobile": "bad"}).json() == {"ok": False}
    assert client.get("/api/whatsapp/zip", params={"mobile": "9000000000"}).status_code == 404
    client.post("/api/whatsapp/start", json={"mobile": "9000000000"})
    started = client.get("/api/whatsapp/session", params={"mobile": "9000000000"}).json()
    assert started["status"] == "COLLECTING" and started["zip_url"] is None and started["name"] is None and started["file_count"] == 0
    assert client.get("/api/whatsapp/zip", params={"mobile": "9000000000"}).status_code == 404
