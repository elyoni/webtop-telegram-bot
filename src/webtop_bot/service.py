"""Glue between Webtop, the local store and Microsoft To Do."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import List, Optional, Tuple

from .config import Config
from .homework import Kid, Lesson, fetch_all, is_school_day, next_school_day, week_start
from .store import Store, Task, Topic
from .todo import TodoClient

logger = logging.getLogger(__name__)

KidLessons = List[Tuple[Kid, List[Lesson]]]


def homework_text(lesson: Lesson) -> str:
    """The task text for a lesson, or '' if it has no homework."""
    if lesson.homework:
        return lesson.homework
    return f"📎 {lesson.files} קבצים מצורפים" if lesson.files else ""


class SyncBusy(Exception):
    """A Webtop update is already running."""


class SyncCooldown(Exception):
    """A Webtop update ran too recently."""

    def __init__(self, retry_at: dt.datetime):
        super().__init__(f"retry at {retry_at:%H:%M}")
        self.retry_at = retry_at


class Service:
    def __init__(self, cfg: Config, store: Store, todo: Optional[TodoClient]):
        self.cfg = cfg
        self.store = store
        self.todo = todo
        self._lock = asyncio.Lock()

    # --- reading (local db only) ----------------------------------------

    def view(self, days: List[dt.date]) -> KidLessons:
        return [(kid, self.store.lessons(kid, days)) for kid in self.store.kids()]

    def stored_days(self) -> List[dt.date]:
        """Days covered by the latest Webtop update."""
        first, last = self.store.get_meta("range_from"), self.store.get_meta("range_to")
        if not first or not last:
            return []
        start, end = dt.date.fromisoformat(first), dt.date.fromisoformat(last)
        return [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]

    def last_sync(self) -> Optional[dt.datetime]:
        raw = self.store.get_meta("last_success")
        return dt.datetime.fromisoformat(raw).astimezone(self.cfg.tz) if raw else None

    # --- updating from Webtop -------------------------------------------

    def _sync_days(self) -> List[dt.date]:
        sunday = week_start(self.cfg.today())
        first = sunday - dt.timedelta(weeks=self.cfg.sync_weeks_back)
        last = sunday + dt.timedelta(weeks=self.cfg.sync_weeks_ahead, days=6)
        days = [first + dt.timedelta(days=i) for i in range((last - first).days + 1)]
        return [d for d in days if is_school_day(d)]

    def check_cooldown(self) -> None:
        """Raise SyncCooldown if the last attempt (failed ones too) was too recent."""
        raw = self.store.get_meta("last_attempt")
        if not raw:
            return
        retry_at = dt.datetime.fromisoformat(raw) + dt.timedelta(minutes=self.cfg.sync_cooldown_minutes)
        if self.cfg.now() < retry_at:
            raise SyncCooldown(retry_at.astimezone(self.cfg.tz))

    async def refresh(self, manual: bool = False) -> List[Task]:
        """Pull Webtop into the local db. Returns new homework not yet announced
        (the caller marks them notified). Manual refreshes are rate limited."""
        if self._lock.locked():
            raise SyncBusy()
        async with self._lock:
            if manual:
                self.check_cooldown()
            self.store.set_meta("last_attempt", self.cfg.now().isoformat())
            days = self._sync_days()
            first_run = not self.store.has_tasks()
            result = await fetch_all(self.cfg, days)
            for kid, lessons in result:
                self.store.upsert_kid(kid)
                self.store.replace_lessons(kid, days, lessons)
                for lesson in lessons:
                    text = homework_text(lesson)
                    if text:
                        self.store.add_task(
                            kid.id, lesson.date, next_school_day(lesson.date), lesson.subject, text,
                            source="homework", notified=first_run,  # don't flood alerts on first run
                        )
            self.store.set_meta("range_from", days[0].isoformat())
            self.store.set_meta("range_to", days[-1].isoformat())
            self.store.set_meta("last_success", self.cfg.now().isoformat())
        await self.push_to_todo()
        await self.pull_completion()
        today = self.cfg.today()
        return [t for t in self.store.unnotified_tasks() if t.due_date >= today]

    async def push_to_todo(self) -> None:
        if not self.todo:
            return
        for task in self.store.tasks_without_todo():
            try:
                list_id = await self._list_for(task.kid_id, task.kid_name)
                body = f"{task.kid_name} · {task.subject} · שיעור {task.lesson_date:%d/%m}\n{task.text}"
                todo_id = await self.todo.create_task(list_id, task.title, body, task.due_date, self.cfg.tz.key)
                self.store.set_todo_ids(task.id, list_id, todo_id)
            except Exception as e:
                logger.warning("To Do push failed for task %s: %s", task.id, e)
                return  # e.g. not logged in; retry next sync

    async def _list_for(self, kid_id: str, kid_name: str) -> str:
        list_id = self.store.todo_list_id(kid_id)
        if list_id and await self.todo.list_exists(list_id):
            return list_id
        list_id = await self.todo.ensure_list(f"שיעורי בית - {kid_name.split()[0]}")
        self.store.set_todo_list_id(kid_id, list_id)
        return list_id

    async def pull_completion(self) -> None:
        """Mark tasks done locally when they were completed (or deleted) in To Do."""
        if not self.todo:
            return
        for task in self.store.open_tasks():
            if not task.todo_task_id:
                continue
            try:
                status = await self.todo.task_status(task.todo_list_id, task.todo_task_id)
            except Exception as e:
                logger.warning("To Do status check failed: %s", e)
                return
            if status in (None, "completed"):
                self.store.set_completed(task.id)

    async def complete(self, task_id: int) -> Optional[Task]:
        task = self.store.task(task_id)
        if not task:
            return None
        self.store.set_completed(task.id)
        if self.todo and task.todo_task_id:
            try:
                await self.todo.set_completed(task.todo_list_id, task.todo_task_id)
            except Exception as e:
                logger.warning("To Do complete failed: %s", e)
        return task

    async def promote_topic(self, topic: Topic) -> Task:
        task, _ = self.store.add_task(
            topic.kid_id, topic.lesson_date, next_school_day(topic.lesson_date), topic.subject, topic.text,
            source="topic", notified=True,
        )
        await self.push_to_todo()
        return task

    async def add_manual(self, kid: Kid, text: str, due: dt.date) -> Task:
        task, _ = self.store.add_task(kid.id, self.cfg.today(), due, "", text, source="manual", notified=True)
        await self.push_to_todo()
        return task
