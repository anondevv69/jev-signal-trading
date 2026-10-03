"""Environment-based configuration. No secrets in code, ever.

Required for live ingestion:
    TELEGRAM_BOT_TOKEN   Bot API token from @BotFather
    DISCORD_BOT_TOKEN    Bot token from the Discord Developer Portal

Optional:
    TELEGRAM_CHAT_IDS    Comma-separated chat/channel IDs to watch (e.g. "-100123, -100456")
    DISCORD_CHANNEL_IDS  Comma-separated channel IDs to watch
    TYPESAFE_BIN         Path to the typesafe CLI (default: ~/workspace/skills/typesafe-ai/bin/typesafe)
    POLL_INTERVAL_SEC    Seconds between ingest polls (default 300)
    DB_PATH              SQLite path (default: ~/workspace/signal-trading/data/signal.db)
    PAPER_START_BALANCE  Virtual starting balance for paper trading (default 10000.0)
"""

import os
from dataclasses import dataclass, field


def _csv(name: str) -> list:
    raw = os.environ.get(name, "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()]


@dataclass
class Config:
    telegram_bot_token: str = ""
    discord_bot_token: str = ""
    telegram_chat_ids: list = field(default_factory=list)
    discord_channel_ids: list = field(default_factory=list)
    typesafe_bin: str = ""
    poll_interval_sec: int = 300
    db_path: str = ""
    paper_start_balance: float = 10000.0

    def redacted(self) -> str:
        """Safe-to-log summary: never includes secret values."""
        return (
            f"telegram_configured={bool(self.telegram_bot_token)} "
            f"discord_configured={bool(self.discord_bot_token)} "
            f"telegram_chats={len(self.telegram_chat_ids)} "
            f"discord_channels={len(self.discord_channel_ids)} "
            f"poll_interval_sec={self.poll_interval_sec} "
            f"db_path={self.db_path} "
            f"paper_start_balance={self.paper_start_balance}"
        )


def load() -> Config:
    home = os.path.expanduser("~")
    return Config(
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        discord_bot_token=os.environ.get("DISCORD_BOT_TOKEN", ""),
        telegram_chat_ids=_csv("TELEGRAM_CHAT_IDS"),
        discord_channel_ids=_csv("DISCORD_CHANNEL_IDS"),
        typesafe_bin=os.environ.get(
            "TYPESAFE_BIN", f"{home}/workspace/skills/typesafe-ai/bin/typesafe"
        ),
        poll_interval_sec=int(os.environ.get("POLL_INTERVAL_SEC", "300")),
        db_path=os.environ.get(
            "DB_PATH", f"{home}/workspace/signal-trading/data/signal.db"
        ),
        paper_start_balance=float(os.environ.get("PAPER_START_BALANCE", "10000.0")),
    )
