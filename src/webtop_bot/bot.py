"""Telegram handlers, scheduled jobs and message formatting."""
from __future__ import annotations

import datetime as dt
import functools
import hashlib
import html
import logging
from typing import Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from telegram import Bot as TelegramBot
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    JobQueue,
    filters,
)

from .config import Config
from .homework import Event, Kid, Lesson, is_school_day, next_school_day, prev_school_day, week_start
from .service import Service, SyncBusy, SyncCooldown, homework_text
from .settings import OFF, POLL_OPTIONS, Settings, fmt
from .store import Task, utc_now

logger = logging.getLogger(__name__)

DAY_NAMES = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # by date.weekday()
MAX_MESSAGE = 3900  # Telegram's limit is 4096

COMMANDS = [
    BotCommand("config", "הגדרות: ילדים, שעות הסיכום והתזכורת"),
    BotCommand("help", "עזרה ומקרא סימנים"),
]
ADMIN_CONFIG = {"users", "sync_times", "todo_poll", "approve", "deny", "admin", "revoke", "revoke?"}
SCHOOL_DAYS = (0, 1, 2, 3, 4, 5)  # PTB: 0 = Sunday
EVENINGS_BEFORE_SCHOOL = (0, 1, 2, 3, 4, 6)  # Sun-Thu and Saturday
RECURRING_AT = dt.time(6, 0)  # when the day's recurring tasks are created
WEEKDAYS_SUNDAY_FIRST = (6, 0, 1, 2, 3, 4, 5)  # date.weekday() numbers
SHORT_DAYS = {6: "א׳", 0: "ב׳", 1: "ג׳", 2: "ד׳", 3: "ה׳", 4: "ו׳", 5: "ש׳"}
SCHOOL_WEEKDAYS = frozenset({6, 0, 1, 2, 3, 4})
MAX_TASK_TEXT = 200
DUE_CHOICES = 7  # days offered for a one-time task

# Persistent keyboard: button text -> command name.
MENU_BUTTONS = {
    "⬅️ אתמול": "yesterday",
    "📅 היום": "today",
    "➡️ מחר": "tomorrow",
    "🗓 השבוע": "week",
    "📋 משימות": "todo",
    "🔄 עדכון": "sync",
}
MENU = ReplyKeyboardMarkup(
    [["⬅️ אתמול", "📅 היום", "➡️ מחר"], ["🗓 השבוע", "📋 משימות"], ["🔄 עדכון"]],
    resize_keyboard=True,
    is_persistent=True,
)

Buttons = List[InlineKeyboardButton]
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
Message = Tuple[str, Buttons]


def e(text: str) -> str:
    return html.escape(text, quote=False)


def day_label(day: dt.date) -> str:
    return f"יום {DAY_NAMES[day.weekday()]} {day:%d/%m}"


def short(text: str, n: int = 24) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def keyboard(buttons: Buttons, extra_rows: Sequence[Buttons] = ()) -> Optional[InlineKeyboardMarkup]:
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)] + [list(r) for r in extra_rows]
    return InlineKeyboardMarkup(rows) if rows else None


def repeat_label(weekdays: Iterable[int]) -> str:
    days = frozenset(weekdays)
    if days == SCHOOL_WEEKDAYS:
        return "כל יום לימודים"
    if len(days) == 7:
        return "כל יום"
    return "בימים " + ", ".join(SHORT_DAYS[d] for d in WEEKDAYS_SUNDAY_FIRST if d in days)


def kid_token(kid: Kid) -> str:
    """Short stand-in for a kid id in callback data (limited to 64 bytes; Hebrew names are long)."""
    return hashlib.sha1(kid.id.encode()).hexdigest()[:10]


def done_button(task: Task, with_kid: bool = False) -> InlineKeyboardButton:
    label = task.subject or short(task.text)
    if with_kid:
        label = f"{task.kid_name.split()[0]} · {label}"
    return InlineKeyboardButton(f"✅ {label} {task.due_date:%d/%m}", callback_data=f"done:{task.id}")


def event_line(ev: Event) -> str:
    line = f"{'🌟' if ev.positive else '⚠️'} <b>{e(ev.type)}</b> {ev.date:%d/%m}"
    if ev.subject:
        line += f" · {e(ev.subject)}"
    if ev.teacher:
        line += f" ({e(ev.teacher)})"
    return line + (f" — <i>{e(ev.remark)}</i>" if ev.remark else "")


class Bot:
    def __init__(self, cfg: Config, service: Service):
        self.cfg = cfg
        self.service = service
        self.store = service.store
        self.settings = Settings(cfg, self.store)

    def allowed_ids(self) -> set[int]:
        return self.admin_ids() | self.store.user_ids()

    def admin_ids(self) -> set[int]:
        """TELEGRAM_ADMIN_IDS (permanent, so nobody can lock everyone out) plus users made admins in /config."""
        return set(self.cfg.admin_ids) | self.store.admin_user_ids()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids()

    # --- plumbing -------------------------------------------------------

    def build(self) -> Application:
        app = (
            Application.builder()
            .token(self.cfg.telegram_token)
            .connect_timeout(20)
            .read_timeout(30)
            .defaults(Defaults(parse_mode=ParseMode.HTML, tzinfo=self.cfg.tz))
            .post_init(self._post_init)
            .build()
        )
        app.add_handler(CommandHandler(["start", "help"], self.cmd_start))
        # Reached from the persistent menu buttons only (see MENU_BUTTONS), not as /commands.
        self.commands = {
            "today": self.cmd_today,
            "yesterday": self.cmd_yesterday,
            "tomorrow": self.cmd_tomorrow,
            "week": self.cmd_week,
            "todo": self.cmd_todo,
            "sync": self.cmd_sync,
        }
        app.add_handler(CommandHandler("config", self._guard(self.cmd_config)))
        app.add_handler(MessageHandler(filters.Text(list(MENU_BUTTONS)), self._guard(self.on_menu)))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._guard(self.on_text)))
        app.add_handler(CallbackQueryHandler(self._guard(self.on_button)))
        app.add_error_handler(self.on_error)

        self.schedule_jobs(app.job_queue)
        if not self.service.last_sync() or self.service.needs_resync():
            app.job_queue.run_once(self.job_sync, when=5)  # first start, or data from an older version
        return app

    def schedule_jobs(self, jq: JobQueue) -> None:
        """(Re)create the scheduled jobs from the current settings."""
        for name in ("digest", "reminder", "sync", "todo_poll", "recurring"):
            for job in jq.get_jobs_by_name(name):
                job.schedule_removal()
        st, tz = self.settings, self.cfg.tz
        # Summary and reminder times are per user: one job per time, for the users who chose it.
        for name, callback, days in (("digest", self.job_digest, SCHOOL_DAYS),
                                     ("reminder", self.job_reminder, EVENINGS_BEFORE_SCHOOL)):
            users_at: Dict[dt.time, List[int]] = {}
            for user_id in sorted(self.allowed_ids()):
                at = st.user_time(user_id, f"{name}_time")
                if at:
                    users_at.setdefault(at, []).append(user_id)
            for at, user_ids in users_at.items():
                jq.run_daily(callback, at.replace(tzinfo=tz), days=days, name=name, data=tuple(user_ids))
        jq.run_daily(self.job_recurring, RECURRING_AT.replace(tzinfo=tz), name="recurring")
        for at in st.sync_times:
            jq.run_daily(self.job_sync, at.replace(tzinfo=tz), name="sync")
        if self.service.todo and st.todo_poll_minutes:
            jq.run_repeating(self.job_todo_poll, interval=st.todo_poll_minutes * 60, first=60, name="todo_poll")

    async def _post_init(self, app: Application) -> None:
        """Register the command menu for allowed chats only; push pending tasks to the task app."""
        for chat_id in self.allowed_ids():
            await self._set_commands(app.bot, chat_id)
        await self.service.delete_orphans()
        await self.service.create_recurring_tasks()  # in case the bot was down at 06:00
        await self.service.push_to_todo()  # e.g. after switching the task app

    async def _set_commands(self, bot: TelegramBot, chat_id: int) -> None:
        try:
            await bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id))
        except Exception as ex:
            logger.warning("Could not set commands for chat %s: %s", chat_id, ex)

    async def on_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.args = []
        context.user_data.pop("new", None)  # leaving a half-finished "new task"
        await self.commands[MENU_BUTTONS[update.effective_message.text]](update, context)

    def _guard(self, handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat, user = update.effective_chat, update.effective_user
            # Both the chat and the sender must be allowed, so adding the bot to a
            # group doesn't open it to the group's other members.
            allowed = self.allowed_ids()
            if not chat or not user or chat.id not in allowed or user.id not in allowed:
                logger.warning("Rejected update from chat %s", chat.id if chat else None)
                if update.callback_query:
                    await update.callback_query.answer("Not authorized")
                elif update.effective_message:
                    await update.effective_message.reply_text("Not authorized. Send /start to see your chat id.")
                return
            if update.effective_message and not update.callback_query:
                try:  # instant "on it" before the real answer
                    await update.effective_message.set_reaction("👍")
                except Exception as ex:
                    logger.debug("Could not react: %s", ex)
            await handler(update, context)

        return wrapper

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Handler error: %s", context.error, exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(f"⚠️ שגיאה: {e(str(context.error))[:300]}")

    async def broadcast(
        self, context: ContextTypes.DEFAULT_TYPE, make: Callable[[set[str]], Optional[Message]],
        chat_ids: Optional[Iterable[int]] = None,
    ) -> None:
        """Send each chat (default: every allowed one) make(its hidden kid ids); None skips
        that chat. A private chat's id is its user's id, so each user gets their own kids."""
        for chat_id in self.allowed_ids() if chat_ids is None else chat_ids:
            message = make(self.store.hidden_kids(chat_id))
            if not message:
                continue
            try:  # one blocked / deleted chat mustn't stop the others
                await self._send_chunks(lambda t, m: context.bot.send_message(chat_id, t, reply_markup=m), *message)
            except Exception as ex:
                logger.warning("Could not send to chat %s: %s", chat_id, ex)

    async def reply(self, update: Update, text: str, buttons: Buttons = (), extra_rows: Sequence[Buttons] = ()) -> None:
        await self._send_chunks(
            lambda t, m: update.effective_message.reply_text(t, reply_markup=m), text, buttons, extra_rows
        )

    @staticmethod
    async def _send_chunks(send, text: str, buttons: Buttons, extra_rows: Sequence[Buttons] = ()) -> None:
        """Split long text on blank lines; buttons go on the last chunk."""
        chunks, current = [], ""
        for block in text.split("\n\n"):
            if current and len(current) + len(block) + 2 > MAX_MESSAGE:
                chunks.append(current)
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        chunks.append(current)
        for i, chunk in enumerate(chunks):
            await send(chunk[:4096], keyboard(list(buttons), extra_rows) if i == len(chunks) - 1 else None)

    # --- formatting -----------------------------------------------------

    def render(
        self, kid_lessons: Sequence[Tuple[Kid, List[Lesson]]], days: List[dt.date],
        hidden: set[str] = frozenset(), events: Optional[List[Event]] = None,
    ) -> Tuple[str, Buttons, List[int]]:
        """Text, buttons and the ids of the homework tasks shown, skipping hidden kids.
        `events` defaults to the teachers' notes on `days`."""
        if events is None:
            events = self.store.events(days)
        kid_lessons = [(kid, lessons) for kid, lessons in kid_lessons if kid.id not in hidden]
        if not kid_lessons and hidden:
            return "כל הילדים מוסתרים — ⚙️ /config", [], []
        parts: List[str] = []
        buttons: Buttons = []
        shown: List[int] = []
        for kid, lessons in kid_lessons:
            lines = [f"👦 <b>{e(kid.name)}</b> ({kid.class_label})"]
            for day in days:
                # Multi-day views skip lessons the teacher left empty, to stay short.
                todays = [l for l in lessons if l.date == day and (len(days) == 1 or l.topic or homework_text(l))]
                if len(days) > 1 or not todays:
                    lines.append(f"\n<u>{day_label(day)}</u>" + ("" if todays else ": אין"))
                for lesson in todays:
                    text = homework_text(lesson)
                    task = self.store.homework_task(kid.id, lesson.date, lesson.slot) if text else None
                    promoted = (
                        self.store.task_for(kid.id, lesson.date, lesson.subject, lesson.topic)
                        if lesson.topic and not task else None
                    )
                    status = task or promoted  # promoted: a topic made a task by older versions
                    # ✅ done, ⬜ open, no mark: class info only, ❓ the teacher wrote nothing
                    if status:
                        mark = "✅ " if status.completed else "⬜ "
                    else:
                        mark = "" if lesson.topic else "❓ "
                    line = f"{mark}{lesson.hour}. <b>{e(lesson.subject)}</b>"
                    twins = [l for l in todays if (l.hour, l.subject) == (lesson.hour, lesson.subject)]
                    if len(twins) > 1 and lesson.teacher:  # split class: say whose lesson
                        line += f" ({e(lesson.teacher)})"
                    if lesson.topic:
                        line += f" — {e(lesson.topic)}"
                    lines.append(line)
                    if status and status.id not in shown:  # lessons can share one task
                        shown.append(status.id)
                        if not status.completed:
                            buttons.append(done_button(status, with_kid=len(kid_lessons) > 1))
                    if task:
                        lines.append(f"    <i>{e(text)}</i>")
            kid_events = [ev for ev in events if ev.kid_id == kid.id]
            if kid_events:
                lines.append("\n<u>הערות מורים</u>")
                lines += [event_line(ev) for ev in kid_events]
            parts.append("\n".join(lines))
        return "\n\n".join(parts) or "אין נתונים", buttons, shown

    @staticmethod
    def render_schedule(kid_lessons: Sequence[Tuple[Kid, List[Lesson]]], day: dt.date, hidden: set[str]) -> str:
        """Just the day's timetable: hour and subject."""
        kid_lessons = [(kid, lessons) for kid, lessons in kid_lessons if kid.id not in hidden]
        if not kid_lessons:
            return "כל הילדים מוסתרים — ⚙️ /config" if hidden else "אין נתונים"
        parts = []
        for kid, lessons in kid_lessons:
            lines = [f"👦 <b>{e(kid.name)}</b> ({kid.class_label})"]
            lines += [f"{l.hour}. {e(l.subject)}" for l in lessons if l.date == day] or ["אין שיעורים"]
            parts.append("\n".join(lines))
        return f"<u>{day_label(day)}</u>\n\n" + "\n\n".join(parts)

    def _tasks_message(self, tasks: List[Task], title: str, hidden: set[str]) -> Optional[Message]:
        tasks = [t for t in tasks if t.kid_id not in hidden]
        return self.render_tasks(tasks, title) if tasks else None

    @staticmethod
    def render_tasks(tasks: List[Task], title: str) -> Tuple[str, Buttons]:
        lines = [f"<b>{title}</b>"]
        kid = None
        tasks = sorted(tasks, key=lambda t: (t.kid_name, t.due_date, t.id))  # one block per kid
        for task in tasks:
            if task.kid_name != kid:
                kid = task.kid_name
                lines.append(f"\n👦 <b>{e(kid)}</b>")
            lines.append(f"• {e(task.title)} <i>(ל-{task.due_date:%d/%m})</i>")
        return "\n".join(lines), [done_button(t, with_kid=True) for t in tasks]

    # --- commands -------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if chat_id not in self.allowed_ids():
            await self.reply(update, f"Your chat id: <code>{chat_id}</code>\nThe bot admin was asked to approve it.")
            await self._request_access(context, update)
            return
        text = (
            "📚 <b>בוט שיעורי בית</b>\n\n"
            "<b>כפתורים</b>\n"
            "📅 היום · ⬅️ אתמול · ➡️ מחר · 🗓 השבוע — שיעורים ושיעורי בית\n"
            "📋 משימות — משימות פתוחות, ➕ משימה חדשה (חד פעמית או קבועה), 🔁 קבועות\n"
            "🔄 עדכון — עדכון מ-Webtop (פעם ב-30 דק׳)\n"
            "✅ סימון משימה כבוצעה (↩️ ביטול)\n\n"
            "<b>סימנים</b>\n"
            "✅ משימה בוצעה\n"
            "⬜ משימה פתוחה\n"
            "בלי סימן — המורה רשם/ה רק נושא, אין שיעורי בית\n"
            "❓ המורה לא רשם/ה כלום על השיעור\n"
            "🌟 / ⚠️ הערת מורה: חיובית / שלילית\n\n"
            "/config — ילדים להצגה, שעות הסיכום והתזכורת שלך"
        )
        if self.is_admin(chat_id):
            text += "\nמנהלים: גם משתמשים ומנהלים, זמני עדכון מ-Webtop, Todoist"
        await update.effective_message.reply_text(text, reply_markup=MENU)

    async def _request_access(self, context: ContextTypes.DEFAULT_TYPE, update: Update) -> None:
        """Tell the admins (once per user per run) that someone asked for access."""
        chat, user = update.effective_chat, update.effective_user
        if chat.type != chat.PRIVATE or not user or user.id in self.settings.pending_users():
            return
        name = " ".join(filter(None, [user.first_name, user.last_name])) or str(user.id)
        self.settings.set_pending(user.id, name)
        handle = f" @{user.username}" if user.username else ""
        text = f"👤 בקשת גישה: {e(name)}{e(handle)} (<code>{user.id}</code>)"
        buttons = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ אישור", callback_data=f"cfg:approve={user.id}"),
            InlineKeyboardButton("✖ דחייה", callback_data=f"cfg:deny={user.id}"),
        ]])
        for admin in self.admin_ids():
            try:
                await context.bot.send_message(admin, text, reply_markup=buttons)
            except Exception as ex:
                logger.warning("Could not notify admin %s: %s", admin, ex)

    def freshness(self) -> str:
        last = self.service.last_sync()
        return f"🕒 עודכן {last:%d/%m %H:%M}" if last else "🕒 טרם עודכן — 🔄 עדכון"

    async def _show(self, update: Update, days: List[dt.date]) -> None:
        stored = set(self.service.stored_days())
        if not stored.issuperset(days):
            span = f"{min(stored):%d/%m}–{max(stored):%d/%m}" if stored else "—"
            await self.reply(update, f"אין נתונים שמורים לתאריך הזה (שמור: {span}).\n{self.freshness()}")
            return
        hidden = self.store.hidden_kids(update.effective_user.id)
        text, buttons, shown = self.render(self.service.view(days), days, hidden=hidden)
        self.store.mark_notified(shown)
        await self.reply(update, f"{text}\n\n{self.freshness()}", buttons)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show(update, [self.cfg.today()])

    async def cmd_yesterday(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show(update, [prev_school_day(self.cfg.today())])

    async def cmd_tomorrow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        day = next_school_day(self.cfg.today())
        if day not in self.service.stored_days():
            await self.reply(update, f"אין נתונים שמורים למחר.\n{self.freshness()}")
            return
        hidden = self.store.hidden_kids(update.effective_user.id)
        await self.reply(update, self.render_schedule(self.service.view([day]), day, hidden))

    async def cmd_week(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        today = self.cfg.today()
        sunday = week_start(today + dt.timedelta(days=1) if not is_school_day(today) else today)
        await self._show(update, [sunday + dt.timedelta(days=i) for i in range(6)])

    async def cmd_todo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._pull_from_todo(context)
        hidden = self.store.hidden_kids(update.effective_user.id)
        tasks = [t for t in self.store.open_tasks(due_from=self.cfg.today() - dt.timedelta(days=7))
                 if t.kid_id not in hidden]
        controls = [[InlineKeyboardButton("➕ משימה חדשה", callback_data="add:start"),
                     InlineKeyboardButton("🔁 קבועות", callback_data="rec:list")]]
        if not tasks:
            await self.reply(update, "🎉 אין משימות פתוחות", extra_rows=controls)
            return
        text, buttons = self.render_tasks(tasks, "📋 משימות פתוחות")
        await self.reply(update, text, buttons, controls)

    async def cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Manual Webtop update, rate limited (SYNC_COOLDOWN_MINUTES, shared by all users)."""
        try:
            self.service.check_cooldown()
        except SyncCooldown as ex:
            await self.reply(update, f"⏳ אפשר לעדכן שוב ב-{ex.retry_at:%H:%M}.\n{self.freshness()}")
            return
        try:
            new = await self.service.refresh(manual=True)
        except SyncBusy:
            await self.reply(update, "🔄 עדכון כבר רץ, נסו שוב בעוד דקה.")
            return
        except SyncCooldown as ex:
            await self.reply(update, f"⏳ אפשר לעדכן שוב ב-{ex.retry_at:%H:%M}.")
            return
        if new:
            await self._announce(context, new)
        await self.reply(update, f"✅ עודכן. שיעורי בית חדשים: {len(new)}")

    # --- admin ----------------------------------------------------------

    # --- /config (admins) ------------------------------------------------

    async def cmd_config(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, markup = self._config_screen("main", update.effective_user.id)
        await update.effective_message.reply_text(text, reply_markup=markup)

    def _config_screen(self, screen: str, user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        """A /config screen for `user_id`; admin screens only for admins."""
        st = self.settings
        btn = InlineKeyboardButton
        back = [btn("⬅️ חזרה", callback_data="cfg:main")]
        admin = self.is_admin(user_id)

        def times(name: str, options: List[str], current: set[str], allow_off: bool) -> List[List[InlineKeyboardButton]]:
            buttons = [btn(("✅ " if t in current else "") + t, callback_data=f"cfg:{name}={t}") for t in options]
            rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
            if allow_off:
                rows.append([btn(("✅ " if not current else "") + "כבוי", callback_data=f"cfg:{name}={OFF}")])
            return rows + [back]

        if screen == "kids":
            kids, hidden = self.store.kids(), self.store.hidden_kids(user_id)
            rows = [[btn(f"{'⬜' if k.id in hidden else '✅'} {k.first_name} ({k.class_label})",
                         callback_data=f"cfg:kid={kid_token(k)}")] for k in kids]
            text = "👦 <b>אילו ילדים להציג?</b> (לכל משתמש בנפרד; לחיצה מחליפה)"
            return (text if kids else "אין נתונים עדיין — לחצו 🔄 עדכון"), InlineKeyboardMarkup(rows + [back])
        if screen in ("digest_time", "reminder_time"):
            title = {"digest_time": "📚 <b>הסיכום היומי שלך</b> (א׳–ו׳)",
                     "reminder_time": "⏰ <b>תזכורת הערב שלך</b> (משימות פתוחות למחר)"}[screen]
            current = st.user_time(user_id, screen)
            options = st.time_options(screen, user_id)
            return title, InlineKeyboardMarkup(times(screen, options, {fmt(current)} if current else set(), True))
        if admin and screen == "sync_times":
            current_sync = {f"{t:%H:%M}" for t in st.sync_times}
            return ("🌐 <b>מתי לעדכן מ-Webtop</b> (לכל הבוט; לחיצה מוסיפה / מסירה)",
                    InlineKeyboardMarkup(times("sync_times", st.time_options("sync_times"), current_sync, False)))
        if admin and screen == "todo_poll":
            rows = [[btn(("✅ " if m == st.todo_poll_minutes else "") + (f"{m} דק׳" if m else "רק עם Webtop"),
                         callback_data=f"cfg:todo_poll={m}") for m in POLL_OPTIONS[i : i + 3]]
                    for i in range(0, len(POLL_OPTIONS), 3)]
            return "🔄 <b>כל כמה זמן לבדוק את Todoist</b> (לכל הבוט)", InlineKeyboardMarkup(rows + [back])
        if admin and screen == "users":
            db_admins = self.store.admin_user_ids()
            rows = [[btn(f"{'⭐' if uid in db_admins else '👤'} {name or uid}", callback_data=f"cfg:admin={uid}"),
                     btn("🚫 הסרה", callback_data=f"cfg:revoke?={uid}")]
                    for uid, name in self.store.users() if uid not in self.cfg.admin_ids]
            for uid, name in st.pending_users().items():
                rows.append([btn(f"✅ {name}", callback_data=f"cfg:approve={uid}"),
                             btn("✖ דחייה", callback_data=f"cfg:deny={uid}")])
            text = ("👥 <b>משתמשים ומנהלים</b>\n"
                    "⭐ מנהל / 👤 משתמש — לחיצה על השם מחליפה\n"
                    "🚫 הסרה מהבוט (עם אישור) · ✅ / ✖ בקשות גישה\n\n"
                    "מנהלים קבועים (TELEGRAM_ADMIN_IDS): "
                    + ", ".join(f"<code>{i}</code>" for i in sorted(self.cfg.admin_ids)))
            return text, InlineKeyboardMarkup(rows + [back])
        if admin and screen.startswith("revoke?="):
            uid = screen.partition("=")[2]
            name = dict(self.store.users()).get(int(uid)) or uid
            return f"להסיר את {e(name)} מהבוט?", InlineKeyboardMarkup([
                [btn("🚫 כן, להסיר", callback_data=f"cfg:revoke={uid}"), btn("ביטול", callback_data="cfg:users")]
            ])
        kids = self.store.kids()
        shown = len(kids) - len(self.store.hidden_kids(user_id) & {k.id for k in kids})
        rows = [
            [btn(f"👦 ילדים להצגה: {shown}/{len(kids)}", callback_data="cfg:kids")],
            [btn(f"📚 הסיכום היומי שלי: {fmt(st.user_time(user_id, 'digest_time'))}", callback_data="cfg:digest_time")],
            [btn(f"⏰ תזכורת הערב שלי: {fmt(st.user_time(user_id, 'reminder_time'))}",
                 callback_data="cfg:reminder_time")],
        ]
        if admin:
            pending = len(st.pending_users())
            rows.append([btn("👥 משתמשים ומנהלים" + (f" · {pending} ממתינים" if pending else ""),
                             callback_data="cfg:users")])
            rows.append([btn("🌐 עדכון מ-Webtop: " + (", ".join(f"{t:%H:%M}" for t in st.sync_times) or "כבוי"),
                             callback_data="cfg:sync_times")])
            if self.service.todo:
                poll = st.todo_poll_minutes
                rows.append([btn(f"🔄 סנכרון Todoist: {f'כל {poll} דק׳' if poll else 'רק עם עדכון Webtop'}",
                                 callback_data="cfg:todo_poll")])
        rows.append([btn("✖ סגירה", callback_data="cfg:close")])
        return "⚙️ <b>הגדרות</b>", InlineKeyboardMarkup(rows)

    async def _config_action(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, op: str) -> None:
        """Handle a /config button: show a screen, or change a setting and redraw."""
        name, _, value = op.partition("=")
        user_id = query.from_user.id
        st, screen, note, reschedule = self.settings, name, None, False
        if name == "close":
            await query.answer()
            await query.edit_message_text("⚙️ ההגדרות נשמרו")
            return
        if name in ADMIN_CONFIG and not self.is_admin(user_id):
            await query.answer("למנהלים בלבד")
            return
        try:  # callback data comes from the client: validate before use
            if value and name in ("digest_time", "reminder_time"):
                st.set_user_time(user_id, name, value)
                screen, note, reschedule = "main", "נשמר", True
            elif value and name == "sync_times":
                st.toggle_sync_time(value)
                note, reschedule = "נשמר", True
            elif value and name == "todo_poll":
                minutes = int(value)
                if minutes not in POLL_OPTIONS:
                    raise ValueError(value)
                st.set_todo_poll_minutes(minutes)
                screen, note, reschedule = "main", "נשמר", True
            elif name == "kid" and value:
                kid = next((k for k in self.store.kids() if kid_token(k) == value), None)
                if not kid:
                    raise ValueError(value)
                self.store.set_kid_hidden(user_id, kid.id, kid.id not in self.store.hidden_kids(user_id))
                screen = "kids"
            elif name == "approve":
                uid = int(value)
                await self._allow(context.bot, uid, st.pending_users().get(uid, ""), user_id)
                screen, note, reschedule = "users", "✅ אושר", True  # the new user gets the default times
            elif name == "deny":
                st.set_pending(int(value), None)
                screen, note = "users", "✖ נדחה"
            elif name == "admin":
                uid = int(value)
                if uid in self.cfg.admin_ids or uid not in self.store.user_ids():
                    raise ValueError(value)
                make_admin = uid not in self.store.admin_user_ids()
                self.store.set_admin(uid, make_admin)
                screen, note = "users", "⭐ מנהל" if make_admin else "👤 משתמש"
            elif name == "revoke":
                removed = await self._revoke(context.bot, int(value))
                screen, note, reschedule = "users", ("🚫 הוסר" if removed else "מנהל קבוע — מוגדר ב-.env"), removed
            elif name == "revoke?":
                screen = f"revoke?={int(value)}"
        except ValueError:
            logger.warning("Bad /config callback %r from %s", op, user_id)
            await query.answer("פעולה לא תקינה")
            return
        if reschedule:
            self.schedule_jobs(context.job_queue)
        await query.answer(note)
        text, markup = self._config_screen(screen, user_id)
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except BadRequest as ex:  # "message is not modified"
            logger.debug("Config redraw skipped: %s", ex)

    async def _allow(self, bot: TelegramBot, user_id: int, name: str, by: int) -> None:
        self.store.add_user(user_id, name, by)
        self.settings.set_pending(user_id, None)
        await self._set_commands(bot, user_id)
        try:
            await bot.send_message(user_id, "✅ קיבלת גישה לבוט. שלחו /start", reply_markup=MENU)
        except Exception as ex:  # the user never opened a chat with the bot
            logger.warning("Could not notify new user %s: %s", user_id, ex)

    async def _revoke(self, bot: TelegramBot, user_id: int) -> bool:
        """Remove a user; False if they are a permanent admin (.env) or not in the list."""
        if user_id in self.cfg.admin_ids or not self.store.remove_user(user_id):
            return False
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(user_id))
        except Exception as ex:
            logger.warning("Could not clear commands for chat %s: %s", user_id, ex)
        return True

    # --- buttons --------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        action, _, raw_id = (query.data or "").partition(":")
        if action in ("done", "undo"):
            await self._toggle_done(query, int(raw_id), done=action == "done")
            return
        if action == "add":
            await self._add_action(query, context, raw_id)
            return
        if action == "rec":
            await self._recurring_action(query, raw_id)
            return
        if action == "cfg":
            await self._config_action(query, context, raw_id)
            return
        await query.answer("הכפתור הזה כבר לא פעיל — לחצו על 📅 היום")

    # --- new task / recurring tasks ----------------------------------------

    async def _edit(self, query: CallbackQuery, text: str, rows: Sequence[Buttons] = ()) -> None:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([list(r) for r in rows]) if rows else None)
        except BadRequest as ex:  # "message is not modified"
            logger.debug("Redraw skipped: %s", ex)

    async def _add_action(self, query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, op: str) -> None:
        """➕ new task: kid → once / every school day / chosen weekdays → (date or days) → text.
        The choices live in user_data["new"] until the text arrives (see on_text)."""
        btn = InlineKeyboardButton
        name, _, value = op.partition("=")
        user_id = query.from_user.id
        cancel = [btn("✖ ביטול", callback_data="add:cancel")]
        state: Dict = context.user_data.get("new") or {}
        if name == "cancel":
            context.user_data.pop("new", None)
            await query.answer()
            await self._edit(query, "בוטל")
            return
        if name == "start":
            hidden = self.store.hidden_kids(user_id)
            kids = [k for k in self.store.kids() if k.id not in hidden]
            if not kids:
                await query.answer("אין ילדים להצגה — ⚙️ /config")
                return
            state = context.user_data["new"] = {}
            if len(kids) == 1:
                state["kid"] = kids[0].id
                name = "kid"
            else:
                await query.answer()
                rows = [[btn(f"👦 {k.first_name}", callback_data=f"add:kid={kid_token(k)}")] for k in kids]
                await self._edit(query, "➕ <b>משימה חדשה</b>\nלמי?", rows + [cancel])
                return
        if not state or (name != "kid" and "kid" not in state):
            await query.answer("פג תוקף — התחילו מחדש (📋 משימות)")
            return
        if name == "kid" and value:
            kid = next((k for k in self.store.kids() if kid_token(k) == value), None)
            if not kid:
                await query.answer("הילד לא נמצא")
                return
            state["kid"] = kid.id
        await query.answer()
        if name == "kid":
            rows = [[btn("1️⃣ פעם אחת", callback_data="add:mode=once")],
                    [btn("📆 כל יום לימודים", callback_data="add:mode=daily")],
                    [btn("🗓 בימים קבועים בשבוע", callback_data="add:mode=weekly")]]
            await self._edit(query, "➕ <b>משימה חדשה</b>\nכל כמה זמן?", rows + [cancel])
        elif name == "mode" and value == "once":
            today = self.cfg.today()
            days = [today + dt.timedelta(days=i) for i in range(DUE_CHOICES)]
            rows = [[btn(day_label(d) + (" · היום" if d == today else ""), callback_data=f"add:due={d.isoformat()}")]
                    for d in days]
            await self._edit(query, "➕ <b>משימה חדשה</b>\nלאיזה תאריך?", rows + [cancel])
        elif name == "mode" and value == "daily":
            state["weekdays"] = sorted(SCHOOL_WEEKDAYS)
            await self._ask_text(query, state)
        elif name == "mode" and value == "weekly":
            state["weekdays"] = []
            await self._edit_weekdays(query, state)
        elif name == "due":
            try:
                due = dt.date.fromisoformat(value)
            except ValueError:
                await query.answer("תאריך לא תקין")
                return
            today = self.cfg.today()
            if not today <= due < today + dt.timedelta(days=DUE_CHOICES):
                await query.answer("תאריך לא תקין")
                return
            state["due"] = due.isoformat()
            await self._ask_text(query, state)
        elif name == "day" and value.isdigit() and int(value) in SHORT_DAYS:
            days = set(state.get("weekdays", []))
            days ^= {int(value)}
            state["weekdays"] = sorted(days)
            await self._edit_weekdays(query, state)
        elif name == "days_done":
            if not state.get("weekdays"):
                await query.answer("בחרו לפחות יום אחד", show_alert=False)
                return
            await self._ask_text(query, state)
        else:
            logger.warning("Bad add callback %r from %s", op, user_id)

    async def _edit_weekdays(self, query: CallbackQuery, state: Dict) -> None:
        btn = InlineKeyboardButton
        chosen = set(state.get("weekdays", []))
        rows = [[btn(("✅ " if d in chosen else "⬜ ") + f"יום {SHORT_DAYS[d]}", callback_data=f"add:day={d}")
                 for d in WEEKDAYS_SUNDAY_FIRST[i : i + 4]] for i in range(0, 7, 4)]
        rows.append([btn("המשך ➡️", callback_data="add:days_done")])
        rows.append([btn("✖ ביטול", callback_data="add:cancel")])
        await self._edit(query, "➕ <b>משימה חדשה</b>\nאילו ימים? (לחיצה מחליפה)", rows)

    async def _ask_text(self, query: CallbackQuery, state: Dict) -> None:
        state["await_text"] = True
        await self._edit(query, "➕ <b>משימה חדשה</b>\n✏️ כתבו עכשיו את המשימה (הודעה אחת)",
                         [[InlineKeyboardButton("✖ ביטול", callback_data="add:cancel")]])

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """The text of a new task, after the ➕ choices; other messages are ignored."""
        state = context.user_data.get("new")
        if not state or not state.get("await_text"):
            return
        text = " ".join((update.effective_message.text or "").split())
        if not text or len(text) > MAX_TASK_TEXT:
            await self.reply(update, f"הטקסט צריך להיות עד {MAX_TASK_TEXT} תווים. נסו שוב.")
            return
        kid = next((k for k in self.store.kids() if k.id == state["kid"]), None)
        context.user_data.pop("new", None)
        if not kid:
            await self.reply(update, "הילד לא נמצא — התחילו מחדש (📋 משימות)")
            return
        if "weekdays" in state:
            rec = await self.service.add_recurring(kid, text, frozenset(state["weekdays"]))
            await self.reply(update, f"🔁 נוספה משימה קבועה: {e(rec.text)}\n"
                                     f"{e(kid.first_name)} · {repeat_label(rec.weekdays)}")
            return
        due = dt.date.fromisoformat(state["due"])
        task = await self.service.add_manual(kid, text, due)
        await self.reply(update, f"➕ נוסף: {e(task.text)} ({e(kid.first_name)}, ל-{due:%d/%m})", [done_button(task)])

    async def _recurring_action(self, query: CallbackQuery, op: str) -> None:
        """🔁 list of recurring tasks, each with a delete button (already created tasks stay)."""
        btn = InlineKeyboardButton
        name, _, value = op.partition("=")
        if name == "del" and value.isdigit():
            await query.answer("🗑 נמחקה" if self.store.delete_recurring(int(value)) else "כבר נמחקה")
        elif name == "close":
            await query.answer()
            await query.message.delete()
            return
        elif name != "list":
            await query.answer("הכפתור הזה כבר לא פעיל")
            return
        else:
            await query.answer()
        hidden = self.store.hidden_kids(query.from_user.id)
        recs = [r for r in self.store.recurring() if r.kid_id not in hidden]
        controls = [btn("➕ חדשה", callback_data="add:start"), btn("✖ סגירה", callback_data="rec:close")]
        if not recs:
            await self._edit(query, "🔁 <b>משימות קבועות</b>\nאין משימות קבועות.", [controls])
            return
        lines = [f"• {e(r.kid_name.split()[0])} · {repeat_label(r.weekdays)} — {e(r.text)}" for r in recs]
        rows = [[btn(f"🗑 {r.kid_name.split()[0]} · {short(r.text)}", callback_data=f"rec:del={r.id}")] for r in recs]
        await self._edit(query, "🔁 <b>משימות קבועות</b>\n" + "\n".join(lines)
                         + "\n\n🗑 מחיקה (משימות שכבר נוצרו נשארות)", rows + [controls])

    async def _toggle_done(self, query: CallbackQuery, task_id: int, done: bool) -> None:
        """Mark done / undo, swapping the pressed button between ✅ and ↩️."""
        task = await self.service.complete(task_id, completed=done)
        if not task:
            await query.answer("Task not found")
            return
        await query.answer("✅ סומן כבוצע" if done else "↩️ הסימון בוטל")

        def swap(button: InlineKeyboardButton) -> InlineKeyboardButton:
            if button.callback_data != query.data:
                return button
            label = button.text.split(" ", 1)[-1]  # drop the ✅ / ↩️ prefix
            if done:
                return InlineKeyboardButton(f"↩️ {label}", callback_data=f"undo:{task.id}")
            return InlineKeyboardButton(f"✅ {label}", callback_data=f"done:{task.id}")

        rows = query.message.reply_markup.inline_keyboard if query.message.reply_markup else ()
        await query.edit_message_reply_markup(InlineKeyboardMarkup([[swap(b) for b in row] for row in rows]))

    # --- jobs -----------------------------------------------------------

    async def _announce(self, context: ContextTypes.DEFAULT_TYPE, tasks: List[Task]) -> None:
        homework = [t for t in tasks if t.source != "app"]
        from_app = [t for t in tasks if t.source == "app"]
        for group, title in ((homework, "🆕 שיעורי בית חדשים"), (from_app, "🆕 משימות חדשות מהאפליקציה")):
            if group:
                await self.broadcast(context, lambda hidden, group=group, title=title: self._tasks_message(group, title, hidden))
        self.store.mark_notified([t.id for t in tasks])

    async def _pull_from_todo(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Sync status from the task app and announce tasks created there."""
        imported = await self.service.pull_from_todo()
        if imported:
            await self._announce(context, imported)

    async def job_todo_poll(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.service.push_to_todo()  # retries earlier failures
        await self._pull_from_todo(context)

    async def job_recurring(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.service.create_recurring_tasks()

    async def job_sync(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            new = await self.service.refresh()
        except SyncBusy:
            return
        except Exception as ex:
            logger.warning("Sync failed: %s", ex)
            await self._alert_sync_failure(context, ex)
            return
        if new:
            await self._announce(context, new)

    async def _alert_sync_failure(self, context: ContextTypes.DEFAULT_TYPE, ex: Exception) -> None:
        """Tell the admins, once until a sync succeeds again."""
        if self.store.get_meta("sync_alerted"):
            return
        self.store.set_meta("sync_alerted", "1")
        text = f"⚠️ העדכון מ-Webtop נכשל: {e(str(ex))[:300]}\n{self.freshness()}"
        for admin in self.admin_ids():
            try:
                await context.bot.send_message(admin, text)
            except Exception as err:
                logger.warning("Could not alert admin %s: %s", admin, err)

    async def job_digest(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Daily summary for the users whose time this is (context.job.data)."""
        today = self.cfg.today()
        view = self.service.view([today])
        for user_id in context.job.data:
            # Today's notes, plus notes the bot first saw since this user's last summary.
            events = self.store.events([today], added_after=self.settings.last_digest(user_id))
            sent_at = utc_now()

            def make(hidden: set[str], events: List[Event] = events) -> Optional[Message]:
                visible = [lessons for kid, lessons in view if kid.id not in hidden]
                visible_ids = {kid.id for kid, _ in view if kid.id not in hidden}
                if not any(visible) and not any(ev.kid_id in visible_ids for ev in events):
                    return None  # no school today (holiday, or nothing synced)
                text, buttons, shown = self.render(view, [today], hidden=hidden, events=events)
                self.store.mark_notified(shown)
                return f"📚 <b>סיכום {day_label(today)}</b>\n\n{text}\n\n{self.freshness()}", buttons

            await self.broadcast(context, make, chat_ids=[user_id])
            self.settings.set_last_digest(user_id, sent_at)

    async def job_reminder(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Evening reminder for the users whose time this is (context.job.data)."""
        await self._pull_from_todo(context)
        tasks = [t for t in self.store.open_tasks(due_from=self.cfg.today())
                 if t.due_date <= next_school_day(self.cfg.today())]
        if tasks:
            await self.broadcast(
                context, lambda hidden: self._tasks_message(tasks, "⏰ תזכורת: משימות פתוחות למחר", hidden),
                chat_ids=context.job.data,
            )
