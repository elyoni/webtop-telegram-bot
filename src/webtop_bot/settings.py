"""Settings changed from Telegram (/config).

Bot-wide ones (admins): Webtop sync times, task-app interval; in the meta table.
Per user: daily summary and evening reminder times; in user_settings.
Until changed, the .env values (Config) apply."""
from __future__ import annotations

import datetime as dt
import json
from typing import Dict, List, Optional, Tuple

from .config import Config
from .store import Store

POLL_OPTIONS = (0, 5, 10, 30, 60)  # minutes; 0 = only with Webtop updates
DIGEST_OPTIONS = ("13:00", "14:00", "15:00", "16:00", "17:00", "18:00")
REMINDER_OPTIONS = ("19:00", "19:30", "20:00", "20:30", "21:00")
SYNC_OPTIONS = ("06:00", "07:00", "10:00", "13:00", "14:00", "15:30", "17:00", "19:00")
OFF = "off"
MAX_PENDING = 10  # access requests kept; anyone can /start the bot, so this is bounded


def fmt(t: Optional[dt.time]) -> str:
    return f"{t:%H:%M}" if t else "כבוי"


class Settings:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store

    def _get(self, name: str) -> Optional[str]:
        return self.store.get_meta(f"cfg_{name}")

    def _set(self, name: str, value: str) -> None:
        self.store.set_meta(f"cfg_{name}", value)

    def _time(self, name: str, default: dt.time) -> Optional[dt.time]:
        raw = self._get(name)
        if raw is None:
            return default
        return None if raw == OFF else dt.time.fromisoformat(raw)

    @property
    def todo_poll_minutes(self) -> int:
        raw = self._get("todo_poll_minutes")
        return self.cfg.todo_poll_minutes if raw is None else int(raw)

    def set_todo_poll_minutes(self, minutes: int) -> None:
        self._set("todo_poll_minutes", str(minutes))

    @property
    def digest_time(self) -> Optional[dt.time]:
        return self._time("digest_time", self.cfg.digest_time)

    @property
    def reminder_time(self) -> Optional[dt.time]:
        return self._time("reminder_time", self.cfg.reminder_time)

    def user_time(self, user_id: int, name: str) -> Optional[dt.time]:
        """A user's digest_time / reminder_time (None = off); the bot default until they pick one."""
        raw = self.store.user_setting(user_id, name)
        if raw is None:
            return getattr(self, name)
        return None if raw == OFF else dt.time.fromisoformat(raw)

    def set_user_time(self, user_id: int, name: str, value: str) -> None:
        """name: digest_time / reminder_time; value: one of time_options(name, user_id) or 'off'."""
        if value != OFF and value not in self.time_options(name, user_id):
            raise ValueError(f"{name}: unexpected value {value!r}")
        self.store.set_user_setting(user_id, name, value)

    def last_digest(self, user_id: int) -> Optional[str]:
        """When the user's last daily summary went out (store.utc_now() format)."""
        return self.store.user_setting(user_id, "last_digest")

    def set_last_digest(self, user_id: int, when: str) -> None:
        self.store.set_user_setting(user_id, "last_digest", when)

    @property
    def sync_times(self) -> Tuple[dt.time, ...]:
        raw = self._get("sync_times")
        if raw is None:
            return self.cfg.sync_times
        return tuple(dt.time.fromisoformat(t) for t in raw.split(",") if t)

    def toggle_sync_time(self, value: str) -> None:
        if value not in self.time_options("sync_times"):
            raise ValueError(f"sync_times: unexpected value {value!r}")
        times = {f"{t:%H:%M}" for t in self.sync_times}
        times ^= {value}
        self._set("sync_times", ",".join(sorted(times)))

    def time_options(self, name: str, user_id: Optional[int] = None) -> List[str]:
        """The preset times, plus the current one (the user's, for per-user times) if it isn't a preset."""
        presets = {"digest_time": DIGEST_OPTIONS, "reminder_time": REMINDER_OPTIONS, "sync_times": SYNC_OPTIONS}[name]
        if name == "sync_times":
            current = self.sync_times
        else:
            current = (getattr(self, name), self.user_time(user_id, name) if user_id is not None else None)
        return sorted(set(presets) | {f"{t:%H:%M}" for t in current if t})

    # Access requests waiting for an admin: {user id: name}.

    def pending_users(self) -> Dict[int, str]:
        return {int(k): v for k, v in json.loads(self._get("pending_users") or "{}").items()}

    def set_pending(self, user_id: int, name: Optional[str]) -> None:
        """Add a request, or remove it with name=None."""
        pending = self.pending_users()
        pending.pop(user_id, None)
        if name is not None:
            pending[user_id] = name  # newest last
            pending = dict(list(pending.items())[-MAX_PENDING:])
        self._set("pending_users", json.dumps(pending, ensure_ascii=False))
