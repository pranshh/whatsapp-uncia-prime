import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from decimal import Decimal

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
AMOUNT_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)?$")
TENURE_PATTERN = re.compile(r"^[0-9]+$")
MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


@dataclass(frozen=True)
class Inbound:
    message_id: str
    sender: str
    type: str
    text: str | None = None
    media_id: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    payload: str | None = None


def normalize_mobile(raw: str | None, default_country_code: str) -> str | None:
    """E.164 with a leading '+'; None when it is not a plausible number. A bare 10-digit number gets the default country code."""
    if not raw or not raw.strip():
        return None
    digits = "".join(c for c in raw if c.isascii() and c.isdigit())
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10:
        digits = default_country_code + digits
    return "+" + digits if 8 <= len(digits) <= 15 else None


def normalize_role(raw: str | None) -> str:
    role = (raw if raw is not None and raw.strip() else "A").strip().upper()
    return role if role in ("A", "CA") else ""


def verify_signature(body: bytes, header: str | None, app_secret: str) -> bool:
    """Checks Meta's X-Hub-Signature-256 ('sha256=<hex>' over the raw body, keyed by the app secret)."""
    if not app_secret or not header or not header.lower().startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:].strip().lower())


def parse_webhook(payload: dict) -> list[Inbound]:
    result: list[Inbound] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            for m in (change.get("value") or {}).get("messages") or []:
                message_id, sender, kind = m.get("id"), m.get("from"), m.get("type")
                if not (message_id and sender and kind):
                    continue
                text = media_id = filename = mime = payload = None
                if kind == "text":
                    text = (m.get("text") or {}).get("body")
                elif kind in ("image", "document"):
                    media = m.get(kind) or {}
                    media_id, filename, mime = media.get("id"), media.get("filename"), media.get("mime_type")
                elif kind == "button":
                    button = m.get("button") or {}
                    text, payload = button.get("text"), button.get("payload")
                elif kind == "interactive":
                    interactive = m.get("interactive") or {}
                    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
                    text, payload = reply.get("title"), reply.get("id")
                result.append(Inbound(message_id, sender, kind, text, media_id, filename, mime, payload))
    return result


def consent_answer(msg: Inbound) -> str | None:
    """'YES' or 'NO' from a typed text or a quick-reply button (text or payload), ignoring case, spaces and trailing punctuation; otherwise None."""
    for value in (msg.text, msg.payload):
        if value and value.strip().rstrip(".! ").casefold() in ("yes", "no"):
            return value.strip().rstrip(".! ").upper()
    return None


def is_email(text: str | None) -> bool:
    return bool(text) and len(text) <= 254 and bool(EMAIL_PATTERN.match(text))


def parse_amount(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = text.replace(",", "").replace("₹", "").replace(" ", "")
    if not AMOUNT_PATTERN.match(cleaned):
        return None
    amount = Decimal(cleaned)
    return amount if 0 < amount < Decimal("1000000000000") else None


def parse_tenure(text: str | None) -> int | None:
    if not text or not TENURE_PATTERN.match(text):
        return None
    months = int(text)
    return months if 1 <= months <= 600 else None


def safe_filename(name: str | None) -> str | None:
    if not name or not name.strip():
        return None
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in base).strip()
    return cleaned or None


def default_filename(msg: Inbound) -> str:
    ext = MIME_EXTENSIONS.get(msg.mime_type or "", ".jpg" if msg.type == "image" else "")
    tail = msg.media_id[-8:] if msg.media_id and len(msg.media_id) > 8 else (msg.media_id or "file")
    return f"{msg.type}_{tail}{ext}"


def unique_name(name: str, existing: set[str]) -> str:
    """'report.pdf' -> 'report (2).pdf' when the name is already taken (case-insensitive)."""
    taken = {e.lower() for e in existing}
    if name.lower() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    n = 2
    while f"{stem} ({n}){ext}".lower() in taken:
        n += 1
    return f"{stem} ({n}){ext}"
