"""Roster handlers — every inline-button interaction on a game card.

Callback data format: "<action>:<arg1>[:<arg2>]"

Actions:
  open:<game_id>           — render a game card (from /games list)
  refresh:<game_id>        — re-render the card in place
  join:<game_id>           — current user joins (auto-routes to confirmed or waitlist)
  leave:<game_id>          — current user leaves
  guest:<game_id>          — prompt for a guest name
  manage:<game_id>         — open the manage view
  back:<game_id>           — back from manage view to game card
  rm:<participant_id>      — remove a participant (with confirmation)
  rm_yes:<participant_id>  — confirm removal
  rm_no:<participant_id>   — cancel removal
  demote:<participant_id>  — move confirmed → bottom of waitlist (auto-promotes top)
  promote:<game_id>        — fill empty confirmed slot from top of waitlist
  promote_one:<pid>        — promote a specific waitlist person (only when slot is open)
  swap_pick:<wait_pid>     — show picker for which confirmed person to bump
  swap_do:<wait_pid>:<conf_pid> — execute the soft swap
  swap_cancel              — cancel a swap-in-progress
"""
from __future__ import annotations

import logging
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from .. import db, views
from .common import touch_member

log = logging.getLogger(__name__)


# ─────────────────────── card posting & refresh ─────────────────────── #

async def post_game_card(context: ContextTypes.DEFAULT_TYPE, chat_id: int, game_id: int) -> None:
    """Send a fresh game card and remember its message_id."""
    tz = context.bot_data["tz"]
    game = db.get_game(game_id)
    if not game:
        return
    participants = db.get_participants(game_id)
    organizer = db.get_member(game["organizer_id"])
    org_name = organizer["display_name"] if organizer else "?"

    text = views.render_game_card(game, participants, tz, org_name)
    confirmed = sum(1 for p in participants if p["status"] == "confirmed")
    kb = views.game_card_keyboard(
        game_id,
        viewer_in_game=None,  # generic — buttons re-render per viewer via refresh
        game_full=confirmed >= game["max_players"],
    )

    msg = await context.bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=kb
    )
    db.set_game_message(game_id, chat_id, msg.message_id)


async def render_card_in_place(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    game_id: int,
    viewer_user_id: int,
) -> None:
    """Edit an existing card message with current state."""
    tz = context.bot_data["tz"]
    game = db.get_game(game_id)
    if not game:
        return
    participants = db.get_participants(game_id)
    organizer = db.get_member(game["organizer_id"])
    org_name = organizer["display_name"] if organizer else "?"

    viewer_state = None
    for p in participants:
        if p["member_id"] == viewer_user_id:
            viewer_state = p["status"]
            break

    text = views.render_game_card(game, participants, tz, org_name)
    confirmed = sum(1 for p in participants if p["status"] == "confirmed")
    kb = views.game_card_keyboard(
        game_id,
        viewer_in_game=viewer_state,
        game_full=confirmed >= game["max_players"],
    )

    query = update.callback_query
    try:
        await query.edit_message_text(
            text=text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
    except BadRequest as e:
        # "message is not modified" is fine — happens on no-op refresh
        if "not modified" not in str(e).lower():
            raise


async def render_manage_in_place(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    game_id: int,
) -> None:
    tz = context.bot_data["tz"]
    game = db.get_game(game_id)
    if not game:
        return
    participants = db.get_participants(game_id)

    text = views.render_manage_view(game, participants, tz)
    kb = views.manage_keyboard(game_id, participants, game["max_players"])

    query = update.callback_query
    try:
        await query.edit_message_text(
            text=text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ─────────────────────── callback router ─────────────────────── #

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from .common import gate
    if not await gate(update):
        return
    await touch_member(update)
    query = update.callback_query
    await query.answer()  # dismiss the loading spinner immediately

    data = query.data or ""
    parts = data.split(":")
    action = parts[0]
    args = parts[1:]
    user = update.effective_user

    try:
        if action == "open":
            game_id = int(args[0])
            await _open_card(context, update, game_id, user.id)

        elif action == "refresh":
            await render_card_in_place(context, update, int(args[0]), user.id)

        elif action == "join":
            await _handle_join(context, update, int(args[0]), user.id)

        elif action == "leave":
            await _handle_leave(context, update, int(args[0]), user.id)

        elif action == "guest":
            await _prompt_guest(context, update, int(args[0]), user.id)

        elif action == "addmem":
            await _show_member_picker(context, update, int(args[0]))

        elif action == "addmem_do":
            await _do_add_member(context, update, int(args[0]), int(args[1]), user.id)

        elif action == "manage":
            await render_manage_in_place(context, update, int(args[0]))

        elif action == "back":
            await render_card_in_place(context, update, int(args[0]), user.id)

        elif action == "rm":
            await _confirm_remove(update, int(args[0]))

        elif action == "rm_yes":
            await _do_remove(context, update, int(args[0]))

        elif action == "rm_no":
            pid = int(args[0])
            p = db.get_participant(pid)
            if p:
                await render_manage_in_place(context, update, p["game_id"])

        elif action == "demote":
            await _do_demote(context, update, int(args[0]))

        elif action == "promote":
            await _do_promote_top(context, update, int(args[0]))

        elif action == "promote_one":
            await _do_promote_one(context, update, int(args[0]))

        elif action == "swap_pick":
            await _show_swap_picker(update, int(args[0]))

        elif action == "swap_do":
            await _do_swap(context, update, int(args[0]), int(args[1]))

        elif action == "swap_cancel":
            # Fall back to the manage view if we can figure out the game
            await query.edit_message_text("Swap cancelled.")

        elif action == "edit_time":
            await _prompt_edit_time(context, update, int(args[0]), user.id)

        elif action == "edit_loc":
            await _prompt_edit_location(context, update, int(args[0]), user.id)

        elif action == "edit_max":
            await _prompt_edit_max(context, update, int(args[0]), user.id)

        elif action == "edit_notes":
            await _prompt_edit_notes(context, update, int(args[0]), user.id)

        elif action == "delete":
            await _confirm_delete(update, int(args[0]))

        elif action == "delete_yes":
            await _do_delete(context, update, int(args[0]))

        elif action == "delete_no":
            await render_manage_in_place(context, update, int(args[0]))

        else:
            log.warning("Unknown callback action: %s", action)

    except Exception:
        log.exception("Callback handler failed for %s", data)
        try:
            await query.answer("Something went wrong.", show_alert=True)
        except Exception:
            pass


# ─────────────────────── action implementations ─────────────────────── #

async def _open_card(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int, viewer_id: int
) -> None:
    """Open a game card as a fresh message (used from the /games list)."""
    tz = context.bot_data["tz"]
    game = db.get_game(game_id)
    if not game:
        await update.callback_query.answer("Game not found.", show_alert=True)
        return
    participants = db.get_participants(game_id)
    organizer = db.get_member(game["organizer_id"])
    org_name = organizer["display_name"] if organizer else "?"

    viewer_state = None
    for p in participants:
        if p["member_id"] == viewer_id:
            viewer_state = p["status"]
            break

    text = views.render_game_card(game, participants, tz, org_name)
    confirmed = sum(1 for p in participants if p["status"] == "confirmed")
    kb = views.game_card_keyboard(
        game_id,
        viewer_in_game=viewer_state,
        game_full=confirmed >= game["max_players"],
    )
    msg = await update.callback_query.message.reply_html(text, reply_markup=kb)
    # Update the canonical card message reference to this newest one.
    # IMPORTANT: only update message_id, NOT chat_id — opening a card in DM
    # must not rewrite the game's group ownership.
    db.set_game_message_only(game_id, msg.message_id)


async def _handle_join(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int, user_id: int
) -> None:
    game = db.get_game(game_id)
    if not game:
        await update.callback_query.answer("Game not found.", show_alert=True)
        return
    try:
        result = db.add_participant(game_id, added_by=user_id, member_id=user_id)
    except ValueError as e:
        msg = str(e)
        if msg.startswith("already_"):
            await update.callback_query.answer(
                f"You're already on this game ({msg.removeprefix('already_')}).",
                show_alert=True,
            )
        else:
            await update.callback_query.answer("Couldn't join.", show_alert=True)
        return

    if result["status"] == "confirmed":
        await update.callback_query.answer("You're in. 🎾")
    else:
        await update.callback_query.answer(f"Waitlisted — position {result['position']}.")
    await render_card_in_place(context, update, game_id, user_id)


async def _handle_leave(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int, user_id: int
) -> None:
    existing = db.member_is_in_game(game_id, user_id)
    if not existing:
        await update.callback_query.answer("You're not on this game.", show_alert=True)
        return
    promoted = db.remove_participant(existing["id"])
    await update.callback_query.answer("Removed.")
    await render_card_in_place(context, update, game_id, user_id)

    if promoted and promoted.get("member_id"):
        await _notify_promoted(context, game_id, promoted)


async def _prompt_guest(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int, user_id: int
) -> None:
    """Tell the user to reply with the guest's name. We stash the pending state."""
    context.user_data["pending_guest"] = {"game_id": game_id, "added_by": user_id}
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"@{update.effective_user.username or update.effective_user.first_name}, "
            "reply with your guest's name (just a name, e.g. <code>Pat</code>). "
            "Send /cancelguest to abort."
        ),
        parse_mode=ParseMode.HTML,
    )


async def on_guest_name_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches the next text message after a 'guest' button, if pending."""
    pending = context.user_data.get("pending_guest")
    if not pending:
        return  # Not waiting for a guest name — let other handlers run
    text = (update.effective_message.text or "").strip()
    if text.startswith("/cancelguest"):
        context.user_data.pop("pending_guest", None)
        await update.effective_message.reply_text("Cancelled.")
        return
    if not text or len(text) > 40:
        await update.effective_message.reply_text("Send a name (under 40 chars), or /cancelguest.")
        return

    game_id = pending["game_id"]
    added_by = pending["added_by"]
    context.user_data.pop("pending_guest", None)

    try:
        result = db.add_participant(game_id, added_by=added_by, guest_name=text)
    except ValueError:
        await update.effective_message.reply_text("Couldn't add guest.")
        return

    where = "confirmed" if result["status"] == "confirmed" else f"waitlist #{result['position']}"
    await update.effective_message.reply_text(f"Added {text} ({where}).")

    # Re-post the card if we have a chat_id
    game = db.get_game(game_id)
    if game and game.get("chat_id"):
        await post_game_card(context, game["chat_id"], game_id)


async def cancel_guest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_guest", None)
    await update.effective_message.reply_text("Cancelled.")


async def _confirm_remove(update: Update, participant_id: int) -> None:
    p = db.get_participant(participant_id)
    if not p:
        await update.callback_query.answer("Already gone.")
        return
    name = p["member_name"] or f"{p['guest_name']} (guest)"
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"Yes, remove {name}", callback_data=f"rm_yes:{participant_id}"),
            InlineKeyboardButton("No", callback_data=f"rm_no:{participant_id}"),
        ]
    ])
    await update.callback_query.edit_message_text(
        f"Remove <b>{name}</b> from this game?",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _do_remove(
    context: ContextTypes.DEFAULT_TYPE, update: Update, participant_id: int
) -> None:
    p = db.get_participant(participant_id)
    if not p:
        await update.callback_query.answer("Already gone.")
        return
    game_id = p["game_id"]
    promoted = db.remove_participant(participant_id)
    await render_manage_in_place(context, update, game_id)
    if promoted and promoted.get("member_id"):
        await _notify_promoted(context, game_id, promoted)


async def _do_demote(
    context: ContextTypes.DEFAULT_TYPE, update: Update, participant_id: int
) -> None:
    p = db.get_participant(participant_id)
    if not p:
        return
    promoted = db.demote_to_waitlist(participant_id)
    await render_manage_in_place(context, update, p["game_id"])
    if promoted and promoted.get("member_id"):
        await _notify_promoted(context, p["game_id"], promoted)


async def _do_promote_top(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int
) -> None:
    promoted = db.promote_top_of_waitlist(game_id)
    await render_manage_in_place(context, update, game_id)
    if promoted and promoted.get("member_id"):
        await _notify_promoted(context, game_id, promoted)


async def _do_promote_one(
    context: ContextTypes.DEFAULT_TYPE, update: Update, participant_id: int
) -> None:
    """Promote a specific waitlist person. Only valid when a slot is open."""
    p = db.get_participant(participant_id)
    if not p or p["status"] != "waitlist":
        return
    game = db.get_game(p["game_id"])
    if not game:
        return
    if db.confirmed_count(p["game_id"]) >= game["max_players"]:
        await update.callback_query.answer("No empty slot — use Swap instead.", show_alert=True)
        return
    # Move this specific participant up; simplest is remove + re-add as confirmed
    # but we'd lose ordering. Instead: just flip status + position.
    with db.transaction() as conn:
        new_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS n FROM participants WHERE game_id = ? AND status = 'confirmed'",
            (p["game_id"],),
        ).fetchone()["n"]
        conn.execute(
            "UPDATE participants SET status = 'confirmed', position = ? WHERE id = ?",
            (new_pos, participant_id),
        )
    # Recompact waitlist
    db._renumber(p["game_id"], "waitlist")  # noqa: SLF001
    await render_manage_in_place(context, update, p["game_id"])
    if p.get("member_id"):
        await _notify_promoted(context, p["game_id"], db.get_participant(participant_id))


async def _show_swap_picker(update: Update, waitlist_pid: int) -> None:
    p = db.get_participant(waitlist_pid)
    if not p:
        return
    participants = db.get_participants(p["game_id"])
    confirmed = [x for x in participants if x["status"] == "confirmed"]
    if not confirmed:
        await update.callback_query.answer("No confirmed players to swap with.", show_alert=True)
        return
    name = p["member_name"] or f"{p['guest_name']} (guest)"
    kb = views.swap_picker_keyboard(waitlist_pid, confirmed)
    await update.callback_query.edit_message_text(
        f"Swap <b>{name}</b> in for which confirmed player?\n\n"
        "<i>The bumped player moves to the top of the waitlist (not deleted), so this is reversible.</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _do_swap(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    waitlist_pid: int,
    confirmed_pid: int,
) -> None:
    try:
        new_confirmed, new_waitlisted = db.swap_with_waitlist(confirmed_pid, waitlist_pid)
    except ValueError as e:
        await update.callback_query.answer(f"Swap failed: {e}", show_alert=True)
        return

    await render_manage_in_place(context, update, new_confirmed["game_id"])

    # Notifications
    if new_confirmed.get("member_id"):
        await _notify_promoted(context, new_confirmed["game_id"], new_confirmed)
    if new_waitlisted.get("member_id"):
        await _notify_bumped(context, new_waitlisted["game_id"], new_waitlisted)


# ─────────────────────── game edit / delete ─────────────────────── #

async def _prompt_edit_time(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int, user_id: int
) -> None:
    context.user_data["pending_edit"] = {"game_id": game_id, "field": "time"}
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"@{update.effective_user.username or update.effective_user.first_name}, "
            "reply with the new date/time (e.g. <code>wed 7</code>, "
            "<code>5/14 8am</code>, <code>fri 7pm</code>). "
            "Send /canceledit to abort."
        ),
        parse_mode=ParseMode.HTML,
    )


async def _prompt_edit_location(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int, user_id: int
) -> None:
    context.user_data["pending_edit"] = {"game_id": game_id, "field": "location"}
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"@{update.effective_user.username or update.effective_user.first_name}, "
            "reply with the new location. Send /canceledit to abort."
        ),
    )


async def _prompt_edit_max(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int, user_id: int
) -> None:
    context.user_data["pending_edit"] = {"game_id": game_id, "field": "max"}
    chat_id = update.effective_chat.id
    game = db.get_game(game_id)
    current = game["max_players"] if game else 4
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"@{update.effective_user.username or update.effective_user.first_name}, "
            f"reply with the new max players (currently {current}, send a number 2-32). "
            "If you shrink below the current confirmed roster, the most-recently-added "
            "players move to the top of the waitlist. Send /canceledit to abort."
        ),
    )


async def _prompt_edit_notes(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int, user_id: int
) -> None:
    context.user_data["pending_edit"] = {"game_id": game_id, "field": "notes"}
    chat_id = update.effective_chat.id
    game = db.get_game(game_id)
    current = game.get("notes") if game else None
    current_line = f"\n\nCurrent notes: <i>{current}</i>" if current else ""
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            f"@{update.effective_user.username or update.effective_user.first_name}, "
            "reply with the new notes/description (e.g. \"bring 4 balls\", \"$5 buy-in\"). "
            "Send <code>clear</code> to remove notes, or /canceledit to abort."
            f"{current_line}"
        ),
        parse_mode=ParseMode.HTML,
    )


async def on_edit_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Catches the next text message after an edit_* button, if pending."""
    pending = context.user_data.get("pending_edit")
    if not pending:
        return  # not waiting for an edit — let other handlers run
    text = (update.effective_message.text or "").strip()
    if text.startswith("/canceledit"):
        context.user_data.pop("pending_edit", None)
        await update.effective_message.reply_text("Edit cancelled.")
        return

    game_id = pending["game_id"]
    field = pending["field"]
    tz = context.bot_data["tz"]

    game = db.get_game(game_id)
    if not game:
        context.user_data.pop("pending_edit", None)
        await update.effective_message.reply_text("Game no longer exists.")
        return

    if field == "time":
        # Late import — parse_datetime lives in newgame.py
        from .newgame import parse_datetime
        from datetime import datetime, timedelta
        dt = parse_datetime(text, tz)
        if dt is None:
            await update.effective_message.reply_text(
                "Couldn't read that. Try something like 'wed 7' or '5/14 8am'."
            )
            return
        if dt < datetime.now(tz) - timedelta(minutes=10):
            await update.effective_message.reply_text("That looks like it's in the past. Try again?")
            return
        db.update_game_time(game_id, dt)
        context.user_data.pop("pending_edit", None)
        await update.effective_message.reply_html(
            f"✓ Time updated to <b>{views.format_when(dt.isoformat(), tz)}</b>."
        )
        await _notify_time_change(context, game_id, dt)

    elif field == "location":
        if not text or len(text) > 100:
            await update.effective_message.reply_text("Need a location under 100 chars.")
            return
        db.update_game_location(game_id, text)
        context.user_data.pop("pending_edit", None)
        await update.effective_message.reply_html(f"✓ Location updated to <b>{text}</b>.")

    elif field == "notes":
        # "clear" or "none" wipes the notes; otherwise truncate to 200 chars
        new_notes: Optional[str]
        if text.lower() in ("clear", "none", "remove", "delete"):
            new_notes = None
        elif len(text) > 200:
            await update.effective_message.reply_text("Keep notes under 200 chars.")
            return
        else:
            new_notes = text
        db.update_game_notes(game_id, new_notes)
        context.user_data.pop("pending_edit", None)
        if new_notes is None:
            await update.effective_message.reply_text("✓ Notes cleared.")
        else:
            await update.effective_message.reply_html(f"✓ Notes updated to <b>{new_notes}</b>.")

    elif field == "max":
        try:
            n = int(text)
            if not (2 <= n <= 32):
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text("Send a number between 2 and 32.")
            return
        result = db.update_game_max(game_id, n)
        context.user_data.pop("pending_edit", None)

        msg = f"✓ Max players updated to <b>{n}</b>."
        if result["demoted"]:
            names = ", ".join(_name(p) for p in result["demoted"])
            msg += f"\nMoved to waitlist: {names}"
        if result["promoted"]:
            names = ", ".join(_name(p) for p in result["promoted"])
            msg += f"\nPromoted to confirmed: {names}"
        await update.effective_message.reply_html(msg)

        # DM affected members
        for p in result["promoted"]:
            if p.get("member_id"):
                await _notify_promoted(context, game_id, p)
        for p in result["demoted"]:
            if p.get("member_id"):
                await _notify_bumped(context, game_id, p)

    # Re-post the card so the group sees fresh state
    game = db.get_game(game_id)
    if game and game.get("chat_id"):
        await post_game_card(context, game["chat_id"], game_id)


async def cancel_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("pending_edit", None)
    await update.effective_message.reply_text("Edit cancelled.")


async def _confirm_delete(update: Update, game_id: int) -> None:
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes, delete game", callback_data=f"delete_yes:{game_id}"),
            InlineKeyboardButton("No, keep it", callback_data=f"delete_no:{game_id}"),
        ]
    ])
    await update.callback_query.edit_message_text(
        "⚠ <b>Delete this game?</b>\n\n"
        "This removes the game and everyone signed up. "
        "Anyone who was confirmed will be notified.",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def _do_delete(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int
) -> None:
    game = db.get_game(game_id)
    if not game:
        await update.callback_query.answer("Already gone.")
        return
    tz = context.bot_data["tz"]
    participants = db.get_participants(game_id)
    # Pull confirmed-member IDs before we delete the rows
    affected = [p for p in participants if p.get("member_id")]

    db.delete_game(game_id)

    await update.callback_query.edit_message_text(
        f"🗑 Game deleted: <s>{views.format_when(game['scheduled_for'], tz)} "
        f"@ {game['location']}</s>",
        parse_mode=ParseMode.HTML,
    )

    # DM everyone who was signed up
    text = (
        f"🗑 The game on <b>{views.format_when(game['scheduled_for'], tz)}</b> "
        f"@ {game['location']} was deleted by "
        f"{update.effective_user.first_name or update.effective_user.username}."
    )
    for p in affected:
        try:
            await context.bot.send_message(
                chat_id=p["member_id"], text=text, parse_mode=ParseMode.HTML
            )
        except Exception as e:
            log.info("Couldn't DM user %s about delete: %s", p["member_id"], e)


async def _notify_time_change(
    context: ContextTypes.DEFAULT_TYPE, game_id: int, new_dt
) -> None:
    """DM everyone signed up that the time changed."""
    tz = context.bot_data["tz"]
    game = db.get_game(game_id)
    if not game:
        return
    participants = db.get_participants(game_id)
    text = (
        f"📅 The game at {game['location']} has been rescheduled to "
        f"<b>{views.format_when(new_dt.isoformat(), tz)}</b>."
    )
    for p in participants:
        if p.get("member_id"):
            try:
                await context.bot.send_message(
                    chat_id=p["member_id"], text=text, parse_mode=ParseMode.HTML
                )
            except Exception as e:
                log.info("Couldn't DM user %s about reschedule: %s", p["member_id"], e)


def _name(p: dict) -> str:
    """Plain-text name for a participant (for inline messages)."""
    if p.get("member_id"):
        return p.get("member_name") or "?"
    return f"{p.get('guest_name')} (guest)"


# ─────────────────────── add-member picker ─────────────────────── #

async def _show_member_picker(
    context: ContextTypes.DEFAULT_TYPE, update: Update, game_id: int
) -> None:
    """Replace the game card with a picker showing members not yet on the roster."""
    game = db.get_game(game_id)
    if not game:
        return
    chat_id = game.get("chat_id")
    members = db.list_members_not_in_game(game_id, chat_id=chat_id)
    if not members:
        await update.callback_query.answer(
            "All known members are already on this game's roster.",
            show_alert=True,
        )
        return

    # Build a lightweight preface so it's clear what's happening
    tz = context.bot_data["tz"]
    when = views.format_when(game["scheduled_for"], tz)
    text = (
        f"<b>Add member to:</b>\n"
        f"<i>{when} @ {game['location']}</i>\n\n"
        f"Tap a name to add them:"
    )
    kb = views.member_picker_keyboard(game_id, members)
    try:
        await update.callback_query.edit_message_text(
            text=text, parse_mode=ParseMode.HTML, reply_markup=kb
        )
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def _do_add_member(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    game_id: int,
    target_member_id: int,
    actor_user_id: int,
) -> None:
    """Add target member to the game, then re-render the game card."""
    # Validate target is known
    target = db.get_member(target_member_id)
    if not target:
        await update.callback_query.answer("That member is gone.", show_alert=True)
        return

    try:
        result = db.add_participant(
            game_id, added_by=actor_user_id, member_id=target_member_id
        )
    except ValueError as e:
        msg = str(e)
        if msg.startswith("already_"):
            await update.callback_query.answer(
                f"{target['display_name']} is already on this game.",
                show_alert=True,
            )
        else:
            await update.callback_query.answer("Couldn't add.", show_alert=True)
        # Fall back to re-rendering the card so the user isn't stuck on the picker
        await render_card_in_place(context, update, game_id, actor_user_id)
        return

    where = "confirmed" if result["status"] == "confirmed" else f"waitlist #{result['position']}"
    await update.callback_query.answer(f"Added {target['display_name']} ({where}).")
    await render_card_in_place(context, update, game_id, actor_user_id)

    # DM the added member if they ended up confirmed (don't spam for waitlist)
    if result["status"] == "confirmed":
        try:
            tz = context.bot_data["tz"]
            game = db.get_game(game_id)
            when = views.format_when(game["scheduled_for"], tz)
            await context.bot.send_message(
                chat_id=target_member_id,
                text=(
                    f"➕ <b>{update.effective_user.first_name or 'A member'}</b> added you "
                    f"to a game.\n\n"
                    f"<b>{when}</b> @ {game['location']}\n\n"
                    f"<i>Tap Leave on the game card in the group if you can't make it.</i>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            log.info("Couldn't DM added member %s: %s", target_member_id, e)


# ─────────────────────── DM notifications ─────────────────────── #

async def _notify_promoted(
    context: ContextTypes.DEFAULT_TYPE, game_id: int, participant: dict
) -> None:
    """DM a member that they've been promoted to confirmed."""
    if not participant.get("member_id"):
        return
    tz = context.bot_data["tz"]
    game = db.get_game(game_id)
    if not game:
        return
    text = (
        f"🎉 You're <b>in</b> for the game on "
        f"{views.format_when(game['scheduled_for'], tz)} @ {game['location']}."
    )
    try:
        await context.bot.send_message(
            chat_id=participant["member_id"], text=text, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        # Most likely the user has never DM'd the bot — silent fail is fine
        log.info("Couldn't DM user %s: %s", participant["member_id"], e)


async def _notify_bumped(
    context: ContextTypes.DEFAULT_TYPE, game_id: int, participant: dict
) -> None:
    if not participant.get("member_id"):
        return
    tz = context.bot_data["tz"]
    game = db.get_game(game_id)
    if not game:
        return
    text = (
        f"⚠ You've been moved to the <b>waitlist</b> for the game on "
        f"{views.format_when(game['scheduled_for'], tz)} @ {game['location']}.\n\n"
        f"You're now #1 on the waitlist."
    )
    try:
        await context.bot.send_message(
            chat_id=participant["member_id"], text=text, parse_mode=ParseMode.HTML
        )
    except Exception as e:
        log.info("Couldn't DM user %s: %s", participant["member_id"], e)


async def on_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Single text-message dispatcher.

    Several flows need to capture the next message from a user (add guest,
    edit time/location/max). Each one stashes a pending-state dict in
    user_data; this handler checks them in order and routes to the right
    function. If nothing is pending, it's a no-op so other group chatter
    flows through untouched.
    """
    from .common import is_authorized
    # Silent gate here — this handler sees *every* text message in the group,
    # so we don't want to reply to ordinary chat with "this is private".
    if not is_authorized(update):
        return
    if context.user_data.get("pending_guest"):
        await on_guest_name_message(update, context)
        return
    if context.user_data.get("pending_edit"):
        await on_edit_message(update, context)
        return
    # nothing pending — ignore


# ─────────────────────── handler registration ─────────────────────── #

def build_roster_handlers() -> list:
    return [
        CallbackQueryHandler(on_callback),
        # Single dispatcher for all "next message" capture flows.
        MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_message),
    ]


def build_cancel_guest_handler():
    from telegram.ext import CommandHandler
    return CommandHandler("cancelguest", cancel_guest)


def build_cancel_edit_handler():
    from telegram.ext import CommandHandler
    return CommandHandler("canceledit", cancel_edit)
