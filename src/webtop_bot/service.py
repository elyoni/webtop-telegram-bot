"""Glue between Webtop, the local store and the task app (Todoist)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Dict, List, Optional, Protocol, Tuple

from .config import Config
from .homework import Kid, Lesson, fetch_all, is_school_day, next_school_day, week_start
from .store import Recurring, Store, Task

logger = logging.getLogger(__name__)

KidLessons = List[Tuple[Kid, List[Lesson]]]
DATA_VERSION = 2  # 2: homework tasks keyed by lesson slot; older data needs a sync to adopt them
REOPEN_CHECK_DAYS = 14  # done tasks due this recently are checked for being reopened in the app


def homework_text(lesson: Lesson) -> str:
    """The task text for a lesson, or '' if it has no homework."""
    if lesson.homework:
        return lesson.homework
    return f"📎 {lesson.files} קבצים מצורפים" if lesson.files else ""


def _body(task: Task) -> str:
    return f"{task.kid_name} · {task.subject} · שיעור {task.lesson_date:%d/%m}\n{task.text}"


class TaskBackend(Protocol):
    """A task app; implemented by todoist.TodoistClient."""

    async def ensure_list(self, kid_name: str) -> str:
        """Id of the kid's task list, creating it if missing."""

    async def list_exists(self, list_id: str) -> bool: ...
    async def create_task(self, list_id: str, title: str, body: str, due: dt.date, tz: str) -> str: ...
    async def open_tasks(self, list_id: str) -> List[Tuple[str, str, Optional[dt.date]]]:
        """(task id, title, due date) of the list's open tasks."""

    async def task_status(self, list_id: str, task_id: str) -> Optional[str]: ...
    async def delete_task(self, list_id: str, task_id: str) -> None: ...
    async def set_completed(self, list_id: str, task_id: str, completed: bool = True) -> None: ...
    async def update_task(self, list_id: str, task_id: str, title: str, body: str) -> None: ...


class SyncBusy(Exception):
    """A Webtop update is already running."""


class SyncCooldown(Exception):
    """A Webtop update ran too recently."""

    def __init__(self, retry_at: dt.datetime):
        super().__init__(f"retry at {retry_at:%H:%M}")
        self.retry_at = retry_at


class Service:
    def __init__(self, cfg: Config, store: Store, todo: Optional[TaskBackend]):
        self.cfg = cfg
        self.store = store
        self.todo = todo
        self._lock = asyncio.Lock()  # Webtop refresh
        self._todo_lock = asyncio.Lock()  # task-app push / pull

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

    def needs_resync(self) -> bool:
        """The db was last synced by an older version (e.g. tasks still keyed by text)."""
        return self.store.get_meta("data_version") != str(DATA_VERSION)

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
            for kid, lessons, events in result:
                self.store.upsert_kid(kid)
                if events is not None:
                    self.store.add_events(kid.id, events)
                self.store.replace_lessons(kid, days, lessons)
                # One task per group per day; a double lesson's texts are joined.
                slots: Dict[Tuple[dt.date, str], Tuple[str, List[str]]] = {}
                for lesson in lessons:
                    text = homework_text(lesson)
                    if text:
                        texts = slots.setdefault((lesson.date, lesson.slot), (lesson.subject, []))[1]
                        if text not in texts:
                            texts.append(text)
                for (date, slot), (subject, texts) in slots.items():
                    self.store.upsert_homework(
                        kid.id, date, next_school_day(date), slot, subject, " · ".join(texts),
                        notified=first_run,  # don't flood alerts on first run
                    )
            self.store.set_meta("range_from", days[0].isoformat())
            self.store.set_meta("range_to", days[-1].isoformat())
            self.store.set_meta("last_success", self.cfg.now().isoformat())
            self.store.set_meta("data_version", str(DATA_VERSION))
            self.store.set_meta("sync_alerted", "")  # a failure after this alerts the admins again
        await self.push_to_todo()
        await self.pull_from_todo()  # imported tasks are unnotified, so returned below
        today = self.cfg.today()
        return [t for t in self.store.unnotified_tasks() if t.due_date >= today]

    async def push_to_todo(self) -> None:
        if not self.todo:
            return
        async with self._todo_lock:
            for task in self.store.tasks_without_todo():
                try:
                    list_id = await self._list_for(task.kid_id, task.kid_name)
                    todo_id = await self.todo.create_task(
                        list_id, task.title, _body(task), task.due_date, self.cfg.tz.key
                    )
                    self.store.set_todo_ids(task.id, list_id, todo_id)
                except Exception as e:
                    logger.warning("Task app push failed for task %s: %s", task.id, e)
                    return  # e.g. not logged in; retry next sync
            for task in self.store.stale_tasks():  # teacher edited the homework
                try:
                    await self.todo.update_task(task.todo_list_id, task.todo_task_id, task.title, _body(task))
                    self.store.clear_stale(task.id)
                except Exception as e:
                    logger.warning("Task app update failed for task %s: %s", task.id, e)
                    return

    async def delete_orphans(self) -> None:
        """Delete task-app copies of duplicate tasks dropped by the db migration."""
        if not self.todo:
            return
        async with self._todo_lock:
            while self.store.orphaned_todo:
                list_id, task_id = self.store.orphaned_todo[-1]
                try:
                    await self.todo.delete_task(list_id, task_id)
                except Exception as e:
                    logger.warning("Task app delete failed: %s", e)
                    return
                self.store.orphaned_todo.pop()
                logger.info("Deleted duplicate task %s from the task app", task_id)

    async def _list_for(self, kid_id: str, kid_name: str) -> str:
        list_id = self.store.todo_list_id(kid_id)
        if list_id and await self.todo.list_exists(list_id):
            return list_id
        list_id = await self.todo.ensure_list(kid_name.split()[0])
        self.store.set_todo_list_id(kid_id, list_id)
        return list_id

    async def pull_from_todo(self) -> List[Task]:
        """Mirror the task app: import tasks created there, and copy completed /
        reopened status (a deleted task counts as done). Returns the imported tasks."""
        if not self.todo:
            return []
        async with self._todo_lock:
            try:
                imported = await self._import_new()
                for task in self.store.open_tasks():
                    if task.todo_task_id:
                        status = await self.todo.task_status(task.todo_list_id, task.todo_task_id)
                        if status in (None, "completed"):
                            self.store.set_completed(task.id)
                recent = self.cfg.today() - dt.timedelta(days=REOPEN_CHECK_DAYS)
                for task in self.store.done_tasks_with_todo(due_from=recent):
                    if await self.todo.task_status(task.todo_list_id, task.todo_task_id) == "notStarted":
                        self.store.set_completed(task.id, False)
            except Exception as e:
                logger.warning("Task app pull failed: %s", e)
                return []
        return imported

    async def _import_new(self) -> List[Task]:
        known = self.store.todo_task_ids()
        imported = []
        for kid_id, list_id in self.store.todo_lists():
            for todo_id, title, due in await self.todo.open_tasks(list_id):
                if todo_id in known:
                    continue
                due = due or next_school_day(self.cfg.today())
                task = self.store.add_app_task(kid_id, self.cfg.today(), due, title, list_id, todo_id)
                if task:
                    imported.append(task)
        return imported

    async def complete(self, task_id: int, completed: bool = True) -> Optional[Task]:
        """Mark a task done, or reopen it with completed=False (undo)."""
        task = self.store.task(task_id)
        if not task:
            return None
        self.store.set_completed(task.id, completed)
        if self.todo and task.todo_task_id:
            async with self._todo_lock:  # so a concurrent pull doesn't read the old status back
                try:
                    await self.todo.set_completed(task.todo_list_id, task.todo_task_id, completed)
                except Exception as e:
                    logger.warning("Task app status update failed: %s", e)
        return task

    async def add_recurring(self, kid: Kid, text: str, weekdays: frozenset[int]) -> Recurring:
        rec = self.store.add_recurring(kid.id, text, weekdays)
        await self.create_recurring_tasks()  # today's task right away, if today is one of its days
        return rec

    async def create_recurring_tasks(self) -> None:
        """Create today's tasks of the recurring ones and push them to the task app."""
        if self.store.materialize_recurring(self.cfg.today()):
            await self.push_to_todo()

    async def add_manual(self, kid: Kid, text: str, due: dt.date) -> Task:
        task, _ = self.store.add_task(kid.id, self.cfg.today(), due, "", text, source="manual", notified=True)
        await self.push_to_todo()
        return task
