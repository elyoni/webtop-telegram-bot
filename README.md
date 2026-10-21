# webtop-bot

Telegram bot for parents: daily Webtop (SmartSchool) homework digests, new-homework alerts,
and a to-do list kept in **Microsoft To Do** (one list per kid). Built on [pywebtop](../pywebtop).

- **Local db**: Webtop data (last, current and next week) is pulled into SQLite; all commands read
  from it, never from Webtop directly.
- **07:00 and 15:30** (`SYNC_TIMES`): automatic Webtop update; alerts on new homework.
- **`/sync`**: manual update, at most once per `SYNC_COOLDOWN_MINUTES` (default 30) across all users;
  failed attempts count too (avoids hammering Webtop or locking the account on bad credentials).
- **16:00** (Sun–Fri): today's lessons per kid — topics for context, homework with ✅ buttons,
  ➕ buttons to turn a topic into a task.
- **20:00** (evenings before school): open tasks due by the next school day.
- Homework becomes a task due the next school day; completing it in Telegram or in To Do syncs both ways.

## Commands

`/today` `/tomorrow` `/week` `/date YYYY-MM-DD` `/todo` `/add <kid> <text> [YYYY-MM-DD]` `/sync` `/kids` `/start`

Admins only: `/users`, `/allow <id> [name]`, `/revoke <id>`.

## Setup

1. Create a bot with @BotFather; put the token in `.env` (see `.env.example`, `chmod 600 .env`).
2. Run the bot, send it `/start`, and put the id it shows in `TELEGRAM_ADMIN_IDS`.
3. Other users send `/start`; admins get an access request with a ready `/allow <id>` command.
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

### Run locally

```sh
uv sync
set -a; . ./.env; set +a     # or direnv (`.envrc` = `dotenv`)
uv run webtop-bot login-microsoft   # once, if using To Do
uv run webtop-bot run
```

### Docker (home server)

`pywebtop` must be checked out next to this directory (the build context is the parent dir).

```sh
docker compose build
docker compose run --rm bot webtop-bot login-microsoft   # once, if using To Do
docker compose up -d
```

## Security notes

- Secrets live only in `.env` (gitignored, excluded from the Docker build context).
- The bot ignores every chat that is not an admin (`TELEGRAM_ADMIN_IDS`) or an allowed user (db);
  in groups, both the group and the sender must be allowed.
- `httpx` request logging is silenced because Telegram API URLs contain the bot token.
