"""Entry point.

Run with:  python -m bot.main

Reads BOT_TOKEN, DB_PATH, and TIMEZONE from environment (.env supported).
"""
from __future__ import annotations

import logging
import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler

from . import db
from .handlers import common, games, newgame, roster


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        level=logging.INFO,
    )
    # Quiet down httpx's per-request INFO logs
    logging.getLogger("httpx").setLevel(logging.WARNING)

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is required (put it in .env)")
    db_path = os.environ.get("DB_PATH", "db.sqlite")
    tz_name = os.environ.get("TIMEZONE", "America/Los_Angeles")

    db.init_db(db_path)

    app = Application.builder().token(token).build()
    app.bot_data["tz"] = ZoneInfo(tz_name)

    # /start, /help
    app.add_handler(CommandHandler("start", common.cmd_start))
    app.add_handler(CommandHandler("help", common.cmd_help))

    # /newgame conversation — register BEFORE the generic text handler in roster
    app.add_handler(newgame.build_newgame_handler())

    # /games, /mygames
    for h in games.build_games_handlers():
        app.add_handler(h)

    # /cancelguest and /canceledit (must come before the generic text handler)
    app.add_handler(roster.build_cancel_guest_handler())
    app.add_handler(roster.build_cancel_edit_handler())

    # All inline-button callbacks + the guest-name capture text handler
    for h in roster.build_roster_handlers():
        app.add_handler(h)

    app.add_error_handler(common.error_handler)

    logging.getLogger(__name__).info("Bot starting…")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
