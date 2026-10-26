# webtop-bot

Telegram bot for parents: daily Webtop (SmartSchool) homework digests, new-homework alerts,
teachers' messages, and a shared to-do list (in Telegram, optionally mirrored to **Todoist**). Built on
[pywebtop](https://github.com/elyoni/pywebtop) (installed automatically).

Each family runs its own copy: one bot, one Webtop parent account, any number of kids.

## Quick start (Docker)

You need a machine that stays on (home server, Raspberry Pi, NAS, VPS) with Docker (with the Compose plugin).
The bot runs the published image [`elyoni/webtop-bot`](https://hub.docker.com/r/elyoni/webtop-bot); no git or build needed.

1. **Get the two files** into a new folder
   ```sh
   mkdir webtop-bot && cd webtop-bot
   curl -fsSLO https://raw.githubusercontent.com/elyoni/webtop-telegram-bot/main/compose.yml
   curl -fsSL -o .env https://raw.githubusercontent.com/elyoni/webtop-telegram-bot/main/.env.example
   chmod 600 .env
   ```
2. **Create your Telegram bot:** in Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot`.
   Put the token in `TELEGRAM_BOT_TOKEN`.
3. **Webtop login:** put the parent's Webtop username and password in `WEBTOP_USERNAME` / `WEBTOP_PASSWORD`.
   Leave `WEBTOP_DATA` empty unless login fails; then see
   [Obtaining the `data` parameter](https://github.com/elyoni/pywebtop#obtaining-the-data-parameter).
4. **Start it**
   ```sh
   docker compose up -d
   docker compose logs -f      # Ctrl+C to stop following
   ```
5. **Make yourself admin:** send `/start` to your bot. It replies with your id.
   Put it in `TELEGRAM_ADMIN_IDS` and run `docker compose up -d` again.
6. **Optional — Todoist:** add `TODOIST_TOKEN` (see Setup below) and `docker compose up -d`.

### The files

| File | What it holds | Edit? |
|---|---|---|
| `.env` | All settings: Webtop login, bot token, admin ids, optional Todoist token and times. Secrets, so `chmod 600`. | Yes: `WEBTOP_USERNAME`, `WEBTOP_PASSWORD`, `TELEGRAM_BOT_TOKEN`, then `TELEGRAM_ADMIN_IDS` (step 5). The rest is optional, documented in the file. |
| `compose.yml` | Which image to run, restart policy, the data volume. | No. To pin a release, change `:latest` to e.g. `:0.4.0`. |

The db (users, tasks, settings) lives in the `bot-data` Docker volume, not in the folder, so it survives updates.
After editing `.env`, run `docker compose up -d` again to apply it.

- **Update:** `docker compose pull && docker compose up -d`
- **Backup the db:** `docker compose cp bot:/data/bot.db ./bot.db.bak`
- **Stop:** `docker compose down` (the volume and its data stay)

Without Compose, the same thing is:
```sh
docker run -d --name webtop-bot --restart unless-stopped --env-file .env -v webtop-bot-data:/data elyoni/webtop-bot:latest
```

To build from source instead: clone the repo, create `.env`, and run `task up`
(= `docker compose -f compose.yml -f compose.build.yml up -d --build`).

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
- **☰ More:** ⬅️ yesterday · 🗓 week · ✉️ teacher messages · 🎒 activities and reminders · 🔄 update from Webtop · ⚙️ settings · ℹ️ version (with a link to its release notes).
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
- **✉️ Teacher messages** (Webtop inbox): each new message is sent in full to every user (except one only
  about kids that user hides). Checked every `MESSAGE_POLL_MINUTES` (default 30, 07:00–22:00) and with each Webtop update.
  ✉️ teacher messages (☰ more) lists the inbox, ⭐ important ones first; tap one to read it. Each message has
  ✔️ read (moves it to 🗄 archive, a button under the list; ↩️ brings it back) and ⭐ important (stays at the top).
  ✔️ mark all read archives everything except ⭐. This state is shared by all users. Attachments, signatures and surveys
  are only named, with a link to Webtop. Opening a message (here or by the alert) marks it read on Webtop.
  On the first run the inbox is only listed: nothing is sent or marked read.
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
| **Last dev**     | `main` branch, no published image: clone the repo, then `task up` (builds from source with `compose.build.yml`).                                   |

`compose.yml` runs the released image; `compose.build.yml` overrides it to build from the checkout.

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
