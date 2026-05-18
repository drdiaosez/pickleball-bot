"""HTTP server that runs alongside the Telegram bot.

Serves three Telegram Mini Apps:

  • /register — every chat member can self-serve their signup status
    (join / leave), plus add and remove their own guests.
  • /manage   — chat admins can edit anyone's signup, change game details,
    or delete the game.
  • /moneyball — the existing tournament scoring app.

Auth model for /api endpoints:
  - The Mini App sends Telegram's signed initData blob in the
    X-Telegram-Init-Data header on every API call.
  - We verify the HMAC using BOT_TOKEN. If valid, we trust the user_id
    that Telegram embedded in initData.
  - The user must be in the bot's `members` table to be authorized.

After every state change on a game endpoint, we re-render the game card
in the original group chat so signups, edits, and deletions are
immediately visible to everyone — same UX as the old in-chat button
flow, just driven from the mini-app instead.

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
from typing import Awaitable, Callable, Optional
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from aiohttp import web
from telegram import Bot

from . import db, moneyball, views, admins, notifications

log = logging.getLogger(__name__)

# Cache for verified initData: hash → (user_id, expiry_ts)
# Each verification is fast but a Mini App makes many calls during a session,
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

    # For moneyball-specific routes, verify the user belongs to the same chat
    # as the money ball they're accessing. This prevents cross-chat data leaks.
    mb_id_str = request.match_info.get("mb_id")
    if mb_id_str is not None:
        try:
            mb_id = int(mb_id_str)
        except ValueError:
            return web.json_response({"error": "bad id"}, status=400)
        mb_chat_id = _get_moneyball_chat_id(mb_id)
        if mb_chat_id is not None:
            row = db._conn.execute(
                "SELECT 1 FROM chat_members WHERE chat_id = ? AND telegram_user_id = ?",
                (mb_chat_id, user_id),
            ).fetchone()
            if row is None:
                return web.json_response({"error": "not a member of this chat"}, status=403)

    # For game-scoped routes, require the user to be a member of the specific
    # chat that owns the game.
    game_id_str = request.match_info.get("game_id")
    if game_id_str is not None:
        try:
            game_id = int(game_id_str)
        except ValueError:
            return web.json_response({"error": "bad id"}, status=400)
        game = db.get_game(game_id)
        if game is None:
            return web.json_response({"error": "game not found"}, status=404)
        chat_id = game.get("chat_id")
        if chat_id is not None:
            member_row = db.get_chat_member(chat_id, user_id)
            if member_row is None:
                return web.json_response({"error": "not a member of this chat"}, status=403)
        request["game"] = game
        request["chat_id"] = chat_id

    request["user_id"] = user_id
    return await handler(request)


def _get_moneyball_chat_id(mb_id: int):
    """Return the chat_id for a moneyball, or None if not found/unset."""
    assert db._conn is not None
    row = db._conn.execute(
        "SELECT chat_id FROM moneyballs WHERE id = ?", (mb_id,)
    ).fetchone()
    return row["chat_id"] if row else None


async def _require_admin(request: web.Request) -> Optional[web.Response]:
    """Helper for admin-only endpoints. Returns a Response on failure, or None on success."""
    bot: Bot = request.app["bot"]
    chat_id = request.get("chat_id")
    user_id = request["user_id"]
    if chat_id is None:
        return web.json_response({"error": "game has no chat"}, status=400)
    if not await admins.is_chat_admin(bot, chat_id, user_id):
        return web.json_response({"error": "admin only"}, status=403)
    return None


# ─────────────────────────────────────────────
# Static / health routes
# ─────────────────────────────────────────────

async def health(request: web.Request) -> web.Response:
    return web.Response(text="ok\n")


async def serve_miniapp(request: web.Request) -> web.Response:
    """Serve the moneyball Mini App HTML. The ?mb_id is read by JavaScript on load."""
    static_dir = Path(__file__).parent / "static"
    html_path = static_dir / "moneyball.html"
    if not html_path.exists():
        return web.Response(text="Mini App not deployed", status=500)
    return web.FileResponse(html_path)


def _serve_static(filename: str) -> Callable[[web.Request], Awaitable[web.Response]]:
    """Build a handler that serves a single file from bot/static/."""
    static_dir = Path(__file__).parent / "static"
    path = static_dir / filename

    async def _handler(request: web.Request) -> web.Response:
        if not path.exists():
            return web.Response(text=f"{filename} not deployed", status=500)
        return web.FileResponse(path)

    return _handler


# ─────────────────────────────────────────────
# Moneyball routes (existing)
# ─────────────────────────────────────────────

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
# Game state serializer (register/manage mini-apps)
# ─────────────────────────────────────────────

def _participant_dict(p: dict) -> dict:
    """Slim a participant row down to what the mini-app actually needs."""
    if p.get("member_id") is not None:
        name = p.get("member_name") or "Unknown"
        kind = "member"
    else:
        name = p.get("guest_name") or "Guest"
        kind = "guest"
    return {
        "id": p["id"],
        "kind": kind,
        "name": name,
        "member_id": p.get("member_id"),
        "added_by": p.get("added_by"),
        "adder_name": p.get("adder_name"),
        "status": p["status"],
        "position": p["position"],
        "is_paid": bool(p.get("is_paid")),
    }


async def _build_game_state(
    request: web.Request, *, include_admin_extras: Optional[bool] = None
) -> dict:
    """Assemble the JSON payload both mini-apps consume."""
    game: dict = request["game"]
    user_id: int = request["user_id"]
    chat_id = request.get("chat_id")
    tz: ZoneInfo = request.app["tz"]

    bot: Bot = request.app["bot"]
    is_admin = False
    if chat_id is not None:
        is_admin = await admins.is_chat_admin(bot, chat_id, user_id)

    participants = db.get_participants(game["id"])
    organizer = db.get_member(game["organizer_id"])
    organizer_name = organizer["display_name"] if organizer else "?"

    # Viewer's own self-registration (if any)
    own = next((p for p in participants if p.get("member_id") == user_id), None)
    # Viewer's guests
    my_guests = [
        p for p in participants
        if p.get("added_by") == user_id and p.get("member_id") is None
    ]

    confirmed = [p for p in participants if p["status"] == "confirmed"]
    waitlist = [p for p in participants if p["status"] == "waitlist"]
    paid_count = sum(1 for p in confirmed if p.get("is_paid"))

    payload = {
        "game": {
            "id": game["id"],
            "scheduled_for": game["scheduled_for"],
            "scheduled_for_label": views.format_when(game["scheduled_for"], tz),
            "location": game["location"],
            "organizer_name": organizer_name,
            "max_players": game["max_players"],
            "notes": game.get("notes") or "",
            "payment_amount_cents": game.get("payment_amount_cents"),
            "payment_amount_label": views.format_money(game.get("payment_amount_cents")),
            "has_payment": views.game_has_payment(game),
        },
        "viewer": {
            "user_id": user_id,
            "is_admin": is_admin,
            "registration": _participant_dict(own) if own else None,
            "guests": [_participant_dict(g) for g in my_guests],
        },
        "counts": {
            "confirmed": len(confirmed),
            "waitlist": len(waitlist),
            "paid": paid_count,
        },
        "participants": {
            "confirmed": [_participant_dict(p) for p in confirmed],
            "waitlist": [_participant_dict(p) for p in waitlist],
        },
    }

    # The admin mini-app needs the picker of members-not-yet-on-the-game.
    # Default to "include only if viewer is admin" so non-admin payloads
    # stay slim; callers can override with the kwarg.
    if include_admin_extras is None:
        include_admin_extras = is_admin
    if include_admin_extras and chat_id is not None:
        payload["available_members"] = [
            {"telegram_id": m["telegram_id"], "display_name": m["display_name"]}
            for m in db.list_members_not_in_game(game["id"], chat_id=chat_id)
        ]

    return payload


# ─────────────────────────────────────────────
# Card refresher
# ─────────────────────────────────────────────

async def _notify_diffs(
    request: web.Request, game_id: int, before: list[dict], actor_id: int
) -> None:
    """Diff `before` against current state and DM affected members.

    Keeps the call sites tidy — each mutating endpoint does
    `before = db.get_participants(...)` upfront, then calls this after
    the mutation. Errors are swallowed: DM failures must never break a
    user-facing API call.
    """
    try:
        after = db.get_participants(game_id)
        await notifications.diff_and_notify(
            bot=request.app["bot"],
            tz=request.app["tz"],
            game_id=game_id,
            before=before,
            after=after,
            actor_id=actor_id,
        )
    except Exception as e:
        log.warning("diff_and_notify failed for game %s: %s", game_id, e)


async def _refresh_card(request: web.Request, game_id: int) -> None:
    """Re-render the game card in the group after a mutating API call.

    Telegram doesn't let us push updates to clients without going through
    the bot, so the canonical "everyone sees fresh state" path is:
    edit the original card message in the group. The mini-app polls our
    GET endpoint on its own to reflect the same change in-app.

    We swallow exceptions here — the API write already succeeded; failing
    to refresh the card is annoying but not a data-correctness issue, and
    we don't want to fail the user's tap because Telegram rate-limited us.
    """
    refresher = request.app.get("refresh_card")
    if refresher is None:
        return
    try:
        await refresher(game_id)
    except Exception as e:
        log.warning("card refresh failed for game %s: %s", game_id, e)


# ─────────────────────────────────────────────
# Member-facing game routes
# ─────────────────────────────────────────────

async def get_game_state(request: web.Request) -> web.Response:
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def post_self_register(request: web.Request) -> web.Response:
    """Set the viewer's own registration.

    body: {"action": "join" | "leave"}

    "join"    → become confirmed (or waitlist if full)
    "leave"   → remove yourself entirely
    """
    game: dict = request["game"]
    user_id: int = request["user_id"]
    try:
        body = await request.json()
        action = str(body.get("action") or "").lower()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "bad json"}, status=400)

    if action not in ("join", "leave"):
        return web.json_response({"error": "bad action"}, status=400)

    existing = db.member_is_in_game(game["id"], user_id)
    before = db.get_participants(game["id"])

    try:
        if action == "leave":
            if not existing:
                return web.json_response({"error": "not registered"}, status=400)
            db.remove_participant(existing["id"])
        elif action == "join":
            if existing:
                # Already on the game in some form — no-op.
                pass
            else:
                db.add_participant(game["id"], added_by=user_id, member_id=user_id)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, user_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def post_self_guest(request: web.Request) -> web.Response:
    """Add a guest under the viewer's name.

    body: {"name": "Pat"}
    """
    game: dict = request["game"]
    user_id: int = request["user_id"]
    try:
        body = await request.json()
        name = str(body.get("name") or "").strip()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "bad json"}, status=400)

    if not name or len(name) > 40:
        return web.json_response({"error": "name must be 1-40 chars"}, status=400)

    before = db.get_participants(game["id"])
    try:
        db.add_participant(game["id"], added_by=user_id, guest_name=name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, user_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def delete_self_guest(request: web.Request) -> web.Response:
    """Remove one of the viewer's own guests.

    Path: /api/game/<game_id>/guest/<pid>
    """
    game: dict = request["game"]
    user_id: int = request["user_id"]
    try:
        pid = int(request.match_info["pid"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad id"}, status=400)

    p = db.get_participant(pid)
    if not p or p["game_id"] != game["id"]:
        return web.json_response({"error": "not found"}, status=404)
    if p.get("member_id") is not None:
        return web.json_response({"error": "not a guest"}, status=400)
    if p.get("added_by") != user_id:
        return web.json_response({"error": "not your guest"}, status=403)

    before = db.get_participants(game["id"])
    db.remove_participant(pid)
    await _notify_diffs(request, game["id"], before, user_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


# ─────────────────────────────────────────────
# Admin routes
# ─────────────────────────────────────────────

async def admin_add_member(request: web.Request) -> web.Response:
    """Add any chat member to the game by their telegram_id.

    body: {"member_id": int}
    """
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    user_id: int = request["user_id"]
    chat_id = request["chat_id"]
    try:
        body = await request.json()
        target = int(body.get("member_id"))
    except (json.JSONDecodeError, TypeError, ValueError):
        return web.json_response({"error": "bad json"}, status=400)

    if db.get_member(target) is None:
        return web.json_response({"error": "unknown member"}, status=404)
    # Verify target is a member of this chat
    if chat_id is not None and db.get_chat_member(chat_id, target) is None:
        return web.json_response({"error": "not in this chat"}, status=400)

    before = db.get_participants(game["id"])
    try:
        db.add_participant(game["id"], added_by=user_id, member_id=target)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, user_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_add_guest(request: web.Request) -> web.Response:
    """Admin adds a guest with an arbitrary `added_by` (defaults to the admin).

    body: {"name": str, "added_by"?: int}  — added_by must be a member of this chat
    """
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    actor_id: int = request["user_id"]
    chat_id = request["chat_id"]
    try:
        body = await request.json()
        name = str(body.get("name") or "").strip()
        added_by = body.get("added_by")
        added_by = int(added_by) if added_by is not None else actor_id
    except (json.JSONDecodeError, TypeError, ValueError):
        return web.json_response({"error": "bad json"}, status=400)
    if not name or len(name) > 40:
        return web.json_response({"error": "name must be 1-40 chars"}, status=400)
    if chat_id is not None and db.get_chat_member(chat_id, added_by) is None:
        return web.json_response({"error": "adder not in chat"}, status=400)

    before = db.get_participants(game["id"])
    try:
        db.add_participant(game["id"], added_by=added_by, guest_name=name)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, actor_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_remove_participant(request: web.Request) -> web.Response:
    """Admin removes any participant (member or guest) from the game."""
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    try:
        pid = int(request.match_info["pid"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad id"}, status=400)

    p = db.get_participant(pid)
    if not p or p["game_id"] != game["id"]:
        return web.json_response({"error": "not found"}, status=404)
    actor_id: int = request["user_id"]
    before = db.get_participants(game["id"])
    db.remove_participant(pid)
    await _notify_diffs(request, game["id"], before, actor_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_move_participant(request: web.Request) -> web.Response:
    """Move a participant between confirmed / waitlist.

    body: {"status": "confirmed" | "waitlist"}

    Implementation notes:
      • confirmed → waitlist uses demote_to_waitlist.
      • Moving INTO confirmed when full pushes someone to the top of the
        waitlist via swap_with_waitlist. We pick the most-recently-confirmed
        person to bump.
    """
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    actor_id: int = request["user_id"]
    try:
        pid = int(request.match_info["pid"])
        body = await request.json()
        new_status = str(body.get("status") or "").lower()
    except (json.JSONDecodeError, TypeError, ValueError, KeyError):
        return web.json_response({"error": "bad request"}, status=400)
    if new_status not in ("confirmed", "waitlist"):
        return web.json_response({"error": "bad status"}, status=400)

    p = db.get_participant(pid)
    if not p or p["game_id"] != game["id"]:
        return web.json_response({"error": "not found"}, status=404)
    if p["status"] == new_status:
        # No-op, but still return current state
        payload = await _build_game_state(request)
        return web.json_response(payload)

    before = db.get_participants(game["id"])
    try:
        if new_status == "confirmed":
            if p["status"] == "waitlist":
                # Reuse the existing "promote / swap" logic.
                if db.confirmed_count(game["id"]) < game["max_players"]:
                    # Room available — just flip status + position.
                    with db.transaction() as conn:
                        new_pos = conn.execute(
                            "SELECT COALESCE(MAX(position), 0) + 1 AS n FROM participants WHERE game_id = ? AND status = 'confirmed'",
                            (game["id"],),
                        ).fetchone()["n"]
                        conn.execute(
                            "UPDATE participants SET status='confirmed', position=? WHERE id=?",
                            (new_pos, pid),
                        )
                    db._renumber(game["id"], "waitlist")  # noqa: SLF001
                else:
                    # Full — soft-swap with the bottom of confirmed (most-recently-added).
                    participants = db.get_participants(game["id"])
                    confirmed = [x for x in participants if x["status"] == "confirmed"]
                    if not confirmed:
                        return web.json_response(
                            {"error": "no confirmed to swap"}, status=400
                        )
                    # Swap with the most-recently-confirmed (highest position)
                    target = max(confirmed, key=lambda x: x["position"])
                    db.swap_with_waitlist(target["id"], pid)
        elif new_status == "waitlist":
            if p["status"] == "confirmed":
                db.demote_to_waitlist(pid)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    await _notify_diffs(request, game["id"], before, actor_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_patch_game(request: web.Request) -> web.Response:
    """Edit game-level fields.

    body keys (any subset):
      • scheduled_for      ISO-8601 string parsed as the new datetime
      • location           non-empty string up to 100 chars
      • max_players        int 2..32
      • notes              string up to 200 chars; pass "" or null to clear
      • payment_amount_cents  int >= 0; 0 or null to clear payment
    """
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, TypeError):
        return web.json_response({"error": "bad json"}, status=400)

    from datetime import datetime, timedelta
    tz: ZoneInfo = request.app["tz"]
    actor_id: int = request["user_id"]

    # Snapshot participants before any patch is applied. Only
    # max_players actually shuffles the roster, but we always diff so
    # behavior stays predictable when more fields gain side-effects.
    before = db.get_participants(game["id"])

    # Apply each field independently so partial updates work.
    if "scheduled_for" in body:
        raw = body["scheduled_for"]
        try:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
        except (TypeError, ValueError):
            return web.json_response({"error": "bad scheduled_for"}, status=400)
        if dt < datetime.now(tz) - timedelta(minutes=10):
            return web.json_response({"error": "time is in the past"}, status=400)
        db.update_game_time(game["id"], dt)

    if "location" in body:
        loc = str(body["location"] or "").strip()
        if not loc or len(loc) > 100:
            return web.json_response({"error": "location 1-100 chars"}, status=400)
        db.update_game_location(game["id"], loc)

    if "max_players" in body:
        try:
            n = int(body["max_players"])
        except (TypeError, ValueError):
            return web.json_response({"error": "bad max_players"}, status=400)
        if not (2 <= n <= 32):
            return web.json_response({"error": "max_players 2-32"}, status=400)
        db.update_game_max(game["id"], n)

    if "notes" in body:
        raw = body["notes"]
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            db.update_game_notes(game["id"], None)
        else:
            text = str(raw).strip()
            if len(text) > 200:
                return web.json_response({"error": "notes <=200 chars"}, status=400)
            db.update_game_notes(game["id"], text)

    if "payment_amount_cents" in body:
        raw = body["payment_amount_cents"]
        if raw is None:
            db.update_game_payment_amount(game["id"], None)
        else:
            try:
                cents = int(raw)
            except (TypeError, ValueError):
                return web.json_response({"error": "bad payment"}, status=400)
            if cents < 0:
                return web.json_response({"error": "negative payment"}, status=400)
            db.update_game_payment_amount(
                game["id"], cents if cents > 0 else None
            )

    # Re-fetch the game so subsequent state-building uses the new values.
    request["game"] = db.get_game(game["id"])
    await _notify_diffs(request, game["id"], before, actor_id)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


async def admin_delete_game(request: web.Request) -> web.Response:
    """Delete the game and notify previously-confirmed members."""
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    actor_id: int = request["user_id"]
    bot: Bot = request.app["bot"]
    tz: ZoneInfo = request.app["tz"]

    participants = db.get_participants(game["id"])
    affected = [p for p in participants if p.get("member_id")]
    db.delete_game(game["id"])

    # Best-effort DMs — the actor saw the game in the mini-app; they
    # don't need a chat-level confirmation. Anyone signed up gets DM'd.
    actor = db.get_member(actor_id)
    actor_name = actor["display_name"] if actor else "An admin"
    when = views.format_when(game["scheduled_for"], tz)
    text = (
        f"🗑 The game on <b>{when}</b> "
        f"@ {game['location']} was deleted by {actor_name}."
    )
    for p in affected:
        try:
            await bot.send_message(
                chat_id=p["member_id"], text=text, parse_mode="HTML"
            )
        except Exception as e:
            log.info("Couldn't DM user %s about delete: %s", p["member_id"], e)

    # Best-effort: replace the original card with a tombstone so the
    # group sees the deletion. We don't have a CallbackQuery here — go
    # straight through edit_message_text.
    if game.get("chat_id") and game.get("message_id"):
        try:
            await bot.edit_message_text(
                chat_id=game["chat_id"],
                message_id=game["message_id"],
                text=(
                    f"🗑 Game deleted: <s>{when} "
                    f"@ {game['location']}</s>"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            log.info("Couldn't edit deleted-game card: %s", e)

    return web.json_response({"deleted": True})


async def admin_pay_toggle(request: web.Request) -> web.Response:
    """Toggle the paid flag on a confirmed participant."""
    err = await _require_admin(request)
    if err is not None:
        return err
    game: dict = request["game"]
    try:
        pid = int(request.match_info["pid"])
    except (KeyError, ValueError):
        return web.json_response({"error": "bad id"}, status=400)
    p = db.get_participant(pid)
    if not p or p["game_id"] != game["id"]:
        return web.json_response({"error": "not found"}, status=404)
    db.toggle_participant_paid(pid)
    await _refresh_card(request, game["id"])
    payload = await _build_game_state(request)
    return web.json_response(payload)


# ─────────────────────────────────────────────
# App factory
# ─────────────────────────────────────────────

def create_app(
    *,
    bot: Bot,
    bot_token: str,
    tz: ZoneInfo,
    completion_callback,
    refresh_card: Callable[[int], Awaitable[None]],
) -> web.Application:
    """Build the aiohttp app.

    `completion_callback` is called when a money ball goes from
    in_progress → completed; it receives (mb_id, moneyball_dict,
    standings_list) and should post results to the Telegram group.

    `refresh_card(game_id)` is invoked after every mutating game API call
    so the in-chat card stays in sync with the mini-app's view. It runs
    on the same asyncio loop as the bot, so it can call
    bot.edit_message_text directly.
    """
    app = web.Application(middlewares=[auth_middleware])
    app["bot"] = bot
    app["bot_token"] = bot_token
    app["tz"] = tz
    app["completion_callback"] = completion_callback
    app["refresh_card"] = refresh_card

    # Static / health
    app.router.add_get("/", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/register", _serve_static("register.html"))
    app.router.add_get("/manage", _serve_static("manage.html"))
    app.router.add_get("/moneyball", serve_miniapp)         # used by Telegram deep links (id in start_param)
    app.router.add_get("/moneyball/{mb_id}", serve_miniapp) # legacy/direct access

    # Moneyball API (existing)
    app.router.add_get("/api/moneyball/{mb_id}", get_moneyball)
    app.router.add_post("/api/moneyball/{mb_id}/score", post_score)

    # Game state (any chat member)
    app.router.add_get("/api/game/{game_id}", get_game_state)

    # Member-facing mutations
    app.router.add_post("/api/game/{game_id}/register", post_self_register)
    app.router.add_post("/api/game/{game_id}/guest", post_self_guest)
    app.router.add_delete("/api/game/{game_id}/guest/{pid}", delete_self_guest)

    # Admin-facing mutations
    app.router.add_post("/api/game/{game_id}/admin/add_member", admin_add_member)
    app.router.add_post("/api/game/{game_id}/admin/add_guest", admin_add_guest)
    app.router.add_delete(
        "/api/game/{game_id}/admin/participant/{pid}", admin_remove_participant
    )
    app.router.add_post(
        "/api/game/{game_id}/admin/move/{pid}", admin_move_participant
    )
    app.router.add_patch("/api/game/{game_id}", admin_patch_game)
    app.router.add_delete("/api/game/{game_id}", admin_delete_game)
    app.router.add_post(
        "/api/game/{game_id}/admin/pay_toggle/{pid}", admin_pay_toggle
    )

    return app


async def run_http_server(app: web.Application, host: str, port: int) -> web.AppRunner:
    """Start the HTTP server. Returns the runner so we can shut it down cleanly."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(f"HTTP server listening on {host}:{port}")
    return runner
