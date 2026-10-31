import asyncio
import logging
import tempfile
import zipfile
from dataclasses import dataclass
from decimal import Decimal

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .util import unique_name

log = logging.getLogger("whatsapp.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id BIGSERIAL PRIMARY KEY,
  mobile TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL,
  step TEXT NOT NULL,
  name TEXT,
  email TEXT,
  loan_amount NUMERIC(18,2),
  tenure_months INTEGER,
  zip_file_name TEXT,
  zip_content BYTEA,
  opt_in_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  modified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (mobile, role)
);
CREATE TABLE IF NOT EXISTS files (
  id BIGSERIAL PRIMARY KEY,
  session_id BIGINT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  content_type TEXT,
  size_bytes BIGINT NOT NULL,
  content BYTEA NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_files_session ON files (session_id);
CREATE TABLE IF NOT EXISTS processed_messages (
  message_id TEXT PRIMARY KEY,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

SESSION_SELECT = """SELECT s.id, s.mobile, s.role, s.status, s.step, s.name, s.email, s.loan_amount, s.tenure_months,
  (SELECT COUNT(*) FROM files f WHERE f.session_id = s.id) AS file_count, (s.zip_content IS NOT NULL) AS has_zip FROM sessions s"""


@dataclass
class Session:
    id: int
    mobile: str
    role: str
    status: str
    step: str
    name: str | None
    email: str | None
    loan_amount: Decimal | None
    tenure_months: int | None
    file_count: int
    has_zip: bool


def _session(row: dict | None) -> Session | None:
    return Session(**row) if row else None


class Database:
    """Postgres store (Neon): sessions, uploaded files and processed message ids. Tables are created on first use."""

    def __init__(self, database_url: str):
        self._pool = AsyncConnectionPool(
            database_url,
            min_size=1,
            max_size=4,
            open=False,
            timeout=20,
            max_idle=120,
            check=AsyncConnectionPool.check_connection,
            kwargs={"row_factory": dict_row, "connect_timeout": 10, "prepare_threshold": None},
        )
        self._schema_ready = False
        self._schema_lock = asyncio.Lock()

    async def open(self) -> None:
        await self._pool.open(wait=False)
        try:
            await self.ensure_schema()
        except Exception as ex:  # Neon may still be waking up; the first real request retries
            log.warning("Schema check at startup failed (%s); will retry on first use", type(ex).__name__)

    async def close(self) -> None:
        await self._pool.close()

    async def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            async with self._pool.connection() as conn:
                await conn.execute(SCHEMA)
            self._schema_ready = True
            log.info("Database schema ready")

    async def ping(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("SELECT 1")

    async def try_mark_message(self, message_id: str) -> bool:
        await self.ensure_schema()
        async with self._pool.connection() as conn:
            cur = await conn.execute("INSERT INTO processed_messages (message_id) VALUES (%s) ON CONFLICT DO NOTHING", (message_id,))
            return cur.rowcount == 1

    async def start_session(self, mobile: str, role: str) -> Session:
        await self.ensure_schema()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """INSERT INTO sessions (mobile, role, status, step, opt_in_at) VALUES (%s, %s, 'COLLECTING', 'AWAIT_HI', now())
                       ON CONFLICT (mobile, role) DO UPDATE SET status = 'COLLECTING', step = 'AWAIT_HI', name = NULL, email = NULL, loan_amount = NULL,
                         tenure_months = NULL, zip_file_name = NULL, zip_content = NULL, completed_at = NULL, opt_in_at = now(), modified_at = now()
                       RETURNING id""",
                    (mobile, role),
                )
                session_id = (await cur.fetchone())["id"]
                await conn.execute("DELETE FROM files WHERE session_id = %s", (session_id,))
            cur = await conn.execute(f"{SESSION_SELECT} WHERE s.id = %s", (session_id,))
            return _session(await cur.fetchone())

    async def find_for_sender(self, mobile: str) -> Session | None:
        await self.ensure_schema()
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"{SESSION_SELECT} WHERE s.mobile = %s ORDER BY (s.status = 'COLLECTING') DESC, s.modified_at DESC LIMIT 1", (mobile,))
            return _session(await cur.fetchone())

    async def get_session(self, mobile: str, role: str) -> Session | None:
        await self.ensure_schema()
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"{SESSION_SELECT} WHERE s.mobile = %s AND s.role = %s", (mobile, role))
            return _session(await cur.fetchone())

    async def update(self, session_id: int, step: str, name: str | None = None, email: str | None = None, loan_amount: Decimal | None = None, tenure_months: int | None = None) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                """UPDATE sessions SET step = %s, name = COALESCE(%s, name), email = COALESCE(%s, email), loan_amount = COALESCE(%s, loan_amount),
                   tenure_months = COALESCE(%s, tenure_months), modified_at = now() WHERE id = %s""",
                (step, name, email, loan_amount, tenure_months, session_id),
            )

    async def add_file(self, session_id: int, filename: str, content_type: str | None, content: bytes) -> str:
        """Stores the file under its name; a repeated name becomes 'name (2).ext'. Returns the stored name."""
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT filename FROM files WHERE session_id = %s", (session_id,))
            stored = unique_name(filename, {r["filename"] for r in await cur.fetchall()})
            await conn.execute(
                "INSERT INTO files (session_id, filename, content_type, size_bytes, content) VALUES (%s, %s, %s, %s, %s)",
                (session_id, stored, content_type, len(content), content),
            )
            await conn.execute("UPDATE sessions SET modified_at = now() WHERE id = %s", (session_id,))
            return stored

    async def file_count(self, session_id: int) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM files WHERE session_id = %s", (session_id,))
            return (await cur.fetchone())["n"]

    async def build_zip(self, session_id: int) -> bytes:
        """Zips the session's files one at a time through a temp file so only one file is in memory at once (plus the finished zip)."""
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT id, filename FROM files WHERE session_id = %s ORDER BY id", (session_id,))
            entries = await cur.fetchall()
            with tempfile.TemporaryFile() as tmp:
                with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
                    for entry in entries:
                        cur = await conn.execute("SELECT content FROM files WHERE id = %s", (entry["id"],))
                        archive.writestr(entry["filename"], bytes((await cur.fetchone())["content"]))
                tmp.seek(0)
                return tmp.read()

    async def complete(self, session_id: int, zip_file_name: str, zip_content: bytes) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE sessions SET status = 'DONE', step = 'DONE', zip_file_name = %s, zip_content = %s, completed_at = now(), modified_at = now() WHERE id = %s",
                (zip_file_name, zip_content, session_id),
            )

    async def get_zip(self, mobile: str, role: str) -> tuple[str, bytes] | None:
        await self.ensure_schema()
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT zip_file_name, zip_content FROM sessions WHERE mobile = %s AND role = %s AND zip_content IS NOT NULL", (mobile, role))
            row = await cur.fetchone()
            return (row["zip_file_name"], bytes(row["zip_content"])) if row else None
