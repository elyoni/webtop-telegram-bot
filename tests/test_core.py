"""Unit tests for the logic that is easy to break silently: homework task keys,
lesson merging, Webtop response checks and admin settings. Uses a temporary
SQLite file; no network."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest

from webtop_bot.homework import Event, Kid, Lesson, WebtopDataError, _checked, _merge_teachers
from webtop_bot.settings import MAX_PENDING, Settings
from webtop_bot.store import Store, utc_now

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


class TestUsersAndEvents:
    def test_user_setAdmin_listedAsAdminUntilRemoved(self, store: Store) -> None:
        # Arrange
        store.add_user(5, "Shiri", added_by=1)

        # Act
        store.set_admin(5, True)
        admins_before = store.admin_user_ids()
        store.remove_user(5)

        # Assert
        assert admins_before == {5}
        assert store.admin_user_ids() == set()

    def test_firstFetch_events_haveNoStampSoTheyAreNotLate(self, store: Store) -> None:
        # Arrange
        old = Event(1, KID.id, dt.date(2026, 9, 1), "מילה טובה", True, "English", "A", 2, "")

        # Act
        store.add_events(KID.id, [old])

        # Assert
        assert store.events(added_after="2000-01-01") == []

    def test_noteEnteredLateForEarlierDay_events_includedAfterLastDigest(self, store: Store) -> None:
        # Arrange
        store.add_events(KID.id, [Event(1, KID.id, dt.date(2026, 9, 1), "מילה טובה", True, "", "", 1, "")])
        last_digest = utc_now()
        late = Event(2, KID.id, DAY - dt.timedelta(days=3), "מילה טובה", True, "English", "A", 2, "")

        # Act
        store.add_events(KID.id, [late])

        # Assert
        assert [ev.id for ev in store.events([DAY], added_after=last_digest)] == [2]
        assert store.events([DAY], added_after=utc_now()) == []


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

    def test_userWithoutChoice_read_getsBotDefault(self, settings: Settings) -> None:
        assert settings.user_time(1, "digest_time") == dt.time(16)

    def test_oneUserTurnsDigestOff_read_otherUserUnchanged(self, settings: Settings) -> None:
        # Act
        settings.set_user_time(1, "digest_time", "off")

        # Assert
        assert settings.user_time(1, "digest_time") is None
        assert settings.user_time(2, "digest_time") == dt.time(16)

    def test_userPicksTime_read_returnsIt(self, settings: Settings) -> None:
        # Act
        settings.set_user_time(1, "reminder_time", "21:00")

        # Assert
        assert settings.user_time(1, "reminder_time") == dt.time(21)

    def test_timeNotOffered_set_raises(self, settings: Settings) -> None:
        with pytest.raises(ValueError):
            settings.set_user_time(1, "digest_time", "03:17")

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


class TestRecurring:
    MONDAY = dt.date(2026, 10, 19)  # weekday() == 0
    TUESDAY = dt.date(2026, 10, 20)

    def test_weeklyOnMonday_materializeMondayAndTuesday_createsOnlyMonday(self, store: Store) -> None:
        # Arrange
        store.add_recurring(KID.id, "violin", frozenset({0}))

        # Act
        monday = store.materialize_recurring(self.MONDAY)
        tuesday = store.materialize_recurring(self.TUESDAY)

        # Assert
        assert [(t.text, t.due_date, t.source) for t in monday] == [("violin", self.MONDAY, "recurring")]
        assert tuesday == []

    def test_sameDay_materializeTwice_createsOnce(self, store: Store) -> None:
        # Arrange
        store.add_recurring(KID.id, "violin", frozenset({0}))
        store.materialize_recurring(self.MONDAY)

        # Act
        again = store.materialize_recurring(self.MONDAY)

        # Assert
        assert again == []
        assert len(store.open_tasks()) == 1

    def test_deletedRecurring_materialize_createsNothing(self, store: Store) -> None:
        # Arrange
        rec = store.add_recurring(KID.id, "violin", frozenset({0}))
        store.delete_recurring(rec.id)

        # Act / Assert
        assert store.materialize_recurring(self.MONDAY) == []

    def test_doneTask_nextWeek_createsNewOpenTask(self, store: Store) -> None:
        # Arrange
        store.add_recurring(KID.id, "violin", frozenset({0}))
        store.set_completed(store.materialize_recurring(self.MONDAY)[0].id)

        # Act
        store.materialize_recurring(self.MONDAY + dt.timedelta(days=7))

        # Assert
        assert [t.due_date for t in store.open_tasks()] == [self.MONDAY + dt.timedelta(days=7)]


class TestAddTaskFlow:
    @staticmethod
    def make_bot(store: Store):
        from webtop_bot.bot import Bot

        bot = Bot.__new__(Bot)
        bot.store = store
        bot.cfg = SimpleNamespace(today=lambda: DAY)
        return bot

    @staticmethod
    def query():
        edits = []

        async def edit_message_text(text, reply_markup=None):
            edits.append((text, reply_markup))

        async def answer(text=None, **_):
            edits.append(("answer", text))

        return SimpleNamespace(
            from_user=SimpleNamespace(id=1), edit_message_text=edit_message_text, answer=answer, edits=edits
        )

    def run(self, bot, ctx, query, op: str) -> None:
        import asyncio

        asyncio.run(bot._add_action(query, ctx, op))

    def test_oneKid_start_skipsKidPickerAndAsksRepeat(self, store: Store) -> None:
        # Arrange
        bot, ctx, query = self.make_bot(store), SimpleNamespace(user_data={}), self.query()

        # Act
        self.run(bot, ctx, query, "start")

        # Assert
        assert ctx.user_data["new"]["kid"] == KID.id
        assert "כל כמה זמן" in query.edits[-1][0]

    def test_weekly_toggleDays_storesChosenWeekdays(self, store: Store) -> None:
        # Arrange
        bot, ctx, query = self.make_bot(store), SimpleNamespace(user_data={}), self.query()
        for op in ("start", "mode=weekly", "day=6", "day=2", "day=2", "day=0"):
            self.run(bot, ctx, query, op)

        # Act
        self.run(bot, ctx, query, "days_done")

        # Assert
        assert ctx.user_data["new"]["weekdays"] == [0, 6]
        assert ctx.user_data["new"]["await_text"] is True

    def test_noState_modeButton_asksToRestart(self, store: Store) -> None:
        # Arrange
        bot, ctx, query = self.make_bot(store), SimpleNamespace(user_data={}), self.query()

        # Act
        self.run(bot, ctx, query, "mode=daily")

        # Assert
        assert "פג תוקף" in query.edits[-1][1]

    def test_dueOutsideChoices_due_isRejected(self, store: Store) -> None:
        # Arrange
        bot, ctx, query = self.make_bot(store), SimpleNamespace(user_data={}), self.query()
        for op in ("start", "mode=once"):
            self.run(bot, ctx, query, op)

        # Act
        self.run(bot, ctx, query, "due=2030-01-01")

        # Assert
        assert "await_text" not in ctx.user_data["new"]


def test_repeatLabel_variousDays_readable() -> None:
    from webtop_bot.bot import SCHOOL_WEEKDAYS, repeat_label

    assert repeat_label(SCHOOL_WEEKDAYS) == "כל יום לימודים"
    assert repeat_label(range(7)) == "כל יום"
    assert repeat_label({1, 6}) == "בימים א׳, ג׳"


class TestActivities:
    MONDAY = dt.date(2026, 10, 19)

    def test_weeklyAndOneTime_activitiesOn_matchTheirDays(self, store: Store) -> None:
        # Arrange
        store.add_activity(KID.id, "football", frozenset({0}), None)
        store.add_activity(KID.id, "birthday", frozenset(), dt.date(2026, 10, 20))

        # Act / Assert
        assert [a.title for a in store.activities_on(self.MONDAY)] == ["football"]
        assert [a.title for a in store.activities_on(dt.date(2026, 10, 20))] == ["birthday"]
        assert store.activities_on(dt.date(2026, 10, 21)) == []

    def test_pastOneTime_purge_keepsWeeklyAndFuture(self, store: Store) -> None:
        # Arrange
        store.add_activity(KID.id, "old", frozenset(), dt.date(2026, 10, 1))
        store.add_activity(KID.id, "soon", frozenset(), dt.date(2026, 10, 25))
        store.add_activity(KID.id, "football", frozenset({0}), None)

        # Act
        store.purge_activities_before(self.MONDAY)

        # Assert
        assert sorted(a.title for a in store.activities(self.MONDAY)) == ["football", "soon"]


class TestReminders:
    def reminders(self, store: Store, day: dt.date):
        from webtop_bot.service import Service

        return Service(SimpleNamespace(), store, None).reminders_for(day)

    def test_lessonMatchesRule_remindersFor_returnsNote(self, store: Store) -> None:
        # Arrange
        store.replace_lessons(KID, [DAY], [Lesson(KID, DAY, 1, "חנג", "T", "", "", 0, 7)])
        store.add_reminder_rule(None, "חנג", "להביא נעליים")

        # Act
        found = self.reminders(store, DAY)

        # Assert
        assert [(r.kid_id, r.matched, r.note) for r in found] == [(KID.id, "חנג", "להביא נעליים")]

    def test_activityMatchesRule_remindersFor_returnsNote(self, store: Store) -> None:
        # Arrange
        store.add_activity(KID.id, "כדורגל 16:00", frozenset({DAY.weekday()}), None)
        store.add_reminder_rule(KID.id, "כדורגל", "בקבוק מים")

        # Act / Assert
        assert [r.note for r in self.reminders(store, DAY)] == ["בקבוק מים"]

    def test_ruleForOtherKid_remindersFor_isSkipped(self, store: Store) -> None:
        # Arrange
        store.replace_lessons(KID, [DAY], [Lesson(KID, DAY, 1, "חנג", "T", "", "", 0, 7)])
        store.add_reminder_rule("name:Other", "חנג", "להביא נעליים")

        # Act / Assert
        assert self.reminders(store, DAY) == []

    def test_noMatchingLesson_remindersFor_isEmpty(self, store: Store) -> None:
        # Arrange
        store.replace_lessons(KID, [DAY], [Lesson(KID, DAY, 1, "מתמטיקה", "T", "", "", 0, 7)])
        store.add_reminder_rule(None, "חנג", "להביא נעליים")

        # Act / Assert
        assert self.reminders(store, DAY) == []


class TestEventKinds:
    def add(self, store: Store, type_: str, positive: bool, event_id: int) -> None:
        store.add_events(KID.id, [Event(event_id, KID.id, DAY, type_, positive, "", "", 1, "")])

    def test_unchosenTypes_eventTypes_praiseIsGoodOthersNeutral(self, store: Store) -> None:
        # Arrange
        self.add(store, "מילה טובה", True, 1)
        self.add(store, "איחור", False, 2)

        # Act / Assert
        assert store.event_types() == [("איחור", "neutral"), ("מילה טובה", "good")]

    def test_chosenKind_eventTypes_overridesDefault(self, store: Store) -> None:
        # Arrange
        self.add(store, "איחור", False, 2)

        # Act
        store.set_event_kind("איחור", "bad")

        # Assert
        assert store.event_types() == [("איחור", "bad")]
        assert store.event_kinds() == {"איחור": "bad"}

    def test_unknownKind_setEventKind_raises(self, store: Store) -> None:
        with pytest.raises(ValueError):
            store.set_event_kind("איחור", "awful")

    def test_eventKind_choiceBeatsWebtopFlag(self) -> None:
        from webtop_bot.bot import event_kind

        ev = Event(1, KID.id, DAY, "מילה טובה", True, "", "", 1, "")
        assert event_kind(ev, {}) == "good"
        assert event_kind(ev, {"מילה טובה": "neutral"}) == "neutral"
