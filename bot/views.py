"""Message formatting and inline-keyboard builders.

Kept separate from handlers so the visual presentation is in one place.
All text uses HTML parse mode (cleaner than Markdown with names that
contain underscores or asterisks).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape


# ─────────────────────── time formatting ─────────────────────── #

def format_when(iso_string: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(iso_string).astimezone(tz)
    # e.g. "Wed May 14 · 6:30 PM"
    return dt.strftime("%a %b %-d · %-I:%M %p")


def format_when_short(iso_string: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(iso_string).astimezone(tz)
    # e.g. "Wed 5/14 · 9:30 AM"
    return dt.strftime("%a %-m/%-d · %-I:%M %p")


# ─────────────────────── money formatting ─────────────────────── #

def format_money(amount_cents: int | None) -> str:
    """Format cents as a $-string. None/0 → empty string.

    Whole dollars print without trailing .00 ("$5"); fractional ones keep
    two decimals ("$7.50"). Negative inputs aren't expected but we clamp.
    """
    if not amount_cents or amount_cents <= 0:
        return ""
    if amount_cents % 100 == 0:
        return f"${amount_cents // 100}"
    return f"${amount_cents / 100:.2f}"


def game_has_payment(game: dict) -> bool:
    amt = game.get("payment_amount_cents")
    return bool(amt and amt > 0)


# ─────────────────────── participant display ─────────────────────── #

def participant_display(p: dict, show_paid: bool = False) -> str:
    """How to render a single participant on a card.

    show_paid: when the parent game has a payment amount set, we prefix
    each name with ✅ / ⬜ to indicate paid status. Callers that don't
    want this (manage view buttons, etc.) leave it False.
    """
    if p["member_id"] is not None:
        name = escape(p["member_name"] or "Unknown")
    else:
        name = f"{escape(p['guest_name'])} <i>(guest of {escape(p['adder_name'])})</i>"
    if show_paid:
        badge = "✅" if p.get("is_paid") else "⬜"
        return f"{badge} {name}"
    return name


# ─────────────────────── game card ─────────────────────── #

def render_game_card(game: dict, participants: list[dict], tz: ZoneInfo, organizer_name: str) -> str:
    """The main message body for a game card."""
    confirmed = [p for p in participants if p["status"] == "confirmed"]
    waitlist = [p for p in participants if p["status"] == "waitlist"]
    has_payment = game_has_payment(game)

    lines = []
    lines.append(f"🎾 <b>{format_when(game['scheduled_for'], tz)}</b>")
    lines.append(f"📍 {escape(game['location'])}")
    lines.append(f"<i>Organized by {escape(organizer_name)}</i>")
    if has_payment:
        lines.append(f"💰 <b>{format_money(game['payment_amount_cents'])}</b> per person")
    if game.get("notes"):
        lines.append(f"📝 {escape(game['notes'])}")
    lines.append("")
    lines.append(f"<b>Confirmed</b> ({len(confirmed)}/{game['max_players']})")
    if confirmed:
        for p in confirmed:
            lines.append(f"  • {participant_display(p, show_paid=has_payment)}")
    else:
        lines.append("  <i>nobody yet</i>")

    if waitlist:
        lines.append("")
        lines.append(f"<b>Waitlist</b> ({len(waitlist)})")
        for i, p in enumerate(waitlist, start=1):
            # Paid status only matters for people who'll actually play.
            # Showing it on waitlist would be confusing.
            lines.append(f"  {i}. {participant_display(p)}")

    if has_payment:
        paid_count = sum(1 for p in confirmed if p.get("is_paid"))
        lines.append("")
        lines.append(f"<i>Paid: {paid_count}/{len(confirmed)}</i>")

    return "\n".join(lines)


def game_card_keyboard(
    game_id: int,
    viewer_in_game: Optional[str],
    game_full: bool,
    has_payment: bool = False,
) -> InlineKeyboardMarkup:
    """Buttons under a game card.

    viewer_in_game: None | "confirmed" | "waitlist"
    has_payment: whether the game has a payment_amount set. When True, we
        surface a 💰 Paid button so anyone can mark themselves/others paid
        without going through Manage.
    """
    rows = []

    # Primary action depends on viewer state
    if viewer_in_game is None:
        if game_full:
            rows.append([
                InlineKeyboardButton("⏳ Join Waitlist", callback_data=f"join:{game_id}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton("✓ Join", callback_data=f"join:{game_id}"),
            ])
    else:
        rows.append([
            InlineKeyboardButton(
                f"✗ Leave ({viewer_in_game})",
                callback_data=f"leave:{game_id}",
            ),
        ])

    rows.append([
        InlineKeyboardButton("+ Add Member", callback_data=f"addmem:{game_id}"),
        InlineKeyboardButton("+ Add Guest", callback_data=f"guest:{game_id}"),
    ])
    if has_payment:
        rows.append([
            InlineKeyboardButton("💰 Paid", callback_data=f"paid:{game_id}"),
        ])
    rows.append([
        InlineKeyboardButton("⚙ Manage", callback_data=f"manage:{game_id}"),
        InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{game_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def member_picker_keyboard(game_id: int, members: list[dict]) -> InlineKeyboardMarkup:
    """Buttons for picking a chat member to add to a game.
    Each member becomes a tappable row. Last row is a cancel."""
    rows = []
    for m in members:
        name = m["display_name"][:32]
        rows.append([
            InlineKeyboardButton(name, callback_data=f"addmem_do:{game_id}:{m['telegram_id']}")
        ])
    rows.append([InlineKeyboardButton("Cancel", callback_data=f"refresh:{game_id}")])
    return InlineKeyboardMarkup(rows)


# ─────────────────────── manage view ─────────────────────── #

def render_manage_view(game: dict, participants: list[dict], tz: ZoneInfo) -> str:
    confirmed = [p for p in participants if p["status"] == "confirmed"]
    waitlist = [p for p in participants if p["status"] == "waitlist"]
    has_payment = game_has_payment(game)

    lines = []
    lines.append(f"<b>Manage:</b> {format_when_short(game['scheduled_for'], tz)} @ {escape(game['location'])}")
    if has_payment:
        amt = format_money(game["payment_amount_cents"])
        paid_count = sum(1 for p in confirmed if p.get("is_paid"))
        lines.append(f"💰 {amt} per person · paid {paid_count}/{len(confirmed)}")
    lines.append("")
    lines.append("<b>CONFIRMED</b>")
    if confirmed:
        for p in confirmed:
            lines.append(f"  {p['position']}. {participant_display(p, show_paid=has_payment)}")
    else:
        lines.append("  <i>nobody yet</i>")
    lines.append("")
    lines.append("<b>WAITLIST</b>")
    if waitlist:
        for p in waitlist:
            lines.append(f"  {p['position']}. {participant_display(p)}")
    else:
        lines.append("  <i>empty</i>")
    lines.append("")
    lines.append("<i>Tap a name below to act on it. Confirmed players can be removed or demoted; waitlist players can be removed or promoted/swapped in.</i>")

    return "\n".join(lines)


def manage_keyboard(game_id: int, participants: list[dict], game_max: int) -> InlineKeyboardMarkup:
    confirmed = [p for p in participants if p["status"] == "confirmed"]
    waitlist = [p for p in participants if p["status"] == "waitlist"]

    rows = []

    # Each confirmed participant gets a row: name → actions
    for p in confirmed:
        label = _short_label(p)
        rows.append([
            InlineKeyboardButton(f"❌ Remove {label}", callback_data=f"rm:{p['id']}"),
            InlineKeyboardButton(f"⬇ {label} to wait", callback_data=f"demote:{p['id']}"),
        ])

    # If there's room and waitlist exists, offer promote-top
    has_space = len(confirmed) < game_max
    if has_space and waitlist:
        rows.append([
            InlineKeyboardButton(
                f"⬆ Promote {_short_label(waitlist[0])} to fill empty slot",
                callback_data=f"promote:{game_id}",
            )
        ])

    # Waitlist actions
    for p in waitlist:
        label = _short_label(p)
        row = [InlineKeyboardButton(f"❌ Remove {label}", callback_data=f"rm:{p['id']}")]
        # If full, offer swap with a confirmed person (we'll prompt for which one)
        if not has_space and confirmed:
            row.append(InlineKeyboardButton(f"🔄 Swap in {label}", callback_data=f"swap_pick:{p['id']}"))
        elif has_space:
            row.append(InlineKeyboardButton(f"⬆ Promote {label}", callback_data=f"promote_one:{p['id']}"))
        rows.append(row)

    # Game settings — edit/delete the game itself
    rows.append([
        InlineKeyboardButton("📅 Edit time", callback_data=f"edit_time:{game_id}"),
        InlineKeyboardButton("📍 Edit location", callback_data=f"edit_loc:{game_id}"),
    ])
    rows.append([
        InlineKeyboardButton("👥 Edit max players", callback_data=f"edit_max:{game_id}"),
        InlineKeyboardButton("📝 Edit notes", callback_data=f"edit_notes:{game_id}"),
    ])
    rows.append([
        InlineKeyboardButton("💰 Edit payment", callback_data=f"edit_pay:{game_id}"),
        InlineKeyboardButton("🗑 Delete game", callback_data=f"delete:{game_id}"),
    ])

    rows.append([InlineKeyboardButton("← Back to game card", callback_data=f"back:{game_id}")])
    return InlineKeyboardMarkup(rows)


def swap_picker_keyboard(waitlist_pid: int, confirmed: list[dict]) -> InlineKeyboardMarkup:
    """When swapping someone in, ask which confirmed player to bump."""
    rows = []
    for p in confirmed:
        rows.append([
            InlineKeyboardButton(
                f"Bump {_short_label(p)} → waitlist",
                callback_data=f"swap_do:{waitlist_pid}:{p['id']}",
            )
        ])
    rows.append([InlineKeyboardButton("Cancel", callback_data="swap_cancel")])
    return InlineKeyboardMarkup(rows)


def _short_label(p: dict) -> str:
    """Compact name for buttons — no HTML, truncated if needed."""
    if p["member_id"] is not None:
        name = p["member_name"] or "?"
    else:
        name = f"{p['guest_name']} (guest)"
    return name if len(name) <= 18 else name[:17] + "…"


# ─────────────────────── game list ─────────────────────── #

def render_game_list_header(count: int, label: str = "Upcoming games") -> str:
    if count == 0:
        return f"<b>{label}</b>\n<i>none scheduled</i>\n\nUse /newgame to add one."
    return f"<b>{label}</b> ({count})"


def game_list_keyboard(games: list[dict], tz: ZoneInfo) -> InlineKeyboardMarkup:
    rows = []
    for g in games:
        label = f"{format_when_short(g['scheduled_for'], tz)} @ {g['location']}"
        if len(label) > 50:
            label = label[:49] + "…"
        rows.append([InlineKeyboardButton(label, callback_data=f"open:{g['id']}")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


# ─────────────────────── paid picker ─────────────────────── #

def render_paid_picker(game: dict, participants: list[dict], tz: ZoneInfo) -> str:
    """Header for the paid-picker view."""
    confirmed = [p for p in participants if p["status"] == "confirmed"]
    paid_count = sum(1 for p in confirmed if p.get("is_paid"))
    amt = format_money(game.get("payment_amount_cents"))
    lines = [
        f"<b>💰 Mark as paid</b>",
        f"<i>{format_when_short(game['scheduled_for'], tz)} @ {escape(game['location'])}</i>",
        f"{amt} per person · {paid_count}/{len(confirmed)} paid",
        "",
        "Tap a name to toggle paid/unpaid:",
    ]
    return "\n".join(lines)


def paid_picker_keyboard(game_id: int, participants: list[dict]) -> InlineKeyboardMarkup:
    """One row per confirmed player, with a ✅/⬜ badge that flips when tapped.

    Waitlist players are omitted — they aren't playing, so there's nothing
    to collect from them. If/when they get promoted, they show up here.
    """
    confirmed = [p for p in participants if p["status"] == "confirmed"]
    rows = []
    for p in confirmed:
        badge = "✅" if p.get("is_paid") else "⬜"
        label = f"{badge} {_short_label(p)}"
        rows.append([InlineKeyboardButton(label, callback_data=f"pay_toggle:{p['id']}")])
    rows.append([InlineKeyboardButton("← Back to game card", callback_data=f"back:{game_id}")])
    return InlineKeyboardMarkup(rows)
