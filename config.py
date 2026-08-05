"""
Central configuration, loaded from environment variables.
Copy .env.example to .env and fill in values; python-dotenv loads it at startup.
"""
import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    discord_token: str = os.environ.get("DISCORD_BOT_TOKEN", "")
    anthropic_api_key: str = os.environ.get("ANTHROPIC_API_KEY", "")
    database_url: str = os.environ.get("DATABASE_URL", "postgresql://localhost:5432/servebot")
    poll_interval_seconds: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
    anthropic_model: str = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    # Default reminder offsets in minutes-before-due, applied when a guild
    # hasn't configured its own defaults yet. e.g. 1440 = 1 day before, 0 = at due time.
    fallback_default_offsets: List[int] = field(default_factory=lambda: [1440, 0])

    def validate(self) -> list:
        problems = []
        if not self.discord_token:
            problems.append("DISCORD_BOT_TOKEN is not set")
        if not self.anthropic_api_key:
            problems.append("ANTHROPIC_API_KEY is not set")
        return problems


config = Config()
