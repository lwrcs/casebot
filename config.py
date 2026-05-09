from dataclasses import dataclass, field
from dotenv import load_dotenv
import os

load_dotenv()


@dataclass
class Settings:
    # Anthropic
    ANTHROPIC_API_KEY: str = field(default_factory=lambda: os.environ["ANTHROPIC_API_KEY"])

    # Discord
    DISCORD_BOT_TOKEN: str = field(default_factory=lambda: os.environ["DISCORD_BOT_TOKEN"])

    # PostgreSQL
    DATABASE_URL: str = field(default_factory=lambda: os.environ["DATABASE_URL"])

    # Encryption
    ENCRYPTION_MASTER_KEY: str = field(default_factory=lambda: os.environ["ENCRYPTION_MASTER_KEY"])

    # Google OAuth (shared app, per-user tokens stored in DB)
    GOOGLE_CREDENTIALS_FILE: str = field(default_factory=lambda: os.getenv("GOOGLE_CREDENTIALS_FILE", "./credentials.json"))
    GOOGLE_CLIENT_ID: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_ID", ""))
    GOOGLE_CLIENT_SECRET: str = field(default_factory=lambda: os.getenv("GOOGLE_CLIENT_SECRET", ""))

    # Web server for OAuth callbacks
    WEB_HOST: str = field(default_factory=lambda: os.getenv("WEB_HOST", "http://localhost:8080"))
    WEB_PORT: int = field(default_factory=lambda: int(os.getenv("PORT", os.getenv("WEB_PORT", "8080"))))

    # Bot settings
    CONVERSATION_HISTORY_TURNS: int = field(default_factory=lambda: int(os.getenv("CONVERSATION_HISTORY_TURNS", "20")))

    # Development: Discord ID of the developer; enables verbose content logging for that user only
    DEV_DISCORD_ID: str = field(default_factory=lambda: os.getenv("DEV_DISCORD_ID", ""))

    # State token signing secret (for OAuth CSRF protection)
    SECRET_KEY: str = field(default_factory=lambda: os.getenv("SECRET_KEY", os.urandom(32).hex()))


settings = Settings()
