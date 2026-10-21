"""Unit tests for the logic that is easy to break silently: homework task keys,
lesson merging, Webtop response checks and admin settings. Uses a temporary
SQLite file; no network."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from webtop_bot.homework import Kid, Lesson, WebtopDataError, _checked, _merge_teachers
from webtop_bot.settings import MAX_PENDING, Settings
from webtop_bot.store import Store

DAY = dt.date(2026, 10, 20)
DUE = dt.date(2026, 10, 21)
KID = Kid(id="name:Yoav", name="Yoav", class_code=4, class_number=3)


@pytest.fixture
def store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "bot.db")
    store.upsert_kid(KID)
    return store


def lesson(hour: int, teacher: str, topic: str = "", homework: str = "", group: int = 7) -> Lesson:
    return Lesson(KID, DAY, hour, "English", teacher, topic, homework, 0, group)


class TestUpsertHomework:
    def test_newSlot_upsert_createsUnnotifiedTask(self, store: Store) -> None:
        # Act
        store.upsert_homework(KID.id, DAY, DUE, "group:7", "English", "page 5", notified=False)

        # Assert
        task = store.homework_task(KID.id, DAY, "group:7")
        assert (task.text, task.completed) == ("page 5", False)
        assert [t.id for t in store.unnotified_tasks()] == [task.id]

    def test_sameText_upsertAgain_changesNothing(self, store: Store) -> None:
        # Arrange
        store.upsert_homework(KID.id, DAY, DUE, "group:7", "English", "page 5", notified=True)

        # Act
        store.upsert_homework(KID.id, DAY, DUE, "group:7", "English", "page 5", notified=False)

        # Assert
        assert store.unnotified_tasks() == []
        assert store.stale_tasks() == []

    def test_openTask_teacherEditsText_updatesSameTaskAndReannounces(self, store: Store) -> None:
        # Arrange
        store.upsert_homework(KID.id, DAY, DUE, "group:7", "English", "page 5", notified=True)
        task_id = store.homework_task(KID.id, DAY, "group:7").id
        store.set_todo_ids(task_id, "list", "todo-1")

        # Act
        store.upsert_homework(KID.id, DAY, DUE, "group:7", "English", "pages 5-6", notified=False)

        # Assert
        task = store.homework_task(KID.id, DAY, "group:7")
        assert (task.id, task.text) == (task_id, "pages 5-6")
        assert [t.id for t in store.unnotified_tasks()] == [task_id]
        assert [t.id for t in store.stale_tasks()] == [task_id]

    def test_doneTask_teacherEditsText_staysDoneAndQuiet(self, store: Store) -> None:
        # Arrange
        store.upsert_homework(KID.id, DAY, DUE, "group:7", "English", "page 5", notified=True)
        task_id = store.homework_task(KID.id, DAY, "group:7").id
        store.set_completed(task_id)

        # Act
        store.upsert_homework(KID.id, DAY, DUE, "group:7", "English", "pages 5-6", notified=False)

        # Assert
        assert store.homework_task(KID.id, DAY, "group:7").completed
        assert store.unnotified_tasks() == []

    def test_textKeyedTaskFromOlderVersion_upsert_adoptsItInsteadOfDuplicating(self, store: Store) -> None:
        # Arrange: a task stored under the old key (kid, date, subject, text)
        old, _ = store.add_task(KID.id, DAY, DUE, "English", "page 5", source="homework", notified=True)
        store.set_completed(old.id)

        # Act
        store.upsert_homework(KID.id, DAY, DUE, "group:7", "English", "page 5", notified=False)

        # Assert
        task = store.homework_task(KID.id, DAY, "group:7")
        assert (task.id, task.completed) == (old.id, True)
        assert store.task_for(KID.id, DAY, "English", "page 5") is None


class TestMergeTeachers:
    def test_copyPerTeacher_oneFilled_mergesIntoOneLessonWithBothTeachers(self) -> None:
        # Act
        merged = _merge_teachers([lesson(2, "A"), lesson(2, "B", topic="reading")])

        # Assert
        assert [(m.teacher, m.topic) for m in merged] == [("A / B", "reading")]

    def test_copyPerTeacher_differentContent_keepsBoth(self) -> None:
        # Act
        merged = _merge_teachers([lesson(2, "A", topic="x"), lesson(2, "B", topic="y")])

        # Assert
        assert [(m.teacher, m.topic) for m in merged] == [("A", "x"), ("B", "y")]

    def test_differentHours_merge_keepsEachHour(self) -> None:
        # Act
        merged = _merge_teachers([lesson(2, "A"), lesson(3, "A")])

        # Assert
        assert [m.hour for m in merged] == [2, 3]


class TestChecked:
    def test_statusFalse_check_raisesWithWebtopMessage(self) -> None:
        with pytest.raises(WebtopDataError, match="boom"):
            _checked({"status": False, "errorDescription": "boom"}, "lessons")

    def test_notADict_check_raises(self) -> None:
        with pytest.raises(WebtopDataError):
            _checked(None, "lessons")

    def test_statusTrueWithoutData_check_returnsNone(self) -> None:
        # A vacation week: Webtop succeeded, there is just nothing in it.
        assert _checked({"status": True, "data": None}, "lessons") is None


class TestSettings:
    @pytest.fixture
    def settings(self, store: Store) -> Settings:
        cfg = SimpleNamespace(
            todo_poll_minutes=10, digest_time=dt.time(16), reminder_time=dt.time(20),
            sync_times=(dt.time(7), dt.time(15, 30)),
        )
        return Settings(cfg, store)

    def test_noOverride_read_usesEnvDefaults(self, settings: Settings) -> None:
        assert (settings.digest_time, settings.todo_poll_minutes) == (dt.time(16), 10)

    def test_digestOff_read_returnsNone(self, settings: Settings) -> None:
        # Act
        settings.set_time("digest_time", "off")

        # Assert
        assert settings.digest_time is None

    def test_timeNotOffered_set_raises(self, settings: Settings) -> None:
        with pytest.raises(ValueError):
            settings.set_time("digest_time", "03:17")

    def test_syncTime_toggleTwice_restoresOriginal(self, settings: Settings) -> None:
        # Act
        settings.toggle_sync_time("19:00")
        added = settings.sync_times
        settings.toggle_sync_time("19:00")

        # Assert
        assert dt.time(19) in added
        assert settings.sync_times == (dt.time(7), dt.time(15, 30))

    def test_manyAccessRequests_add_keepsOnlyNewest(self, settings: Settings) -> None:
        # Act
        for user_id in range(MAX_PENDING + 5):
            settings.set_pending(user_id, f"user {user_id}")

        # Assert
        assert list(settings.pending_users()) == list(range(5, MAX_PENDING + 5))
