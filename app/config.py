import os
from dataclasses import dataclass

# field name -> environment variable
REQUIRED_ENV = {
    "phone_number_id": "WHATSAPP_PHONE_NUMBER_ID",
    "access_token": "WHATSAPP_ACCESS_TOKEN",
    "verify_token": "WHATSAPP_VERIFY_TOKEN",
    "app_secret": "WHATSAPP_APP_SECRET",
    "start_template_name": "START_TEMPLATE_NAME",
    "public_base_url": "PUBLIC_BASE_URL",
    "database_url": "DATABASE_URL",
    "allowed_origin": "ALLOWED_ORIGIN",
}


@dataclass(frozen=True)
class Settings:
    phone_number_id: str = ""
    access_token: str = ""
    verify_token: str = ""
    app_secret: str = ""
    graph_api_version: str = "v25.0"
    graph_base_url: str = "https://graph.facebook.com"
    start_template_name: str = ""
    start_template_language: str = "en_US"
    default_country_code: str = "91"
    public_base_url: str = ""
    max_file_mb: int = 25
    database_url: str = ""
    allowed_origin: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ.get
        return cls(
            phone_number_id=env("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
            access_token=env("WHATSAPP_ACCESS_TOKEN", "").strip(),
            verify_token=env("WHATSAPP_VERIFY_TOKEN", "").strip(),
            app_secret=env("WHATSAPP_APP_SECRET", "").strip(),
            graph_api_version=env("GRAPH_API_VERSION", "v25.0").strip(),
            graph_base_url=env("GRAPH_BASE_URL", "https://graph.facebook.com").strip().rstrip("/"),
            start_template_name=env("START_TEMPLATE_NAME", "").strip(),
            start_template_language=env("START_TEMPLATE_LANGUAGE", "en_US").strip(),
            default_country_code=env("DEFAULT_COUNTRY_CODE", "91").strip(),
            public_base_url=env("PUBLIC_BASE_URL", "").strip().rstrip("/"),
            max_file_mb=int(env("MAX_FILE_MB", "25") or "25"),
            database_url=env("DATABASE_URL", "").strip(),
            allowed_origin=env("ALLOWED_ORIGIN", "").strip(),
        )

    def missing(self) -> list[str]:
        return [env_name for field, env_name in REQUIRED_ENV.items() if not getattr(self, field)]
