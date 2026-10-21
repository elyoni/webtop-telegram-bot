"""Telegram handlers, scheduled jobs and message formatting."""
from __future__ import annotations

import datetime as dt
import functools
import html
import logging
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from .config import Config
from .homework import Event, Kid, Lesson, is_school_day, next_school_day, prev_school_day, week_start
from .service import Service, SyncBusy, SyncCooldown, homework_text
from .store import Task

logger = logging.getLogger(__name__)

DAY_NAMES = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # by date.weekday()
MAX_MESSAGE = 3900  # Telegram's limit is 4096

COMMANDS = [
    BotCommand("today", "שיעורי בית היום"),
    BotCommand("yesterday", "יום הלימודים הקודם"),
    BotCommand("tomorrow", "יום הלימודים הבא"),
    BotCommand("week", "השבוע"),
    BotCommand("date", "תאריך: /date YYYY-MM-DD"),
    BotCommand("todo", "משימות פתוחות"),
    BotCommand("add", "הוספת משימה: /add <ילד> <טקסט> [YYYY-MM-DD]"),
    BotCommand("sync", "עדכון מ-Webtop"),
    BotCommand("kids", "בחירת ילדים להצגה"),
    BotCommand("help", "עזרה ומקרא סימנים"),
]
ADMIN_COMMANDS = COMMANDS + [
    BotCommand("users", "משתמשים מורשים"),
    BotCommand("allow", "הרשאה: /allow <id> [שם]"),
    BotCommand("revoke", "הסרת הרשאה: /revoke <id>"),
]

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


def keyboard(buttons: Buttons) -> Optional[InlineKeyboardMarkup]:
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons[i : i + 2] for i in range(0, len(buttons), 2)])


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


def kids_keyboard(kids: List[Kid], hidden: set[str]) -> InlineKeyboardMarkup:
    """One toggle per kid: ✅ shown, ⬜ hidden."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{'⬜' if k.id in hidden else '✅'} {k.first_name} ({k.class_label})",
                              callback_data=f"kid:{k.id}")]
        for k in kids
    ])


class Bot:
    def __init__(self, cfg: Config, service: Service):
        self.cfg = cfg
        self.service = service
        self.store = service.store
        self._access_requests: set[int] = set()  # users already reported to the admins

    def allowed_ids(self) -> set[int]:
        return set(self.cfg.admin_ids) | self.store.user_ids()

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.cfg.admin_ids

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
        self.commands = {
            "today": self.cmd_today,
            "yesterday": self.cmd_yesterday,
            "tomorrow": self.cmd_tomorrow,
            "week": self.cmd_week,
            "date": self.cmd_date,
            "todo": self.cmd_todo,
            "add": self.cmd_add,
            "sync": self.cmd_sync,
            "kids": self.cmd_kids,
        }
        for name, handler in self.commands.items():
            app.add_handler(CommandHandler(name, self._guard(handler)))
        for name, handler in {"users": self.cmd_users, "allow": self.cmd_allow, "revoke": self.cmd_revoke}.items():
            app.add_handler(CommandHandler(name, self._guard(handler, admin=True)))
        app.add_handler(MessageHandler(filters.Text(list(MENU_BUTTONS)), self._guard(self.on_menu)))
        app.add_handler(CallbackQueryHandler(self._guard(self.on_button)))
        app.add_error_handler(self.on_error)

        jq = app.job_queue
        school_days = (0, 1, 2, 3, 4, 5)  # PTB: 0 = Sunday
        jq.run_daily(self.job_digest, self.cfg.digest_time.replace(tzinfo=self.cfg.tz), days=school_days)
        # Evening before a school day: Sun-Thu and Saturday.
        jq.run_daily(self.job_reminder, self.cfg.reminder_time.replace(tzinfo=self.cfg.tz), days=(0, 1, 2, 3, 4, 6))
        for at in self.cfg.sync_times:
            jq.run_daily(self.job_sync, at.replace(tzinfo=self.cfg.tz))
        if self.service.todo and self.cfg.todo_poll_minutes:
            jq.run_repeating(self.job_todo_poll, interval=self.cfg.todo_poll_minutes * 60, first=60)
        if not self.service.last_sync():
            jq.run_once(self.job_sync, when=5)  # first start: fill the db
        return app

    async def _post_init(self, app: Application) -> None:
        """Register the command menu for allowed chats only; push pending tasks to the task app."""
        for chat_id in self.allowed_ids():
            await self._set_commands(app.bot, chat_id)
        await self.service.delete_orphans()
        await self.service.push_to_todo()  # e.g. after switching the task app

    async def _set_commands(self, bot, chat_id: int) -> None:
        try:
            commands = ADMIN_COMMANDS if self.is_admin(chat_id) else COMMANDS
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id))
        except Exception as ex:
            logger.warning("Could not set commands for chat %s: %s", chat_id, ex)

    async def on_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.args = []
        await self.commands[MENU_BUTTONS[update.effective_message.text]](update, context)

    def _guard(self, handler: Handler, admin: bool = False) -> Handler:
        @functools.wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat, user = update.effective_chat, update.effective_user
            # Both the chat and the sender must be allowed, so adding the bot to a
            # group doesn't open it to the group's other members.
            allowed = set(self.cfg.admin_ids) if admin else self.allowed_ids()
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
        self, context: ContextTypes.DEFAULT_TYPE, make: Callable[[set[str]], Optional[Message]]
    ) -> None:
        """Send each allowed chat make(its hidden kid ids); None skips that chat.
        A private chat's id is its user's id, so each user gets their own kids."""
        for chat_id in self.allowed_ids():
            message = make(self.store.hidden_kids(chat_id))
            if message:
                await self._send_chunks(lambda t, m: context.bot.send_message(chat_id, t, reply_markup=m), *message)

    async def reply(self, update: Update, text: str, buttons: Buttons = ()) -> None:
        await self._send_chunks(lambda t, m: update.effective_message.reply_text(t, reply_markup=m), text, buttons)

    @staticmethod
    async def _send_chunks(send, text: str, buttons: Buttons) -> None:
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
            await send(chunk[:4096], keyboard(list(buttons)) if i == len(chunks) - 1 else None)

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
            return "כל הילדים מוסתרים — /kids", [], []
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
                    task = self.store.task_for(kid.id, lesson.date, lesson.subject, text) if text else None
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
            return "כל הילדים מוסתרים — /kids" if hidden else "אין נתונים"
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
            "<b>פקודות</b>\n"
            "/today — שיעורי היום ושיעורי הבית\n"
            "/yesterday — יום הלימודים הקודם (להוסיף משימות ששכחנו)\n"
            "/tomorrow — מערכת השעות של מחר\n"
            "/week — השבוע\n"
            "/date YYYY-MM-DD — יום מסוים\n"
            "/todo — משימות פתוחות\n"
            "/add &lt;ילד&gt; &lt;טקסט&gt; [YYYY-MM-DD] — הוספת משימה\n"
            "/kids — בחירת ילדים להצגה (לכל משתמש בנפרד)\n"
            "/sync — עדכון מ-Webtop (פעם ב-30 דק׳)\n"
            "/help — העזרה הזו\n\n"
            "<b>סימנים</b>\n"
            "✅ משימה בוצעה\n"
            "⬜ משימה פתוחה\n"
            "בלי סימן — המורה רשם/ה רק נושא, אין שיעורי בית\n"
            "❓ המורה לא רשם/ה כלום על השיעור\n"
            "🌟 / ⚠️ הערת מורה: חיובית / שלילית\n\n"
            "<b>כפתורים</b>\n"
            "✅ סימון משימה כבוצעה (↩️ ביטול)\n"
            "שכחנו משהו? /add"
        )
        if self.is_admin(chat_id):
            text += "\n\n<b>ניהול</b>\n/users\n/allow &lt;id&gt; [שם]\n/revoke &lt;id&gt;"
        await update.effective_message.reply_text(text, reply_markup=MENU)

    async def _request_access(self, context: ContextTypes.DEFAULT_TYPE, update: Update) -> None:
        """Tell the admins (once per user per run) that someone asked for access."""
        chat, user = update.effective_chat, update.effective_user
        if chat.type != chat.PRIVATE or not user or user.id in self._access_requests:
            return
        self._access_requests.add(user.id)
        name = " ".join(filter(None, [user.first_name, user.last_name]))
        handle = f" @{user.username}" if user.username else ""
        text = f"👤 בקשת גישה: {e(name)}{e(handle)} (<code>{user.id}</code>)\n/allow {user.id} {e(name)}"
        for admin in self.cfg.admin_ids:
            try:
                await context.bot.send_message(admin, text)
            except Exception as ex:
                logger.warning("Could not notify admin %s: %s", admin, ex)

    def freshness(self) -> str:
        last = self.service.last_sync()
        return f"🕒 עודכן {last:%d/%m %H:%M}" if last else "🕒 טרם עודכן — /sync"

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

    async def cmd_date(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            day = dt.date.fromisoformat(context.args[0])
        except (IndexError, ValueError):
            await self.reply(update, "Usage: /date YYYY-MM-DD")
            return
        await self._show(update, [day])

    async def cmd_todo(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._pull_from_todo(context)
        hidden = self.store.hidden_kids(update.effective_user.id)
        tasks = [t for t in self.store.open_tasks(due_from=self.cfg.today() - dt.timedelta(days=7))
                 if t.kid_id not in hidden]
        if not tasks:
            await self.reply(update, "🎉 אין משימות פתוחות")
            return
        await self.reply(update, *self.render_tasks(tasks, "📋 משימות פתוחות"))

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        args = list(context.args)
        if len(args) < 2:
            await self.reply(update, "Usage: /add &lt;kid&gt; &lt;text&gt; [YYYY-MM-DD]")
            return
        matches = [k for k in self.store.kids() if args[0] in k.name]
        if len(matches) != 1:
            names = ", ".join(e(k.first_name) for k in self.store.kids())
            await self.reply(update, f"Kid {e(args[0])!r} not found or ambiguous. Kids: {names}")
            return
        due = next_school_day(self.cfg.today())
        try:
            due = dt.date.fromisoformat(args[-1])
            args = args[:-1]
        except ValueError:
            pass
        task = await self.service.add_manual(matches[0], " ".join(args[1:]), due)
        await self.reply(update, f"➕ נוסף: {e(task.text)} ({e(matches[0].first_name)}, ל-{due:%d/%m})", [done_button(task)])

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

    async def cmd_kids(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        kids = self.store.kids()
        if not kids:
            await self.reply(update, "אין נתונים עדיין — /sync")
            return
        hidden = self.store.hidden_kids(update.effective_user.id)
        await update.effective_message.reply_text(
            "👦 <b>אילו ילדים להציג?</b> (לחיצה מחליפה)", reply_markup=kids_keyboard(kids, hidden)
        )

    # --- admin ----------------------------------------------------------

    async def cmd_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        lines = ["<b>מנהלים</b>"] + [f"• <code>{i}</code>" for i in sorted(self.cfg.admin_ids)]
        lines += ["", "<b>משתמשים</b>"]
        lines += [f"• <code>{i}</code> {e(name)}" for i, name in self.store.users()] or ["אין"]
        await self.reply(update, "\n".join(lines))

    async def cmd_allow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            user_id = int(context.args[0])
        except (IndexError, ValueError):
            await self.reply(update, "Usage: /allow &lt;id&gt; [name]")
            return
        name = " ".join(context.args[1:])
        self.store.add_user(user_id, name, update.effective_user.id)
        self._access_requests.discard(user_id)
        await self._set_commands(context.bot, user_id)
        await self.reply(update, f"✅ <code>{user_id}</code> {e(name)} מורשה")
        try:
            await context.bot.send_message(user_id, "✅ קיבלת גישה לבוט. שלחו /start", reply_markup=MENU)
        except Exception as ex:  # the user never opened a chat with the bot
            logger.warning("Could not notify new user %s: %s", user_id, ex)

    async def cmd_revoke(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            user_id = int(context.args[0])
        except (IndexError, ValueError):
            await self.reply(update, "Usage: /revoke &lt;id&gt;")
            return
        if self.is_admin(user_id):
            await self.reply(update, "Admins are set in TELEGRAM_ADMIN_IDS, not in the db.")
            return
        if not self.store.remove_user(user_id):
            await self.reply(update, f"<code>{user_id}</code> לא ברשימה")
            return
        try:
            await context.bot.delete_my_commands(scope=BotCommandScopeChat(user_id))
        except Exception as ex:
            logger.warning("Could not clear commands for chat %s: %s", user_id, ex)
        await self.reply(update, f"🚫 <code>{user_id}</code> הוסר")

    # --- buttons --------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        action, _, raw_id = (query.data or "").partition(":")
        if action in ("done", "undo"):
            await self._toggle_done(query, int(raw_id), done=action == "done")
            return
        if action == "kid":
            await self._toggle_kid(query, raw_id)
            return
        await query.answer("הכפתור הזה כבר לא פעיל — /today")

    async def _toggle_kid(self, query, kid_id: str) -> None:
        """Show / hide a kid for the user who pressed, and redraw the /kids toggles."""
        user_id = query.from_user.id
        kids = self.store.kids()
        kid = next((k for k in kids if k.id == kid_id), None)
        if not kid:
            await query.answer("Kid not found")
            return
        hide = kid_id not in self.store.hidden_kids(user_id)
        self.store.set_kid_hidden(user_id, kid_id, hide)
        await query.answer(f"{'⬜ הוסתר' if hide else '✅ מוצג'}: {kid.first_name}")
        await query.edit_message_reply_markup(kids_keyboard(kids, self.store.hidden_kids(user_id)))

    async def _toggle_done(self, query, task_id: int, done: bool) -> None:
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

    async def job_sync(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            new = await self.service.refresh()
        except SyncBusy:
            return
        except Exception as ex:
            logger.warning("Sync failed: %s", ex)
            return
        if new:
            await self._announce(context, new)

    async def job_digest(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        today = self.cfg.today()
        view = self.service.view([today])
        events = self.store.events([today], unnotified=True)  # also notes entered late

        def make(hidden: set[str]) -> Optional[Message]:
            if all(kid.id in hidden for kid, _ in view):
                return None
            text, buttons, shown = self.render(view, [today], hidden=hidden, events=events)
            self.store.mark_notified(shown)
            return f"📚 <b>סיכום {day_label(today)}</b>\n\n{text}\n\n{self.freshness()}", buttons

        await self.broadcast(context, make)
        self.store.mark_events_notified([ev.id for ev in events])

    async def job_reminder(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._pull_from_todo(context)
        tasks = [t for t in self.store.open_tasks(due_from=self.cfg.today())
                 if t.due_date <= next_school_day(self.cfg.today())]
        if tasks:
            await self.broadcast(context, lambda hidden: self._tasks_message(tasks, "⏰ תזכורת: משימות פתוחות למחר", hidden))
