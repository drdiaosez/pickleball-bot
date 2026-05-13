"""Entry point.

Run with:  python -m bot.main

Reads from .env:
  BOT_TOKEN         — required (from BotFather)
  DB_PATH           — defaults to db.sqlite
  TIMEZONE          — IANA name, defaults to America/Los_Angeles
  ALLOWED_GROUP_ID  — optional; locks bot to one group
  PUBLIC_URL        — required for /moneyball; e.g. https://pickle.example.com
  HTTP_HOST         — defaults to 127.0.0.1
  HTTP_PORT         — defaults to 8080
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler

from . import db, moneyball, http_server
from .handlers import common, games, moneyball as mb_handlers, merge, newgame, roster


async def amain() -> None:
    load_dotenv()
    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    log = logging.getLogger(__name__)

    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is required (put it in .env)")
    db_path = os.environ.get("DB_PATH", "db.sqlite")
    tz_name = os.environ.get("TIMEZONE", "America/Los_Angeles")
    http_host = os.environ.get("HTTP_HOST", "127.0.0.1")
    http_port = int(os.environ.get("HTTP_PORT", "8080"))

    db.init_db(db_path)
    moneyball.init_moneyball_schema()

    app = Application.builder().token(token).build()
    app.bot_data["tz"] = ZoneInfo(tz_name)

    # /start, /help
    app.add_handler(CommandHandler("start", common.cmd_start))
    app.add_handler(CommandHandler("help", common.cmd_help))

    # /newgame conversation — must register before generic text handler
    app.add_handler(newgame.build_newgame_handler())

    # /games, /mygames, /past, /week
    for h in games.build_games_handlers():
        app.add_handler(h)

    # /moneyball, /leaderboard, plus mb_pick callback
    for h in mb_handlers.build_moneyball_handlers():
        app.add_handler(h)

    # /merge (admin) + its confirmation callback
    for h in merge.build_merge_handlers():
        app.add_handler(h)

    # /cancelguest, /canceledit (must come before generic text handler)
    app.add_handler(roster.build_cancel_guest_handler())
    app.add_handler(roster.build_cancel_edit_handler())

    # Roster callbacks + text dispatcher
    for h in roster.build_roster_handlers():
        app.add_handler(h)

    app.add_error_handler(common.error_handler)

    # ─── HTTP server setup ───
    def chat_id_for_game(game_id: int):
        g = db.get_game(game_id)
        return g.get("chat_id") if g else None

    completion_cb = mb_handlers.schedule_completion_announcement(app, chat_id_for_game)
    http_app = http_server.create_app(bot_token=token, completion_callback=completion_cb)

    # ─── Boot ───
    log.info("Bot starting…")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=["message", "callback_query"])
    log.info("Telegram polling started")

    http_runner = await http_server.run_http_server(http_app, http_host, http_port)

    # Block until a signal, then shut down cleanly
    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows
    await stop.wait()

    log.info("Shutting down…")
    await http_runner.cleanup()
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
