"""Settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Optional, Tuple
from zoneinfo import ZoneInfo


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _time(name: str, default: str) -> dt.time:
    raw = os.environ.get(name, default).strip()
    try:
        return dt.time.fromisoformat(raw)
    except ValueError:
        raise SystemExit(f"{name} must be HH:MM, got {raw!r}") from None


def _times(name: str, default: str) -> Tuple[dt.time, ...]:
    raw = os.environ.get(name, default)
    try:
        return tuple(dt.time.fromisoformat(t.strip()) for t in raw.split(",") if t.strip())
    except ValueError:
        raise SystemExit(f"{name} must be comma-separated HH:MM, got {raw!r}") from None


def data_dir() -> Path:
    path = Path(os.environ.get("DATA_DIR", "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass(frozen=True)
class Config:
    webtop_username: str
    webtop_password: str
    webtop_data: Optional[str]
    telegram_token: str
    allowed_chat_ids: FrozenSet[int]
    ms_client_id: Optional[str]
    data_dir: Path
    tz: ZoneInfo
    digest_time: dt.time
    reminder_time: dt.time
    sync_times: Tuple[dt.time, ...]
    sync_cooldown_minutes: int
    sync_weeks_back: int
    sync_weeks_ahead: int

    @classmethod
    def from_env(cls) -> "Config":
        chat_ids = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
        return cls(
            webtop_username=required("WEBTOP_USERNAME"),
            webtop_password=required("WEBTOP_PASSWORD"),
            webtop_data=os.environ.get("WEBTOP_DATA") or None,
            telegram_token=required("TELEGRAM_BOT_TOKEN"),
            allowed_chat_ids=frozenset(int(c) for c in chat_ids.replace(" ", "").split(",") if c),
            ms_client_id=os.environ.get("MS_CLIENT_ID") or None,
            data_dir=data_dir(),
            tz=ZoneInfo(os.environ.get("BOT_TIMEZONE", "Asia/Jerusalem")),
            digest_time=_time("DIGEST_TIME", "16:00"),
            reminder_time=_time("REMINDER_TIME", "20:00"),
            sync_times=_times("SYNC_TIMES", "07:00,15:30"),
            sync_cooldown_minutes=int(os.environ.get("SYNC_COOLDOWN_MINUTES", "30")),
            sync_weeks_back=int(os.environ.get("SYNC_WEEKS_BACK", "1")),
            sync_weeks_ahead=int(os.environ.get("SYNC_WEEKS_AHEAD", "1")),
        )

    def now(self) -> dt.datetime:
        return dt.datetime.now(self.tz)

    def today(self) -> dt.date:
        return self.now().date()
