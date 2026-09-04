import os
import io
import re
import html as html_lib
import asyncio
import logging
import aiohttp
from collections import deque
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from amazon_api import (
    is_amazon_url, is_amazon_search_url, resolve_amazon_url,
    extract_asin, get_product_by_asin, make_affiliate_url,
    get_short_affiliate_link,
)
from caption import build_amazon_caption, _safe_truncate, _TAG_RE
from database import is_duplicate, mark_posted, cleanup_old_entries
from storage import load_config, save_config, init_db
from watermark import apply_watermark

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
    logger.error("ADMIN_ID env var must be a number!")

# Multi-product posts ke beech gap (Telegram flood limit se bachne ke liye)
MULTI_POST_DELAY = 3.0
MAX_PRODUCTS_PER_MESSAGE = 15

# Invisible marker — bot jo status message draft channel mein daalta hai uske
# aakhir mein lagta hai, taaki bot apne hi message ko dobara na uthaye.
# U+2063 INVISIBLE SEPARATOR — screen pe kuch dikhta nahi.
SELF_MARKER = "\u2063"

URL_REGEX = re.compile(r"(https?://[^\s\]\[<>\"']+)")

FOOTER_LINE_PATTERN = re.compile(
    r'^[-—\s]*(deal\s*from|buy\s*on|shop\s*on|source\s*:|via\s*:|'
    r'brought\s*by|available\s*on|check\s*on|grab\s*on|get\s*it\s*on|'
    r'amazon\s*deal|flipkart\s*deal|meesho\s*deal|deal\s*by|'
    r'posted\s*by|bot\s*by)\b.*$',
    re.IGNORECASE
)

# Channels we've already offered to set as source (avoid DM spam)
_offered_channels = set()

# Bot ne khud jo messages channel mein bheje unki IDs (loop guard)
_own_msg_ids = deque(maxlen=1000)
_own_msg_set = set()


def _remember_own(m):
    """Bot ke apne channel message ko yaad rakho taaki dobara process na ho."""
    if not m:
        return m
    try:
        key = (m.chat_id, m.message_id)
    except Exception:
        return m
    if len(_own_msg_ids) == _own_msg_ids.maxlen:
        _own_msg_set.discard(_own_msg_ids[0])
    _own_msg_ids.append(key)
    _own_msg_set.add(key)
    return m


def _is_own_message(msg, bot_id) -> bool:
    """Teen tarah se check karo ki ye message bot ne khud to nahi bheja."""
    if msg.from_user and msg.from_user.id == bot_id:
        return True
    try:
        if (msg.chat_id, msg.message_id) in _own_msg_set:
            return True
    except Exception:
        pass
    body = msg.text or msg.caption or ""
    return SELF_MARKER in body


# =============================================================================
# HELPERS
# =============================================================================
def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID


def extract_urls(text: str) -> list:
    return URL_REGEX.findall(text) if text else []


def get_amazon_urls(urls: list) -> list:
    return [u for u in urls if is_amazon_url(u)]


async def _download_image(url: str) -> bytes | None:
    """Download image from URL, return bytes or None."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.error(f"Image download fail: {e}")
    return None


async def _get_photo_bytes(bot, msg) -> bytes | None:
    """Download photo from a Telegram message, return bytes or None."""
    if not msg.photo:
        return None
    try:
        file = await bot.get_file(msg.photo[-1].file_id)
        return bytes(await file.download_as_bytearray())
    except Exception as e:
        logger.error(f"Photo download fail: {e}")
    return None


# =============================================================================
# CHANNEL MATCHING / NOTIFY HELPERS
# =============================================================================
def chat_matches(chat, ident: str) -> bool:
    """Check if a Telegram chat matches a stored identifier (@name or -100...)."""
    if not chat or not ident:
        return False
    ident = ident.strip()
    if not ident:
        return False
    if ident.startswith("@"):
        return (chat.username or "").lower() == ident[1:].lower()
    try:
        return chat.id == int(ident)
    except (ValueError, TypeError):
        return (chat.username or "").lower() == ident.lower()


def same_channel(ident_a: str, ident_b: str) -> bool:
    """Rough comparison of two stored channel identifiers."""
    a = (ident_a or "").strip().lstrip("@").lower()
    b = (ident_b or "").strip().lstrip("@").lower()
    return bool(a) and a == b


async def dm_admin(context, text, **kwargs):
    """Send a status message to the admin's DM. Returns Message or None."""
    if not ADMIN_ID:
        return None
    try:
        return await context.bot.send_message(chat_id=ADMIN_ID, text=text, **kwargs)
    except Exception as e:
        logger.error(f"Admin DM fail: {e}")
        return None


async def _edit_or_notify(wait_msg, notify, text, **kwargs):
    """Edit the 'please wait' message if possible, else send a fresh one."""
    if wait_msg:
        try:
            await wait_msg.edit_text(text + SELF_MARKER, **kwargs)
            return
        except Exception:
            pass
    await notify(text, **kwargs)


async def _delete_quiet(m):
    if m:
        try:
            await m.delete()
        except Exception:
            pass


# =============================================================================
# MARKUP BUILDER
# =============================================================================
def build_final_markup(config: dict):
    buttons_cfg = config.get("buttons", {})
    row = []
    for btn_key in ["btn1", "btn2"]:
        btn = buttons_cfg.get(btn_key, {})
        if btn.get("enabled") and btn.get("label") and btn.get("url"):
            row.append(InlineKeyboardButton(btn["label"], url=btn["url"]))
    return InlineKeyboardMarkup([row]) if row else None


# =============================================================================
# TEXT → HTML CONVERTER
# =============================================================================
def _py_to_utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


class _Ent:
    """Lightweight stand-in for telegram.MessageEntity (which is immutable)."""
    __slots__ = ("offset", "length", "type", "url")

    def __init__(self, offset, length, type_, url=None):
        self.offset = offset
        self.length = length
        self.type   = type_
        self.url    = url


def _clone_ent(ent, offset=None, length=None) -> _Ent:
    return _Ent(
        ent.offset if offset is None else offset,
        ent.length if length is None else length,
        ent.type,
        getattr(ent, "url", None),
    )


def replace_url_keep_entities(text: str, entities: list, old_url: str, new_url: str):
    """
    Text mein old_url ko new_url se replace karo AUR entity offsets bhi
    saath mein shift kar do — warna bold/link galat jagah lag jaate hain.
    Returns (new_text, new_entities).
    """
    if not old_url or old_url == new_url:
        return text, entities
    idx = text.find(old_url)
    if idx < 0:
        return text, entities

    start_u16 = _py_to_utf16_len(text[:idx])
    old_u16   = _py_to_utf16_len(old_url)
    new_u16   = _py_to_utf16_len(new_url)
    end_u16   = start_u16 + old_u16
    delta     = new_u16 - old_u16

    new_text = text[:idx] + new_url + text[idx + len(old_url):]

    new_ents = []
    for ent in (entities or []):
        s = ent.offset
        e = ent.offset + ent.length
        if e <= start_u16:
            new_ents.append(_clone_ent(ent))                              # poora pehle
        elif s >= end_u16:
            new_ents.append(_clone_ent(ent, offset=s + delta))            # poora baad
        elif s <= start_u16 and e >= end_u16:
            new_ents.append(_clone_ent(ent, length=ent.length + delta))   # URL ko cover karta hai
        else:
            continue   # aadha overlap — drop, warna HTML toot jayega
    return new_text, new_ents


async def replace_amazon_links(text: str, entities: list, urls: list):
    """Har Amazon link ko affiliate link se badlo, entities safe rakhte hue."""
    for url in urls:
        if not is_amazon_url(url):
            continue
        try:
            short = await get_short_affiliate_link(url)
        except Exception as e:
            logger.error(f"Affiliate link banane mein fail: {e}")
            continue
        text, entities = replace_url_keep_entities(text, entities, url, short)
    return text, entities


def remove_footer(plain_text: str, entities: list):
    lines = plain_text.split('\n')
    while lines and not lines[-1].strip():
        lines.pop()
    changed = True
    while changed and lines:
        changed = False
        if FOOTER_LINE_PATTERN.match(lines[-1].strip()):
            lines.pop()
            changed = True
    cleaned = '\n'.join(lines).rstrip()
    cutoff_utf16 = _py_to_utf16_len(cleaned)
    filtered = [e for e in (entities or []) if e.offset + e.length <= cutoff_utf16]
    return cleaned, filtered


def _build_utf16_map(text: str) -> list:
    mapping = []
    for py_idx, ch in enumerate(text):
        mapping.append(py_idx)
        if ord(ch) > 0xFFFF:
            mapping.append(py_idx)
    mapping.append(len(text))
    return mapping


def entities_to_html(text: str, entities: list) -> str:
    if not entities:
        return html_lib.escape(text)

    utf16_map  = _build_utf16_map(text)
    open_tags  = [""] * len(text)
    close_tags = [""] * len(text)

    for ent in sorted(entities, key=lambda e: (e.offset, -e.length)):
        s_utf16 = ent.offset
        e_utf16 = ent.offset + ent.length
        s = utf16_map[s_utf16] if s_utf16 < len(utf16_map) else s_utf16
        e = utf16_map[e_utf16] if e_utf16 < len(utf16_map) else e_utf16
        if e > len(text) or s >= len(text) or e <= s:
            continue
        etype = ent.type

        if etype == "url":
            open_tags[s]    = '<b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b>'
        elif etype == "text_link":
            url = html_lib.escape(ent.url or "")
            open_tags[s]    = f'<a href="{url}"><b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b></a>'
        elif etype == "bold":
            open_tags[s]    = '<b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b>'
        elif etype == "italic":
            open_tags[s]    = '<i>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</i>'
        elif etype == "underline":
            open_tags[s]    = '<u>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</u>'
        elif etype == "strikethrough":
            open_tags[s]    = '<s>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</s>'
        elif etype == "code":
            open_tags[s]    = '<code>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</code>'
        elif etype == "pre":
            open_tags[s]    = '<pre>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</pre>'
        elif etype == "spoiler":
            open_tags[s]    = '<tg-spoiler>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</tg-spoiler>'

    result = []
    for i, ch in enumerate(text):
        result.append(open_tags[i])
        result.append(html_lib.escape(ch))
        result.append(close_tags[i])
    return ''.join(result)


# =============================================================================
# SILENT UI HELPERS
# =============================================================================
def _silent_status_text(silent: bool) -> str:
    if silent:
        body = (
            "Status: <b>🔕 SILENT</b>\n\n"
            "Post channel mein normal aati hai, par subscribers ke phone pe "
            "sound aur popup nahi hota. Unread badge phir bhi dikhta hai.\n\n"
            "<i>Zyada deals post karte waqt yahi behtar hai — log mute nahi karte.</i>"
        )
    else:
        body = (
            "Status: <b>🔔 LOUD</b>\n\n"
            "Har post pe subscribers ko poori notification jaati hai "
            "(sound + popup).\n\n"
            "<i>Din mein bahut deals ja rahi hain to log mute kar sakte hain.</i>"
        )
    return "🔔 <b>Notification Settings</b>\n\n" + body


def _silent_kb(silent: bool) -> InlineKeyboardMarkup:
    toggle = "🔔 Loud karo" if silent else "🔕 Silent karo"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle,   callback_data="silent_toggle")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


# =============================================================================
# WATERMARK UI HELPERS
# =============================================================================
def _watermark_status_text(wm: dict) -> str:
    status   = "✅ ON" if wm.get("enabled", True) else "❌ OFF"
    wm_text  = wm.get("text", "@DealKoti")
    return (
        f"🖼️ <b>Watermark Settings</b>\n\n"
        f"Status : <b>{status}</b>\n"
        f"Text   : <code>{html_lib.escape(wm_text)}</code>\n\n"
        f"<i>Watermark har image ke bottom-right corner pe lagta hai.</i>"
    )


def _watermark_kb(wm: dict) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Turn OFF" if wm.get("enabled", True) else "🟢 Turn ON"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Text Badlo",  callback_data="wm_set_text")],
        [InlineKeyboardButton(toggle_label,     callback_data="wm_toggle")],
        [InlineKeyboardButton("❌ Cancel",       callback_data="cancel")],
    ])


# =============================================================================
# SETBUTTON UI HELPERS
# =============================================================================
def _setbutton_status_text(buttons: dict) -> str:
    b1 = buttons.get("btn1", {})
    b2 = buttons.get("btn2", {})
    return (
        f"📌 <b>Button 1</b> — {'✅ ON' if b1.get('enabled') else '❌ OFF'}\n"
        f"   Naam: {html_lib.escape(b1.get('label', '-'))}\n"
        f"   Link: <code>{html_lib.escape(b1.get('url') or '—')}</code>\n\n"
        f"📌 <b>Button 2</b> — {'✅ ON' if b2.get('enabled') else '❌ OFF'}\n"
        f"   Naam: {html_lib.escape(b2.get('label', '-'))}\n"
        f"   Link: <code>{html_lib.escape(b2.get('url') or '—')}</code>"
    )


def _setbutton_main_kb(buttons: dict) -> InlineKeyboardMarkup:
    b1_label = buttons.get("btn1", {}).get("label", "Button 1")
    b2_label = buttons.get("btn2", {}).get("label", "Button 2")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✏️ {b1_label}", callback_data="sb_btn1")],
        [InlineKeyboardButton(f"✏️ {b2_label}", callback_data="sb_btn2")],
        [InlineKeyboardButton("❌ Cancel",       callback_data="cancel")],
    ])


def _setbutton_detail_kb(btn_key: str, btn: dict) -> InlineKeyboardMarkup:
    toggle_label = "🟢 Turn ON" if not btn.get("enabled") else "🔴 Turn OFF"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Rename",   callback_data=f"sb_{btn_key}_rename")],
        [InlineKeyboardButton("🔗 Set Link", callback_data=f"sb_{btn_key}_link")],
        [InlineKeyboardButton(toggle_label,  callback_data=f"sb_{btn_key}_toggle")],
        [InlineKeyboardButton("⬅️ Back",     callback_data="sb_main")],
    ])


def _btn_detail_text(btn_key: str, btn: dict) -> str:
    num    = btn_key[-1]
    label  = btn.get("label", f"Button {num}")
    url    = btn.get("url") or "Set nahi hua"
    status = "✅ ON" if btn.get("enabled") else "❌ OFF"
    return (
        f"🎛️ <b>Button {num} Settings</b>\n\n"
        f"📝 Naam  : <b>{html_lib.escape(label)}</b>\n"
        f"🔗 Link  : <code>{html_lib.escape(url)}</code>\n"
        f"Status : {status}"
    )


# =============================================================================
# COMMANDS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 <b>DealsKoti Bot chalu hai!</b>\n\n"
        "Do tarike se kaam karta hai:\n"
        "1️⃣ Mujhe seedha deal ka message bhejo — reply yahin milega\n"
        "2️⃣ Ya draft channel mein post karo — reply wahin milega\n\n"
        "Ek message mein kitne bhi Amazon links daal sakte ho — "
        "har product ki <b>alag post</b> jayegi.\n\n"
        "/help daao saare commands dekhne ke liye.",
        parse_mode="HTML"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 <b>DealsKoti Bot — Saare Commands</b>\n\n"
        "ℹ️ /start — Bot ki info\n"
        "📖 /help — Ye list\n"
        "📊 /status — Poora status ek jagah\n"
        "📢 /setchannel — Post karne wala channel set karo\n"
        "📥 /setsource — Draft channel set karo (auto pickup)\n"
        "🔔 /silent — Notification silent ya loud\n"
        "🖼️ /watermark — Watermark settings (ON/OFF + text)\n"
        "🎛️ /setbutton — Post ke neeche buttons set karo\n"
        "🧪 /testamz — Amazon Creators API test karo\n"
        "💾 /exportconfig — Config ka JSON backup\n\n"
        "<b>⚡ Shortcuts</b>\n"
        "<code>/silent on</code> — Chup-chaap post karo\n"
        "<code>/silent off</code> — Poori notification bhejo\n"
        "<code>/watermark on</code> — Watermark ON\n"
        "<code>/watermark off</code> — Watermark OFF\n"
        "<code>/setsource off</code> — Auto pickup band\n\n"
        "<b>📌 Kaise kaam karta hai</b>\n"
        "• 1 Amazon product link → full deal post (price, rating, image)\n"
        "• Kai Amazon links → <b>har product ki alag post</b>\n"
        "• Search / deals / storefront page → ignore\n"
        "• Non-Amazon message → jaisa ka waisa forward\n\n"
        "<b>💬 Status reply kahan aayega</b>\n"
        "• DM se bheja → DM mein\n"
        "• Draft channel se → usi post ke reply mein",
        parse_mode="HTML"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    config  = load_config()
    channel = config.get("channel", "") or "❌ Set nahi hua (/setchannel)"
    source  = config.get("source_channel", "") or "❌ Set nahi hua (/setsource)"
    silent  = config.get("silent", True)
    wm      = config.get("watermark", {"enabled": True, "text": "@DealKoti"})
    buttons = config.get("buttons", {})
    b1      = buttons.get("btn1", {})
    b2      = buttons.get("btn2", {})

    lines = [
        "⚙️ <b>Bot Status</b>\n",
        f"📥 <b>Draft Channel :</b> <code>{html_lib.escape(source)}</code>",
        f"📢 <b>Post Channel  :</b> <code>{html_lib.escape(channel)}</code>\n",
        f"🔔 <b>Notification:</b> {'🔕 Silent' if silent else '🔔 Loud'}",
        f"🖼️ <b>Watermark:</b> {'✅ ON' if wm.get('enabled') else '❌ OFF'} — "
        f"<code>{html_lib.escape(wm.get('text', '@DealKoti'))}</code>\n",
        "🎛️ <b>Buttons:</b>",
        f"  Button 1: {'✅ ON' if b1.get('enabled') else '❌ OFF'} — <b>{html_lib.escape(b1.get('label', '-'))}</b>",
        f"  Button 2: {'✅ ON' if b2.get('enabled') else '❌ OFF'} — <b>{html_lib.escape(b2.get('label', '-'))}</b>",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_silent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Silent (no sound/popup) ya loud notification."""
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    args   = context.args or []
    config = load_config()

    if args:
        arg = args[0].lower()
        if arg in ("on", "chalu", "haan"):
            config["silent"] = True
            save_config(config)
            await update.message.reply_text(
                "🔕 <b>Silent posting ON.</b>\n"
                "Ab posts subscribers ke phone pe bina sound ke jaayengi.",
                parse_mode="HTML"
            )
            return
        elif arg in ("off", "band", "nahi"):
            config["silent"] = False
            save_config(config)
            await update.message.reply_text(
                "🔔 <b>Silent posting OFF.</b>\n"
                "Ab har post pe poori notification jaayegi.",
                parse_mode="HTML"
            )
            return

    silent = config.get("silent", True)
    await update.message.reply_text(
        _silent_status_text(silent),
        parse_mode="HTML",
        reply_markup=_silent_kb(silent)
    )


async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    context.user_data["action"] = "wait_channel_id"
    config  = load_config()
    current = config.get("channel", "") or "Set nahi hua"
    await update.message.reply_text(
        f"📢 <b>Post Channel Set Karo</b>\n\n"
        f"Current: <code>{html_lib.escape(current)}</code>\n\n"
        f"Naya channel ID type karo (jaise @mychannel ya -100123456789):",
        parse_mode="HTML"
    )


async def cmd_setsource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set the DRAFT channel — bot auto-picks every post from here."""
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    args   = context.args or []
    config = load_config()

    if args and args[0].lower() in ("off", "band", "clear", "remove", "hatao"):
        config["source_channel"] = ""
        save_config(config)
        await update.message.reply_text(
            "❌ <b>Draft channel hata diya.</b>\n"
            "Ab sirf DM wala system chalega — mujhe message bhejo.",
            parse_mode="HTML"
        )
        return

    context.user_data.clear()
    context.user_data["action"] = "wait_source_id"
    current = config.get("source_channel", "") or "Set nahi hua"
    await update.message.reply_text(
        f"📥 <b>Draft Channel Set Karo</b>\n\n"
        f"Current: <code>{html_lib.escape(current)}</code>\n\n"
        f"<b>Pehle ye karo:</b>\n"
        f"1. Draft channel mein mujhe <b>admin</b> banao "
        f"(post karne ki permission ke saath — status reply wahin bhejta hoon)\n"
        f"2. Phir yahan channel ID type karo (@mydraft ya -100123456789)\n\n"
        f"<i>Tip: agar ID nahi pata to bas draft channel mein koi bhi post daal do — "
        f"main khud yahan ID bhej dunga ek button ke saath.</i>",
        parse_mode="HTML"
    )


async def cmd_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    args   = context.args or []
    config = load_config()
    wm     = config.setdefault("watermark", {"enabled": True, "text": "@DealKoti"})

    if args:
        arg = args[0].lower()
        if arg == "on":
            wm["enabled"] = True
            config["watermark"] = wm
            save_config(config)
            await update.message.reply_text(
                "✅ Watermark <b>ON</b> kar diya!\n"
                "Ab har image pe watermark lagega.", parse_mode="HTML"
            )
            return
        elif arg == "off":
            wm["enabled"] = False
            config["watermark"] = wm
            save_config(config)
            await update.message.reply_text(
                "✅ Watermark <b>OFF</b> kar diya!\n"
                "Ab images bina watermark ke jayengi.", parse_mode="HTML"
            )
            return

    await update.message.reply_text(
        _watermark_status_text(wm),
        parse_mode="HTML",
        reply_markup=_watermark_kb(wm)
    )


async def cmd_setbutton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    config  = load_config()
    buttons = config.get("buttons", {})
    await update.message.reply_text(
        "🎛️ <b>Button Settings</b>\n\n"
        + _setbutton_status_text(buttons)
        + "\n\n<i>Kaun sa configure karna hai?</i>",
        parse_mode="HTML",
        reply_markup=_setbutton_main_kb(buttons)
    )


async def cmd_testamz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔄 Amazon Creators API test ho rahi hai...")
    try:
        test_asin = "B08N5WRWNW"
        product   = await get_product_by_asin(test_asin)
        short     = make_affiliate_url(test_asin)
        if product and product.get("title"):
            await update.message.reply_text(
                f"✅ <b>Amazon Creators API kaam kar raha hai!</b>\n\n"
                f"🏷️ Title   : <code>{html_lib.escape(product['title'][:80])}</code>\n"
                f"💰 Price   : <b>{product.get('deal_price', 'N/A')}</b>\n"
                f"📉 Discount: <b>{product.get('discount_pct', 0)}%</b>\n"
                f"⭐ Rating  : <b>{product.get('rating', 'N/A')}</b>\n"
                f"👥 Reviews : <b>{product.get('review_count', 'N/A')}</b>\n"
                f"🖼️ Image   : <b>{'Mili ✅' if product.get('image_url') else 'Nahi mili ❌'}</b>\n"
                f"🔗 Link    : <code>{short}</code>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "⚠️ Amazon API se product data nahi mila.\n"
                "CREDENTIAL_ID aur CREDENTIAL_SECRET check karo."
            )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Amazon API error:\n<code>{html_lib.escape(str(e))}</code>",
            parse_mode="HTML"
        )


async def cmd_exportconfig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    import json
    config      = load_config()
    config_json = json.dumps(config, indent=2, ensure_ascii=False)
    await update.message.reply_text(
        f"📦 <b>Config Export (Backup)</b>\n\n"
        f"<pre>{html_lib.escape(config_json)}</pre>",
        parse_mode="HTML"
    )


# =============================================================================
# AMAZON LINK CLASSIFICATION
# =============================================================================
async def classify_amazon_urls(urls: list) -> dict:
    """
    Har Amazon URL ko resolve karke teen buckets mein daalo:
      products — ASIN mil gaya (ASIN se dedup)
      searches — search / deals / browse / storefront page
      unknown  — Amazon ka hai par ASIN nahi mila aur search bhi nahi lagta

    ASIN check PEHLE hota hai — kyunki search se nikla product link
    (.../dp/B0XXXX/ref=sr_1_1?keywords=shoes) product hai, search page nahi.
    """
    products, searches, unknown = [], [], []
    seen_asins = set()

    for url in urls:
        try:
            resolved = await resolve_amazon_url(url)
        except Exception as e:
            logger.error(f"Redirect resolve fail ({url[:60]}): {e}")
            resolved = url

        asin = extract_asin(resolved) or extract_asin(url)
        if asin:
            if asin in seen_asins:
                continue
            seen_asins.add(asin)
            products.append({"url": url, "resolved": resolved, "asin": asin})
        elif is_amazon_search_url(resolved) or is_amazon_search_url(url):
            searches.append(url)
        else:
            unknown.append(url)

    return {"products": products, "searches": searches, "unknown": unknown}


# =============================================================================
# SINGLE PRODUCT POSTER — ek ASIN ki ek post
# =============================================================================
async def post_amazon_product(context, item, channel, markup,
                              wm_enabled, wm_text, silent):
    """
    Ek Amazon product ko channel pe post karo.
    Returns (status, detail, img_source):
      "posted"    — ho gaya, detail = title
      "duplicate" — pehle ja chuki, detail = "title — X ghante pehle"
      "nodata"    — API se data nahi mila, detail = affiliate link
      "error"     — post fail, detail = error text
    """
    asin       = item["asin"]
    short_link = make_affiliate_url(asin)

    try:
        product = await get_product_by_asin(asin)
    except Exception as e:
        logger.error(f"API fail {asin}: {e}")
        product = None

    if not product or not product.get("title"):
        return "nodata", short_link, ""

    title = product["title"]

    dup, dup_time = is_duplicate(title)
    if dup:
        return "duplicate", f"{title[:60]} — {dup_time}", ""

    caption_html = build_amazon_caption(product, short_link)

    # ONLY Amazon API image — NEVER original post photo
    # (original mein doosre channel ka watermark ho sakta hai)
    img_bytes = None
    image_url = product.get("image_url", "")
    if image_url:
        img_bytes = await _download_image(image_url)
        if not img_bytes:
            logger.warning(f"Amazon image download fail: {image_url[:80]}")
    if img_bytes and wm_enabled:
        img_bytes = apply_watermark(img_bytes, wm_text)

    if img_bytes:
        img_source = "Amazon API" + (" + Watermark ✅" if wm_enabled else "")
    else:
        img_source = ""

    try:
        if img_bytes:
            await context.bot.send_photo(
                chat_id=channel,
                photo=InputFile(io.BytesIO(img_bytes), filename="deal.jpg"),
                caption=caption_html,
                parse_mode="HTML",
                reply_markup=markup,
                disable_notification=silent,
            )
        else:
            await context.bot.send_message(
                chat_id=channel,
                text=caption_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
                disable_notification=silent,
            )
        mark_posted(title)
        return "posted", title, img_source
    except Exception as e:
        logger.error(f"Post fail {asin}: {e}")
        return "error", str(e), ""


# =============================================================================
# CORE PROCESSOR — same logic for DM messages and draft-channel posts
# =============================================================================
async def process_and_post(context, msg, notify, config=None, source_tag: str = ""):
    """
    msg     — the incoming Telegram message (DM message OR channel post)
    notify  — async callable(text, **kwargs); DM se aaya to DM mein reply,
              draft channel se aaya to usi post ke reply mein.
    """
    # ── Parse message content ──────────────────────────────────────────────
    if msg.caption is not None:
        raw_plain    = msg.caption or ""
        raw_entities = list(msg.caption_entities or [])
        has_photo    = True
    elif msg.text:
        raw_plain    = msg.text or ""
        raw_entities = list(msg.entities or [])
        has_photo    = False
    else:
        raw_plain    = ""
        raw_entities = []
        has_photo    = bool(msg.photo)

    all_urls    = extract_urls(raw_plain)
    amazon_urls = get_amazon_urls(all_urls)

    if not raw_plain.strip() and not all_urls and not has_photo:
        await notify("⚠️ Message mein koi text ya link nahi mila.")
        return

    # ── Load config ────────────────────────────────────────────────────────
    if config is None:
        config = load_config()
    channel      = config.get("channel", "").strip()
    final_markup = build_final_markup(config)
    silent       = config.get("silent", True)
    wm_cfg       = config.get("watermark", {"enabled": True, "text": "@DealKoti"})
    wm_enabled   = wm_cfg.get("enabled", True)
    wm_text      = wm_cfg.get("text", "@DealKoti")

    bell = "🔕 Silent" if silent else "🔔 Loud"

    if not channel:
        await notify(
            "⚠️ <b>Channel set nahi hua!</b>\n/setchannel se pehle channel set karo.",
            parse_mode="HTML"
        )
        return

    try:
        cleanup_old_entries()
    except Exception:
        pass

    # ==========================================================================
    # AMAZON PATH
    # ==========================================================================
    if amazon_urls:
        wait_msg = await notify("⏳ Amazon links check ho rahe hain...")

        buckets  = await classify_amazon_urls(amazon_urls)
        products = buckets["products"]
        searches = buckets["searches"]
        unknown  = buckets["unknown"]

        # ── Koi product nahi, sirf search/deals pages → poora ignore ───────
        if not products and not unknown:
            await _edit_or_notify(
                wait_msg, notify,
                f"🚫 <b>Skip!</b> Sirf Amazon search/deals page mile "
                f"({len(searches)} link) — kuch bhi post nahi kiya.\n"
                f"Specific product ke link bhejo.",
                parse_mode="HTML"
            )
            return

        # ── Products mile → har ek ki ALAG post ────────────────────────────
        if products:
            if len(products) > MAX_PRODUCTS_PER_MESSAGE:
                await notify(
                    f"⚠️ {len(products)} products mile — sirf pehle "
                    f"{MAX_PRODUCTS_PER_MESSAGE} post kar raha hoon."
                )
                products = products[:MAX_PRODUCTS_PER_MESSAGE]

            total = len(products)
            if total > 1:
                await _edit_or_notify(
                    wait_msg, notify,
                    f"⏳ <b>{total} Amazon products</b> mile — alag alag post kar raha hoon...",
                    parse_mode="HTML"
                )
            else:
                await _delete_quiet(wait_msg)
            wait_msg = None

            posted, dupes, nodata, errors = [], [], [], []
            img_note = ""

            for i, item in enumerate(products):
                status, detail, img_source = await post_amazon_product(
                    context, item, channel, final_markup,
                    wm_enabled, wm_text, silent
                )
                if status == "posted":
                    posted.append(detail)
                    if not img_note:
                        img_note = img_source
                elif status == "duplicate":
                    dupes.append(detail)
                elif status == "nodata":
                    nodata.append(detail)
                else:
                    errors.append(detail)

                if i < total - 1:
                    await asyncio.sleep(MULTI_POST_DELAY)

            # ── Single product + API fail → purana fallback (text post) ────
            if total == 1 and not posted and nodata:
                short_link = nodata[0]
                amazon_url = products[0]["url"]
                cleaned_plain, cleaned_entities = remove_footer(raw_plain, raw_entities)
                cleaned_plain, cleaned_entities = replace_url_keep_entities(
                    cleaned_plain, cleaned_entities, amazon_url, short_link
                )
                body_html    = entities_to_html(cleaned_plain, cleaned_entities)
                caption_html = _safe_truncate("🙏Jai Shree Ram Dosto🙏\n\n" + body_html, 1020)
                try:
                    await context.bot.send_message(
                        chat_id=channel,
                        text=caption_html,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=final_markup,
                        disable_notification=silent,
                    )
                    await notify(
                        "✅ <b>Post ho gaya!</b>\n"
                        "⚠️ Amazon API se product data nahi mila — "
                        "original text affiliate link ke saath post kar diya.\n"
                        f"🔔 {bell}\n"
                        f"📢 <code>{html_lib.escape(channel)}</code>" + source_tag,
                        parse_mode="HTML", disable_web_page_preview=True
                    )
                except Exception as e:
                    await notify(
                        f"❌ <b>Post fail!</b>\n<code>{html_lib.escape(str(e))}</code>",
                        parse_mode="HTML"
                    )
                return

            # ── Single product success → chhota reply ──────────────────────
            if total == 1 and len(posted) == 1:
                lines = ["✅ <b>Amazon Deal Post Ho Gaya!</b>"]
                if img_note:
                    lines.append(f"🖼️ Image: {img_note}")
                else:
                    lines.append("🖼️ Image nahi mili — sirf text post kiya.")
                lines.append(f"🔔 {bell}")
                lines.append(f"📢 <code>{html_lib.escape(channel)}</code>")
                if source_tag:
                    lines.append(source_tag.strip())
                await notify(
                    "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
                )
                return

            # ── Summary (multi ya mixed) ───────────────────────────────────
            lines = []
            if posted:
                lines.append(f"✅ <b>{len(posted)} deal post ho gayi!</b>")
                for t in posted[:10]:
                    lines.append(f"   • {html_lib.escape(t[:55])}")
                if img_note:
                    lines.append(f"🖼️ Image: {img_note}")
            else:
                lines.append("⚠️ <b>Koi deal post nahi hui.</b>")
            if dupes:
                lines.append(f"\n🔁 <b>{len(dupes)} duplicate skip:</b>")
                for d in dupes[:5]:
                    lines.append(f"   • {html_lib.escape(d)}")
            if nodata:
                lines.append(f"\n⚠️ <b>{len(nodata)} ka Amazon data nahi mila</b> — skip kiya.")
            if errors:
                lines.append(f"\n❌ <b>{len(errors)} post fail:</b>")
                for e in errors[:3]:
                    lines.append(f"   • {html_lib.escape(e[:80])}")
            if searches:
                lines.append(f"\n🚫 {len(searches)} search/deals page ignore kiye.")
            if unknown:
                lines.append(f"\n❓ {len(unknown)} Amazon link se product pehchan nahi paya.")
            if posted:
                lines.append(f"\n🔔 {bell}")
            lines.append(f"📢 <code>{html_lib.escape(channel)}</code>")
            if source_tag:
                lines.append(source_tag.strip())

            await notify(
                "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
            )
            return

        # ── Sirf unknown Amazon links (ASIN nahi mila, search bhi nahi) ────
        await _delete_quiet(wait_msg)
        cleaned_plain, cleaned_entities = remove_footer(raw_plain, raw_entities)
        cleaned_plain, cleaned_entities = await replace_amazon_links(
            cleaned_plain, cleaned_entities, unknown
        )
        body_html  = entities_to_html(cleaned_plain, cleaned_entities)
        final_html = _safe_truncate("🙏Jai Shree Ram Dosto🙏\n\n" + body_html, 3800)
        try:
            await context.bot.send_message(
                chat_id=channel,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=final_markup,
                disable_notification=silent,
            )
            note = f"\n🚫 {len(searches)} search page ignore kiye." if searches else ""
            await notify(
                "✅ <b>Post ho gaya!</b>\n"
                "⚠️ Amazon product link pehchan nahi paya — "
                "text post kar diya affiliate link ke saath."
                + note
                + f"\n🔔 {bell}"
                + f"\n📢 <code>{html_lib.escape(channel)}</code>" + source_tag,
                parse_mode="HTML", disable_web_page_preview=True
            )
        except Exception as e:
            await notify(
                f"❌ <b>Post fail!</b>\n<code>{html_lib.escape(str(e))}</code>",
                parse_mode="HTML"
            )
        return

    # ==========================================================================
    # NON-AMAZON MESSAGE
    # ==========================================================================
    cleaned_plain, cleaned_entities = remove_footer(raw_plain, raw_entities)

    dup_key = raw_plain.strip()[:300]
    if dup_key:
        dup, dup_time = is_duplicate(dup_key)
        if dup:
            await notify(
                f"⚠️ <b>Duplicate!</b> Yeh message {dup_time} already post ho chuka hai — skip kiya.",
                parse_mode="HTML"
            )
            return

    body_html = entities_to_html(cleaned_plain, cleaned_entities)

    try:
        if msg.photo:
            img_bytes = await _get_photo_bytes(context.bot, msg)
            if img_bytes and wm_enabled:
                img_bytes = apply_watermark(img_bytes, wm_text)

            final_caption = None
            if raw_plain.strip():
                final_caption = _safe_truncate(
                    "🙏Jai Shree Ram Dosto🙏\n\n" + body_html, 1020
                )

            if img_bytes:
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=InputFile(io.BytesIO(img_bytes), filename="post.jpg"),
                    caption=final_caption,
                    parse_mode="HTML" if final_caption else None,
                    reply_markup=final_markup,
                    disable_notification=silent,
                )
            else:
                await context.bot.copy_message(
                    chat_id=channel,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=final_caption,
                    parse_mode="HTML" if final_caption else None,
                    reply_markup=final_markup,
                    disable_notification=silent,
                )

            wm_tag = " + Watermark ✅" if (wm_enabled and img_bytes) else ""
            await notify(
                f"✅ <b>Post ho gaya!</b>\n"
                f"🖼️ Photo ke saath{wm_tag}.\n"
                f"🔔 {bell}\n"
                f"📢 <code>{html_lib.escape(channel)}</code>" + source_tag,
                parse_mode="HTML"
            )

        elif msg.document or msg.video or msg.animation or msg.video_note:
            final_caption = None
            if raw_plain.strip():
                final_caption = _safe_truncate(
                    "🙏Jai Shree Ram Dosto🙏\n\n" + body_html, 1020
                )
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=final_caption,
                parse_mode="HTML" if final_caption else None,
                reply_markup=final_markup,
                disable_notification=silent,
            )
            await notify(
                f"✅ <b>Post ho gaya!</b>\n"
                f"🔔 {bell}\n"
                f"📢 <code>{html_lib.escape(channel)}</code>" + source_tag,
                parse_mode="HTML"
            )

        else:
            final_html = "🙏Jai Shree Ram Dosto🙏\n\n" + body_html
            await context.bot.send_message(
                chat_id=channel,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=final_markup,
                disable_notification=silent,
            )
            await notify(
                f"✅ <b>Post ho gaya!</b>\n"
                f"🔔 {bell}\n"
                f"📢 <code>{html_lib.escape(channel)}</code>" + source_tag,
                parse_mode="HTML"
            )

        if dup_key:
            mark_posted(dup_key)

    except Exception as e:
        await notify(
            f"❌ <b>Post fail!</b>\n<code>{html_lib.escape(str(e))}</code>",
            parse_mode="HTML"
        )


# =============================================================================
# ENTRY POINT 1 — Admin DM (reply DM mein)
# =============================================================================
async def handle_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    if context.user_data and context.user_data.get("action"):
        await handle_text_input(update, context)
        return

    msg = update.message
    if not msg:
        return

    async def notify(text, **kwargs):
        try:
            return await msg.reply_text(text, **kwargs)
        except Exception as e:
            logger.error(f"DM reply fail: {e}")
            return None

    await process_and_post(context, msg, notify)


# =============================================================================
# ENTRY POINT 2 — Draft channel post (reply usi channel mein)
# =============================================================================
async def _offer_source_setup(context, chat):
    """Unknown channel se post aayi — admin ko ID + button bhejo (ek hi baar)."""
    if chat.id in _offered_channels:
        return
    _offered_channels.add(chat.id)
    title = chat.title or "Channel"
    await dm_admin(
        context,
        f"📥 <b>Naye channel se post aayi</b>\n\n"
        f"Naam : <b>{html_lib.escape(title)}</b>\n"
        f"ID   : <code>{chat.id}</code>\n\n"
        f"Kya isse draft channel banana hai? "
        f"Iske baad yahan ki har post automatically post channel pe chali jayegi.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Haan, draft bana do", callback_data=f"srcset_{chat.id}"),
            InlineKeyboardButton("❌ Nahi",                callback_data="cancel"),
        ]])
    )


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return

    # ── LOOP GUARD: bot ka apna status message dobara process na ho ────────
    if _is_own_message(msg, context.bot.id):
        return

    config = load_config()
    source = (config.get("source_channel") or "").strip()
    target = (config.get("channel") or "").strip()

    # Apne hi post channel ko kabhi na uthao
    if target and chat_matches(msg.chat, target):
        return

    if not source:
        await _offer_source_setup(context, msg.chat)
        return

    if not chat_matches(msg.chat, source):
        return

    if same_channel(source, target):
        logger.warning("Source aur target channel same hain — skip.")
        await dm_admin(
            context,
            "⚠️ <b>Draft aur post channel same hai!</b>\n"
            "Infinite loop se bachne ke liye post skip kar di. "
            "/setsource se alag draft channel set karo.",
            parse_mode="HTML"
        )
        return

    src_name   = msg.chat.title or source
    source_tag = f"\n📥 Source: <b>{html_lib.escape(src_name)}</b>"

    async def notify(text, **kwargs):
        """Status reply DRAFT CHANNEL mein, hamesha silent (ye sirf receipt hai)."""
        try:
            sent = await msg.reply_text(
                text + SELF_MARKER, disable_notification=True, **kwargs
            )
            return _remember_own(sent)
        except Exception as e:
            # Channel mein post permission nahi? To DM pe bhej do.
            logger.error(f"Draft channel reply fail: {e} — DM pe bhej raha hoon")
            return await dm_admin(context, text, **kwargs)

    logger.info(f"Draft channel post pakda: {msg.chat.id} / msg {msg.message_id}")
    await process_and_post(context, msg, notify, config=config, source_tag=source_tag)


# =============================================================================
# CALLBACK HANDLER
# =============================================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.from_user or not is_admin(query.from_user.id):
        await query.answer("Unauthorized.")
        return
    await query.answer()
    data = query.data or ""

    # ── Cancel ────────────────────────────────────────────────────────────
    if data == "cancel":
        if context.user_data is not None:
            context.user_data.clear()
        try:
            await query.edit_message_text("❌ Cancel ho gaya.")
        except Exception:
            pass
        return

    # ── Silent toggle ─────────────────────────────────────────────────────
    if data == "silent_toggle":
        cfg = load_config()
        cfg["silent"] = not cfg.get("silent", True)
        save_config(cfg)
        try:
            await query.edit_message_text(
                _silent_status_text(cfg["silent"]),
                parse_mode="HTML",
                reply_markup=_silent_kb(cfg["silent"])
            )
        except Exception:
            pass
        return

    # ── Draft channel quick-set ───────────────────────────────────────────
    if data.startswith("srcset_"):
        chat_id = data.split("_", 1)[1]
        cfg     = load_config()
        target  = (cfg.get("channel") or "").strip()
        if same_channel(chat_id, target):
            try:
                await query.edit_message_text(
                    "⚠️ Ye to tumhara post channel hi hai! Draft channel alag hona chahiye."
                )
            except Exception:
                pass
            return
        cfg["source_channel"] = chat_id
        save_config(cfg)
        try:
            await query.edit_message_text(
                f"✅ <b>Draft channel set ho gaya!</b>\n"
                f"📥 <code>{html_lib.escape(chat_id)}</code>\n\n"
                f"Ab yahan jo bhi post karoge, wo automatically transform hoke "
                f"post channel pe chali jayegi — aur status reply wahin milega.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # ── Watermark callbacks ────────────────────────────────────────────────
    if data == "wm_toggle":
        cfg = load_config()
        wm  = cfg.setdefault("watermark", {"enabled": True, "text": "@DealKoti"})
        wm["enabled"] = not wm.get("enabled", True)
        cfg["watermark"] = wm
        save_config(cfg)
        try:
            await query.edit_message_text(
                _watermark_status_text(wm),
                parse_mode="HTML",
                reply_markup=_watermark_kb(wm)
            )
        except Exception:
            pass
        return

    if data == "wm_set_text":
        context.user_data["action"] = "wm_wait_text"
        try:
            await query.edit_message_text(
                "✏️ <b>Watermark Text Set Karo</b>\n\n"
                "Naya watermark text type karo (max 30 characters):",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if data == "wm_confirm_text":
        new_text = context.user_data.pop("wm_pending_text", None)
        context.user_data.pop("action", None)
        if new_text:
            cfg = load_config()
            wm  = cfg.setdefault("watermark", {"enabled": True, "text": "@DealKoti"})
            wm["text"] = new_text
            cfg["watermark"] = wm
            save_config(cfg)
        try:
            await query.edit_message_text(
                f"✅ <b>Watermark text set ho gaya!</b>\n\n"
                f"Text: <code>{html_lib.escape(new_text or '@DealKoti')}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # ── /setbutton callbacks ───────────────────────────────────────────────
    if data == "sb_main":
        cfg     = load_config()
        buttons = cfg.get("buttons", {})
        try:
            await query.edit_message_text(
                "🎛️ <b>Button Settings</b>\n\n"
                + _setbutton_status_text(buttons)
                + "\n\n<i>Kaun sa configure karna hai?</i>",
                parse_mode="HTML",
                reply_markup=_setbutton_main_kb(buttons)
            )
        except Exception:
            pass
        return

    if data in ("sb_btn1", "sb_btn2"):
        btn_key = data.split("_")[1]
        cfg     = load_config()
        btn     = cfg.get("buttons", {}).get(btn_key, {})
        try:
            await query.edit_message_text(
                _btn_detail_text(btn_key, btn),
                parse_mode="HTML",
                reply_markup=_setbutton_detail_kb(btn_key, btn)
            )
        except Exception:
            pass
        return

    if data in ("sb_btn1_toggle", "sb_btn2_toggle"):
        btn_key = data.split("_")[1]
        cfg     = load_config()
        buttons = cfg.setdefault("buttons", {})
        btn     = buttons.setdefault(btn_key, {})
        btn["enabled"]   = not btn.get("enabled", False)
        buttons[btn_key] = btn
        cfg["buttons"]   = buttons
        save_config(cfg)
        try:
            await query.edit_message_text(
                _btn_detail_text(btn_key, btn),
                parse_mode="HTML",
                reply_markup=_setbutton_detail_kb(btn_key, btn)
            )
        except Exception:
            pass
        return

    if data in ("sb_btn1_rename", "sb_btn2_rename"):
        btn_key = data.split("_")[1]
        context.user_data["action"]     = f"sb_{btn_key}_wait_label"
        context.user_data["sb_btn_key"] = btn_key
        try:
            await query.edit_message_text(
                f"📝 <b>Button {btn_key[-1]}</b> ka naya naam type karo (max 20 characters):",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if data in ("sb_btn1_link", "sb_btn2_link"):
        btn_key = data.split("_")[1]
        context.user_data["action"]     = f"sb_{btn_key}_wait_link"
        context.user_data["sb_btn_key"] = btn_key
        try:
            await query.edit_message_text(
                f"🔗 <b>Button {btn_key[-1]}</b> ka link type karo\n"
                f"(https:// ya t.me/ se shuru hona chahiye):",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if data in ("sb_btn1_confirm", "sb_btn2_confirm"):
        btn_key  = data.split("_")[1]
        new_val  = context.user_data.pop(f"sb_{btn_key}_pending", None)
        field    = context.user_data.pop(f"sb_{btn_key}_field", None)
        context.user_data.pop("action", None)
        cfg      = load_config()
        buttons  = cfg.setdefault("buttons", {})
        btn      = buttons.setdefault(btn_key, {})
        if new_val and field:
            btn[field]       = new_val
            buttons[btn_key] = btn
            cfg["buttons"]   = buttons
            save_config(cfg)
        btn = load_config().get("buttons", {}).get(btn_key, btn)
        try:
            await query.edit_message_text(
                f"✅ <b>Saved!</b>\n\n" + _btn_detail_text(btn_key, btn),
                parse_mode="HTML",
                reply_markup=_setbutton_detail_kb(btn_key, btn)
            )
        except Exception:
            pass
        return

    if data in ("sb_btn1_cancel_edit", "sb_btn2_cancel_edit"):
        btn_key = data.split("_")[1]
        context.user_data.pop(f"sb_{btn_key}_pending", None)
        context.user_data.pop(f"sb_{btn_key}_field", None)
        context.user_data.pop("action", None)
        btn = load_config().get("buttons", {}).get(btn_key, {})
        try:
            await query.edit_message_text(
                "❌ Cancel ho gaya.\n\n" + _btn_detail_text(btn_key, btn),
                parse_mode="HTML",
                reply_markup=_setbutton_detail_kb(btn_key, btn)
            )
        except Exception:
            pass
        return


# =============================================================================
# TEXT INPUT HANDLER
# =============================================================================
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    action = context.user_data.get("action")
    text   = (update.message.text or "").strip()

    if not action:
        return

    # /setchannel flow
    if action == "wait_channel_id":
        if not text:
            await update.message.reply_text("⚠️ Channel ID khali nahi ho sakta.")
            return
        cfg = load_config()
        if same_channel(text, cfg.get("source_channel", "")):
            await update.message.reply_text(
                "⚠️ Ye to draft channel hai! Post channel alag hona chahiye, "
                "warna infinite loop ban jayega."
            )
            return
        cfg["channel"] = text
        save_config(cfg)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ <b>Post channel set ho gaya!</b>\n📢 <code>{html_lib.escape(text)}</code>",
            parse_mode="HTML"
        )
        return

    # /setsource flow
    if action == "wait_source_id":
        if not text:
            await update.message.reply_text("⚠️ Channel ID khali nahi ho sakta.")
            return
        cfg = load_config()
        if same_channel(text, cfg.get("channel", "")):
            await update.message.reply_text(
                "⚠️ Ye to tumhara post channel hai! Draft channel alag hona chahiye, "
                "warna bot apni hi post baar baar uthata rahega."
            )
            return
        cfg["source_channel"] = text
        save_config(cfg)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ <b>Draft channel set ho gaya!</b>\n"
            f"📥 <code>{html_lib.escape(text)}</code>\n\n"
            f"Ab yahan post karo — main khud uthaake transform karke "
            f"post channel pe bhej dunga, aur status reply wahin dunga.\n\n"
            f"<i>Dhyan rakhna: bot ko is channel mein admin banana zaroori hai.</i>",
            parse_mode="HTML"
        )
        return

    # /watermark text flow
    if action == "wm_wait_text":
        if len(text) > 30:
            await update.message.reply_text("⚠️ Max 30 characters hone chahiye.")
            return
        context.user_data["wm_pending_text"] = text
        context.user_data["action"] = None
        await update.message.reply_text(
            f"📋 <b>Preview:</b>\n\nWatermark text: <code>{html_lib.escape(text)}</code>\n\nSave karo?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Save",    callback_data="wm_confirm_text"),
                InlineKeyboardButton("❌ Cancel",  callback_data="cancel"),
            ]])
        )
        return

    # /setbutton label flow
    if action.endswith("_wait_label"):
        btn_key = context.user_data.get("sb_btn_key", "btn1")
        if len(text) > 20:
            await update.message.reply_text("⚠️ Naam max 20 characters hona chahiye.")
            return
        context.user_data[f"sb_{btn_key}_pending"] = text
        context.user_data[f"sb_{btn_key}_field"]   = "label"
        context.user_data["action"] = None
        await update.message.reply_text(
            f"📋 <b>Preview:</b>\n\nButton naam: <b>{html_lib.escape(text)}</b>\n\nSave karo?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done",   callback_data=f"sb_{btn_key}_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"sb_{btn_key}_cancel_edit"),
            ]])
        )
        return

    # /setbutton link flow
    if action.endswith("_wait_link"):
        btn_key = context.user_data.get("sb_btn_key", "btn1")
        if text.startswith("t.me/"):
            text = "https://" + text
        if not (text.startswith("https://") or text.startswith("http://")):
            await update.message.reply_text(
                "⚠️ Valid link daalo (https:// ya t.me/ se shuru hona chahiye)."
            )
            return
        context.user_data[f"sb_{btn_key}_pending"] = text
        context.user_data[f"sb_{btn_key}_field"]   = "url"
        context.user_data["action"] = None
        await update.message.reply_text(
            f"📋 <b>Preview:</b>\n\nButton link: <code>{html_lib.escape(text)}</code>\n\nSave karo?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done",   callback_data=f"sb_{btn_key}_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"sb_{btn_key}_cancel_edit"),
            ]])
        )
        return


# =============================================================================
# MAIN
# =============================================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable set nahi hai!")
    if ADMIN_ID == 0:
        raise ValueError("ADMIN_ID environment variable set nahi hai ya invalid hai!")

    try:
        init_db()
    except Exception as e:
        logger.error(f"DB init failed: {e}")
        raise

    async def post_init(application):
        await application.bot.set_my_commands([
            ("start",         "ℹ️ Bot ki info"),
            ("help",          "📖 Saari commands"),
            ("status",        "📊 Poora status ek jagah"),
            ("setchannel",    "📢 Post channel set karo"),
            ("setsource",     "📥 Draft channel set karo (auto pickup)"),
            ("silent",        "🔔 Notification silent ya loud"),
            ("watermark",     "🖼️ Watermark ON/OFF aur text"),
            ("setbutton",     "🎛️ Post ke buttons configure karo"),
            ("testamz",       "🧪 Amazon API test karo"),
            ("exportconfig",  "💾 Config ka backup lo"),
        ])
        logger.info("Bot commands Telegram pe register ho gayi.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    dm_only = filters.ChatType.PRIVATE

    app.add_handler(CommandHandler("start",        cmd_start,        filters=dm_only))
    app.add_handler(CommandHandler("help",         cmd_help,         filters=dm_only))
    app.add_handler(CommandHandler("status",       cmd_status,       filters=dm_only))
    app.add_handler(CommandHandler("setchannel",   cmd_setchannel,   filters=dm_only))
    app.add_handler(CommandHandler("setsource",    cmd_setsource,    filters=dm_only))
    app.add_handler(CommandHandler("silent",       cmd_silent,       filters=dm_only))
    app.add_handler(CommandHandler("watermark",    cmd_watermark,    filters=dm_only))
    app.add_handler(CommandHandler("setbutton",    cmd_setbutton,    filters=dm_only))
    app.add_handler(CommandHandler("testamz",      cmd_testamz,      filters=dm_only))
    app.add_handler(CommandHandler("exportconfig", cmd_exportconfig, filters=dm_only))

    app.add_handler(CallbackQueryHandler(handle_callback))

    # Admin DM se message → reply DM mein
    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_deal
    ))

    # Draft channel se auto pickup → reply usi channel mein
    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST,
        handle_channel_post
    ))

    logger.info("DealsKoti Bot start ho raha hai...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "channel_post", "callback_query"],
    )


if __name__ == "__main__":
    main()
