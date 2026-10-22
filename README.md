# webtop-bot

Telegram bot for parents: daily Webtop (SmartSchool) homework digests, new-homework alerts,
and a shared to-do list (in Telegram, optionally mirrored to **Todoist**). Built on
[pywebtop](https://github.com/elyoni/pywebtop) (installed automatically).

Each family runs its own copy: one bot, one Webtop parent account, any number of kids.

## Quick start (Docker)

You need a machine that stays on (home server, Raspberry Pi, NAS, VPS) with Docker and git.

1. **Clone and configure**
   ```sh
   git clone https://github.com/elyoni/webtop-telegram-bot.git
   cd webtop-telegram-bot
   cp .env.example .env && chmod 600 .env
   ```
2. **Create your Telegram bot:** in Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot`.
   Put the token in `TELEGRAM_BOT_TOKEN`.
3. **Webtop login:** put the parent's Webtop username and password in `WEBTOP_USERNAME` / `WEBTOP_PASSWORD`.
   Leave `WEBTOP_DATA` empty unless login fails; then see
   [Obtaining the `data` parameter](https://github.com/elyoni/pywebtop#obtaining-the-data-parameter).
4. **Start it**
   ```sh
   docker compose up -d --build
   docker compose logs -f      # Ctrl+C to stop following
   ```
5. **Make yourself admin:** send `/start` to your bot. It replies with your id.
   Put it in `TELEGRAM_ADMIN_IDS` and run `docker compose up -d` again.
6. **Optional — Todoist:** add `TODOIST_TOKEN` (see Setup below) and `docker compose up -d`.

Your data (db, logs) lives in the `bot-data` Docker volume. Update with `git pull && docker compose up -d --build`.

- **Local db**: Webtop data (last, current and next week) is pulled into SQLite; all commands read
  from it, never from Webtop directly.
- **07:00 and 15:30** (`SYNC_TIMES`): automatic Webtop update; alerts on new homework.
- **🔄 update button** (☰ more): manual update, at most once per `SYNC_COOLDOWN_MINUTES` (default 30) across all users;
  failed attempts count too (avoids hammering Webtop or locking the account on bad credentials).
- **16:00** (Sun–Fri): today's lessons per kid — topics for context, homework with ✅ buttons,
  ➕ buttons to turn a topic into a task.
- **20:00** (evenings before school): open tasks due by the next school day.
- Homework becomes a task due the next school day; completing it in Telegram or in the task app syncs both ways.

## Using the bot

Everything is a button; only `/start`, `/help` and `/config` are commands.

- **Bottom menu:** 📅 today · ➡️ tomorrow · 📋 tasks · ☰ more.
- **☰ More:** ⬅️ yesterday · 🗓 week · 🎒 activities and reminders · 🔄 update from Webtop · ⚙️ settings.
- **📋 Tasks:** open tasks with ✅ buttons, plus
  - **➕ New task:** pick the kid → how often → the text.
    *Once* (pick a date from the next 7 days), *every school day* (Sun–Fri), or *chosen weekdays*.
    One kid per task: add it once per kid for several.
  - **🔁 Recurring:** list of the recurring tasks, each with 🗑 delete (tasks already created stay).
- A recurring task becomes a normal task, due that day, at 06:00 (and at bot start, if it was down).
  It then shows in the evening reminder and syncs to Todoist like any other task.
- **🎒 Activities** (☰ more): after-school activities and reminders, for any allowed user.
  - **➕ Activity:** kid → *weekly* (chosen weekdays, e.g. חוג) or *one date* (birthday, trip; pick one of the
    next 7 days or type `DD/MM`) → name (put the hour in the name, e.g. `כדורגל 16:00`).
    Shown under that day's lessons in today / tomorrow / week and the daily summary. One-time ones disappear after their date.
  - **🔔 Reminder:** kid (or all) → text to find in a lesson or activity name (e.g. `חנג`) → note (e.g. `להביא נעליים`).
    The evening before a matching lesson or activity (evening reminder, and ➡️ tomorrow) the bot adds
    "🔔 Yoav · חנג — להביא נעליים". 🗑 deletes either.
- **Teacher notes** are 🌟 good, ⚠️ bad or ⚪ neutral. Webtop's types differ per school, so admins choose in
  `/config` → 🏷 teacher-note types (tap to cycle). Every type starts neutral (Webtop's own good/bad flag is ignored).
- `/config` — everyone: 👦 which kids to show (per user), their own daily summary and evening reminder time (or off).
  Admins also get: users (approve / remove, make ⭐ admin), Webtop sync times and the Todoist interval (bot-wide).
  `TELEGRAM_ADMIN_IDS` are permanent admins; others can be made admins from `/config`.
  Changes apply at once and override the `.env` defaults.

## Setup

1. Create a bot with @BotFather; put the token in `.env` (see `.env.example`, `chmod 600 .env`).
2. Run the bot, send it `/start`, and put the id it shows in `TELEGRAM_ADMIN_IDS`.
3. Other users send `/start`; admins get an access request with ✅ approve / ✖ deny buttons.
   Allowed users are stored in the db and all share the same Webtop account.
   Only admins and allowed users get answers or alerts.
4. To-do list: works in Telegram out of the box (📋 tasks, ➕, ✅ buttons), no extra setup.
   Tasks live in the bot's SQLite db. Optional: mirror them to Todoist (below).

   | | Telegram only (default) | + Todoist (optional) |
   |---|---|---|
   | Setup | none | one API token |
   | Cost | free | free plan is enough (5 projects) |
   | Family sharing | via the bot chat | share the project from the Todoist app |
   | Add / tick tasks | ➕ button, ✅ buttons | also in the Todoist app; bot picks them up |
   | Why Todoist | | token from Settings, no app registration; sections per kid; shared projects |

   Todoist setup: Todoist → Settings → Integrations → Developer → copy the API token into `TODOIST_TOKEN`.
   The bot creates one project "שיעורי בית" with a section per kid; share it from the Todoist app.
   Every `TODO_POLL_MINUTES` (default 10) the bot mirrors the app: tasks added in a kid's section are
   announced in Telegram, and done / reopened / deleted (= done) status is copied. Edits in the app are not.

### Run without Docker

Needs [uv](https://docs.astral.sh/uv/) and git.

```sh
uv sync
set -a; . ./.env; set +a     # or direnv (`.envrc` = `dotenv`)
uv run webtop-bot run
uv run --with pytest pytest -q    # unit tests (no network)
```

### Tasks ([Taskfile](https://taskfile.dev))

`task` lists them. Main ones: `task test`, `task run` (foreground), `task start|stop|restart|status|logs`
(background, log in `data/bot.log`), `task deploy` (tests → refuses uncommitted changes → db backup → restart),
`task build` / `task up` (Docker), `task dev` (Docker with `./src` and `./data` mounted: code edits need only a
container restart, no rebuild; `task dev -- -d` + `task dev-down` for background), `task release -- 0.4.1` (bump, test, commit, tag) then `task publish`
(push main + tag, watch CI).

### Versions: dev vs release

|                  | How to get it                                                                                                                                     |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Last release** | `docker pull elyoni/webtop-bot:latest` (or a pinned `:0.3.0`). Source: latest `vX.Y.Z` [tag](https://github.com/elyoni/webtop-telegram-bot/tags). |
| **Last dev**     | `main` branch, no published image: `git clone https://github.com/elyoni/webtop-telegram-bot.git && docker compose up -d --build`.                 |

To run the released image, replace `build: .` in `compose.yml` with `image: elyoni/webtop-bot:latest`.

### CI / release (GitHub Actions)

`.github/workflows/ci.yml`:

- **Every push to `main` and every PR:** unit tests.
- **New tag `vX.Y.Z` only:** tests, then build and push to Docker Hub as `:X.Y.Z` and `:latest`.
  The tag must equal `version` in `pyproject.toml` (job fails otherwise).

One-time setup: repo → Settings → Secrets and variables → Actions → add `DOCKERHUB_USERNAME`
and `DOCKERHUB_TOKEN` (Docker Hub → Account settings → Personal access tokens, Read & Write).

Release:

```sh
# bump version in pyproject.toml, commit, then:
git tag v0.3.1 && git push origin main v0.3.1
```

### Developing pywebtop alongside

The `pywebtop` dependency is pinned to a git commit in `pyproject.toml`. To work on a local checkout,
run `uv pip install -e ../pywebtop` (undone by the next `uv sync`).

## Security notes

- Secrets live only in `.env` (gitignored, excluded from the Docker build context).
- The bot ignores every chat that is not an admin (`TELEGRAM_ADMIN_IDS`) or an allowed user (db);
  in groups, both the group and the sender must be allowed.
- `httpx` request logging is silenced because Telegram API URLs contain the bot token.
