# webtop-bot

Telegram bot for parents: daily Webtop (SmartSchool) homework digests, new-homework alerts,
and a shared to-do list in **Todoist** or **Microsoft To Do**. Built on
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
- **`/sync`**: manual update, at most once per `SYNC_COOLDOWN_MINUTES` (default 30) across all users;
  failed attempts count too (avoids hammering Webtop or locking the account on bad credentials).
- **16:00** (Sun–Fri): today's lessons per kid — topics for context, homework with ✅ buttons,
  ➕ buttons to turn a topic into a task.
- **20:00** (evenings before school): open tasks due by the next school day.
- Homework becomes a task due the next school day; completing it in Telegram or in the task app syncs both ways.

## Commands

`/today` `/yesterday` `/tomorrow` `/week` `/date YYYY-MM-DD` `/todo` `/add <kid> <text> [YYYY-MM-DD]` `/sync` `/kids` `/help`

Admins only: `/config` — buttons for users (approve / remove), daily summary and reminder times, Webtop sync times, and the Todoist interval. Changes apply at once and override the `.env` values.

## Setup

1. Create a bot with @BotFather; put the token in `.env` (see `.env.example`, `chmod 600 .env`).
2. Run the bot, send it `/start`, and put the id it shows in `TELEGRAM_ADMIN_IDS`.
3. Other users send `/start`; admins get an access request with ✅ approve / ✖ deny buttons.
   Allowed users are stored in the db and all share the same Webtop account.
   Only admins and allowed users get answers or alerts.
4. Optional — Todoist (easiest; projects can be shared with family):
   Todoist → Settings → Integrations → Developer → copy the API token into `TODOIST_TOKEN`.
   The bot creates one project "שיעורי בית" with a section per kid (free plan: 5 projects); share it from the Todoist app.
   If both `TODOIST_TOKEN` and `MS_CLIENT_ID` are set, Todoist is used.
   Every `TODO_POLL_MINUTES` (default 10) the bot mirrors the app: tasks added in a kid's section are
   announced in Telegram, and done / reopened / deleted (= done) status is copied. Edits in the app are not.
5. Optional — Microsoft To Do (needs an Azure directory; personal accounts alone can no longer register apps):
   1. portal.azure.com → Entra ID → App registrations → New: *Personal Microsoft accounts only*, no redirect URI.
   2. Authentication → *Allow public client flows* = Yes.
   3. API permissions → Microsoft Graph → Delegated → `Tasks.ReadWrite`.
   4. Put the Application (client) ID in `MS_CLIENT_ID`, then run `webtop-bot login-microsoft` once
      and follow the device-code prompt. The token cache is stored in `DATA_DIR` (mode 600).

   Without `TODOIST_TOKEN` or `MS_CLIENT_ID`, tasks are tracked only in the bot's SQLite db.

### Run without Docker

Needs [uv](https://docs.astral.sh/uv/) and git.

```sh
uv sync
set -a; . ./.env; set +a     # or direnv (`.envrc` = `dotenv`)
uv run webtop-bot login-microsoft   # once, if using To Do
uv run webtop-bot run
```

### Docker: Microsoft To Do login

```sh
docker compose run --rm bot webtop-bot login-microsoft   # once, before `docker compose up -d`
```

### Developing pywebtop alongside

The `pywebtop` dependency is pinned to a git commit in `pyproject.toml`. To work on a local checkout,
run `uv pip install -e ../pywebtop` (undone by the next `uv sync`).

## Security notes

- Secrets live only in `.env` (gitignored, excluded from the Docker build context).
- The bot ignores every chat that is not an admin (`TELEGRAM_ADMIN_IDS`) or an allowed user (db);
  in groups, both the group and the sender must be allowed.
- `httpx` request logging is silenced because Telegram API URLs contain the bot token.
