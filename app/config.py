"""Environment-driven configuration. Every knob is an env var (see .env.example)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: str = "dev"  # dev | prod | test
    database_url: str = "sqlite:///./var/dev.db"
    secret_key: str = "dev-insecure-secret-change-me"
    base_url: str = "http://localhost:8000"
    timezone: str = "America/Los_Angeles"

    cookie_secure: bool = False
    force_https: bool = False  # redirect http->https using X-Forwarded-Proto
    trusted_proxy: bool = True  # read client IP from X-Forwarded-For

    allowed_email_domain: str = "csuchico.edu"  # default; adminsettings override

    max_upload_mb: int = 10  # per file; STEP assemblies can be large, raise on hosts with enough storage
    storage_backend: str = "fs"  # fs | db  (use "db" on hosts with ephemeral disks)
    upload_dir: str = "./var/uploads"

    backup_dir: str = "./var/backups"
    backup_put_url: str = ""  # optional HTTP PUT / WebDAV destination
    backup_put_auth: str = ""  # optional "user:password" for the PUT destination

    email_provider: str = "console"  # console | memory | brevo | resend | smtp
    email_from: str = "machineshop@example.edu"
    email_from_name: str = "Gene Haas Advanced Manufacturing Lab"
    brevo_api_key: str = ""
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    staff_notify_email: str = ""  # new-submission alerts
    reply_to_email: str = ""  # replies to approval emails ("add a machine") land here; falls back to the first STAFF_NOTIFY_EMAIL

    scheduler_enabled: bool = True
    bootstrap_admin_username: str = ""  # create first admin on boot if no staff exist
    bootstrap_admin_password: str = ""
    bootstrap_reset: bool = False  # recovery: on boot, (re)set that admin's password, unlock and re-enable it

    def validate_for_prod(self) -> None:
        """Fail fast on unsafe production configuration."""
        if self.env != "prod":
            return
        problems = []
        if len(self.secret_key) < 32 or self.secret_key.startswith("dev-"):
            problems.append("SECRET_KEY must be a random string of 32+ characters")
        if not self.base_url.startswith("https://"):
            problems.append("BASE_URL must be an https:// URL")
        if self.database_url.startswith("sqlite"):
            problems.append("DATABASE_URL must be Postgres in production (SQLite is for local dev only)")
        if self.email_provider in ("console", "memory"):
            problems.append("EMAIL_PROVIDER must be brevo, resend or smtp in production")
        if problems:
            raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))

    @property
    def secure_cookies(self) -> bool:
        return self.cookie_secure or self.env == "prod"

    @property
    def sqlalchemy_url(self) -> str:
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url


@lru_cache
def get_config() -> Config:
    return Config()
