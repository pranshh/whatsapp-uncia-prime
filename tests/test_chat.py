import io
import zipfile

import pytest

from app.chat import ChatService
from app.config import Settings
from app.util import Inbound

from .fakes import FakeMeta, FakeStore

F = "919163476539"
MOBILE = "+919163476539"


@pytest.fixture
def world():
    store = FakeStore()
    meta = FakeMeta({"M1": (b"pdf-one", "application/pdf"), "M2": (b"jpeg", "image/jpeg"), "M3": (b"pdf-two", "application/pdf"), "BIG": (b"x" * (2 * 1024 * 1024), "application/pdf")})
    settings = Settings(max_file_mb=1, default_country_code="91")
    return store, meta, ChatService(store, meta, settings)


_n = 0


async def say(chat, text=None, **kw):
    global _n
    _n += 1
    await chat.handle(Inbound(f"id{_n}", kw.pop("sender", F), kw.pop("type", "text"), text, **kw))


async def to_docs(chat):
    for t in ["hi", "yes", "Ravi Kumar", "ravi@example.com", "5,00,000", "36"]:
        await say(chat, t)


async def test_full_flow_with_revalidation(world):
    store, meta, chat = world
    await say(chat, "HI"); assert "YES to continue" in meta.last and "NO to opt out" in meta.last
    await say(chat, "maybe"); assert meta.last == "Please reply YES to continue or NO to opt out."
    await say(chat, "Yes"); assert meta.last == "Great! What is your full name?"
    await say(chat, "A"); assert meta.last == "Please enter your full name."
    await say(chat, "Ravi Kumar"); assert meta.last == "Your email ID?"
    await say(chat, "nope"); assert "valid email" in meta.last
    await say(chat, "ravi@example.com"); assert meta.last == "Loan amount you need?"
    await say(chat, "abc"); assert "number" in meta.last
    await say(chat, "0"); assert "number" in meta.last
    await say(chat, "5,00,000"); assert meta.last == "Loan tenure in months?"
    await say(chat, "601"); assert "tenure" in meta.last
    await say(chat, "36"); assert meta.last == "Please upload all your documents one by one. When you are done, type DONE."
    await say(chat, "done"); assert "haven't received" in meta.last

    await say(chat, type="document", media_id="M1", filename="report.pdf", mime_type="application/pdf"); assert meta.last == "Received report.pdf"
    await say(chat, type="document", media_id="M3", filename="report.pdf"); assert meta.last == "Received report (2).pdf"
    await say(chat, type="image", media_id="M2", mime_type="image/jpeg"); assert meta.last == "Received image_M2.jpg" or meta.last.startswith("Received image_")
    await say(chat, "what now"); assert "type DONE" in meta.last
    await say(chat, "DONE"); assert meta.last == "Thank you! Your documents are submitted. We'll get back to you."

    session = await store.get_session(MOBILE, "A")
    assert (session.status, session.name, session.email, int(session.loan_amount), session.tenure_months, session.file_count, session.has_zip) == ("DONE", "Ravi Kumar", "ravi@example.com", 500000, 36, 3, True)
    name, content = await store.get_zip(MOBILE, "A")
    assert name == "919163476539_A_documents.zip"
    assert sorted(zipfile.ZipFile(io.BytesIO(content)).namelist()) == sorted(["report.pdf", "report (2).pdf", "image_M2.jpg"]) or len(zipfile.ZipFile(io.BytesIO(content)).namelist()) == 3

    await say(chat, "hi"); assert "already submitted" in meta.last
    await say(chat, "restart"); assert "cleared" in meta.last and "YES to continue" in meta.last
    after = await store.get_session(MOBILE, "A")
    assert (after.status, after.name, after.file_count, after.has_zip) == ("COLLECTING", None, 0, False)


async def test_duplicate_message_ignored(world):
    store, meta, chat = world
    msg = Inbound("dup", F, "text", "hi")
    await chat.handle(msg)
    await chat.handle(msg)
    assert len(meta.sent) == 1


async def test_no_session_non_hi_and_restart(world):
    store, meta, chat = world
    await say(chat, "hello there"); assert "Reply HI" in meta.last
    await say(chat, "restart"); assert "Reply HI" in meta.last
    assert not store.sessions


async def test_started_session_waits_in_consent_for_any_message(world):
    store, meta, chat = world
    await store.start_session(MOBILE, "CA")
    assert (await store.get_session(MOBILE, "CA")).step == "CONSENT"
    await say(chat, "what"); assert meta.last == "Please reply YES to continue or NO to opt out."
    await say(chat, "hi"); assert meta.last == "Please reply YES to continue or NO to opt out."
    await say(chat, type="image", media_id="M2", mime_type="image/jpeg"); assert meta.last == "Please reply YES to continue or NO to opt out."
    assert (await store.get_session(MOBILE, "CA")).step == "CONSENT"
    await say(chat, "YES"); assert meta.last == "Great! What is your full name?"
    assert (await store.get_session(MOBILE, "CA")).step == "NAME"


@pytest.mark.parametrize("kw", [
    {"text": "yes"}, {"text": "YES"}, {"text": " Yes! "},
    {"type": "button", "text": "Yes", "payload": "Yes"},
    {"type": "button", "text": "ok", "payload": "YES"},
    {"type": "interactive", "text": "Yes", "payload": "yes-id"},
])
async def test_consent_yes_in_every_shape(world, kw):
    store, meta, chat = world
    await store.start_session(MOBILE, "A")
    await say(chat, kw.pop("text"), **kw)
    assert meta.last == "Great! What is your full name?"
    assert (await store.get_session(MOBILE, "A")).step == "NAME"


@pytest.mark.parametrize("kw", [{"text": "no"}, {"text": "NO"}, {"text": "No."}, {"type": "button", "text": "No", "payload": "No"}, {"type": "interactive", "text": "No", "payload": "NO"}])
async def test_consent_no_opts_out_and_stays_quiet(world, kw):
    store, meta, chat = world
    await store.start_session(MOBILE, "A")
    await say(chat, kw.pop("text"), **kw)
    assert meta.last == "No problem, you won't hear from us again about this offer. Thank you!"
    s = await store.get_session(MOBILE, "A")
    assert (s.status, s.name, s.email, s.file_count, s.has_zip) == ("OPTED_OUT", None, None, 0, False)
    sent_before = len(meta.sent)
    for word in ["hi", "yes", "restart", "hello"]:
        await say(chat, word)
        assert meta.last == "You have opted out. Contact us if you change your mind."
    assert len(meta.sent) == sent_before + 4 and (await store.get_session(MOBILE, "A")).status == "OPTED_OUT"
    await store.start_session(MOBILE, "A")
    assert (await store.get_session(MOBILE, "A")).status == "COLLECTING"
    await say(chat, "yes"); assert meta.last == "Great! What is your full name?"


async def test_media_before_docs_step_reasks_question(world):
    store, meta, chat = world
    await say(chat, "hi")
    await say(chat, "yes")
    await say(chat, type="document", media_id="M1", filename="a.pdf"); assert meta.last == "Please enter your full name."


async def test_oversize_and_failed_download(world):
    store, meta, chat = world
    await to_docs(chat)
    await say(chat, type="document", media_id="BIG", filename="big.pdf"); assert "larger than 1 MB" in meta.last
    await say(chat, type="document", media_id="MISSING", filename="gone.pdf"); assert "couldn't download gone.pdf" in meta.last
    assert (await store.get_session(MOBILE, "A")).file_count == 0


async def test_active_session_preferred_over_done(world):
    store, meta, chat = world
    await to_docs(chat)
    await say(chat, type="document", media_id="M1", filename="a.pdf")
    await say(chat, "done")
    await store.start_session(MOBILE, "CA")
    await say(chat, "yes")
    assert "full name" in meta.last
    assert (await store.get_session(MOBILE, "CA")).step == "NAME"
