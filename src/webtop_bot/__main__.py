"""Entry point: `webtop-bot run` or `webtop-bot login-microsoft`."""
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
    sub.add_parser("login-microsoft", help="One-time Microsoft To Do login (device code)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.command == "login-microsoft":
        from .config import data_dir, required
        from .todo import login_device_flow

        user = login_device_flow(required("MS_CLIENT_ID"), data_dir())
        print(f"Logged in as {user}")
        return 0

    from .bot import Bot
    from .config import Config
    from .service import Service
    from .store import Store
    from .todo import TodoClient

    cfg = Config.from_env()
    if not cfg.allowed_chat_ids:
        logging.warning("TELEGRAM_ALLOWED_CHAT_IDS is empty: only /start will answer (to show your chat id)")
    todo = TodoClient(cfg.ms_client_id, cfg.data_dir) if cfg.ms_client_id else None
    if not todo:
        logging.info("MS_CLIENT_ID not set: tasks are kept locally only")
    service = Service(cfg, Store(cfg.data_dir / "bot.db"), todo)
    Bot(cfg, service).build().run_polling(bootstrap_retries=-1)  # keep retrying if the network is down at boot
    return 0


if __name__ == "__main__":
    sys.exit(main())
