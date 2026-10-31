import logging

import httpx

from .config import Settings

log = logging.getLogger("whatsapp.meta")


class MetaError(Exception):
    """A Meta Graph API call failed; the message never contains the access token."""


def _cut(text: str, limit: int = 400) -> str:
    return text if len(text) <= limit else text[:limit]


class MetaClient:
    """Meta WhatsApp Cloud API: send template / text, the two-step media download (id -> url -> bytes) and a credentials check."""

    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self._s = settings
        self._http = http

    def _url(self, path: str) -> str:
        return f"{self._s.graph_base_url}/{self._s.graph_api_version}/{path}"

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._s.access_token}"}

    async def send_template(self, to: str, name: str, language: str) -> None:
        await self._post_message({"messaging_product": "whatsapp", "to": _digits(to), "type": "template", "template": {"name": name, "language": {"code": language}}})

    async def send_text(self, to: str, body: str) -> None:
        await self._post_message({"messaging_product": "whatsapp", "to": _digits(to), "type": "text", "text": {"body": body}})

    async def _post_message(self, payload: dict) -> None:
        try:
            response = await self._http.post(self._url(f"{self._s.phone_number_id}/messages"), json=payload, headers=self._auth())
        except httpx.HTTPError as ex:
            raise MetaError(f"WhatsApp send failed: {type(ex).__name__}") from ex
        if response.status_code >= 400:
            log.warning("WhatsApp send failed %s: %s", response.status_code, _cut(response.text))
            raise MetaError(f"WhatsApp send failed ({response.status_code}): {_cut(response.text)}")

    async def download_media(self, media_id: str) -> tuple[bytes, str | None]:
        try:
            meta = await self._http.get(self._url(media_id), headers=self._auth())
            if meta.status_code >= 400:
                raise MetaError(f"Media lookup failed ({meta.status_code}): {_cut(meta.text)}")
            info = meta.json()
            file = await self._http.get(info["url"], headers=self._auth())
        except httpx.HTTPError as ex:
            raise MetaError(f"Media download failed: {type(ex).__name__}") from ex
        except (KeyError, ValueError) as ex:
            raise MetaError("Media lookup returned no url") from ex
        if file.status_code >= 400:
            raise MetaError(f"Media download failed ({file.status_code})")
        return file.content, info.get("mime_type")

    async def check(self) -> str | None:
        """None when Meta accepts the token and phone number id, otherwise the reason."""
        if not self._s.phone_number_id or not self._s.access_token:
            return "WHATSAPP_PHONE_NUMBER_ID or WHATSAPP_ACCESS_TOKEN is not set"
        try:
            response = await self._http.get(self._url(f"{self._s.phone_number_id}?fields=display_phone_number"), headers=self._auth())
        except httpx.HTTPError as ex:
            return f"Meta unreachable: {type(ex).__name__}"
        return None if response.status_code < 400 else f"Meta answered {response.status_code}: {_cut(response.text)}"


def _digits(mobile: str) -> str:
    return "".join(c for c in mobile if c.isdigit())
