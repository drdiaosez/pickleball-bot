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


# Short names of the two Mini Apps registered in BotFather via /newapp.
# These are the path segment in t.me/<bot_username>/<short_name>?startapp=<id>.
# Keep in sync with the BotFather setup steps in README.md.
REGISTER_APP_SHORT_NAME = "register"
MANAGE_APP_SHORT_NAME = "manage"


def _miniapp_url(bot_username: str, short_name: str, game_id: int) -> str:
    """Build the t.me deep link that opens a Mini App with a start_param.

    Telegram passes start_param through to the Web App as
    Telegram.WebApp.initDataUnsafe.start_param, which is how the page
    learns which game it's viewing.
    """
    return f"https://t.me/{bot_username}/{short_name}?startapp={game_id}"


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
    bot_username: Optional[str] = None,
    viewer_registered: bool = False,
    has_payment: bool = False,
) -> InlineKeyboardMarkup:
    """Buttons under a game card.

    The card offers three or four buttons that replace the old long
    per-action strip (Join / Add Member / Add Guest / Manage / Leave)
    with two Mini App entry points + Paid + Refresh.

    Mini apps launch from URL deep links of the form
        https://t.me/<bot_username>/<short_name>?startapp=<game_id>
    rather than InlineKeyboardButton(web_app=…), because Telegram only
    accepts `web_app` inline buttons in private chats. Group cards must
    use URL deep links that point at a BotFather-registered Web App
    short name. The two short names we expect are REGISTER_APP_SHORT_NAME
    and MANAGE_APP_SHORT_NAME.

    Args:
        game_id: The game being shown.
        bot_username: The bot's @username (no leading @), used to build
            the t.me deep link. We fetch this from getMe at startup and
            cache it in bot_data; the keyboard falls back to a friendly
            callback-data error if it isn't available.
        viewer_registered: True if the user opening this card has either
            registered themselves (any status) or added at least one
            guest under their name. Drives the Register vs. Update label.
        has_payment: Whether the game has a payment_amount set. When True,
            we surface a 💰 Paid button that opens the existing in-chat
            paid picker (unchanged from the old card flow).
    """
    rows = []

    # 1. Register / Update Registration
    reg_label = "✏️ Update Registration" if viewer_registered else "📝 Register"
    if bot_username:
        rows.append([
            InlineKeyboardButton(
                reg_label,
                url=_miniapp_url(bot_username, REGISTER_APP_SHORT_NAME, game_id),
            ),
        ])
    else:
        # Fallback used only when bot_username isn't known yet. Surfaces a
        # readable error if someone taps it instead of silently dropping.
        rows.append([
            InlineKeyboardButton(reg_label, callback_data=f"miniapp_unavailable:{game_id}"),
        ])

    # 2. Manage (admin-only — UI gating happens in the mini-app)
    if bot_username:
        rows.append([
            InlineKeyboardButton(
                "⚙ Manage (admin only)",
                url=_miniapp_url(bot_username, MANAGE_APP_SHORT_NAME, game_id),
            ),
        ])
    else:
        rows.append([
            InlineKeyboardButton(
                "⚙ Manage (admin only)",
                callback_data=f"miniapp_unavailable:{game_id}",
            ),
        ])

    # 3. Paid — unchanged in-chat picker.
    if has_payment:
        rows.append([
            InlineKeyboardButton("💰 Paid", callback_data=f"paid:{game_id}"),
        ])

    # 4. Refresh — re-render the card with the latest state for this viewer.
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{game_id}"),
    ])
    return InlineKeyboardMarkup(rows)


def recent_locations_keyboard(locations: list[str]) -> InlineKeyboardMarkup:
    """Quick-pick keyboard for the /newgame location step.

    One row per recent location (tapping picks it), plus a final row with a
    "Different location" button that opens the free-text path. Callback data
    is a short index — the actual location string is stashed in user_data by
    the caller so we don't blow the 64-byte callback_data cap on long names.
    """
    rows = []
    for idx, loc in enumerate(locations):
        # Button label can be long; we only need to keep callback_data short.
        label = loc if len(loc) <= 40 else loc[:37] + "…"
        rows.append([InlineKeyboardButton(f"📍 {label}", callback_data=f"newloc:{idx}")])
    rows.append([InlineKeyboardButton("✏️ Different location", callback_data="newloc:new")])
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
