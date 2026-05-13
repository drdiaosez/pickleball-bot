"""HTTP server that runs alongside the Telegram bot.

Serves:
  GET  /                          — health check
  GET  /moneyball/<id>            — the Mini App HTML for a specific tournament
  GET  /api/moneyball/<id>        — current state (roster + scores + standings)
  POST /api/moneyball/<id>/score  — record/update a single match score

Auth model for /api endpoints:
  - The Mini App sends Telegram's signed initData blob in the
    X-Telegram-Init-Data header on every API call.
  - We verify the HMAC using BOT_TOKEN. If valid, we trust the user_id
    that Telegram embedded in initData.
  - The user must be in the bot's `members` table to be authorized.

The server uses aiohttp and shares the same asyncio loop as the bot.
Started from main.py after the bot is wired up.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl

from aiohttp import web

from . import db, moneyball

log = logging.getLogger(__name__)

# Cache for verified initData: hash → (user_id, expiry_ts)
# Each verification is fast but the Mini App makes ~30 calls during a tournament,
# so caching saves work without affecting security (the auth_date in initData
# already enforces freshness).
_AUTH_CACHE: dict[str, tuple[int, float]] = {}
_AUTH_CACHE_TTL_S = 300  # 5 min — well under Telegram's recommended 24h initData lifetime


# ─────────────────────────────────────────────
# Auth
# ─────────────────────────────────────────────

def verify_init_data(init_data: str, bot_token: str) -> Optional[int]:
    """Verify Telegram WebApp initData and return the user ID if valid.

    Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

    The blob is a URL-encoded querystring. We:
      1. Parse it into key=value pairs
      2. Pop the 'hash' field
      3. Build a 'data check string' = sorted "key=value" lines joined by '\n'
      4. Compute HMAC_SHA256(data_check_string, secret_key) where
         secret_key = HMAC_SHA256("WebAppData", bot_token)
      5. Constant-time compare against the popped hash
    """
    if not init_data:
        return None

    # Quick cache check
    cache_key = hashlib.sha256(init_data.encode()).hexdigest()
    if cache_key in _AUTH_CACHE:
        user_id, expiry = _AUTH_CACHE[cache_key]
        if expiry > time.time():
            return user_id

    try:
        # parse_qsl preserves order, so use it then sort manually
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    # Build the data-check string
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret_key, data_check.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed, received_hash):
        return None

    # Freshness check — reject initData older than 24 hours
    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date < time.time() - 86400:
        return None

    # Extract user.id from the JSON user field
    try:
        user = json.loads(pairs.get("user", "{}"))
        user_id = int(user["id"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None

    _AUTH_CACHE[cache_key] = (user_id, time.time() + _AUTH_CACHE_TTL_S)
    return user_id


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Apply auth to /api/* routes. Non-API routes pass through."""
    if not request.path.startswith("/api/"):
        return await handler(request)

    init_data = request.headers.get("X-Telegram-Init-Data", "")
    bot_token = request.app["bot_token"]
    user_id = verify_init_data(init_data, bot_token)

    if user_id is None:
        # Allow local dev access via a dev header — only when explicitly enabled
        dev_user = os.environ.get("DEV_BYPASS_USER_ID")
        if dev_user and request.headers.get("X-Dev-Bypass") == os.environ.get("DEV_BYPASS_SECRET", "no"):
            user_id = int(dev_user)
        else:
            return web.json_response({"error": "unauthorized"}, status=401)

    # Verify the user is actually a known member
    if db.get_member(user_id) is None:
        return web.json_response({"error": "not a member"}, status=403)

    request["user_id"] = user_id
    return await handler(request)


# ─────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────

async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def serve_miniapp(request: web.Request) -> web.Response:
    """Serve the Mini App HTML. The ?mb_id is read by JavaScript on load."""
    static_dir = Path(__file__).parent / "static"
    html_path = static_dir / "moneyball.html"
    if not html_path.exists():
        return web.Response(text="Mini App not deployed", status=500)
    return web.FileResponse(html_path)


async def get_moneyball(request: web.Request) -> web.Response:
    try:
        mb_id = int(request.match_info["mb_id"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad id"}, status=400)
    mb = moneyball.get_moneyball(mb_id)
    if not mb:
        return web.json_response({"error": "not found"}, status=404)
    standings = moneyball.compute_standings(mb)
    return web.json_response({
        "moneyball": mb,
        "standings": standings,
        "schedule": moneyball.SCHEDULE,
    })


async def post_score(request: web.Request) -> web.Response:
    try:
        mb_id = int(request.match_info["mb_id"])
        body = await request.json()
        round_num = int(body["round"])
        court = int(body["court"])
        sa = body.get("scoreA")
        sb = body.get("scoreB")
        if sa is not None: sa = int(sa)
        if sb is not None: sb = int(sb)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as e:
        return web.json_response({"error": f"bad request: {e}"}, status=400)

    # Confirm the user is a player in this money ball (or the creator)
    mb = moneyball.get_moneyball(mb_id)
    if not mb:
        return web.json_response({"error": "not found"}, status=404)
    user_id = request["user_id"]
    is_player = any(p["member_id"] == user_id for p in mb["players"])
    is_creator = mb.get("created_by") == user_id
    if not (is_player or is_creator):
        return web.json_response({"error": "not in this money ball"}, status=403)

    try:
        updated = moneyball.update_match_score(mb_id, round_num, court, sa, sb)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    standings = moneyball.compute_standings(updated)
    # If just-completed, notify the group chat
    app_data = request.app
    if updated["status"] == "completed":
        app_data["completion_callback"](mb_id, updated, standings)

    return web.json_response({
        "moneyball": updated,
        "standings": standings,
    })


# ─────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────

def create_app(bot_token: str, completion_callback) -> web.Application:
    """Build the aiohttp app. The completion_callback is called when a
    money ball goes from in_progress → completed; it receives
    (mb_id, moneyball_dict, standings_list) and should post results to
    the Telegram group.
    """
    app = web.Application(middlewares=[auth_middleware])
    app["bot_token"] = bot_token
    app["completion_callback"] = completion_callback

    app.router.add_get("/", health)
    app.router.add_get("/moneyball", serve_miniapp)         # used by Telegram deep links (id in start_param)
    app.router.add_get("/moneyball/{mb_id}", serve_miniapp) # legacy/direct access
    app.router.add_get("/api/moneyball/{mb_id}", get_moneyball)
    app.router.add_post("/api/moneyball/{mb_id}/score", post_score)

    return app


async def run_http_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start the HTTP server. Returns the runner so we can shut it down cleanly."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(f"HTTP server listening on {host}:{port}")
    return runner
