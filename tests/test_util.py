import hashlib
import hmac
from decimal import Decimal

from app.util import (
    Inbound,
    default_filename,
    is_email,
    normalize_mobile,
    normalize_role,
    parse_amount,
    parse_tenure,
    parse_webhook,
    safe_filename,
    unique_name,
    verify_signature,
)


def test_mobile_normalisation():
    assert normalize_mobile("9163476539", "91") == "+919163476539"
    assert normalize_mobile("+91 91634-76539", "91") == "+919163476539"
    assert normalize_mobile(" 919163476539", "91") == "+919163476539"
    assert normalize_mobile("09163476539", "91") == "+919163476539"
    assert normalize_mobile("12", "91") is None
    assert normalize_mobile("", "91") is None
    assert normalize_mobile(None, "91") is None


def test_role():
    assert normalize_role(None) == "A"
    assert normalize_role("") == "A"
    assert normalize_role("ca") == "CA"
    assert normalize_role("X") == ""


def test_signature():
    body = b'{"a":1}'
    good = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature(body, good, "secret")
    assert not verify_signature(body, good, "other")
    assert not verify_signature(body, "sha256=" + "0" * 64, "secret")
    assert not verify_signature(body, None, "secret")
    assert not verify_signature(body, good, "")
    assert not verify_signature(body, "sha256=zz", "secret")


def test_answers():
    assert is_email("ravi@example.com") and not is_email("not-an-email") and not is_email(None)
    assert parse_amount("5,00,000") == Decimal("500000")
    assert parse_amount("₹ 1200.50") == Decimal("1200.50")
    assert parse_amount("0") is None and parse_amount("abc") is None and parse_amount("-5") is None and parse_amount("NaN") is None
    assert parse_tenure("36") == 36 and parse_tenure("0") is None and parse_tenure("601") is None and parse_tenure("3.5") is None and parse_tenure("²") is None


def test_filenames():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("a:b?.pdf") == "a_b_.pdf"
    assert safe_filename("  ") is None
    assert unique_name("report.pdf", {"report.pdf"}) == "report (2).pdf"
    assert unique_name("report.pdf", {"report.pdf", "report (2).pdf"}) == "report (3).pdf"
    assert unique_name("Report.PDF", {"report.pdf"}) == "Report (2).PDF"
    assert default_filename(Inbound("m", "s", "image", media_id="1234567890", mime_type="image/png")) == "image_34567890.png"
    assert default_filename(Inbound("m", "s", "image", media_id="123")) == "image_123.jpg"


def test_parse_webhook():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "1", "from": "919163476539", "type": "text", "text": {"body": "hi"}},
        {"id": "2", "from": "919163476539", "type": "document", "document": {"id": "M1", "filename": "a.pdf", "mime_type": "application/pdf"}},
        {"id": "3", "from": "919163476539", "type": "image", "image": {"id": "M2", "mime_type": "image/jpeg"}},
        {"from": "x", "type": "text"},
    ]}}, {"value": {"statuses": [{"id": "s"}]}}]}]}
    got = parse_webhook(payload)
    assert [m.message_id for m in got] == ["1", "2", "3"]
    assert got[0].text == "hi" and got[1].filename == "a.pdf" and got[2].media_id == "M2"
    assert parse_webhook({}) == []
