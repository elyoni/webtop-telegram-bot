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
from .homework import Kid, Lesson, is_school_day, next_school_day, week_start
from .service import Service, SyncBusy, SyncCooldown, homework_text
from .store import Task

logger = logging.getLogger(__name__)

DAY_NAMES = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # by date.weekday()
MAX_MESSAGE = 3900  # Telegram's limit is 4096

COMMANDS = [
    BotCommand("today", "שיעורי בית היום"),
    BotCommand("tomorrow", "יום הלימודים הבא"),
    BotCommand("week", "השבוע"),
    BotCommand("date", "תאריך: /date YYYY-MM-DD"),
    BotCommand("todo", "משימות פתוחות"),
    BotCommand("add", "הוספת משימה: /add <ילד> <טקסט> [YYYY-MM-DD]"),
    BotCommand("sync", "עדכון מ-Webtop"),
    BotCommand("kids", "הילדים"),
    BotCommand("help", "עזרה"),
]

# Persistent keyboard: button text -> command name.
MENU_BUTTONS = {
    "📅 היום": "today",
    "➡️ מחר": "tomorrow",
    "🗓 השבוע": "week",
    "📋 משימות": "todo",
    "🔄 עדכון": "sync",
}
MENU = ReplyKeyboardMarkup(
    [["📅 היום", "➡️ מחר"], ["🗓 השבוע", "📋 משימות"], ["🔄 עדכון"]],
    resize_keyboard=True,
    is_persistent=True,
)

Buttons = List[InlineKeyboardButton]
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


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


class Bot:
    def __init__(self, cfg: Config, service: Service):
        self.cfg = cfg
        self.service = service
        self.store = service.store

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
        if not self.service.last_sync():
            jq.run_once(self.job_sync, when=5)  # first start: fill the db
        return app

    async def _post_init(self, app: Application) -> None:
        """Register the command menu for allowed chats only."""
        for chat_id in self.cfg.allowed_chat_ids:
            try:
                await app.bot.set_my_commands(COMMANDS, scope=BotCommandScopeChat(chat_id))
            except Exception as ex:
                logger.warning("Could not set commands for chat %s: %s", chat_id, ex)

    async def on_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.args = []
        await self.commands[MENU_BUTTONS[update.effective_message.text]](update, context)

    def _guard(self, handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            chat, user = update.effective_chat, update.effective_user
            # Both the chat and the sender must be allowed, so adding the bot to a
            # group doesn't open it to the group's other members.
            allowed = self.cfg.allowed_chat_ids
            if not chat or not user or chat.id not in allowed or user.id not in allowed:
                logger.warning("Rejected update from chat %s", chat.id if chat else None)
                if update.callback_query:
                    await update.callback_query.answer("Not authorized")
                elif update.effective_message:
                    await update.effective_message.reply_text("Not authorized. Send /start to see your chat id.")
                return
            await handler(update, context)

        return wrapper

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Handler error: %s", context.error, exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(f"⚠️ שגיאה: {e(str(context.error))[:300]}")

    async def broadcast(self, context: ContextTypes.DEFAULT_TYPE, text: str, buttons: Buttons = ()) -> None:
        for chat_id in self.cfg.allowed_chat_ids:
            await self._send_chunks(lambda t, m: context.bot.send_message(chat_id, t, reply_markup=m), text, buttons)

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
        self, kid_lessons: Sequence[Tuple[Kid, List[Lesson]]], days: List[dt.date], with_topics: bool
    ) -> Tuple[str, Buttons, List[int]]:
        """Text, buttons and the ids of the homework tasks shown."""
        parts: List[str] = []
        buttons: Buttons = []
        shown: List[int] = []
        for kid, lessons in kid_lessons:
            lines = [f"👦 <b>{e(kid.name)}</b> ({kid.class_label})"]
            for day in days:
                todays = [l for l in lessons if l.date == day and (l.topic or homework_text(l))]
                if len(days) > 1 or not todays:
                    lines.append(f"\n<u>{day_label(day)}</u>" + ("" if todays else ": אין"))
                for lesson in todays:
                    line = f"{lesson.hour}. <b>{e(lesson.subject)}</b>"
                    if lesson.topic:
                        line += f" — {e(lesson.topic)}"
                    lines.append(line)
                    text = homework_text(lesson)
                    task = self.store.task_for(kid.id, lesson.date, lesson.subject, text) if text else None
                    if task:
                        shown.append(task.id)
                        mark = "✔️" if task.completed else "📝"
                        lines.append(f"    {mark} <i>{e(text)}</i>")
                        if not task.completed:
                            buttons.append(done_button(task, with_kid=len(kid_lessons) > 1))
                    elif with_topics and lesson.topic:
                        topic = self.store.add_topic(kid.id, lesson.date, lesson.subject, lesson.topic)
                        data = f"topic:{topic.id}"
                        promoted = self.store.task_for(kid.id, lesson.date, lesson.subject, lesson.topic)
                        if not promoted and all(b.callback_data != data for b in buttons):
                            label = f"➕ {kid.first_name} · {short(lesson.subject, 16)}"
                            buttons.append(InlineKeyboardButton(label, callback_data=data))
            parts.append("\n".join(lines))
        return "\n\n".join(parts) or "אין נתונים", buttons, shown

    @staticmethod
    def render_tasks(tasks: List[Task], title: str) -> Tuple[str, Buttons]:
        lines = [f"<b>{title}</b>"]
        kid = None
        for task in tasks:
            if task.kid_name != kid:
                kid = task.kid_name
                lines.append(f"\n👦 <b>{e(kid)}</b>")
            lines.append(f"• {e(task.title)} <i>(ל-{task.due_date:%d/%m})</i>")
        return "\n".join(lines), [done_button(t, with_kid=True) for t in tasks]

    # --- commands -------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        if chat_id not in self.cfg.allowed_chat_ids:
            await self.reply(update, f"Your chat id: <code>{chat_id}</code>\nAdd it to TELEGRAM_ALLOWED_CHAT_IDS.")
            return
        await update.effective_message.reply_text(
            "📚 <b>Webtop homework bot</b>\n"
            "/today — היום\n/tomorrow — יום הלימודים הבא\n/week — השבוע\n/date YYYY-MM-DD\n"
            "/todo — משימות פתוחות\n/add &lt;ילד&gt; &lt;טקסט&gt; [YYYY-MM-DD]\n/sync — עדכון מ-Webtop (פעם ב-30 דק׳)\n/kids",
            reply_markup=MENU,
        )

    def freshness(self) -> str:
        last = self.service.last_sync()
        return f"🕒 עודכן {last:%d/%m %H:%M}" if last else "🕒 טרם עודכן — /sync"

    async def _show(self, update: Update, days: List[dt.date]) -> None:
        stored = set(self.service.stored_days())
        if not stored.issuperset(days):
            span = f"{min(stored):%d/%m}–{max(stored):%d/%m}" if stored else "—"
            await self.reply(update, f"אין נתונים שמורים לתאריך הזה (שמור: {span}).\n{self.freshness()}")
            return
        text, buttons, shown = self.render(self.service.view(days), days, with_topics=len(days) == 1)
        self.store.mark_notified(shown)
        await self.reply(update, f"{text}\n\n{self.freshness()}", buttons)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show(update, [self.cfg.today()])

    async def cmd_tomorrow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._show(update, [next_school_day(self.cfg.today())])

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
        await self.service.pull_completion()
        tasks = self.store.open_tasks(due_from=self.cfg.today() - dt.timedelta(days=7))
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
        await self.reply(update, "🔄 מעדכן מ-Webtop…")
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
        lines = [f"👦 {e(k.name)} ({k.class_label})" for k in self.store.kids()]
        await self.reply(update, "\n".join(lines) or "אין נתונים עדיין — /sync")

    # --- buttons --------------------------------------------------------

    async def on_button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        action, _, raw_id = (query.data or "").partition(":")
        if action == "done":
            task = await self.service.complete(int(raw_id))
            await query.answer("✅ סומן כבוצע" if task else "Task not found")
        elif action == "topic":
            topic = self.store.topic(int(raw_id))
            if not topic:
                await query.answer("Topic not found")
                return
            task = await self.service.promote_topic(topic)
            await query.answer("➕ נוספה משימה")
            await query.message.reply_text(f"➕ {e(task.title)} (ל-{task.due_date:%d/%m})", reply_markup=keyboard([done_button(task)]))
        else:
            await query.answer()
            return
        # Drop the pressed button from the message.
        rows = query.message.reply_markup.inline_keyboard if query.message.reply_markup else ()
        remaining = [b for row in rows for b in row if b.callback_data != query.data]
        await query.edit_message_reply_markup(keyboard(remaining))

    # --- jobs -----------------------------------------------------------

    async def _announce(self, context: ContextTypes.DEFAULT_TYPE, tasks: List[Task]) -> None:
        text, buttons = self.render_tasks(tasks, "🆕 שיעורי בית חדשים")
        await self.broadcast(context, text, buttons)
        self.store.mark_notified([t.id for t in tasks])

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
        text, buttons, shown = self.render(self.service.view([today]), [today], with_topics=True)
        self.store.mark_notified(shown)
        await self.broadcast(context, f"📚 <b>סיכום {day_label(today)}</b>\n\n{text}\n\n{self.freshness()}", buttons)

    async def job_reminder(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.service.pull_completion()
        tasks = [t for t in self.store.open_tasks(due_from=self.cfg.today())
                 if t.due_date <= next_school_day(self.cfg.today())]
        if tasks:
            await self.broadcast(context, *self.render_tasks(tasks, "⏰ תזכורת: משימות פתוחות למחר"))
