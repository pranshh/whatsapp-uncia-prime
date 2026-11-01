import io
import zipfile
from decimal import Decimal

from app.db import Session
from app.meta import MetaError
from app.util import unique_name


class FakeStore:
    """In-memory stand-in for app.db.Database with the same behaviour."""

    def __init__(self):
        self.sessions: dict[tuple[str, str], dict] = {}
        self.files: dict[int, list[tuple[str, str | None, bytes]]] = {}
        self.marked: set[str] = set()
        self._next = 1

    def _view(self, s: dict) -> Session:
        return Session(s["id"], s["mobile"], s["role"], s["status"], s["step"], s["name"], s["email"], s["loan_amount"], s["tenure_months"], len(self.files[s["id"]]), s["zip"] is not None)

    async def ping(self): ...

    async def try_mark_message(self, message_id):
        if message_id in self.marked:
            return False
        self.marked.add(message_id)
        return True

    async def start_session(self, mobile, role):
        s = self.sessions.get((mobile, role))
        if s is None:
            s = {"id": self._next, "mobile": mobile, "role": role}
            self._next += 1
            self.sessions[(mobile, role)] = s
        s.update(status="COLLECTING", step="CONSENT", name=None, email=None, loan_amount=None, tenure_months=None, zip=None, zip_name=None, seq=self._next)
        self._next += 1
        self.files[s["id"]] = []
        return self._view(s)

    async def opt_out(self, session_id):
        s = self._by_id(session_id)
        s.update(status="OPTED_OUT", step="OPTED_OUT", name=None, email=None, loan_amount=None, tenure_months=None, zip=None, zip_name=None)
        self.files[session_id] = []

    async def find_for_sender(self, mobile):
        mine = [s for s in self.sessions.values() if s["mobile"] == mobile]
        if not mine:
            return None
        return self._view(max(mine, key=lambda s: (s["status"] == "COLLECTING", s["seq"])))

    async def get_session(self, mobile, role):
        s = self.sessions.get((mobile, role))
        return self._view(s) if s else None

    def _by_id(self, session_id):
        return next(s for s in self.sessions.values() if s["id"] == session_id)

    async def update(self, session_id, step, name=None, email=None, loan_amount=None, tenure_months=None):
        s = self._by_id(session_id)
        s["step"] = step
        for key, value in (("name", name), ("email", email), ("loan_amount", loan_amount), ("tenure_months", tenure_months)):
            if value is not None:
                s[key] = value

    async def add_file(self, session_id, filename, content_type, content):
        stored = unique_name(filename, {f[0] for f in self.files[session_id]})
        self.files[session_id].append((stored, content_type, content))
        return stored

    async def file_count(self, session_id):
        return len(self.files[session_id])

    async def build_zip(self, session_id):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, _, content in self.files[session_id]:
                archive.writestr(name, content)
        return buffer.getvalue()

    async def complete(self, session_id, zip_file_name, zip_content):
        s = self._by_id(session_id)
        s.update(status="DONE", step="DONE", zip=zip_content, zip_name=zip_file_name)

    async def get_zip(self, mobile, role):
        s = self.sessions.get((mobile, role))
        return (s["zip_name"], s["zip"]) if s and s["zip"] is not None else None


class FakeMeta:
    def __init__(self, media: dict[str, tuple[bytes, str]] | None = None):
        self.sent: list[tuple[str, str]] = []
        self.media = media or {}
        self.fail_send = False
        self.check_result: str | None = None

    async def send_template(self, to, name, language):
        if self.fail_send:
            raise MetaError("WhatsApp send failed (401): bad token")
        self.sent.append((to, f"template:{name}:{language}"))

    async def send_text(self, to, body):
        self.sent.append((to, body))

    async def download_media(self, media_id):
        if media_id not in self.media:
            raise MetaError("Media lookup failed (404)")
        return self.media[media_id]

    async def check(self):
        return self.check_result

    @property
    def last(self) -> str:
        return self.sent[-1][1]
