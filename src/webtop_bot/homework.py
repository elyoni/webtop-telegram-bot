"""Fetch kids and their lessons/homework from Webtop."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from webtop import WebtopClient

from .config import Config


@dataclass(frozen=True)
class Kid:
    id: str
    name: str
    class_code: Optional[int]
    class_number: Optional[int]

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name else "?"

    @property
    def class_label(self) -> str:
        return f"{self.class_code}-{self.class_number}"


@dataclass(frozen=True)
class Lesson:
    kid: Kid
    date: dt.date
    hour: int
    subject: str
    teacher: str
    topic: str
    homework: str
    files: int


def week_start(day: dt.date) -> dt.date:
    """The Sunday that starts the school week containing `day`."""
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def study_year(day: dt.date) -> int:
    """Israeli school years run September-June and are named by the year they end."""
    return day.year + 1 if day.month >= 9 else day.year


def is_school_day(day: dt.date) -> bool:
    return day.weekday() != 5  # Saturday


def next_school_day(day: dt.date) -> dt.date:
    nxt = day + dt.timedelta(days=1)
    return nxt if is_school_day(nxt) else nxt + dt.timedelta(days=1)


def make_client(cfg: Config) -> WebtopClient:
    kwargs: Dict[str, Any] = {}
    if cfg.webtop_data:
        kwargs["data"] = cfg.webtop_data
    return WebtopClient(cfg.webtop_username, cfg.webtop_password, **kwargs)


async def fetch_kids(client: WebtopClient) -> List[Kid]:
    dashboard = await client.get_students()
    data = dashboard.get("data") if isinstance(dashboard, dict) else None
    children = data.get("childrens") if isinstance(data, dict) else None
    return [
        Kid(
            id=c["id"],
            name=f"{c.get('firstName', '')} {c.get('lastName', '')}".strip(),
            class_code=c.get("classCode"),
            class_number=c.get("classNum"),
        )
        for c in children or []
        if c.get("id")
    ]


async def fetch_lessons(
    client: WebtopClient, kid: Kid, days: Iterable[dt.date], today: dt.date
) -> List[Lesson]:
    """Lessons of `kid` on the given days (one API call per school week)."""
    weeks: Dict[dt.date, Any] = {}
    lessons: List[Lesson] = []
    for day in sorted(set(days)):
        sunday = week_start(day)
        if sunday not in weeks:
            weeks[sunday] = await client.get_lessons_and_homework(
                week_index=(sunday - week_start(today)).days // 7,
                study_year=study_year(day),
                encrypted_student_id=kid.id,
                class_code=kid.class_code,
            )
        lessons.extend(_parse_day(weeks[sunday], kid, day))
    return lessons


def _parse_day(week: Any, kid: Kid, day: dt.date) -> List[Lesson]:
    entries = week.get("data") if isinstance(week, dict) else None
    for entry in entries or []:
        if str(entry.get("date", ""))[:10] != day.isoformat():
            continue
        return [
            Lesson(
                kid=kid,
                date=day,
                hour=item.get("hour_no") or hour.get("hour") or 0,
                subject=(item.get("subject_name") or "").strip(),
                teacher=(item.get("teacher") or "").strip(),
                topic=(item.get("descClass") or "").strip(),
                homework=(item.get("homeWork") or "").strip(),
                files=len(item.get("files") or []),
            )
            for hour in entry.get("hoursData") or []
            for item in hour.get("scheduale") or []
        ]
    return []


async def fetch_all(cfg: Config, days: Iterable[dt.date]) -> List[Tuple[Kid, List[Lesson]]]:
    """Log in once and fetch every kid's lessons for `days`."""
    days = list(days)
    async with make_client(cfg) as client:
        await client.login()
        kids = await fetch_kids(client)
        return [(kid, await fetch_lessons(client, kid, days, cfg.today())) for kid in kids]
