"""Telegram command handlers, button callbacks and the scheduled daily digest.

User-facing texts are Latvian; command names stay English.
"""
from __future__ import annotations

import html
import logging
import secrets
import shlex
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, ChatMigrated, Forbidden, TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from . import config, routes, surfr, woo
from .analysis import sectors_from_toggles, toggles_from_sectors
from .checker import gather_results
from .config import Spot, Subscription
from .messages import (
    DIRECTION_LABELS_LV, any_windows, build_digest, describe_spot,
    format_window, split_message, unit_label,
)

log = logging.getLogger(__name__)

MAX_SPOT_NAME = 25  # keeps callback_data under Telegram's 64-byte limit
LOCATION_WAIT = timedelta(minutes=15)

# (chat_id, user_id) -> {"name": str, "expires": datetime}. In-memory is fine
# for a single-process bot — after a restart the admin just redoes /addspot.
_pending_locations: dict = {}

DENIED_LV = "Atvaino — to var tikai grupas admini vai config.yaml norādītie admini."

HELP_LV = """\
🪁 <b>KiteBot</b> — vēja prognozes taviem kaita spotiem.

<b>Prognoze</b>
/prognoze — prognoze visiem spotiem
/marsruts — dienas maršruts, ja vējš maina spotus

<b>Mans profils</b>
/home — saglabāt mājas vietu maršrutiem
/quiver 12 9 7 — saglabāt kaitu izmērus
/weight 85 — saglabāt svaru (kaitu ieteikumiem)
/profile — apskatīt profilu
/menu — prognoze ar pogām (viss vai viens spots)
/check — prognoze visiem spotiem
/check &lt;spots&gt; — vienam spotam

<b>Ikdienas ziņa</b>
/subscribe — ieslēgt ikdienas prognozi šajā čatā
/myspots — izvēlēties, kurus spotus rādīt šī čata ziņā
/unsubscribe — atslēgt
/testdigest — izmēģināt ikdienas ziņu (adminiem)

<b>Spoti</b>
/spots — spotu saraksts
/addspot Nosaukums — pievienot spotu (pēc tam atsūti atrašanās vietu 📎)
/manage — pārvaldība ar pogām: virzieni, dzēšana (adminiem)
/delspot &lt;nosaukums&gt; — dzēst ar komandu

<b>Braucēju statistika (WOO / Surfr)</b>
/records — komandas lēcienu rekordu tabula
/riders — braucēji un rekordi; 🔗 apvieno WOO+Surfr vienam cilvēkam
/woorider Vārds — pievienot braucēju no WOO (adminiem)
/surfrider Vārds — pievienot braucēju no Surfr (adminiem)
/setrecord Vārds 6.2 — labot rekordu manuāli (adminiem)

/id — čata un lietotāja ID

Vēja dati: open-meteo.com · braucēju statistika: woosports.com
"""

MANAGE_TEXT_LV = (
    "🛠 <b>Spotu pārvaldība</b>\n"
    "🧭 — atzīmēt derīgos vēja virzienus · 🗑 — dzēst spotu.\n"
    "Jaunu spotu pievieno ar /addspot Nosaukums un atrašanās vietu."
)

DIR_TEXT_LV = (
    "🧭 <b>{name}</b> — kuri vēja virzieni der?\n"
    "Virziens = NO kurienes pūš vējš. Spied, lai ieslēgtu/izslēgtu; izmaiņas "
    "saglabājas uzreiz. Ja nav atzīmēts neviens — der jebkurš virziens."
)


def addspot_usage(settings) -> str:
    # The full lat/lon + key=value form still works; it lives in the README so
    # the in-chat help stays simple.
    return ("Uzraksti “/addspot Nosaukums” un pēc tam atsūti spota "
            "atrašanās vietu (📎 → Location).")


def _spots_word(n: int) -> str:
    return "spots" if n % 10 == 1 and n % 100 != 11 else "spoti"


def _find_spot(spots: list, name: str) -> "Spot | None":
    return next((s for s in spots if s.name.lower() == name.lower()), None)


def _validate_name(name: str) -> "str | None":
    if not name:
        return "Nosaukums nedrīkst būt tukšs."
    if len(name) > MAX_SPOT_NAME:
        return f"Nosaukums par garu (maksimums {MAX_SPOT_NAME} zīmes)."
    if ":" in name:
        return "Nosaukumā nedrīkst būt kols (:)."
    return None


def _is_float(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


# --- permissions ------------------------------------------------------------

async def _is_admin(context: ContextTypes.DEFAULT_TYPE, chat, user, settings) -> bool:
    """Users in admin_user_ids always; group admins in their group; private
    chats are open unless admin_user_ids restricts them."""
    if user is not None and user.id in settings.admin_user_ids:
        return True
    if chat is None or user is None:
        return False
    if chat.type == ChatType.PRIVATE:
        return not settings.admin_user_ids
    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError as exc:
        log.warning("could not check admin status in chat %s: %s", chat.id, exc)
        return False
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def _can_configure(update: Update, context: ContextTypes.DEFAULT_TYPE, settings) -> bool:
    if await _is_admin(context, update.effective_chat, update.effective_user, settings):
        return True
    if update.effective_message is not None:
        await update.effective_message.reply_text(DENIED_LV)
    return False


async def _can_manage_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   settings) -> bool:
    """Subscriptions in a PRIVATE chat only affect that person's own DM, so
    anyone may manage them there even when admin_user_ids restricts editing.
    Group subscriptions stay admin-only."""
    chat = update.effective_chat
    if chat is not None and chat.type == ChatType.PRIVATE:
        return True
    return await _can_configure(update, context, settings)


async def _can_edit_spots(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          settings) -> bool:
    """Adding spots and tuning their wind directions is open to every group
    member — the group is the trust boundary. Private chats keep the admin
    gate (any Telegram user can DM a bot). Deleting stays admin-only."""
    chat = update.effective_chat
    if chat is not None and chat.type != ChatType.PRIVATE:
        return True
    return await _can_configure(update, context, settings)


async def _cb_can_edit_spots(query, context: ContextTypes.DEFAULT_TYPE, settings) -> bool:
    chat = query.message.chat if query.message is not None else None
    if chat is not None and chat.type != ChatType.PRIVATE:
        return True
    return await _cb_admin(query, context, settings)


async def _cb_admin(query, context: ContextTypes.DEFAULT_TYPE, settings) -> bool:
    chat = query.message.chat if query.message is not None else None
    if await _is_admin(context, chat, query.from_user, settings):
        return True
    await query.answer(DENIED_LV, show_alert=True)
    return False


def _subscription_for(msg) -> Subscription:
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    return Subscription(chat_id=msg.chat_id, thread_id=thread_id)


# --- keyboards ----------------------------------------------------------------

def _menu_keyboard(spots: list) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🌍 Visi spoti", callback_data="check:*")]]
    row: list = []
    for s in spots:
        row.append(InlineKeyboardButton(s.name, callback_data=f"check:{s.name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _manage_keyboard(spots: list) -> InlineKeyboardMarkup:
    rows = []
    for s in spots:
        rows.append([
            InlineKeyboardButton(f"🧭 {s.name}", callback_data=f"dir_open:{s.name}"),
            InlineKeyboardButton("🗑 Dzēst", callback_data=f"del:{s.name}"),
        ])
    return InlineKeyboardMarkup(rows)


def _dir_keyboard(spot: Spot) -> InlineKeyboardMarkup:
    toggles = toggles_from_sectors(spot.good_directions)
    rows: list = []
    row: list = []
    for i, label in enumerate(DIRECTION_LABELS_LV):
        mark = "✅ " if toggles[i] else ""
        row.append(InlineKeyboardButton(mark + label, callback_data=f"dir:{spot.name}:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("✳️ Jebkurš virziens", callback_data=f"dir:{spot.name}:any"),
        InlineKeyboardButton("✔️ Gatavs", callback_data=f"dir:{spot.name}:done"),
    ])
    rows.append([InlineKeyboardButton("⬅️ Atpakaļ uz sarakstu", callback_data="manage")])
    return InlineKeyboardMarkup(rows)


# --- forecast ------------------------------------------------------------------

async def _run_check(context: ContextTypes.DEFAULT_TYPE, chat_id: int, thread_id,
                     spots: list, settings, all_spots: list) -> None:
    try:
        await context.bot.send_chat_action(
            chat_id=chat_id, action=ChatAction.TYPING, message_thread_id=thread_id)
    except TelegramError:
        pass
    results = await gather_results(spots, settings)
    parts = split_message(build_digest(results, settings))
    for i, part in enumerate(parts):
        await context.bot.send_message(
            chat_id=chat_id, text=part, parse_mode=ParseMode.HTML,
            message_thread_id=thread_id,
            reply_markup=_menu_keyboard(all_spots) if i == len(parts) - 1 else None,
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is not None:
        await msg.reply_html(HELP_LV)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    spots = config.load_spots(settings)
    if not spots:
        await msg.reply_text("Vēl nav neviena spota.\n" + addspot_usage(settings))
        return
    await msg.reply_text("🪁 Ko pārbaudīt?", reply_markup=_menu_keyboard(spots))


async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    all_spots = config.load_spots(settings)
    if not all_spots:
        await msg.reply_text("Vēl nav neviena spota.\n" + addspot_usage(settings))
        return
    spots = all_spots
    if context.args:
        query = " ".join(context.args).strip().lower()
        spots = [s for s in all_spots if query in s.name.lower()]
        if not spots:
            await msg.reply_text("Neviens spots neatbilst. /spots parādīs sarakstu.")
            return
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    await _run_check(context, msg.chat_id, thread_id, spots, settings, all_spots)


async def cmd_prognoze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forecast for ALL spots, nothing else — a pure planning view."""
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    spots = config.load_spots(settings)
    if not spots:
        await msg.reply_text("Vēl nav neviena spota.\n" + addspot_usage(settings))
        return
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    await _run_check(context, msg.chat_id, thread_id, spots, settings, spots)


ROUTE_DAYS_LV = ["Šodien", "Rīt", "Parīt"]
ROUTE_PREFS_LV = {"s": "stiprāku vēju", "l": "vieglāku vēju", "a": "jebkādu vēju"}

# (chat_id, user_id) -> pending route waiting for the user's shared location
_pending_route: dict = {}
# (chat_id, user_id) -> pending /home waiting for the user's shared location
_pending_home: dict = {}
# share tokens for the "📤 Nosūtīt grupai" button on DM route results
_route_share: dict = {}


async def cmd_marsruts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    spots = config.load_spots(settings)
    if len(spots) < 2:
        await msg.reply_text("Maršrutam vajag vismaz divus spotus.")
        return
    days = min(settings.forecast_days, len(ROUTE_DAYS_LV))
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(ROUTE_DAYS_LV[i], callback_data=f"route:{i}")
        for i in range(days)
    ]])
    await msg.reply_text("🗺 Kurai dienai plānot maršrutu?", reply_markup=keyboard)


ROUTE_DISTS = [("50", "≤50 km"), ("100", "≤100 km"), ("200", "≤200 km"), ("0", "♾️ Vienalga")]


def _dist_label(code: str) -> str:
    return dict(ROUTE_DISTS).get(code, "♾️").replace("♾️ Vienalga", "bez limita")


def _profile_kite_args(user) -> tuple:
    """(weight_kg, quiver) from the user's profile, for kite recommendations."""
    if user is None:
        return None, None
    profile = config.get_profile(user.id)
    return profile.get("weight_kg"), profile.get("quiver")


async def _route_text(settings, offset: int, prefer: "str | None",
                      origin: "tuple | None" = None,
                      max_drive_km: "float | None" = None,
                      weight_kg: "float | None" = None,
                      quiver: "list | None" = None) -> str:
    spots = config.load_spots(settings)
    results = await gather_results(spots, settings)
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    target = (now + timedelta(days=offset)).date()
    day_lv = ROUTE_DAYS_LV[offset] if offset < len(ROUTE_DAYS_LV) else target.isoformat()
    items = [(r.spot, w) for r in results for w in r.windows if w.start.date() == target]
    if not items:
        return f"🗺 {day_lv}: nevienā spotā nav braucama vēja — maršrutu nesanāk."
    depart = now if (offset == 0 and origin is not None) else None
    legs, total, single = routes.day_route(items, prefer=prefer, origin=origin,
                                           depart_earliest=depart, max_drive_km=max_drive_km)
    if legs and total >= single + 0.5:
        return routes.format_route(legs, total, settings,
                                   weight_kg=weight_kg, quiver=quiver)
    name, best_hours, drive_km, within = routes.best_single(items, origin, max_drive_km)
    hours = f"{round(best_hours * 2) / 2:g}".replace(".", ",")
    drive_txt = f", 🚗 ~{round(drive_km)} km no tevis" if drive_km is not None else ""
    label = unit_label(settings.wind_unit)
    condition_lines = []
    for spot, w in items:
        if spot.name != name:
            continue
        line = format_window(w, label)
        kite = routes.pick_kite((w.min_speed + w.max_speed) / 2,
                                settings.wind_unit, weight_kg, quiver)
        if kite is not None:
            line += f" · 🪁 {kite:g}"
        condition_lines.append(line)
    conditions = "\n".join(condition_lines)
    if not within:
        head = (f"🗺 {day_lv}: {round(max_drive_km)} km robežās no tevis braucama vēja "
                f"nav. Tuvākais spots ar vēju: <b>{html.escape(name)}</b> "
                f"(~{hours} h ūdenī{drive_txt}):")
    else:
        head = (f"🗺 {day_lv}: braukāt apkārt neatmaksājas — labākais ir palikt "
                f"spotā <b>{html.escape(name)}</b> (~{hours} h ūdenī{drive_txt}):")
    return head + "\n" + conditions


async def _cb_route(query, context, settings, arg: str) -> None:
    """Step 1 → 2: the day is picked, ask for the wind preference by editing
    the same message (one message total in the chat)."""
    try:
        offset = max(0, min(int(arg), settings.forecast_days - 1))
    except ValueError:
        await query.answer()
        return
    await query.answer()
    day_lv = ROUTE_DAYS_LV[offset] if offset < len(ROUTE_DAYS_LV) else str(offset)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💨 Stiprāku", callback_data=f"routew:{offset}:s"),
        InlineKeyboardButton("🍃 Vieglāku", callback_data=f"routew:{offset}:l"),
        InlineKeyboardButton("🤷 Vienalga", callback_data=f"routew:{offset}:a"),
    ]])
    try:
        await query.edit_message_text(f"🗺 {day_lv} — kādu vēju gribi?", reply_markup=keyboard)
    except BadRequest:
        pass


async def _cb_route_wind(query, context, settings, payload: str) -> None:
    """Step 2 → 3: wind picked, ask how far the rider is willing to drive."""
    day_arg, _, pref_key = payload.partition(":")
    try:
        offset = max(0, min(int(day_arg), settings.forecast_days - 1))
    except ValueError:
        await query.answer()
        return
    await query.answer()
    day_lv = ROUTE_DAYS_LV[offset] if offset < len(ROUTE_DAYS_LV) else str(offset)
    rows = []
    row = []
    for code, label in ROUTE_DISTS:
        row.append(InlineKeyboardButton(label, callback_data=f"routed:{offset}:{pref_key}:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    try:
        await query.edit_message_text(
            f"🗺 {day_lv}, {ROUTE_PREFS_LV.get(pref_key, '')} — cik tālu kopā esi gatavs braukt?",
            reply_markup=InlineKeyboardMarkup(rows))
    except BadRequest:
        pass


def _parse_route_args(payload: str, settings) -> "tuple | None":
    parts = payload.split(":")
    try:
        offset = max(0, min(int(parts[0]), settings.forecast_days - 1))
    except (ValueError, IndexError):
        return None
    pref_key = parts[1] if len(parts) > 1 else "a"
    dist_code = parts[2] if len(parts) > 2 else "0"
    prefer = {"s": "strong", "l": "light"}.get(pref_key)
    max_drive = float(dist_code) if dist_code.isdigit() and dist_code != "0" else None
    return offset, pref_key, prefer, dist_code, max_drive


async def _cb_route_dist(query, context, settings, payload: str) -> None:
    """Step 3 → result (groups) or step 4 (DM: optional location)."""
    parsed = _parse_route_args(payload, settings)
    if parsed is None:
        await query.answer()
        return
    offset, pref_key, prefer, dist_code, max_drive = parsed
    m = query.message
    if m is None:
        await query.answer()
        return
    if m.chat.type == ChatType.PRIVATE:
        await query.answer()
        user = query.from_user
        if user is not None:
            _pending_route[(m.chat.id, user.id)] = {
                "offset": offset, "prefer": prefer, "max_drive": max_drive,
                "expires": datetime.now(timezone.utc) + LOCATION_WAIT,
            }
        day_lv = ROUTE_DAYS_LV[offset] if offset < len(ROUTE_DAYS_LV) else str(offset)
        buttons = []
        if user is not None and config.get_profile(user.id).get("home"):
            buttons.append(InlineKeyboardButton(
                "🏠 No manām mājām", callback_data=f"routeh:{offset}:{pref_key}:{dist_code}"))
        buttons.append(InlineKeyboardButton(
            "⏭️ Bez atrašanās vietas", callback_data=f"routego:{offset}:{pref_key}:{dist_code}"))
        try:
            await query.edit_message_text(
                f"🗺 {day_lv}, {ROUTE_PREFS_LV.get(pref_key, '')}, {_dist_label(dist_code)} — "
                "atsūti savu atrašanās vietu (📎 → Location), lai ņemtu vērā braucienu "
                "no tevis, vai izvēlies pogu.",
                reply_markup=InlineKeyboardMarkup([buttons]))
        except BadRequest:
            pass
        return
    # group: compute right away, the config message becomes the result
    await query.answer("Plānoju maršrutu…")
    try:
        await query.edit_message_text("⏳ Plānoju maršrutu…")
    except BadRequest:
        pass
    weight_kg, quiver = _profile_kite_args(query.from_user)
    text = await _route_text(settings, offset, prefer, max_drive_km=max_drive,
                             weight_kg=weight_kg, quiver=quiver)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except BadRequest:
        pass


async def _cb_route_go(query, context, settings, payload: str) -> None:
    """DM final step without a location."""
    parsed = _parse_route_args(payload, settings)
    if parsed is None:
        await query.answer()
        return
    offset, _, prefer, _, max_drive = parsed
    m = query.message
    user = query.from_user
    if m is not None and user is not None:
        _pending_route.pop((m.chat.id, user.id), None)
    await query.answer("Plānoju maršrutu…")
    if m is None:
        return
    try:
        await query.edit_message_text("⏳ Plānoju maršrutu…")
    except BadRequest:
        pass
    weight_kg, quiver = _profile_kite_args(user)
    text = await _route_text(settings, offset, prefer, max_drive_km=max_drive,
                             weight_kg=weight_kg, quiver=quiver)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=_route_share_keyboard(text))
    except BadRequest:
        pass


async def _cb_route_home(query, context, settings, payload: str) -> None:
    """DM final step using the saved home location."""
    parsed = _parse_route_args(payload, settings)
    if parsed is None:
        await query.answer()
        return
    offset, _, prefer, _, max_drive = parsed
    m = query.message
    user = query.from_user
    home = config.get_profile(user.id).get("home") if user is not None else None
    if home is None:
        await query.answer("Mājas vieta nav saglabāta — /home.", show_alert=True)
        return
    if m is not None and user is not None:
        _pending_route.pop((m.chat.id, user.id), None)
    await query.answer("Plānoju maršrutu…")
    if m is None:
        return
    try:
        await query.edit_message_text("⏳ Plānoju maršrutu…")
    except BadRequest:
        pass
    weight_kg, quiver = _profile_kite_args(user)
    text = await _route_text(settings, offset, prefer,
                             origin=(home["lat"], home["lon"]), max_drive_km=max_drive,
                             weight_kg=weight_kg, quiver=quiver)
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=_route_share_keyboard(text))
    except BadRequest:
        pass


def _route_share_keyboard(text: str) -> "InlineKeyboardMarkup | None":
    """A 'send to group' button for DM results, when a group is subscribed."""
    if not any(s.chat_id < 0 for s in config.load_subscriptions()):
        return None
    token = secrets.token_hex(4)
    _route_share[token] = text
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Nosūtīt grupai", callback_data=f"rshare:{token}"),
    ]])


async def _cb_route_share(query, context, settings, token: str) -> None:
    text = _route_share.get(token)
    if text is None:
        await query.answer("Šis maršruts vairs nav pieejams — uztaisi jaunu.", show_alert=True)
        return
    user = query.from_user
    group_ids = {s.chat_id for s in config.load_subscriptions() if s.chat_id < 0}
    sent = 0
    for chat_id in group_ids:
        try:
            member = await context.bot.get_chat_member(chat_id, user.id) if user else None
            if member is None or member.status in ("left", "kicked"):
                continue
            await context.bot.send_message(chat_id=chat_id, text=text,
                                           parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramError as exc:
            log.warning("route share to %s failed: %s", chat_id, exc)
    await query.answer("Nosūtīts grupai." if sent else "Neizdevās nosūtīt nevienai grupai.",
                       show_alert=not sent)


async def cmd_spots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    spots = config.load_spots(settings)
    if not spots:
        await msg.reply_text("Vēl nav neviena spota.\n" + addspot_usage(settings))
        return
    label = unit_label(settings.wind_unit)
    text = "🪁 <b>Spoti</b>\n\n" + "\n".join(describe_spot(s, label) for s in spots)
    for part in split_message(text):
        await msg.reply_html(part)


# --- adding spots ----------------------------------------------------------------

def _parse_sectors(raw: str) -> list:
    sectors = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        lo, sep, hi = chunk.partition("-")
        if not sep:
            raise ValueError(f"nesaprotu sektoru “{chunk}” — jābūt, piemēram, 290-20")
        lo_f, hi_f = float(lo), float(hi)
        if not (0 <= lo_f <= 360 and 0 <= hi_f <= 360):
            raise ValueError("sektoru robežām jābūt 0–360 grādos")
        sectors.append([lo_f, hi_f])
    return sectors


def _apply_spot_options(spot: Spot, tokens: list, label: str) -> None:
    """Apply key=value option tokens onto a spot; raises ValueError on bad input."""
    for token in tokens:
        key, sep, value = token.partition("=")
        if not sep:
            raise ValueError(f"jābūt atslēga=vērtība, saņēmu: {token}")
        key = key.lower()
        if key == "min":
            spot.min_wind = float(value)
        elif key == "max":
            spot.max_wind = float(value)
        elif key == "dirs":
            spot.good_directions = _parse_sectors(value)
        elif key == "cell":
            if value not in ("land", "sea", "nearest"):
                raise ValueError("cell jābūt land, sea vai nearest")
            spot.cell_selection = value
        elif key == "model":
            spot.model = config.normalize_model(value)
        else:
            raise ValueError(f"nezināma opcija “{key}”")
    if not 0 < spot.min_wind < spot.max_wind:
        raise ValueError(f"jābūt 0 < min < max (vēja ātrums, {label})")


async def cmd_addspot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    settings = config.load_settings()
    if not await _can_edit_spots(update, context, settings):
        return
    usage = addspot_usage(settings)
    raw = (msg.text or "").split(maxsplit=1)
    if len(raw) < 2:
        await msg.reply_text(usage)
        return
    try:
        tokens = shlex.split(raw[1])
    except ValueError as exc:
        await msg.reply_text(f"Neizdevās nolasīt: {exc}\n\n{usage}")
        return
    if not tokens:
        await msg.reply_text(usage)
        return

    full_form = len(tokens) >= 3 and _is_float(tokens[1]) and _is_float(tokens[2])
    label = unit_label(settings.wind_unit)
    if not full_form:
        kv_tokens = [t for t in tokens if "=" in t]
        name = " ".join(t for t in tokens if "=" not in t).strip()
        error = _validate_name(name)
        if error:
            await msg.reply_text(f"⚠️ {error}")
            return
        if kv_tokens:
            # settings-only update of an existing spot, e.g. /addspot Liepāja cell=sea
            spots = config.load_spots(settings)
            existing = _find_spot(spots, name)
            if existing is None:
                await msg.reply_text("Neatradu spotu ar tādu nosaukumu (/spots parādīs "
                                     "sarakstu). Jaunam spotam vajag lat/lon vai atrašanās vietu.")
                return
            try:
                _apply_spot_options(existing, kv_tokens, label)
            except ValueError as exc:
                await msg.reply_text(f"⚠️ {exc}\n\n{usage}")
                return
            config.save_spots(spots)
            await msg.reply_html("Atjaunināts: " + describe_spot(existing, label))
            return
        # simple flow: /addspot Name → wait for a shared location
        _pending_locations[(msg.chat_id, user.id)] = {
            "name": name,
            "expires": datetime.now(timezone.utc) + LOCATION_WAIT,
        }
        exists = _find_spot(config.load_spots(settings), name) is not None
        note = " (šāds spots jau ir — atjaunināšu tā vietu)" if exists else ""
        await msg.reply_text(
            f"Labi, “{name}”{note}! Tagad atsūti spota atrašanās vietu šajā čatā: "
            "📎 (pielikums) → Location → izvēlies vietu kartē pēc iespējas tuvāk "
            "startam pie ūdens.\n"
            "Ja grupā es atrašanās vietu neredzu (Telegram privātuma režīms), "
            "izdari to pie manis privāti vai padari mani par grupas adminu."
        )
        return

    # full form; an update keeps the existing spot's unspecified settings
    name = tokens[0].strip()
    error = _validate_name(name)
    if error:
        await msg.reply_text(f"⚠️ {error}")
        return
    spots = config.load_spots(settings)
    existing = _find_spot(spots, name)
    lo_default, hi_default = config.UNIT_DEFAULT_RANGE[settings.wind_unit]
    try:
        spot = Spot(
            name=name, lat=float(tokens[1]), lon=float(tokens[2]),
            min_wind=existing.min_wind if existing else lo_default,
            max_wind=existing.max_wind if existing else hi_default,
            good_directions=[list(s) for s in existing.good_directions] if existing else [],
            cell_selection=existing.cell_selection if existing else "land",
        )
        _apply_spot_options(spot, tokens[3:], label)
        if not (-90 <= spot.lat <= 90 and -180 <= spot.lon <= 180):
            raise ValueError("lat/lon ārpus robežām")
    except ValueError as exc:
        await msg.reply_text(f"⚠️ {exc}\n\n{usage}")
        return
    if existing is not None:
        spots[spots.index(existing)] = spot
    else:
        spots.append(spot)
    config.save_spots(spots)
    prefix = "Atjaunināts: " if existing else "✅ Pievienots: "
    await msg.reply_html(prefix + describe_spot(spot, label))


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or msg.location is None or user is None:
        return
    route_pending = _pending_route.pop((msg.chat_id, user.id), None)
    if route_pending is not None:
        if datetime.now(timezone.utc) > route_pending["expires"]:
            await msg.reply_text("Laiks pagājis — sāc vēlreiz ar /marsruts.")
            return
        settings = config.load_settings()
        origin = (msg.location.latitude, msg.location.longitude)
        weight_kg, quiver = _profile_kite_args(user)
        text = await _route_text(settings, route_pending["offset"],
                                 route_pending["prefer"], origin=origin,
                                 max_drive_km=route_pending.get("max_drive"),
                                 weight_kg=weight_kg, quiver=quiver)
        keyboard = _route_share_keyboard(text) if msg.chat.type == ChatType.PRIVATE else None
        await msg.reply_html(text, reply_markup=keyboard)
        return
    home_pending = _pending_home.pop((msg.chat_id, user.id), None)
    if home_pending is not None:
        if datetime.now(timezone.utc) > home_pending["expires"]:
            await msg.reply_text("Laiks pagājis — sāc vēlreiz ar /home.")
            return
        config.update_profile(user.id, home={
            "lat": round(msg.location.latitude, 5),
            "lon": round(msg.location.longitude, 5),
        })
        await msg.reply_text("🏠 Mājas vieta saglabāta! Maršrutu plānošanā tagad būs "
                             "poga “No manām mājām”.")
        return
    pending = _pending_locations.get((msg.chat_id, user.id))
    if pending is None:
        return
    _pending_locations.pop((msg.chat_id, user.id), None)
    if datetime.now(timezone.utc) > pending["expires"]:
        await msg.reply_text("Laiks pagājis — sāc vēlreiz ar /addspot Nosaukums.")
        return
    settings = config.load_settings()
    lo, hi = config.UNIT_DEFAULT_RANGE[settings.wind_unit]
    spot = Spot(name=pending["name"],
                lat=round(msg.location.latitude, 5),
                lon=round(msg.location.longitude, 5),
                min_wind=lo, max_wind=hi)
    spots = config.load_spots(settings)
    existing = _find_spot(spots, spot.name)
    if existing is not None:
        spot.min_wind, spot.max_wind = existing.min_wind, existing.max_wind
        spot.good_directions = existing.good_directions
        spot.cell_selection = existing.cell_selection
        spots[spots.index(existing)] = spot
    else:
        spots.append(spot)
    config.save_spots(spots)
    label = unit_label(settings.wind_unit)
    prefix = "Atjaunināts: " if existing else "✅ Pievienots: "
    await msg.reply_html(prefix + describe_spot(spot, label))
    await msg.reply_html(
        DIR_TEXT_LV.format(name=html.escape(spot.name)),
        reply_markup=_dir_keyboard(spot),
    )


async def cmd_delspot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_configure(update, context, settings):
        return
    raw = (msg.text or "").split(maxsplit=1)
    if len(raw) < 2:
        await msg.reply_text("Lietošana: /delspot <nosaukums> — vai /manage ar pogām.")
        return
    try:
        name = " ".join(shlex.split(raw[1])).strip().lower()
    except ValueError:
        name = raw[1].strip().lower()
    spots = config.load_spots(settings)
    remaining = [s for s in spots if s.name.lower() != name]
    if len(remaining) == len(spots):
        await msg.reply_text("Spota ar tādu nosaukumu nav — /spots parādīs sarakstu.")
        return
    config.save_spots(remaining)
    await msg.reply_text(f"Dzēsts. Palika {len(remaining)} {_spots_word(len(remaining))}.")


# --- manage menu (buttons) --------------------------------------------------------

async def cmd_manage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_configure(update, context, settings):
        return
    spots = config.load_spots(settings)
    if not spots:
        await msg.reply_text("Vēl nav neviena spota.\n" + addspot_usage(settings))
        return
    await msg.reply_html(MANAGE_TEXT_LV, reply_markup=_manage_keyboard(spots))


# --- button callbacks --------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    data = query.data
    settings = config.load_settings()
    if data.startswith("check:"):
        await _cb_check(query, context, settings, data[len("check:"):])
    elif data == "manage":
        await _cb_manage_view(query, context, settings)
    elif data.startswith("del:"):
        await _cb_delete_confirm(query, context, settings, data[len("del:"):])
    elif data.startswith("delok:"):
        await _cb_delete(query, context, settings, data[len("delok:"):])
    elif data.startswith("dir_open:"):
        await _cb_dir_open(query, context, settings, data[len("dir_open:"):])
    elif data.startswith("dir:"):
        await _cb_dir_toggle(query, context, settings, data[len("dir:"):])
    elif data.startswith("sub:"):
        await _cb_sub_toggle(query, context, settings, data[len("sub:"):])
    elif data.startswith("wadd:"):
        await _cb_woo_add(query, context, settings, data[len("wadd:"):])
    elif data.startswith("wdel:"):
        await _cb_woo_del(query, context, settings, data[len("wdel:"):])
    elif data.startswith("wm1:"):
        await _cb_merge_pick(query, context, settings, data[len("wm1:"):])
    elif data.startswith("wm2:"):
        await _cb_merge_do(query, context, settings, data[len("wm2:"):])
    elif data == "riders":
        await _cb_riders_view(query, context, settings)
    elif data.startswith("route:"):
        await _cb_route(query, context, settings, data[len("route:"):])
    elif data.startswith("routew:"):
        await _cb_route_wind(query, context, settings, data[len("routew:"):])
    elif data.startswith("routed:"):
        await _cb_route_dist(query, context, settings, data[len("routed:"):])
    elif data.startswith("routego:"):
        await _cb_route_go(query, context, settings, data[len("routego:"):])
    elif data.startswith("routeh:"):
        await _cb_route_home(query, context, settings, data[len("routeh:"):])
    elif data.startswith("rshare:"):
        await _cb_route_share(query, context, settings, data[len("rshare:"):])
    else:
        await query.answer()


async def _cb_check(query, context, settings, target: str) -> None:
    spots = config.load_spots(settings)
    selected = spots if target == "*" else [s for s in spots if s.name.lower() == target.lower()]
    if not selected:
        await query.answer("Šis spots vairs nav sarakstā.", show_alert=True)
        return
    await query.answer("Skatos prognozi…")
    m = query.message
    if m is None:
        return
    thread_id = m.message_thread_id if getattr(m, "is_topic_message", False) else None
    await _run_check(context, m.chat.id, thread_id, selected, settings, spots)


async def _cb_manage_view(query, context, settings) -> None:
    if not await _cb_admin(query, context, settings):
        return
    await query.answer()
    spots = config.load_spots(settings)
    try:
        if spots:
            await query.edit_message_text(MANAGE_TEXT_LV, parse_mode=ParseMode.HTML,
                                          reply_markup=_manage_keyboard(spots))
        else:
            await query.edit_message_text("Vairs nav neviena spota. Pievieno ar /addspot Nosaukums.")
    except BadRequest:
        pass  # e.g. "message is not modified"


async def _cb_delete_confirm(query, context, settings, name: str) -> None:
    if not await _cb_admin(query, context, settings):
        return
    spot = _find_spot(config.load_spots(settings), name)
    if spot is None:
        await query.answer("Spots nav atrasts.", show_alert=True)
        return
    await query.answer()
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Jā, dzēst", callback_data=f"delok:{spot.name}"),
        InlineKeyboardButton("↩️ Atcelt", callback_data="manage"),
    ]])
    try:
        await query.edit_message_text(f"Tiešām dzēst spotu “{spot.name}”?", reply_markup=keyboard)
    except BadRequest:
        pass


async def _cb_delete(query, context, settings, name: str) -> None:
    if not await _cb_admin(query, context, settings):
        return
    spots = config.load_spots(settings)
    remaining = [s for s in spots if s.name.lower() != name.lower()]
    if len(remaining) == len(spots):
        await query.answer("Spots jau ir dzēsts.", show_alert=True)
    else:
        config.save_spots(remaining)
        await query.answer("Dzēsts.")
    try:
        if remaining:
            await query.edit_message_text(MANAGE_TEXT_LV, parse_mode=ParseMode.HTML,
                                          reply_markup=_manage_keyboard(remaining))
        else:
            await query.edit_message_text("Vairs nav neviena spota. Pievieno ar /addspot Nosaukums.")
    except BadRequest:
        pass


async def _cb_dir_open(query, context, settings, name: str) -> None:
    if not await _cb_can_edit_spots(query, context, settings):
        return
    spot = _find_spot(config.load_spots(settings), name)
    if spot is None:
        await query.answer("Spots nav atrasts.", show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_text(
            DIR_TEXT_LV.format(name=html.escape(spot.name)),
            parse_mode=ParseMode.HTML, reply_markup=_dir_keyboard(spot),
        )
    except BadRequest:
        pass


async def _cb_dir_toggle(query, context, settings, payload: str) -> None:
    name, _, action = payload.rpartition(":")
    if not name:
        await query.answer()
        return
    if not await _cb_can_edit_spots(query, context, settings):
        return
    spots = config.load_spots(settings)
    spot = _find_spot(spots, name)
    if spot is None:
        await query.answer("Spots nav atrasts.", show_alert=True)
        return
    if action == "done":
        await query.answer("Saglabāts.")
        label = unit_label(settings.wind_unit)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Atpakaļ uz sarakstu", callback_data="manage")]])
        try:
            await query.edit_message_text("✅ " + describe_spot(spot, label),
                                          parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except BadRequest:
            pass
        return
    if action == "any":
        spot.good_directions = []
    else:
        try:
            index = int(action)
        except ValueError:
            await query.answer()
            return
        toggles = toggles_from_sectors(spot.good_directions)
        toggles[index % 8] = not toggles[index % 8]
        spot.good_directions = sectors_from_toggles(toggles)
    config.save_spots(spots)
    await query.answer()
    try:
        await query.edit_message_reply_markup(_dir_keyboard(spot))
    except BadRequest:
        pass


# --- subscriptions & info -------------------------------------------------------

# --- personal profile (home, quiver, weight) -------------------------------------

async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    _pending_home[(msg.chat_id, user.id)] = {
        "expires": datetime.now(timezone.utc) + LOCATION_WAIT,
    }
    note = ("" if msg.chat.type == ChatType.PRIVATE
            else "\n(Labāk dari to privātā sarunā ar mani — grupā tavu vietu redzēs visi.)")
    await msg.reply_text("🏠 Atsūti savu mājas atrašanās vietu (📎 → Location) — "
                         f"izmantošu to maršrutu plānošanā.{note}")


async def cmd_quiver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    parts = (msg.text or "").split()[1:]
    sizes = []
    for part in parts:
        try:
            size = float(part.replace(",", "."))
        except ValueError:
            sizes = []
            break
        if not 2 <= size <= 25:
            sizes = []
            break
        sizes.append(size)
    if not sizes or len(sizes) > 8:
        await msg.reply_text("Lietošana: /quiver 12 9 7 — tavi kaitu izmēri m² (2–25, līdz 8 kaitiem).")
        return
    config.update_profile(user.id, quiver=sorted(sizes, reverse=True))
    await msg.reply_text("🪁 Saglabāts: " + ", ".join(f"{s:g}" for s in sorted(sizes, reverse=True))
                         + " m². Maršrutos tagad ieteiks, kurus ņemt līdzi (vajag arī /weight).")


async def cmd_weight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    parts = (msg.text or "").split()
    try:
        weight = float(parts[1].replace(",", "."))
    except (IndexError, ValueError):
        weight = 0
    if not 30 <= weight <= 200:
        await msg.reply_text("Lietošana: /weight 85 — tavs svars kilogramos (30–200).")
        return
    config.update_profile(user.id, weight_kg=weight)
    await msg.reply_text(f"⚖️ Saglabāts: {weight:g} kg. Maršrutos tagad ieteiks kaitu "
                         "izmērus (vajag arī /quiver).")


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if msg is None or user is None:
        return
    profile = config.get_profile(user.id)
    home = "saglabāta ✅" if profile.get("home") else "nav (/home)"
    quiver = (", ".join(f"{s:g}" for s in profile["quiver"]) + " m²"
              if profile.get("quiver") else "nav (/quiver 12 9 7)")
    weight = f"{profile['weight_kg']:g} kg" if profile.get("weight_kg") else "nav (/weight 85)"
    await msg.reply_text(f"👤 Tavs profils:\n🏠 Mājas vieta: {home}\n"
                         f"🪁 Kaiti: {quiver}\n⚖️ Svars: {weight}")


async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_manage_subscription(update, context, settings):
        return
    if config.add_subscription(_subscription_for(msg)):
        note = "" if settings.post_when_no_wind else " dienās, kad ir braucams vējš"
        await msg.reply_text(
            f"✅ Ikdienas kaita prognoze šeit ieslēgta — sūtīšu ap {settings.daily_post_time} "
            f"({settings.timezone}){note}. Ar /myspots vari izvēlēties, kurus spotus šeit rādīt."
        )
    else:
        await msg.reply_text("Šis čats jau saņem ikdienas prognozi. Atslēgt: /unsubscribe.")


MYSPOTS_TEXT_LV = (
    "📌 <b>Kurus spotus rādīt šī čata ikdienas ziņā?</b>\n"
    "Spied, lai ieslēgtu/izslēgtu; izmaiņas saglabājas uzreiz. "
    "Ja nav atzīmēts neviens — rādīšu visus."
)


def _myspots_keyboard(spots: list, sub: Subscription) -> InlineKeyboardMarkup:
    selected = {n.lower() for n in sub.spots}
    rows: list = []
    row: list = []
    for s in spots:
        mark = "✅ " if s.name.lower() in selected else ""
        row.append(InlineKeyboardButton(mark + s.name, callback_data=f"sub:{s.name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton("✳️ Visi spoti", callback_data="sub:*"),
        InlineKeyboardButton("✔️ Gatavs", callback_data="sub:done"),
    ])
    return InlineKeyboardMarkup(rows)


async def cmd_myspots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_manage_subscription(update, context, settings):
        return
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    sub = config.find_subscription(msg.chat_id, thread_id)
    if sub is None:
        await msg.reply_text("Šis čats vēl nesaņem ikdienas prognozi — vispirms /subscribe.")
        return
    spots = config.load_spots(settings)
    if not spots:
        await msg.reply_text("Vēl nav neviena spota.\n" + addspot_usage(settings))
        return
    await msg.reply_html(MYSPOTS_TEXT_LV, reply_markup=_myspots_keyboard(spots, sub))


async def _cb_sub_toggle(query, context, settings, action: str) -> None:
    m = query.message
    if m is None:
        await query.answer()
        return
    # own-DM subscriptions are personal: no admin gate in private chats
    if m.chat.type != ChatType.PRIVATE and not await _cb_admin(query, context, settings):
        return
    thread_id = m.message_thread_id if getattr(m, "is_topic_message", False) else None
    sub = config.find_subscription(m.chat.id, thread_id)
    if sub is None:
        await query.answer("Šis čats nav pierakstīts — vispirms /subscribe.", show_alert=True)
        return
    spots = config.load_spots(settings)
    if action == "done":
        which = ", ".join(sub.spots) if sub.spots else "visi spoti"
        await query.answer("Saglabāts.")
        try:
            await query.edit_message_text(f"✅ Šī čata ikdienas ziņā: {which}")
        except BadRequest:
            pass
        return
    if action == "*":
        sub = config.set_subscription_spots(m.chat.id, thread_id, ())
    else:
        spot = _find_spot(spots, action)
        if spot is None:
            await query.answer("Spots nav atrasts.", show_alert=True)
            return
        if spot.name.lower() in {n.lower() for n in sub.spots}:
            new_names = tuple(n for n in sub.spots if n.lower() != spot.name.lower())
        else:
            new_names = sub.spots + (spot.name,)
        sub = config.set_subscription_spots(m.chat.id, thread_id, new_names)
    if sub is None:
        await query.answer()
        return
    await query.answer()
    try:
        await query.edit_message_reply_markup(_myspots_keyboard(spots, sub))
    except BadRequest:
        pass


async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_manage_subscription(update, context, settings):
        return
    if config.remove_subscription(_subscription_for(msg)):
        await msg.reply_text("Ikdienas prognoze šeit atslēgta.")
    else:
        await msg.reply_text("Šis čats nebija pierakstīts.")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    user = update.effective_user
    sub = _subscription_for(msg)
    lines = [f"Čata ID: <code>{msg.chat_id}</code>"]
    if sub.thread_id is not None:
        lines.append(f"Temata ID: <code>{sub.thread_id}</code>")
    if user is not None:
        lines.append(f"Tavs lietotāja ID: <code>{user.id}</code>")
    existing = config.find_subscription(sub.chat_id, sub.thread_id)
    if existing is None:
        lines.append("Ikdienas prognoze šeit: nē")
    else:
        which = html.escape(", ".join(existing.spots)) if existing.spots else "visi spoti"
        lines.append(f"Ikdienas prognoze šeit: jā ({which})")
    await msg.reply_html("\n".join(lines))


# --- daily digest ----------------------------------------------------------------

DAILY_TITLE_LV = "Ikdienas kaita prognoze"


# --- WOO rider recap ------------------------------------------------------------

# /woorider search results awaiting an admin's button tap; woo_id -> candidate
_woo_candidates: dict = {}


def _num_lv(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def _woo_token(settings) -> str:
    return settings.woo_token or woo.DEFAULT_TOKEN


def _surfr_token(settings) -> str:
    return settings.surfr_token or surfr.DEFAULT_TOKEN


def _yesterday_range(settings) -> tuple:
    """(start_epoch, end_epoch, iso_date) of yesterday in the bot's timezone."""
    tz = ZoneInfo(settings.timezone)
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=1)
    return int(start.timestamp()), int(midnight.timestamp()), start.date().isoformat()


async def build_woo_section(settings, update_records: bool) -> tuple:
    """(section_html | None, latvian_status) for the 'yesterday's heroes' part.
    Merges WOO and Surfr riders; any provider failure is logged and skipped."""
    riders = config.load_riders()
    if not riders:
        return None, "nav pievienotu braucēju (/woorider, /surfrider)"
    start, end, date_str = _yesterday_range(settings)
    woo_ids = {r["ids"]["woo"] for r in riders if "woo" in r["ids"]}
    surfr_ids = {r["ids"]["surfr"] for r in riders if "surfr" in r["ids"]}
    stats: dict = {}
    failures = []
    if woo_ids:
        try:
            day = await woo.day_stats(_woo_token(settings), start, end, woo_ids)
            stats.update({f"woo:{k}": v for k, v in day.items()})
        except woo.WooError as exc:
            log.warning("WOO recap failed: %s", exc)
            failures.append("WOO")
    if surfr_ids:
        try:
            day = await surfr.day_stats(_surfr_token(settings), date_str, surfr_ids)
            stats.update({f"surfr:{k}": v for k, v in day.items()})
        except surfr.SurfrError as exc:
            log.warning("Surfr recap failed: %s", exc)
            failures.append("Surfr")
    failure_note = f" ({'/'.join(failures)} API neatbildēja)" if failures else ""
    if failures and not stats:
        return None, f"{'/'.join(failures)} API šobrīd neatbild, sadaļa izlaista"
    lines, updated, changed = woo.summarize(riders, stats)
    if changed and update_records:
        config.save_riders(updated)
    if not lines:
        return None, f"neviens no {len(riders)} braucējiem vakar nebrauca, sadaļu nerādu{failure_note}"
    section = "🏆 <b>Vakardienas varoņi</b>\n" + "\n".join(html.escape(line) for line in lines)
    return section, "sadaļa iekļauta" + failure_note


def _rider_pfx(rider: dict) -> str:
    """Short id prefix for callback data (two full WOO UUIDs would not fit)."""
    return sorted(rider["ids"].values())[0][:8]


def _riders_view() -> tuple:
    riders = config.load_riders()
    if not riders:
        return ("🏄 <b>Braucēji</b>\n\nVēl neviena braucēja. "
                "Pievieno ar: /woorider Vārds vai /surfrider Vārds", None)
    lines = ["🏄 <b>Braucēji</b>", ""]
    rows = []
    for r in riders:
        tags = "+".join(woo.PROVIDER_LABELS.get(p, p) for p in sorted(r["ids"]))
        lines.append(f"{html.escape(r['name'])} — rekords {_num_lv(r['record_height_m'])} m · {tags}")
        rows.append([
            InlineKeyboardButton(f"🔗 {r['name']}", callback_data=f"wm1:{_rider_pfx(r)}"),
            InlineKeyboardButton("🗑", callback_data=f"wdel:{_rider_pfx(r)}"),
        ])
    lines += ["", "Pievienot: /woorider Vārds vai /surfrider Vārds.",
              "🔗 apvieno vienu cilvēku, kas lieto abas lietotnes, vienā ierakstā."]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_records(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Public jump-record leaderboard of the crew — no admin gate."""
    msg = update.effective_message
    if msg is None:
        return
    riders = config.load_riders()
    if not riders:
        await msg.reply_text("Vēl nav neviena braucēja — admins var pievienot ar "
                             "/woorider vai /surfrider.")
        return
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Lēcienu rekordi</b>", ""]
    ordered = sorted(riders, key=lambda r: -r["record_height_m"])
    for i, r in enumerate(ordered):
        prefix = medals[i] if i < len(medals) else f"{i + 1}."
        lines.append(f"{prefix} {html.escape(r['name'])} — {_num_lv(r['record_height_m'])} m")
    await msg.reply_html("\n".join(lines))


async def cmd_riders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_configure(update, context, settings):
        return
    text, keyboard = _riders_view()
    await msg.reply_html(text, reply_markup=keyboard)


async def cmd_woorider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_configure(update, context, settings):
        return
    raw = (msg.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        await msg.reply_text("Lietošana: /woorider Vārds vai Uzvārds — meklēšu WOO datos.")
        return
    query = raw[1].strip()
    await msg.reply_text(f"Meklēju “{query}” pēdējo 30 dienu WOO datos — tas var aizņemt "
                         "kādas 10–20 sekundes…")
    try:
        await context.bot.send_chat_action(
            chat_id=msg.chat_id, action=ChatAction.TYPING,
            message_thread_id=msg.message_thread_id if msg.is_topic_message else None)
    except TelegramError:
        pass
    now = int(datetime.now(timezone.utc).timestamp())
    try:
        found = await woo.find_riders(_woo_token(settings), query, now - 30 * 86400, now)
    except woo.WooError as exc:
        log.warning("WOO search failed: %s", exc)
        await msg.reply_text("WOO šobrīd nav sasniedzams — pamēģini vēlāk.")
        return
    if not found:
        await msg.reply_text(
            "Neatradu nevienu atbilstošu braucēju. Braucējam jābūt braukušam pēdējās "
            "30 dienās un ar publisku WOO profilu; pamēģini īsāku vārda daļu.")
        return
    await _offer_candidates(msg, found, provider="woo")


async def _offer_candidates(msg, found: list, provider: str) -> None:
    rows = []
    for candidate in found:
        candidate["provider"] = provider
        _woo_candidates[candidate["rider_id"]] = candidate
        extra = f" {candidate['country']}" if candidate.get("country") else ""
        rows.append([InlineKeyboardButton(
            f"{candidate['name']}{extra} ({_num_lv(candidate['best_height_m'])} m)",
            callback_data=f"wadd:{candidate['rider_id']}")])
    await msg.reply_text("Kurš no šiem?", reply_markup=InlineKeyboardMarkup(rows))


async def cmd_surfrider(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_configure(update, context, settings):
        return
    raw = (msg.text or "").split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        await msg.reply_text("Lietošana: /surfrider Vārds — meklēšu Surfr datos.\n"
                             "Ja braucējs sen nav braucis un zināms viņa Surfr id: "
                             "/surfrider id=12345 Vārds record=13.6")
        return
    query = raw[1].strip()
    tokens = query.split()
    if tokens[0].lower().startswith("id="):
        # direct add for riders with no recent sessions (unfindable via scan)
        rider_id = tokens[0][3:].strip()
        if not rider_id.isdigit():
            await msg.reply_text("⚠️ id jābūt skaitlim, piemēram: /surfrider id=12345 Vārds")
            return
        record = 0.0
        name_tokens = []
        for token in tokens[1:]:
            if token.lower().startswith("record="):
                try:
                    record = float(token[len("record="):].replace(",", "."))
                except ValueError:
                    await msg.reply_text("⚠️ record= jābūt skaitlim metros, piem. record=13.6")
                    return
            else:
                name_tokens.append(token)
        name = " ".join(name_tokens).strip() or f"Surfr {rider_id}"
        config.add_rider("surfr", rider_id, name, record)
        await msg.reply_text(f"✅ {name} pievienots (Surfr id {rider_id}), "
                             f"rekords {_num_lv(record)} m. Labot: /setrecord {name} X")
        return
    await msg.reply_text(f"Meklēju “{query}” šīs nedēļas (tad mēneša) Surfr datos — "
                         "tas var aizņemt minūti…")
    try:
        await context.bot.send_chat_action(
            chat_id=msg.chat_id, action=ChatAction.TYPING,
            message_thread_id=msg.message_thread_id if msg.is_topic_message else None)
    except TelegramError:
        pass
    try:
        found = await surfr.find_riders(_surfr_token(settings), query)
    except surfr.SurfrError as exc:
        log.warning("Surfr search failed: %s", exc)
        await msg.reply_text("Surfr šobrīd nav sasniedzams — pamēģini vēlāk.")
        return
    if not found:
        await msg.reply_text(
            "Neatradu nevienu atbilstošu braucēju. Braucējam jābūt braukušam šajā "
            "mēnesī ar redzamu Surfr profilu; pamēģini īsāku vārda daļu.")
        return
    await _offer_candidates(msg, found, provider="surfr")


async def _cb_woo_add(query, context, settings, rider_id: str) -> None:
    if not await _cb_admin(query, context, settings):
        return
    candidate = _woo_candidates.get(rider_id)
    if candidate is None:
        await query.answer("Meklējums novecojis — palaid meklēšanu vēlreiz.", show_alert=True)
        return
    config.add_rider(candidate.get("provider", "woo"), rider_id,
                     candidate["name"], candidate["best_height_m"])
    await query.answer("Pievienots.")
    try:
        await query.edit_message_text(
            f"✅ {candidate['name']} pievienots — sākuma rekords "
            f"{_num_lv(candidate['best_height_m'])} m (nesenā perioda labākais lēciens; "
            "precizēt: /setrecord).")
    except BadRequest:
        pass


async def cmd_setrecord(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_configure(update, context, settings):
        return
    parts = (msg.text or "").split()
    usage = "Lietošana: /setrecord Vārds 6.2 (metros; vārds var būt daļa no /riders saraksta vārda)"
    if len(parts) < 3:
        await msg.reply_text(usage)
        return
    try:
        value = float(parts[-1].replace(",", "."))
    except ValueError:
        await msg.reply_text(usage)
        return
    if not 0 <= value <= 50:
        await msg.reply_text("Rekordam jābūt 0–50 m robežās.")
        return
    needle = woo.normalize(" ".join(parts[1:-1]))
    riders = config.load_riders()
    matches = [r for r in riders if needle in woo.normalize(r["name"])]
    if not matches:
        await msg.reply_text("Neatradu tādu braucēju — /riders parādīs sarakstu.")
        return
    if len(matches) > 1:
        await msg.reply_text("Vairāki braucēji atbilst: "
                             + ", ".join(r["name"] for r in matches) + ". Precizē vārdu.")
        return
    rider = matches[0]
    old = rider["record_height_m"]
    rider["record_height_m"] = value
    config.save_riders(riders)
    await msg.reply_text(f"✅ {rider['name']}: rekords {_num_lv(old)} m → {_num_lv(value)} m.")


async def _edit_riders_view(query) -> None:
    text, keyboard = _riders_view()
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except BadRequest:
        pass


async def _cb_woo_del(query, context, settings, prefix: str) -> None:
    if not await _cb_admin(query, context, settings):
        return
    rider = config.find_rider_by_prefix(config.load_riders(), prefix)
    if rider is None:
        await query.answer("Jau bija dzēsts.")
    else:
        config.remove_rider(next(iter(rider["ids"].values())))
        await query.answer("Dzēsts.")
    await _edit_riders_view(query)


async def _cb_riders_view(query, context, settings) -> None:
    if not await _cb_admin(query, context, settings):
        return
    await query.answer()
    await _edit_riders_view(query)


async def _cb_merge_pick(query, context, settings, prefix: str) -> None:
    if not await _cb_admin(query, context, settings):
        return
    riders = config.load_riders()
    rider = config.find_rider_by_prefix(riders, prefix)
    if rider is None:
        await query.answer("Braucējs nav atrasts.", show_alert=True)
        return
    others = [r for r in riders if r is not rider]
    if not others:
        await query.answer("Nav neviena cita braucēja, ar ko apvienot.", show_alert=True)
        return
    await query.answer()
    rows = []
    for other in others:
        tags = "+".join(woo.PROVIDER_LABELS.get(p, p) for p in sorted(other["ids"]))
        rows.append([InlineKeyboardButton(
            f"➕ {other['name']} ({tags})",
            callback_data=f"wm2:{prefix}|{_rider_pfx(other)}")])
    rows.append([InlineKeyboardButton("⬅️ Atpakaļ", callback_data="riders")])
    try:
        await query.edit_message_text(
            f"Apvienot “{rider['name']}” ar: (paliks vārds “{rider['name']}”, "
            "rekords — lielākais no abiem)",
            reply_markup=InlineKeyboardMarkup(rows))
    except BadRequest:
        pass


async def _cb_merge_do(query, context, settings, payload: str) -> None:
    if not await _cb_admin(query, context, settings):
        return
    prefix_a, _, prefix_b = payload.partition("|")
    merged = config.merge_riders(prefix_a, prefix_b)
    if merged is None:
        await query.answer("Neizdevās apvienot — atver /riders vēlreiz.", show_alert=True)
    else:
        await query.answer(f"Apvienots: {merged['name']}.")
    await _edit_riders_view(query)


def _filter_results(results: list, sub: Subscription) -> list:
    """Apply a subscription's spot filter; stale-only filters fall back to all."""
    if not sub.spots:
        return results
    wanted = {n.lower() for n in sub.spots}
    filtered = [r for r in results if r.spot.name.lower() in wanted]
    return filtered or results


def _daily_text(results: list, settings, extra: "str | None" = None) -> str:
    digest = build_digest(results, settings, title=DAILY_TITLE_LV)
    if extra:
        digest = digest + "\n\n" + extra
    if settings.daily_greeting:
        return html.escape(settings.daily_greeting) + "\n\n" + digest
    return digest


async def cmd_testdigest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return
    settings = config.load_settings()
    if not await _can_configure(update, context, settings):
        return
    spots = config.load_spots(settings)
    if not spots:
        await msg.reply_text("Vēl nav neviena spota.\n" + addspot_usage(settings))
        return
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    try:
        await context.bot.send_chat_action(
            chat_id=msg.chat_id, action=ChatAction.TYPING, message_thread_id=thread_id)
    except TelegramError:
        pass
    results = await gather_results(spots, settings)
    woo_section, woo_status = await build_woo_section(settings, update_records=False)
    sub = config.find_subscription(msg.chat_id, thread_id)
    if sub is not None:
        results = _filter_results(results, sub)
    would_send = any_windows(results) or settings.post_when_no_wind
    if would_send:
        intro = "ℹ️ Tests — šādi izskatās šī čata ikdienas ziņa:"
    else:
        intro = ("ℹ️ Tests — šodien īstā ikdienas ziņa NEtiktu sūtīta (nav braucama "
                 "vēja, un post_when_no_wind ir izslēgts). Saturs būtu šāds:")
    subs = config.load_subscriptions()
    outro = (f"Pierakstīti {len(subs)} čati · sūtīšanas laiks {settings.daily_post_time} "
             f"({settings.timezone}).")
    if sub is not None and sub.spots:
        outro += f"\nŠī čata spotu filtrs: {', '.join(sub.spots)}."
    outro += f"\nWOO: {woo_status}."
    text = intro + "\n\n" + _daily_text(results, settings, extra=woo_section) + "\n\n" + outro
    for part in split_message(text):
        await msg.reply_html(part)


async def daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = config.load_settings()
    subs = config.load_subscriptions()
    if not subs:
        log.info("daily digest: no subscribed chats, skipping")
        return
    spots = config.load_spots(settings)
    if not spots:
        log.info("daily digest: no spots configured, skipping")
        return
    results = await gather_results(spots, settings, robust=True)
    woo_section, _ = await build_woo_section(settings, update_records=True)
    keyboard = _menu_keyboard(spots)
    cache: dict = {}
    for sub in subs:
        filtered = _filter_results(results, sub)
        if not any_windows(filtered) and not settings.post_when_no_wind:
            log.info("daily digest: nothing rideable for chat %s, staying quiet", sub.chat_id)
            continue
        key = tuple(sorted(n.lower() for n in sub.spots))
        if key not in cache:
            cache[key] = split_message(_daily_text(filtered, settings, extra=woo_section))
        await _send_digest(context, sub, cache[key], keyboard)


async def _send_digest(context: ContextTypes.DEFAULT_TYPE, sub: Subscription,
                       parts: list, keyboard: InlineKeyboardMarkup) -> None:
    async def send_to(chat_id: int) -> None:
        for i, part in enumerate(parts):
            await context.bot.send_message(
                chat_id=chat_id, text=part, parse_mode=ParseMode.HTML,
                message_thread_id=sub.thread_id,
                reply_markup=keyboard if i == len(parts) - 1 else None,
            )

    try:
        await send_to(sub.chat_id)
    except Forbidden:
        log.warning("chat %s blocked/kicked the bot — dropping its subscription", sub.chat_id)
        config.drop_chat(sub.chat_id)
    except ChatMigrated as exc:
        log.info("chat %s migrated to %s — updating subscription", sub.chat_id, exc.new_chat_id)
        config.migrate_chat(sub.chat_id, exc.new_chat_id)
        try:
            await send_to(exc.new_chat_id)
        except TelegramError as retry_exc:
            log.warning("retry to migrated chat %s failed: %s", exc.new_chat_id, retry_exc)
    except TelegramError as exc:
        log.warning("could not post digest to chat %s: %s", sub.chat_id, exc)


# --- wiring -------------------------------------------------------------------

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("error while handling an update", exc_info=context.error)


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("prognoze", "prognoze visiem spotiem"),
        BotCommand("marsruts", "dienas maršruts starp spotiem"),
        BotCommand("profile", "mans profils: mājas, kaiti, svars"),
        BotCommand("home", "saglabāt mājas vietu"),
        BotCommand("quiver", "saglabāt kaitu izmērus"),
        BotCommand("weight", "saglabāt svaru"),
        BotCommand("menu", "prognoze ar pogām"),
        BotCommand("check", "prognoze visiem spotiem"),
        BotCommand("spots", "spotu saraksts"),
        BotCommand("subscribe", "ieslēgt ikdienas prognozi šeit"),
        BotCommand("myspots", "kurus spotus rādīt šī čata ziņā"),
        BotCommand("unsubscribe", "atslēgt ikdienas prognozi"),
        BotCommand("addspot", "pievienot spotu (nosaukums + vieta)"),
        BotCommand("manage", "spotu pārvaldība (adminiem)"),
        BotCommand("testdigest", "izmēģināt ikdienas ziņu (adminiem)"),
        BotCommand("records", "lēcienu rekordu tabula"),
        BotCommand("riders", "braucēju pārvaldība (adminiem)"),
        BotCommand("woorider", "pievienot WOO braucēju (adminiem)"),
        BotCommand("surfrider", "pievienot Surfr braucēju (adminiem)"),
        BotCommand("setrecord", "labot braucēja rekordu (adminiem)"),
        BotCommand("delspot", "dzēst spotu"),
        BotCommand("id", "čata un lietotāja ID"),
        BotCommand("help", "palīdzība"),
    ])
    me = await app.bot.get_me()
    log.info("connected as @%s", me.username)


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["start", "menu"], cmd_menu))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("prognoze", cmd_prognoze))
    app.add_handler(CommandHandler("marsruts", cmd_marsruts))
    app.add_handler(CommandHandler("home", cmd_home))
    app.add_handler(CommandHandler("quiver", cmd_quiver))
    app.add_handler(CommandHandler("weight", cmd_weight))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("spots", cmd_spots))
    app.add_handler(CommandHandler("addspot", cmd_addspot))
    app.add_handler(CommandHandler("delspot", cmd_delspot))
    app.add_handler(CommandHandler("manage", cmd_manage))
    app.add_handler(CommandHandler("testdigest", cmd_testdigest))
    app.add_handler(CommandHandler("records", cmd_records))
    app.add_handler(CommandHandler("riders", cmd_riders))
    app.add_handler(CommandHandler("woorider", cmd_woorider))
    app.add_handler(CommandHandler("surfrider", cmd_surfrider))
    app.add_handler(CommandHandler("setrecord", cmd_setrecord))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("myspots", cmd_myspots))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_error_handler(on_error)
