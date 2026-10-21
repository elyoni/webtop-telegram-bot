"""Fetch kids and their lessons/homework from Webtop."""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional, Tuple

from webtop import WebtopClient

from .config import Config

logger = logging.getLogger(__name__)


def kid_id(name: str) -> str:
    return f"name:{name}"


@dataclass(frozen=True)
class Kid:
    id: str  # stable, see kid_id(); Webtop's own id changes on every login
    name: str
    class_code: Optional[int]
    class_number: Optional[int]
    webtop_id: str = ""  # encrypted per-session id, needed for API calls

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


@dataclass(frozen=True)
class Event:
    """A teacher's note on a kid: "מילה טובה", missing equipment, etc."""
    id: int  # Webtop's eventID
    kid_id: str
    date: dt.date
    type: str
    positive: bool
    subject: str
    teacher: str
    hour: int
    remark: str


def week_start(day: dt.date) -> dt.date:
    """The Sunday that starts the school week containing `day`."""
    return day - dt.timedelta(days=(day.weekday() + 1) % 7)


def study_year(day: dt.date) -> int:
    """Israeli school years run September-June and are named by the year they end."""
    return day.year + 1 if day.month >= 9 else day.year


def is_school_day(day: dt.date) -> bool:
    return day.weekday() != 5  # Saturday


def prev_school_day(day: dt.date) -> dt.date:
    prev = day - dt.timedelta(days=1)
    return prev if is_school_day(prev) else prev - dt.timedelta(days=1)


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
            id=kid_id(name),
            name=name,
            class_code=c.get("classCode"),
            class_number=c.get("classNum"),
            webtop_id=c["id"],
        )
        for c in children or []
        if c.get("id")
        for name in [f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()]
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
                encrypted_student_id=kid.webtop_id,
                class_code=kid.class_code,
            )
        lessons.extend(_parse_day(weeks[sunday], kid, day))
    return lessons


def _parse_day(week: Any, kid: Kid, day: dt.date) -> List[Lesson]:
    entries = week.get("data") if isinstance(week, dict) else None
    for entry in entries or []:
        if str(entry.get("date", ""))[:10] != day.isoformat():
            continue
        lessons = [
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
        return _merge_teachers(lessons)
    return []


def _merge_teachers(lessons: List[Lesson]) -> List[Lesson]:
    """Webtop lists a lesson once per teacher, often with only one copy filled in.
    Merge each hour+subject into one lesson naming all its teachers, unless the
    copies hold different content (then keep the filled ones, each with its teacher)."""
    groups: Dict[Tuple[int, str], List[Lesson]] = {}
    for lesson in lessons:
        groups.setdefault((lesson.hour, lesson.subject), []).append(lesson)
    merged: List[Lesson] = []
    for group in groups.values():
        contents = {(l.topic, l.homework, l.files): l for l in group if l.topic or l.homework or l.files}
        if len(contents) > 1:
            merged.extend(contents.values())
            continue
        teachers = " / ".join(dict.fromkeys(l.teacher for l in group if l.teacher))
        merged.append(replace(next(iter(contents.values()), group[0]), teacher=teachers))
    return merged


async def fetch_events(client: WebtopClient, kid: Kid) -> Optional[List[Event]]:
    """The kid's events this school year, or None if Webtop failed (lessons still sync)."""
    try:
        resp = await client.get_discipline_events(encrypted_student_id=kid.webtop_id, class_code=kid.class_code)
        rows = resp["data"]["dataTable"]["diciplineEvents"] or []
    except Exception as e:
        logger.warning("Could not fetch events for %s: %s", kid.name, e)
        return None
    return [
        Event(
            id=r["eventID"],
            kid_id=kid.id,
            date=dt.date.fromisoformat(str(r.get("eventDate", ""))[:10]),
            type=(r.get("eventType") or "").strip(),
            positive=r.get("eventTypeId") == 1,  # 1 = praise ("מילה טובה")
            subject=(r.get("subjectName") or "").strip(),
            teacher=(r.get("teacherName") or "").strip(),
            hour=r.get("hourNum") or 0,
            remark=(r.get("remark") or "").strip(),
        )
        for r in rows
        if r.get("eventID") and r.get("eventDate")
    ]


async def fetch_all(
    cfg: Config, days: Iterable[dt.date]
) -> List[Tuple[Kid, List[Lesson], Optional[List[Event]]]]:
    """Log in once and fetch every kid's lessons for `days`, and their events."""
    days = list(days)
    async with make_client(cfg) as client:
        await client.login()
        kids = await fetch_kids(client)
        return [
            (kid, await fetch_lessons(client, kid, days, cfg.today()), await fetch_events(client, kid))
            for kid in kids
        ]
