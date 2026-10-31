import asyncio
import logging

from .config import Settings
from .meta import MetaClient, MetaError
from .util import Inbound, default_filename, is_email, normalize_mobile, parse_amount, parse_tenure, safe_filename

log = logging.getLogger("whatsapp.chat")

AWAIT_HI, NAME, EMAIL, AMOUNT, TENURE, DOCS, DONE = "AWAIT_HI", "NAME", "EMAIL", "AMOUNT", "TENURE", "DOCS", "DONE"


def _is(text: str | None, word: str) -> bool:
    return text is not None and text.strip().casefold() == word.casefold()


class ChatService:
    """The question flow: HI -> name -> email -> loan amount -> tenure -> documents -> DONE (zip). Invalid answers are re-asked."""

    def __init__(self, store, meta: MetaClient, settings: Settings):
        self._store = store
        self._meta = meta
        self._s = settings
        self._downloads = asyncio.Semaphore(2)

    async def handle(self, msg: Inbound) -> None:
        if not await self._store.try_mark_message(msg.message_id):
            return
        mobile = normalize_mobile(msg.sender, self._s.default_country_code)
        if mobile is None:
            return

        text = msg.text.strip() if msg.text else None
        session = await self._store.find_for_sender(mobile)

        if _is(text, "RESTART"):
            if session is None:
                await self._reply(mobile, "Reply HI to start your loan application.")
                return
            await self._store.start_session(session.mobile, session.role)
            await self._reply(mobile, "Your session has been cleared. Reply HI to start again.")
            return

        if session is None:
            if not _is(text, "HI"):
                await self._reply(mobile, "Reply HI to start your loan application.")
                return
            session = await self._store.start_session(mobile, "A")
        elif session.status == "DONE":
            await self._reply(mobile, "Your application is already submitted. Reply RESTART to begin again.")
            return

        step = session.step
        if step == AWAIT_HI:
            if not _is(text, "HI"):
                await self._reply(mobile, "Reply HI to start your loan application.")
                return
            await self._store.update(session.id, NAME)
            await self._reply(mobile, "Hello! Let's get your loan application started.\nWhat is your full name?")
        elif step == NAME:
            if text and 2 <= len(text) <= 100 and any(c.isalpha() for c in text):
                await self._store.update(session.id, EMAIL, name=text)
                await self._reply(mobile, "Your email ID?")
            else:
                await self._reply(mobile, "Please enter your full name.")
        elif step == EMAIL:
            if is_email(text):
                await self._store.update(session.id, AMOUNT, email=text)
                await self._reply(mobile, "Loan amount you need?")
            else:
                await self._reply(mobile, "That doesn't look like a valid email. Your email ID?")
        elif step == AMOUNT:
            amount = parse_amount(text)
            if amount is not None:
                await self._store.update(session.id, TENURE, loan_amount=amount)
                await self._reply(mobile, "Loan tenure in months?")
            else:
                await self._reply(mobile, "Please enter the loan amount as a number, e.g. 500000.")
        elif step == TENURE:
            tenure = parse_tenure(text)
            if tenure is not None:
                await self._store.update(session.id, DOCS, tenure_months=tenure)
                await self._reply(mobile, "Please upload all your documents one by one. When you are done, type DONE.")
            else:
                await self._reply(mobile, "Please enter the tenure in months as a number, e.g. 36.")
        elif step == DOCS:
            await self._docs(session, mobile, msg, text)

    async def _docs(self, session, mobile: str, msg: Inbound, text: str | None) -> None:
        if msg.media_id:
            await self._store_file(session, mobile, msg)
            return
        if not _is(text, "DONE"):
            await self._reply(mobile, "Please upload your documents (image, PDF, xlsx or zip), or type DONE when you have finished.")
            return
        count = await self._store.file_count(session.id)
        if count == 0:
            await self._reply(mobile, "I haven't received any documents yet. Please upload them, then type DONE.")
            return
        zip_bytes = await self._store.build_zip(session.id)
        await self._store.complete(session.id, f"{mobile.lstrip('+')}_{session.role}_documents.zip", zip_bytes)
        log.info("Session %s/%s DONE with %d file(s)", mobile, session.role, count)
        await self._reply(mobile, "Thank you! Your documents are submitted. We'll get back to you.")

    async def _store_file(self, session, mobile: str, msg: Inbound) -> None:
        name = safe_filename(msg.filename) or default_filename(msg)
        try:
            async with self._downloads:
                content, mime = await self._meta.download_media(msg.media_id)
            if len(content) > self._s.max_file_mb * 1024 * 1024:
                await self._reply(mobile, f"{name} is larger than {self._s.max_file_mb} MB. Please send a smaller file.")
                return
            stored = await self._store.add_file(session.id, name, mime or msg.mime_type, content)
            await self._reply(mobile, f"Received {stored}")
        except MetaError:
            log.warning("Media %s for %s could not be downloaded", msg.media_id, mobile)
            await self._reply(mobile, f"I couldn't download {name}. Please send it again.")

    async def _reply(self, mobile: str, body: str) -> None:
        await self._meta.send_text(mobile, body)
