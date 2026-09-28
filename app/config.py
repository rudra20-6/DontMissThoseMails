"""All runtime configuration, loaded from environment variables (or a local .env file).

Every variable is documented in `.env.example`.
"""

from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value: str) -> list[str]:
    return [part.strip().lower() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App ---
    app_base_url: str = "http://localhost:8000"
    admin_token: str = "change-me"
    timezone: str = "Asia/Kolkata"
    database_url: str = "sqlite:///./dontmiss.db"
    log_level: str = "INFO"
    run_scheduler: bool = True

    # --- Mail ---
    mail_provider: str = "graph"  # "graph" (Microsoft Graph / Outlook) or "imap"
    mail_poll_minutes: int = 5
    mail_lookback_hours: int = 24  # on first run, how far back to look
    mail_max_per_poll: int = 25

    ms_client_id: str = ""
    ms_client_secret: str = ""
    ms_tenant: str = "common"
    ms_scopes: str = "offline_access User.Read Mail.Read"

    imap_host: str = "outlook.office365.com"
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"

    # --- WhatsApp Cloud API ---
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_recipient: str = ""  # your personal number, digits only, with country code e.g. 919876543210
    whatsapp_verify_token: str = "change-me-verify"
    whatsapp_app_secret: str = ""  # optional; enables webhook signature verification
    whatsapp_api_version: str = "v21.0"
    whatsapp_template_name: str = "hello_world"  # template used to re-open the 24h window
    whatsapp_template_lang: str = "en_US"
    whatsapp_template_has_param: bool = False  # True if your template has a single {{1}} body param
    whatsapp_dry_run: bool = False  # log messages instead of sending

    # --- Jev (decisions) ---
    jev_api_key: str = ""
    jev_api_base_url: str = "https://thejevai.com"
    jev_model: str = "typesafe/jev-1.13"

    # --- Gemini (language) ---
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # --- Behaviour ---
    quiet_hours_start: int = 23  # local hour; no messages from this hour...
    quiet_hours_end: int = 7  # ...until this hour (queued and sent afterwards)
    digest_hour: int = 8  # local hour for the daily digest
    event_nag_hour: int = 18  # local hour for the daily "have you registered?" nag
    deadline_offsets_hours: str = "72,24,6,1"  # reminders before a deadline
    event_offsets_hours: str = "24,2"  # reminders before a registered event starts
    registration_offsets_hours: str = "48,24,6,1"  # reminders before a registration deadline
    importance_immediate_min: float = 1.5  # Jev score (0..4) at/above which a mail is pushed right away
    importance_drop_below: float = 0.6  # Jev score below which a mail is ignored completely
    ignore_senders: str = ""  # comma separated substrings, e.g. "noreply@linkedin.com,newsletter"
    priority_senders: str = "moodle,lms,dean,registrar,academic,exam,office"  # substrings; always important
    ignore_subject_keywords: str = ""

    @field_validator("whatsapp_recipient")
    @classmethod
    def _digits_only(cls, v: str) -> str:
        return "".join(ch for ch in v if ch.isdigit())

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def ignore_sender_list(self) -> list[str]:
        return _split_csv(self.ignore_senders)

    @property
    def priority_sender_list(self) -> list[str]:
        return _split_csv(self.priority_senders)

    @property
    def ignore_subject_list(self) -> list[str]:
        return _split_csv(self.ignore_subject_keywords)

    @staticmethod
    def _hours(value: str) -> list[float]:
        return sorted({float(x) for x in value.split(",") if x.strip()}, reverse=True)

    @property
    def deadline_offsets(self) -> list[float]:
        return self._hours(self.deadline_offsets_hours)

    @property
    def event_offsets(self) -> list[float]:
        return self._hours(self.event_offsets_hours)

    @property
    def registration_offsets(self) -> list[float]:
        return self._hours(self.registration_offsets_hours)

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        # Render / Neon / Supabase hand out postgres:// URLs; SQLAlchemy + psycopg3 wants this form.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
