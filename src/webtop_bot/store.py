"""SQLite storage: known kids, homework tasks (and their To Do ids), lesson topics."""
from __future__ import annotations

import datetime as dt
import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .homework import Event, Kid, Lesson, kid_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS kids (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    class_code INTEGER,
    class_number INTEGER
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kid_id TEXT NOT NULL,
    lesson_date TEXT NOT NULL,
    due_date TEXT NOT NULL,
    subject TEXT NOT NULL,
    text TEXT NOT NULL,
    source TEXT NOT NULL,              -- homework | topic | manual | app (created in the task app)
    completed INTEGER NOT NULL DEFAULT 0,
    notified INTEGER NOT NULL DEFAULT 0,
    todo_list_id TEXT,
    todo_task_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS topics (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    kid_id TEXT NOT NULL,
    lesson_date TEXT NOT NULL,
    subject TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY,
    kid_id TEXT NOT NULL,
    date TEXT NOT NULL,
    hour INTEGER NOT NULL,
    subject TEXT NOT NULL,
    teacher TEXT NOT NULL,
    topic TEXT NOT NULL,
    homework TEXT NOT NULL,
    files INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS lessons_by_date ON lessons (date, kid_id);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS todo_lists (
    kid_id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,            -- Telegram user id (= private chat id)
    name TEXT NOT NULL DEFAULT '',
    added_by INTEGER,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS hidden_kids (  -- kids a Telegram user chose not to see (see /kids)
    user_id INTEGER NOT NULL,
    kid_id TEXT NOT NULL,
    PRIMARY KEY (user_id, kid_id)
);
CREATE TABLE IF NOT EXISTS events (       -- teacher notes from Webtop ("מילה טובה", ...)
    id INTEGER PRIMARY KEY,               -- Webtop's eventID
    kid_id TEXT NOT NULL,
    date TEXT NOT NULL,
    type TEXT NOT NULL,
    positive INTEGER NOT NULL,
    subject TEXT NOT NULL,
    teacher TEXT NOT NULL,
    hour INTEGER NOT NULL,
    remark TEXT NOT NULL,
    notified INTEGER NOT NULL DEFAULT 0   -- shown in a daily summary
);
"""


def item_key(kid_id: str, date: dt.date, subject: str, text: str) -> str:
    return hashlib.sha1(f"{kid_id}|{date}|{subject}|{text}".encode()).hexdigest()


@dataclass
class Task:
    id: int
    kid_id: str
    kid_name: str
    lesson_date: dt.date
    due_date: dt.date
    subject: str
    text: str
    source: str
    completed: bool
    todo_list_id: Optional[str]
    todo_task_id: Optional[str]

    @property
    def title(self) -> str:
        return f"{self.subject}: {self.text}" if self.subject else self.text


_TASK_SELECT = """
SELECT t.id, t.kid_id, k.name, t.lesson_date, t.due_date, t.subject, t.text, t.source,
       t.completed, t.todo_list_id, t.todo_task_id
FROM tasks t JOIN kids k ON k.id = t.kid_id
"""


def _task(row: sqlite3.Row) -> Task:
    return Task(
        id=row[0],
        kid_id=row[1],
        kid_name=row[2],
        lesson_date=dt.date.fromisoformat(row[3]),
        due_date=dt.date.fromisoformat(row[4]),
        subject=row[5],
        text=row[6],
        source=row[7],
        completed=bool(row[8]),
        todo_list_id=row[9],
        todo_task_id=row[10],
    )


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path)
        self._db.executescript(SCHEMA)
        self._db.commit()
        self.orphaned_todo: List[tuple[str, str]] = []  # (list id, task id) to delete in the task app
        self._migrate_kid_ids()

    def _migrate_kid_ids(self) -> None:
        """Older versions keyed kids by Webtop's id, which changes on every login,
        so a kid could appear several times. Merge them under the stable kid_id()."""
        db = self._db
        rows = db.execute("SELECT id, name FROM kids ORDER BY rowid").fetchall()  # oldest first
        old_ids: Dict[str, List[str]] = {}
        for old, name in rows:
            if old != kid_id(name):
                old_ids.setdefault(name, []).append(old)
        for name, olds in old_ids.items():
            new = kid_id(name)
            # Tasks and topics: keep the oldest copy (it holds done / notified / task-app ids).
            for table, cols in (("tasks", "lesson_date, subject, text"), ("topics", "lesson_date, subject, text")):
                for old in olds:
                    for row_id, date, subject, text in db.execute(
                        f"SELECT id, {cols} FROM {table} WHERE kid_id = ? ORDER BY id", (old,)
                    ).fetchall():
                        key = item_key(new, dt.date.fromisoformat(date), subject, text)
                        if db.execute(f"SELECT 1 FROM {table} WHERE key = ?", (key,)).fetchone():
                            if table == "tasks":
                                self.orphaned_todo += db.execute(
                                    "SELECT todo_list_id, todo_task_id FROM tasks WHERE id = ? AND todo_task_id IS NOT NULL",
                                    (row_id,),
                                ).fetchall()
                            db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
                        else:
                            db.execute(f"UPDATE {table} SET kid_id = ?, key = ? WHERE id = ?", (new, key, row_id))
            # Lessons are a copy of Webtop: keep the newest login's.
            for old in olds[:-1]:
                db.execute("DELETE FROM lessons WHERE kid_id = ?", (old,))
            db.execute("UPDATE lessons SET kid_id = ? WHERE kid_id = ?", (new, olds[-1]))
            # Task-app list: keep the oldest.
            if not db.execute("SELECT 1 FROM todo_lists WHERE kid_id = ?", (new,)).fetchone():
                db.execute(
                    "UPDATE todo_lists SET kid_id = ? WHERE rowid = (SELECT MIN(rowid) FROM todo_lists WHERE kid_id IN (%s))"
                    % ",".join("?" * len(olds)), (new, *olds),
                )
            db.execute("DELETE FROM todo_lists WHERE kid_id IN (%s)" % ",".join("?" * len(olds)), olds)
            db.execute(
                "INSERT OR IGNORE INTO kids (id, name, class_code, class_number) "
                "SELECT ?, name, class_code, class_number FROM kids WHERE id = ?", (new, olds[-1]),
            )
            db.execute("DELETE FROM kids WHERE id IN (%s)" % ",".join("?" * len(olds)), olds)
        db.commit()

    # kids

    def upsert_kid(self, kid: Kid) -> None:
        self._db.execute(
            "INSERT INTO kids (id, name, class_code, class_number) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, class_code=excluded.class_code, "
            "class_number=excluded.class_number",
            (kid.id, kid.name, kid.class_code, kid.class_number),
        )
        self._db.commit()

    def kids(self) -> List[Kid]:
        rows = self._db.execute("SELECT id, name, class_code, class_number FROM kids ORDER BY name")
        return [Kid(*row) for row in rows]

    def hidden_kids(self, user_id: int) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT kid_id FROM hidden_kids WHERE user_id = ?", (user_id,))}

    def set_kid_hidden(self, user_id: int, kid_id: str, hidden: bool) -> None:
        if hidden:
            self._db.execute("INSERT OR IGNORE INTO hidden_kids (user_id, kid_id) VALUES (?, ?)", (user_id, kid_id))
        else:
            self._db.execute("DELETE FROM hidden_kids WHERE user_id = ? AND kid_id = ?", (user_id, kid_id))
        self._db.commit()

    # events (teacher notes)

    def add_events(self, kid_id: str, events: List[Event]) -> None:
        """Insert the kid's new events. On the kid's first fetch they count as
        already summarized, so the bot doesn't post the whole year at once."""
        first_run = self._db.execute("SELECT 1 FROM events WHERE kid_id = ? LIMIT 1", (kid_id,)).fetchone() is None
        self._db.executemany(
            "INSERT INTO events (id, kid_id, date, type, positive, subject, teacher, hour, remark, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "type=excluded.type, subject=excluded.subject, teacher=excluded.teacher, remark=excluded.remark",
            [
                (ev.id, ev.kid_id, ev.date.isoformat(), ev.type, int(ev.positive), ev.subject, ev.teacher,
                 ev.hour, ev.remark, int(first_run))
                for ev in events
            ],
        )
        self._db.commit()

    def events(self, days: List[dt.date] = (), unnotified: bool = False) -> List[Event]:
        """Events on `days`, plus (with unnotified=True) any not yet summarized."""
        marks = ",".join("?" * len(days))
        where = [f"date IN ({marks})"] if days else []
        if unnotified:
            where.append("notified = 0")
        rows = self._db.execute(
            "SELECT id, kid_id, date, type, positive, subject, teacher, hour, remark FROM events "
            f"WHERE {' OR '.join(where) or '0'} ORDER BY date, hour, id",
            tuple(d.isoformat() for d in days),
        )
        return [Event(r[0], r[1], dt.date.fromisoformat(r[2]), r[3], bool(r[4]), *r[5:]) for r in rows]

    def mark_events_notified(self, event_ids: List[int]) -> None:
        self._db.executemany("UPDATE events SET notified = 1 WHERE id = ?", [(i,) for i in event_ids])
        self._db.commit()

    # lessons (local copy of Webtop)

    def replace_lessons(self, kid: Kid, days: List[dt.date], lessons: List[Lesson]) -> None:
        """Store `lessons` as the full content of `days` for `kid`."""
        with self._db:
            self._db.executemany(
                "DELETE FROM lessons WHERE kid_id = ? AND date = ?", [(kid.id, d.isoformat()) for d in days]
            )
            self._db.executemany(
                "INSERT INTO lessons (kid_id, date, hour, subject, teacher, topic, homework, files) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (kid.id, l.date.isoformat(), l.hour, l.subject, l.teacher, l.topic, l.homework, l.files)
                    for l in lessons
                ],
            )

    def lessons(self, kid: Kid, days: List[dt.date]) -> List[Lesson]:
        marks = ",".join("?" * len(days))
        rows = self._db.execute(
            f"SELECT date, hour, subject, teacher, topic, homework, files FROM lessons "
            f"WHERE kid_id = ? AND date IN ({marks}) ORDER BY date, hour, id",
            (kid.id, *(d.isoformat() for d in days)),
        )
        return [Lesson(kid, dt.date.fromisoformat(r[0]), *r[1:]) for r in rows]

    # meta (sync bookkeeping)

    def get_meta(self, key: str) -> Optional[str]:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._db.commit()

    # tasks

    def has_tasks(self) -> bool:
        return self._db.execute("SELECT 1 FROM tasks LIMIT 1").fetchone() is not None

    def add_task(
        self,
        kid_id: str,
        lesson_date: dt.date,
        due_date: dt.date,
        subject: str,
        text: str,
        source: str,
        notified: bool = False,
    ) -> tuple[Task, bool]:
        """Insert a task unless the same one exists. Returns (task, created)."""
        key = item_key(kid_id, lesson_date, subject, text)
        cur = self._db.execute(
            "INSERT OR IGNORE INTO tasks (key, kid_id, lesson_date, due_date, subject, text, source, notified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key, kid_id, lesson_date.isoformat(), due_date.isoformat(), subject, text, source, int(notified)),
        )
        self._db.commit()
        row = self._db.execute(_TASK_SELECT + "WHERE t.key = ?", (key,)).fetchone()
        return _task(row), cur.rowcount == 1

    def task(self, task_id: int) -> Optional[Task]:
        row = self._db.execute(_TASK_SELECT + "WHERE t.id = ?", (task_id,)).fetchone()
        return _task(row) if row else None

    def task_for(self, kid_id: str, date: dt.date, subject: str, text: str) -> Optional[Task]:
        row = self._db.execute(
            _TASK_SELECT + "WHERE t.key = ?", (item_key(kid_id, date, subject, text),)
        ).fetchone()
        return _task(row) if row else None

    def open_tasks(self, due_from: Optional[dt.date] = None) -> List[Task]:
        sql = _TASK_SELECT + "WHERE t.completed = 0"
        params: tuple = ()
        if due_from:
            sql += " AND t.due_date >= ?"
            params = (due_from.isoformat(),)
        rows = self._db.execute(sql + " ORDER BY t.due_date, k.name, t.id", params)
        return [_task(r) for r in rows]

    def unnotified_tasks(self) -> List[Task]:
        rows = self._db.execute(_TASK_SELECT + "WHERE t.notified = 0 ORDER BY k.name, t.lesson_date, t.id")
        return [_task(r) for r in rows]

    def add_app_task(self, kid_id: str, today: dt.date, due_date: dt.date, text: str,
                     list_id: str, todo_task_id: str) -> Optional[Task]:
        """Import a task created in the task app. Returns it, or None if already known."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO tasks (key, kid_id, lesson_date, due_date, subject, text, source, "
            "todo_list_id, todo_task_id) VALUES (?, ?, ?, ?, '', ?, 'app', ?, ?)",
            (f"app:{todo_task_id}", kid_id, today.isoformat(), due_date.isoformat(), text, list_id, todo_task_id),
        )
        self._db.commit()
        return self.task(cur.lastrowid) if cur.rowcount == 1 else None

    def todo_task_ids(self) -> set[str]:
        return {r[0] for r in self._db.execute("SELECT todo_task_id FROM tasks WHERE todo_task_id IS NOT NULL")}

    def done_tasks_with_todo(self, due_from: dt.date) -> List[Task]:
        rows = self._db.execute(
            _TASK_SELECT + "WHERE t.completed = 1 AND t.todo_task_id IS NOT NULL AND t.due_date >= ?",
            (due_from.isoformat(),),
        )
        return [_task(r) for r in rows]

    def tasks_without_todo(self) -> List[Task]:
        rows = self._db.execute(_TASK_SELECT + "WHERE t.todo_task_id IS NULL AND t.completed = 0")
        return [_task(r) for r in rows]

    def mark_notified(self, task_ids: List[int]) -> None:
        self._db.executemany("UPDATE tasks SET notified = 1 WHERE id = ?", [(i,) for i in task_ids])
        self._db.commit()

    def set_completed(self, task_id: int, completed: bool = True) -> None:
        self._db.execute("UPDATE tasks SET completed = ? WHERE id = ?", (int(completed), task_id))
        self._db.commit()

    def set_todo_ids(self, task_id: int, list_id: str, todo_task_id: str) -> None:
        self._db.execute(
            "UPDATE tasks SET todo_list_id = ?, todo_task_id = ? WHERE id = ?", (list_id, todo_task_id, task_id)
        )
        self._db.commit()

    # users allowed besides the admins
    def users(self) -> List[tuple[int, str]]:
        return [(r[0], r[1]) for r in self._db.execute("SELECT id, name FROM users ORDER BY added_at, id")]

    def user_ids(self) -> set[int]:
        return {r[0] for r in self._db.execute("SELECT id FROM users")}

    def add_user(self, user_id: int, name: str, added_by: int) -> None:
        self._db.execute(
            "INSERT INTO users (id, name, added_by) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
            (user_id, name, added_by),
        )
        self._db.commit()

    def remove_user(self, user_id: int) -> bool:
        cur = self._db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self._db.commit()
        return cur.rowcount == 1

    # To Do lists

    def todo_lists(self) -> List[tuple[str, str]]:
        """(kid id, list id) for every kid with a task-app list."""
        return [(r[0], r[1]) for r in self._db.execute("SELECT kid_id, list_id FROM todo_lists")]

    def todo_list_id(self, kid_id: str) -> Optional[str]:
        row = self._db.execute("SELECT list_id FROM todo_lists WHERE kid_id = ?", (kid_id,)).fetchone()
        return row[0] if row else None

    def set_todo_list_id(self, kid_id: str, list_id: str) -> None:
        self._db.execute(
            "INSERT INTO todo_lists (kid_id, list_id) VALUES (?, ?) "
            "ON CONFLICT(kid_id) DO UPDATE SET list_id=excluded.list_id",
            (kid_id, list_id),
        )
        self._db.commit()
