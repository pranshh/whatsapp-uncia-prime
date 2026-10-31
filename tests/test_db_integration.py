import io
import os
import zipfile

import pytest

from app.chat import ChatService
from app.config import Settings
from app.db import Database
from app.util import Inbound

from .fakes import FakeMeta

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="set TEST_DATABASE_URL to run against a real Postgres")

SENDER, MOBILE = "910000000001", "+910000000001"


async def test_full_flow_on_real_postgres():
    db = Database(os.environ["TEST_DATABASE_URL"])
    await db.open()
    meta = FakeMeta({"M1": (b"pdf-one", "application/pdf"), "M2": (b"jpeg-bytes", "image/jpeg"), "M3": (b"pdf-two", "application/pdf")})
    chat = ChatService(db, meta, Settings(default_country_code="91", max_file_mb=25))
    n = 0

    async def say(text=None, **kw):
        nonlocal n
        n += 1
        await chat.handle(Inbound(f"itest-{os.getpid()}-{n}", SENDER, kw.pop("type", "text"), text, **kw))

    try:
        await db.start_session(MOBILE, "A")
        for t in ["hi", "Test User", "test@example.com", "5,00,000", "36"]:
            await say(t)
        await say(type="document", media_id="M1", filename="report.pdf", mime_type="application/pdf")
        await say(type="document", media_id="M3", filename="report.pdf")
        await say(type="image", media_id="M2", mime_type="image/jpeg")
        assert [m for _, m in meta.sent if m.startswith("Received")][:2] == ["Received report.pdf", "Received report (2).pdf"]
        await say("DONE")
        assert meta.sent[-1][1] == "Thank you! Your documents are submitted. We'll get back to you."

        s = await db.get_session(MOBILE, "A")
        assert (s.status, s.name, s.email, int(s.loan_amount), s.tenure_months, s.file_count, s.has_zip) == ("DONE", "Test User", "test@example.com", 500000, 36, 3, True)
        name, content = await db.get_zip(MOBILE, "A")
        assert name == "910000000001_A_documents.zip"
        archive = zipfile.ZipFile(io.BytesIO(content))
        assert sorted(archive.namelist()) == ["image_M2.jpg", "report (2).pdf", "report.pdf"] and archive.read("report.pdf") == b"pdf-one" and archive.read("report (2).pdf") == b"pdf-two"

        before = len(meta.sent)
        same = Inbound("itest-dup", SENDER, "text", "hi")
        await chat.handle(same)
        await chat.handle(same)
        assert len(meta.sent) == before + 1

        await say("restart")
        s = await db.get_session(MOBILE, "A")
        assert (s.status, s.name, s.file_count, s.has_zip) == ("COLLECTING", None, 0, False)
        assert await db.get_zip(MOBILE, "A") is None
        await db.ping()
    finally:
        async with db._pool.connection() as conn:
            await conn.execute("DELETE FROM sessions WHERE mobile = %s", (MOBILE,))
            await conn.execute("DELETE FROM processed_messages WHERE message_id LIKE 'itest-%'")
        await db.close()
