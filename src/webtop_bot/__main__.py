"""Entry point: `webtop-bot run`."""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional, Sequence


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="webtop-bot")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="Run the bot (default)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from .bot import Bot
    from .config import Config
    from .service import Service
    from .store import Store
    cfg = Config.from_env()
    if not cfg.admin_ids:
        logging.warning("TELEGRAM_ADMIN_IDS is empty: only /start will answer (to show your chat id)")
    service = Service(cfg, Store(cfg.data_dir / "bot.db"), task_backend(cfg))
    Bot(cfg, service).build().run_polling(bootstrap_retries=-1)  # keep retrying if the network is down at boot
    return 0


def task_backend(cfg):
    """Todoist if TODOIST_TOKEN is set, else none (tasks are kept locally only)."""
    if cfg.todoist_token:
        from .todoist import TodoistClient

        logging.info("Task backend: Todoist")
        return TodoistClient(cfg.todoist_token)
    logging.info("No TODOIST_TOKEN: tasks are kept locally only")
    return None


if __name__ == "__main__":
    sys.exit(main())
