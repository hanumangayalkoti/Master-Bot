import os
import io
import re
import json
import html as html_lib
import asyncio
import logging
import aiohttp
from collections import deque
from datetime import datetime, timedelta, timezone

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
)
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes, AIORateLimiter
)

from amazon_api import (
    is_amazon_url, is_amazon_search_url, resolve_amazon_url,
    extract_asin, get_products_by_asins, get_product_by_asin,
    make_affiliate_url, make_cart_url, get_short_affiliate_link,
)
from caption import (
    build_amazon_caption, wrap_plain_post, _safe_truncate, _TAG_RE,
    FIELD_LABELS, FIELD_ORDER,
)
from database import (
    is_duplicate, mark_posted, cleanup_old_entries,
    queue_add_amazon, queue_add_other, queue_fetch_all, queue_delete,
    queue_bump_tries, queue_purge_old, queue_clear, queue_counts,
)
from storage import load_config, save_config, init_db
from watermark import apply_watermark

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0
    logger.error("ADMIN_ID env var must be a number!")

# Telegram channel limit ~20 msg/min. 4 second = 15/min — andar rehne ke liye.
POST_GAP_SECONDS    = 4.0
MAX_PER_MESSAGE     = 15      # ek message se max itni products
QUEUE_MAX_AGE_HOURS = 4       # isse purani deal drop
MAX_POST_TRIES      = 3       # queue item itni baar fail hua to drop

SELF_MARKER = "\u2063"        # invisible — bot apne message pehchanne ke liye

URL_REGEX = re.compile(r"(https?://[^\s\]\[<>\"']+)")

FOOTER_LINE_PATTERN = re.compile(
    r'^[-—\s]*(deal\s*from|buy\s*on|shop\s*on|source\s*:|via\s*:|'
    r'brought\s*by|available\s*on|check\s*on|grab\s*on|get\s*it\s*on|'
    r'amazon\s*deal|flipkart\s*deal|meesho\s*deal|deal\s*by|'
    r'posted\s*by|bot\s*by)\b.*$',
    re.IGNORECASE
)

_offered_channels = set()
_own_msg_ids = deque(maxlen=1000)
_own_msg_set = set()

# Ek hi batch chale — overlap se bachne ke liye
_flush_lock = asyncio.Lock()


def _remember_own(m):
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
    if msg.from_user and msg.from_user.id == bot_id:
        return True
    try:
        if (msg.chat_id, msg.message_id) in _own_msg_set:
            return True
    except Exception:
        pass
    return SELF_MARKER in (msg.text or msg.caption or "")


# =============================================================================
# BASIC HELPERS
# =============================================================================
def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID


def extract_urls(text: str) -> list:
    return URL_REGEX.findall(text) if text else []


def get_amazon_urls(urls: list) -> list:
    return [u for u in urls if is_amazon_url(u)]


async def _download_image(url: str) -> bytes | None:
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


async def _get_photo_bytes(bot, file_id: str) -> bytes | None:
    if not file_id:
        return None
    try:
        file = await bot.get_file(file_id)
        return bytes(await file.download_as_bytearray())
    except Exception as e:
        logger.error(f"Photo download fail: {e}")
    return None


def chat_matches(chat, ident: str) -> bool:
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


def same_channel(a: str, b: str) -> bool:
    x = (a or "").strip().lstrip("@").lower()
    y = (b or "").strip().lstrip("@").lower()
    return bool(x) and x == y


async def dm_admin(context, text, **kwargs):
    if not ADMIN_ID:
        return None
    try:
        return await context.bot.send_message(chat_id=ADMIN_ID, text=text, **kwargs)
    except Exception as e:
        logger.error(f"Admin DM fail: {e}")
        return None


async def _edit_or_notify(wait_msg, notify, text, **kwargs):
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


def _next_hour_delay() -> float:
    """Agle ghante ke top (1:00, 2:00...) tak kitne second."""
    now = datetime.now()
    nxt = (now + timedelta(hours=1)).replace(minute=0, second=5, microsecond=0)
    return max(10.0, (nxt - now).total_seconds())


# =============================================================================
# ENTITY / HTML
# =============================================================================
def _py_to_utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


class _Ent:
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


def ents_to_json(entities) -> list:
    out = []
    for e in (entities or []):
        out.append({
            "offset": e.offset,
            "length": e.length,
            "type":   str(getattr(e.type, "value", e.type)),
            "url":    getattr(e, "url", None),
        })
    return out


def ents_from_json(raw) -> list:
    return [_Ent(d["offset"], d["length"], d["type"], d.get("url"))
            for d in (raw or [])]


def replace_url_keep_entities(text: str, entities: list, old_url: str, new_url: str):
    """URL badlo aur entity offsets bhi shift karo — warna formatting khisak jaati hai."""
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
            new_ents.append(_clone_ent(ent))
        elif s >= end_u16:
            new_ents.append(_clone_ent(ent, offset=s + delta))
        elif s <= start_u16 and e >= end_u16:
            new_ents.append(_clone_ent(ent, length=ent.length + delta))
        else:
            continue
    return new_text, new_ents


async def replace_amazon_links(text: str, entities: list, urls: list):
    for url in urls:
        if not is_amazon_url(url):
            continue
        try:
            short = await get_short_affiliate_link(url)
        except Exception as e:
            logger.error(f"Affiliate link fail: {e}")
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

    # Safety: footer remover poora message kha gaya to original hi rakho.
    # ("Flipkart deal ..." jaisi single-line post pe aisa hota tha.)
    if not cleaned.strip() and plain_text.strip():
        return plain_text.rstrip(), list(entities or [])

    cutoff = _py_to_utf16_len(cleaned)
    return cleaned, [e for e in (entities or []) if e.offset + e.length <= cutoff]


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
        s_u16 = ent.offset
        e_u16 = ent.offset + ent.length
        s = utf16_map[s_u16] if s_u16 < len(utf16_map) else s_u16
        e = utf16_map[e_u16] if e_u16 < len(utf16_map) else e_u16
        if e > len(text) or s >= len(text) or e <= s:
            continue
        etype = str(getattr(ent.type, "value", ent.type))

        pairs = {
            "url":           ("<b>", "</b>"),
            "bold":          ("<b>", "</b>"),
            "italic":        ("<i>", "</i>"),
            "underline":     ("<u>", "</u>"),
            "strikethrough": ("<s>", "</s>"),
            "code":          ("<code>", "</code>"),
            "pre":           ("<pre>", "</pre>"),
            "spoiler":       ("<tg-spoiler>", "</tg-spoiler>"),
        }
        if etype == "text_link":
            url = html_lib.escape(ent.url or "")
            open_tags[s]    = f'<a href="{url}"><b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b></a>'
        elif etype in pairs:
            o, c = pairs[etype]
            open_tags[s]    = o + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + c

    result = []
    for i, ch in enumerate(text):
        result.append(open_tags[i])
        result.append(html_lib.escape(ch))
        result.append(close_tags[i])
    return ''.join(result)


# =============================================================================
# UI BUILDERS
# =============================================================================
def build_final_markup(config: dict, asin: str = ""):
    """Inline buttons. Cart button sirf Amazon post (asin) ke saath aata hai."""
    btns = config.get("buttons", {})
    rows = []

    cart = btns.get("cart", {})
    if asin and cart.get("enabled"):
        url = make_cart_url(asin)
        if url:
            rows.append([InlineKeyboardButton(
                cart.get("label") or "🛒 Add to Cart", url=url
            )])

    row = []
    for key in ("btn1", "btn2"):
        b = btns.get(key, {})
        if b.get("enabled") and b.get("label") and b.get("url"):
            row.append(InlineKeyboardButton(b["label"], url=b["url"]))
    if row:
        rows.append(row)

    return InlineKeyboardMarkup(rows) if rows else None


def _onoff(v) -> str:
    return "✅" if v else "❌"


def _silent_status_text(silent: bool) -> str:
    if silent:
        return (
            "🔔 <b>Notification</b>\n\n"
            "Status: <b>🔕 SILENT</b>\n\n"
            "Post channel mein normal aati hai, par subscriber ke phone pe "
            "awaaz nahi hoti. Unread count phir bhi badhta hai."
        )
    return (
        "🔔 <b>Notification</b>\n\n"
        "Status: <b>🔔 LOUD</b>\n\n"
        "Har post pe poori notification jaati hai."
    )


def _silent_kb(silent: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔔 Loud karo" if silent else "🔕 Silent karo",
                              callback_data="silent_toggle")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


def _watermark_status_text(wm: dict) -> str:
    return (
        f"🖼️ <b>Watermark</b>\n\n"
        f"Status : <b>{'✅ ON' if wm.get('enabled', True) else '❌ OFF'}</b>\n"
        f"Text   : <code>{html_lib.escape(wm.get('text', '@DealKoti'))}</code>\n\n"
        f"<i>Image ke bottom-right corner pe lagta hai.</i>"
    )


def _watermark_kb(wm: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Text Badlo", callback_data="wm_set_text")],
        [InlineKeyboardButton("🔴 Turn OFF" if wm.get("enabled", True) else "🟢 Turn ON",
                              callback_data="wm_toggle")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
    ])


# ── /amz_post ─────────────────────────────────────────────────────────────
def _amz_status_text(cfg: dict) -> str:
    detailed = cfg.get("amz_detailed", True)
    f        = cfg.get("amz_fields", {})
    if detailed:
        on_list = [FIELD_LABELS.get(k, k) for k in FIELD_ORDER if f.get(k)]
        extra   = "🖼️ Image ON" if f.get("image") else "🖼️ Image OFF"
        body = (
            f"Mode: <b>✅ DETAILED</b>\n"
            f"{extra}\n"
            f"🔗 Link {'ON' if f.get('link', True) else 'OFF'}\n\n"
            f"<b>Caption mein:</b> {html_lib.escape(', '.join(on_list)) or '—'}"
        )
    else:
        body = (
            "Mode: <b>❌ MINIMAL</b>\n\n"
            "Sirf <b>Price + Link</b> jaayega. Koi image nahi, "
            "koi detail nahi."
        )
    return (
        "🛍️ <b>Amazon Post Settings</b>\n\n" + body +
        "\n\n<i>Photo caption ki limit 1024 character hai — sab on karne pe "
        "kuch fields chhoot jaayengi, main bata dunga.</i>"
    )


def _amz_kb(cfg: dict) -> InlineKeyboardMarkup:
    detailed = cfg.get("amz_detailed", True)
    f        = cfg.get("amz_fields", {})
    rows = [[InlineKeyboardButton(
        "🔻 MINIMAL karo (price + link)" if detailed else "🔺 DETAILED karo",
        callback_data="amz_mode"
    )]]

    if detailed:
        order = ["image", "link"] + FIELD_ORDER
        pair  = []
        for k in order:
            pair.append(InlineKeyboardButton(
                f"{_onoff(f.get(k))} {FIELD_LABELS.get(k, k)}",
                callback_data=f"amzf_{k}"
            ))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)

    rows.append([InlineKeyboardButton("❌ Close", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# ── /park_post ────────────────────────────────────────────────────────────
def _park_status_text(cfg: dict, amz_n: int, oth_n: int) -> str:
    if cfg.get("park_post"):
        nxt = (datetime.now() + timedelta(hours=1)).replace(minute=0)
        body = (
            f"Status: <b>🅿️ PARK ON</b>\n\n"
            f"Deals park ho rahi hain, har ghante ke top pe "
            f"(agla: <b>{nxt.strftime('%H:00')}</b>) ek saath jaayengi.\n"
            f"Amazon deals discount ke hisaab se sorted, non-Amazon "
            f"beech mein arrival order se.\n\n"
            f"Queue mein: <b>{amz_n}</b> Amazon + <b>{oth_n}</b> other\n"
            f"<i>{QUEUE_MAX_AGE_HOURS} ghante se purani deal apne aap drop ho jaati hai.</i>"
        )
    else:
        body = (
            "Status: <b>⚡ INSTANT</b>\n\n"
            "Deal aate hi turant post ho jaati hai."
        )
    return "🅿️ <b>Park Post</b>\n\n" + body


def _park_kb(cfg: dict, pending: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        "⚡ INSTANT karo" if cfg.get("park_post") else "🅿️ PARK karo",
        callback_data="park_toggle"
    )]]
    if pending:
        rows.append([InlineKeyboardButton(f"📤 Abhi bhej do ({pending})",
                                          callback_data="park_flush")])
        rows.append([InlineKeyboardButton(f"🗑️ Queue khali karo ({pending})",
                                          callback_data="park_clear")])
    rows.append([InlineKeyboardButton("❌ Close", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# ── /setbutton ────────────────────────────────────────────────────────────
def _setbutton_status_text(btns: dict) -> str:
    b1   = btns.get("btn1", {})
    b2   = btns.get("btn2", {})
    cart = btns.get("cart", {})
    return (
        f"🛒 <b>Add to Cart</b> — {_onoff(cart.get('enabled'))}\n"
        f"   Naam: {html_lib.escape(cart.get('label', '-'))}\n"
        f"   <i>Sirf Amazon post ke neeche. Cart mein daalne se "
        f"attribution 24 ghante se 89 din ho jaati hai.</i>\n\n"
        f"📌 <b>Button 1</b> — {_onoff(b1.get('enabled'))}\n"
        f"   Naam: {html_lib.escape(b1.get('label', '-'))}\n"
        f"   Link: <code>{html_lib.escape(b1.get('url') or '—')}</code>\n\n"
        f"📌 <b>Button 2</b> — {_onoff(b2.get('enabled'))}\n"
        f"   Naam: {html_lib.escape(b2.get('label', '-'))}\n"
        f"   Link: <code>{html_lib.escape(b2.get('url') or '—')}</code>"
    )


def _setbutton_main_kb(btns: dict) -> InlineKeyboardMarkup:
    cart = btns.get("cart", {})
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{_onoff(cart.get('enabled'))} Add to Cart",
                              callback_data="sb_cart")],
        [InlineKeyboardButton(f"✏️ {btns.get('btn1', {}).get('label', 'Button 1')}",
                              callback_data="sb_btn1")],
        [InlineKeyboardButton(f"✏️ {btns.get('btn2', {}).get('label', 'Button 2')}",
                              callback_data="sb_btn2")],
        [InlineKeyboardButton("❌ Close", callback_data="cancel")],
    ])


def _cart_detail_text(cart: dict) -> str:
    return (
        f"🛒 <b>Add to Cart Button</b>\n\n"
        f"Naam   : <b>{html_lib.escape(cart.get('label', '-'))}</b>\n"
        f"Status : {_onoff(cart.get('enabled'))}\n\n"
        f"<i>Link bot khud banata hai (ASIN + tera tag), "
        f"tujhe kuch nahi daalna. Ye sirf Amazon product post "
        f"ke neeche dikhega.</i>"
    )


def _cart_detail_kb(cart: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Rename", callback_data="sb_cart_rename")],
        [InlineKeyboardButton("🔴 Turn OFF" if cart.get("enabled") else "🟢 Turn ON",
                              callback_data="sb_cart_toggle")],
        [InlineKeyboardButton("⬅️ Back", callback_data="sb_main")],
    ])


def _setbutton_detail_kb(key: str, btn: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Rename",   callback_data=f"sb_{key}_rename")],
        [InlineKeyboardButton("🔗 Set Link", callback_data=f"sb_{key}_link")],
        [InlineKeyboardButton("🔴 Turn OFF" if btn.get("enabled") else "🟢 Turn ON",
                              callback_data=f"sb_{key}_toggle")],
        [InlineKeyboardButton("⬅️ Back", callback_data="sb_main")],
    ])


def _btn_detail_text(key: str, btn: dict) -> str:
    return (
        f"🎛️ <b>Button {key[-1]} Settings</b>\n\n"
        f"📝 Naam  : <b>{html_lib.escape(btn.get('label', '-'))}</b>\n"
        f"🔗 Link  : <code>{html_lib.escape(btn.get('url') or 'Set nahi hua')}</code>\n"
        f"Status : {_onoff(btn.get('enabled'))}"
    )


# ── /header, /footer ──────────────────────────────────────────────────────
def _hf_status_text(kind: str, d: dict) -> str:
    name = "Header" if kind == "header" else "Footer"
    spot = "post ke sabse upar" if kind == "header" else "post ke sabse neeche"
    return (
        f"{'🔝' if kind == 'header' else '🔚'} <b>{name}</b>\n\n"
        f"Status : {_onoff(d.get('enabled'))}\n"
        f"Text   : <code>{html_lib.escape(d.get('text') or '—')}</code>\n\n"
        f"<i>{spot} lagta hai — Amazon aur non-Amazon dono post mein.</i>"
    )


def _hf_kb(kind: str, d: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Text Badlo", callback_data=f"hf_{kind}_text")],
        [InlineKeyboardButton("🔴 Turn OFF" if d.get("enabled") else "🟢 Turn ON",
                              callback_data=f"hf_{kind}_toggle")],
        [InlineKeyboardButton("❌ Close", callback_data="cancel")],
    ])


# =============================================================================
# COMMANDS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 <b>DealsKoti Bot chalu hai!</b>\n\n"
        "1️⃣ Mujhe deal bhejo — turant post, reply yahin\n"
        "2️⃣ Draft channel mein post karo — reply wahin\n\n"
        "Ek message mein kitne bhi Amazon links — har product ki alag post.\n\n"
        "/help se saare commands dekho.",
        parse_mode=ParseMode.HTML
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 <b>DealsKoti Bot — Commands</b>\n\n"
        "ℹ️ /start — Bot ki info\n"
        "📖 /help — Ye list\n"
        "📊 /status — Poora status\n"
        "📢 /setchannel — Post channel\n"
        "📥 /setsource — Draft channel (auto pickup)\n"
        "🛍️ /amz_post — Amazon post ki har detail on/off\n"
        "🅿️ /park_post — Hourly batch ya instant\n"
        "📋 /queue — Queue dekho / bhejo / khali karo\n"
        "🔔 /silent — Notification silent ya loud\n"
        "🔝 /header — Post ka header\n"
        "🔚 /footer — Post ka footer\n"
        "🖼️ /watermark — Watermark on/off + text\n"
        "🎛️ /setbutton — Buttons + Add to Cart\n"
        "🧪 /testamz — Amazon API test\n"
        "💾 /exportconfig — Config backup\n\n"
        "<b>⚡ Shortcuts</b>\n"
        "<code>/silent on|off</code>\n"
        "<code>/park_post on|off</code>\n"
        "<code>/amz_post on|off</code> (detailed / minimal)\n"
        "<code>/watermark on|off</code>\n"
        "<code>/setsource off</code>",
        parse_mode=ParseMode.HTML
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg     = load_config()
    amz_n, oth_n = queue_counts()
    f       = cfg.get("amz_fields", {})
    btns    = cfg.get("buttons", {})
    wm      = cfg.get("watermark", {})
    hdr     = cfg.get("header", {})
    ftr     = cfg.get("footer", {})

    on_fields = [FIELD_LABELS.get(k, k) for k in FIELD_ORDER if f.get(k)]

    lines = [
        "⚙️ <b>Bot Status</b>\n",
        f"📥 Draft  : <code>{html_lib.escape(cfg.get('source_channel') or '❌ /setsource')}</code>",
        f"📢 Post   : <code>{html_lib.escape(cfg.get('channel') or '❌ /setchannel')}</code>\n",
        f"🅿️ Mode      : <b>{'PARK (hourly)' if cfg.get('park_post') else 'INSTANT'}</b>",
        f"📋 Queue     : {amz_n} Amazon + {oth_n} other",
        f"🔔 Notify    : {'🔕 Silent' if cfg.get('silent') else '🔔 Loud'}",
        f"🛍️ Amazon    : <b>{'DETAILED' if cfg.get('amz_detailed') else 'MINIMAL (price+link)'}</b>",
        f"🖼️ Image     : {_onoff(f.get('image'))}   Watermark: {_onoff(wm.get('enabled'))}",
        f"🔝 Header    : {_onoff(hdr.get('enabled'))}   🔚 Footer: {_onoff(ftr.get('enabled'))}\n",
        f"🛒 Cart btn  : {_onoff(btns.get('cart', {}).get('enabled'))}",
        f"📌 Button 1  : {_onoff(btns.get('btn1', {}).get('enabled'))} "
        f"{html_lib.escape(btns.get('btn1', {}).get('label', '-'))}",
        f"📌 Button 2  : {_onoff(btns.get('btn2', {}).get('enabled'))} "
        f"{html_lib.escape(btns.get('btn2', {}).get('label', '-'))}\n",
        f"<b>Fields ON:</b> {html_lib.escape(', '.join(on_fields)) or '—'}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_amz_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg  = load_config()
    args = context.args or []
    if args:
        a = args[0].lower()
        if a in ("on", "detailed", "full"):
            cfg["amz_detailed"] = True
            save_config(cfg)
            await update.message.reply_text("✅ Amazon post <b>DETAILED</b> mode.",
                                            parse_mode=ParseMode.HTML)
            return
        if a in ("off", "minimal", "min"):
            cfg["amz_detailed"] = False
            save_config(cfg)
            await update.message.reply_text(
                "✅ Amazon post <b>MINIMAL</b> mode — sirf price + link.",
                parse_mode=ParseMode.HTML)
            return
    await update.message.reply_text(_amz_status_text(cfg), parse_mode=ParseMode.HTML,
                                    reply_markup=_amz_kb(cfg))


async def cmd_park_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg   = load_config()
    args  = context.args or []
    amz_n, oth_n = queue_counts()

    if args:
        a = args[0].lower()
        if a in ("on", "chalu"):
            cfg["park_post"] = True
            save_config(cfg)
            nxt = (datetime.now() + timedelta(hours=1)).replace(minute=0)
            await update.message.reply_text(
                f"🅿️ <b>Park mode ON.</b>\nAgli batch <b>{nxt.strftime('%H:00')}</b> baje.",
                parse_mode=ParseMode.HTML)
            return
        if a in ("off", "band"):
            cfg["park_post"] = False
            save_config(cfg)
            await update.message.reply_text(
                "⚡ <b>Instant mode ON.</b>\nQueue mein pade posts abhi bhej raha hoon...",
                parse_mode=ParseMode.HTML)
            context.application.create_task(
                flush_queue(context.application, reason="park off")
            )
            return

    await update.message.reply_text(
        _park_status_text(cfg, amz_n, oth_n), parse_mode=ParseMode.HTML,
        reply_markup=_park_kb(cfg, amz_n + oth_n))


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg = load_config()
    items = queue_fetch_all()
    amz   = [i for i in items if i["kind"] == "amazon"]
    oth   = [i for i in items if i["kind"] == "other"]

    if not items:
        await update.message.reply_text(
            "📋 <b>Queue khali hai.</b>\n\n"
            f"Mode: <b>{'PARK (hourly)' if cfg.get('park_post') else 'INSTANT'}</b>",
            parse_mode=ParseMode.HTML)
        return

    oldest = min(i["arrived_at"] for i in items)
    age    = datetime.now() - oldest
    lines = [
        f"📋 <b>Queue — {len(items)} items</b>\n",
        f"🛍️ Amazon : <b>{len(amz)}</b>",
        f"📝 Other  : <b>{len(oth)}</b>",
        f"⏱️ Sabse purani: {int(age.total_seconds() // 60)} minute",
        f"\n<i>Post karte waqt price fresh laaya jayega, "
        f"aur mari hui deals drop ho jaayengi.</i>",
    ]
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML,
        reply_markup=_park_kb(cfg, len(items)))


async def cmd_silent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg  = load_config()
    args = context.args or []
    if args:
        a = args[0].lower()
        if a in ("on", "chalu"):
            cfg["silent"] = True
            save_config(cfg)
            await update.message.reply_text("🔕 Silent posting <b>ON</b>.",
                                            parse_mode=ParseMode.HTML)
            return
        if a in ("off", "band"):
            cfg["silent"] = False
            save_config(cfg)
            await update.message.reply_text("🔔 Silent posting <b>OFF</b>.",
                                            parse_mode=ParseMode.HTML)
            return
    s = cfg.get("silent", True)
    await update.message.reply_text(_silent_status_text(s), parse_mode=ParseMode.HTML,
                                    reply_markup=_silent_kb(s))


async def cmd_header(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg = load_config()
    await update.message.reply_text(
        _hf_status_text("header", cfg.get("header", {})),
        parse_mode=ParseMode.HTML,
        reply_markup=_hf_kb("header", cfg.get("header", {})))


async def cmd_footer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg = load_config()
    await update.message.reply_text(
        _hf_status_text("footer", cfg.get("footer", {})),
        parse_mode=ParseMode.HTML,
        reply_markup=_hf_kb("footer", cfg.get("footer", {})))


async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    context.user_data["action"] = "wait_channel_id"
    cfg = load_config()
    await update.message.reply_text(
        f"📢 <b>Post Channel Set Karo</b>\n\n"
        f"Current: <code>{html_lib.escape(cfg.get('channel') or 'Set nahi hua')}</code>\n\n"
        f"Channel ID type karo (@mychannel ya -100123456789):",
        parse_mode=ParseMode.HTML)


async def cmd_setsource(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    args = context.args or []
    cfg  = load_config()
    if args and args[0].lower() in ("off", "band", "clear", "remove", "hatao"):
        cfg["source_channel"] = ""
        save_config(cfg)
        await update.message.reply_text(
            "❌ <b>Draft channel hata diya.</b>\nAb sirf DM wala system chalega.",
            parse_mode=ParseMode.HTML)
        return
    context.user_data.clear()
    context.user_data["action"] = "wait_source_id"
    await update.message.reply_text(
        f"📥 <b>Draft Channel Set Karo</b>\n\n"
        f"Current: <code>{html_lib.escape(cfg.get('source_channel') or 'Set nahi hua')}</code>\n\n"
        f"1. Draft channel mein mujhe <b>admin</b> banao (post permission ke saath)\n"
        f"2. Phir channel ID type karo\n\n"
        f"<i>ID nahi pata? Draft channel mein koi bhi post daal do — "
        f"main ID button ke saath bhej dunga.</i>",
        parse_mode=ParseMode.HTML)


async def cmd_watermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg  = load_config()
    wm   = cfg.setdefault("watermark", {"enabled": True, "text": "@DealKoti"})
    args = context.args or []
    if args:
        a = args[0].lower()
        if a == "on":
            wm["enabled"] = True
            save_config(cfg)
            await update.message.reply_text("✅ Watermark <b>ON</b>.",
                                            parse_mode=ParseMode.HTML)
            return
        if a == "off":
            wm["enabled"] = False
            save_config(cfg)
            await update.message.reply_text("✅ Watermark <b>OFF</b>.",
                                            parse_mode=ParseMode.HTML)
            return
    await update.message.reply_text(_watermark_status_text(wm),
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=_watermark_kb(wm))


async def cmd_setbutton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    btns = load_config().get("buttons", {})
    await update.message.reply_text(
        "🎛️ <b>Button Settings</b>\n\n" + _setbutton_status_text(btns),
        parse_mode=ParseMode.HTML, reply_markup=_setbutton_main_kb(btns))


async def cmd_testamz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔄 Amazon Creators API test ho rahi hai...")
    try:
        asin    = "B08N5WRWNW"
        product = await get_product_by_asin(asin)
        if product and product.get("title"):
            got = []
            for k in ("deal_badge", "stock_note", "seller", "brand",
                      "sales_rank", "features"):
                v = product.get(k)
                if v:
                    got.append(FIELD_LABELS.get(
                        {"deal_badge": "deal", "stock_note": "stock"}.get(k, k), k))
            await update.message.reply_text(
                f"✅ <b>API kaam kar raha hai!</b>\n\n"
                f"🏷️ {html_lib.escape(product['title'][:70])}\n"
                f"💰 {product.get('deal_price') or 'N/A'} "
                f"(MRP {product.get('actual_price') or 'N/A'}, "
                f"{product.get('discount_pct', 0)}% off)\n"
                f"⭐ {product.get('rating') or 'N/A'} / "
                f"{product.get('review_count') or 'N/A'} reviews\n"
                f"🖼️ Image: {'✅' if product.get('image_url') else '❌'}\n"
                f"🛒 Cart: <code>{html_lib.escape(product.get('cart_link', '')[:70])}</code>\n\n"
                f"<b>Extra fields mile:</b> "
                f"{html_lib.escape(', '.join(got)) if got else 'koi nahi'}",
                parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await update.message.reply_text(
                "⚠️ Product data nahi mila.\n"
                "CREDENTIAL_ID / CREDENTIAL_SECRET / PARTNER_TAG check karo.")
    except Exception as e:
        await update.message.reply_text(
            f"❌ API error:\n<code>{html_lib.escape(str(e))}</code>",
            parse_mode=ParseMode.HTML)


async def cmd_exportconfig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    cfg = json.dumps(load_config(), indent=2, ensure_ascii=False)
    if len(cfg) > 3500:
        cfg = cfg[:3500] + "\n... (kata gaya)"
    await update.message.reply_text(
        f"📦 <b>Config Backup</b>\n\n<pre>{html_lib.escape(cfg)}</pre>",
        parse_mode=ParseMode.HTML)


# =============================================================================
# POSTING ENGINE
# =============================================================================
async def _send_with_retry(coro_factory, tries: int = 3):
    """
    Telegram ka 429 respect karo — retry_after padh ke ruko.
    coro_factory: har attempt pe naya coroutine banata hai.
    """
    last_err = None
    for attempt in range(tries):
        try:
            return await coro_factory()
        except RetryAfter as e:
            wait = float(getattr(e, "retry_after", 5)) + 1
            logger.warning(f"429 — {wait:.0f}s ruk raha hoon (try {attempt+1})")
            await asyncio.sleep(wait)
            last_err = e
        except (TimedOut, NetworkError) as e:
            await asyncio.sleep(2 + attempt * 2)
            last_err = e
    if last_err:
        raise last_err
    return None


async def post_amazon_product(context, product: dict, cfg: dict):
    """
    Ek Amazon product post karo. Returns (status, detail, note)
      posted / duplicate / error
    """
    asin       = product.get("asin", "")
    channel    = cfg.get("channel", "").strip()
    silent     = cfg.get("silent", True)
    detailed   = cfg.get("amz_detailed", True)
    fields     = cfg.get("amz_fields", {})
    wm         = cfg.get("watermark", {})
    title      = (product.get("title") or "").strip()

    dup, when = is_duplicate(title or asin)
    if dup:
        return "duplicate", f"{title[:55] or asin} — {when}", ""

    want_image = detailed and fields.get("image", True)
    short_link = product.get("affiliate_link") or make_affiliate_url(asin)

    img_bytes = None
    if want_image and product.get("image_url"):
        img_bytes = await _download_image(product["image_url"])
        if img_bytes and wm.get("enabled", True):
            img_bytes = apply_watermark(img_bytes, wm.get("text", "@DealKoti"))

    caption, skipped = build_amazon_caption(
        product, short_link, cfg, has_image=bool(img_bytes)
    )
    markup = build_final_markup(cfg, asin=asin)

    note = ""
    if img_bytes:
        note = "Amazon API" + (" + Watermark" if wm.get("enabled", True) else "")

    try:
        if img_bytes:
            await _send_with_retry(lambda: context.bot.send_photo(
                chat_id=channel,
                photo=InputFile(io.BytesIO(img_bytes), filename=f"{asin}.jpg"),
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_notification=silent,
            ))
        else:
            await _send_with_retry(lambda: context.bot.send_message(
                chat_id=channel,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=markup,
                disable_notification=silent,
            ))
        mark_posted(title or asin)
        return "posted", title or asin, note
    except Exception as e:
        logger.error(f"Post fail {asin}: {e}")
        return "error", str(e)[:120], ""


async def post_other(context, payload: dict, cfg: dict):
    """Non-Amazon post. Returns (status, detail)."""
    channel = cfg.get("channel", "").strip()
    silent  = cfg.get("silent", True)
    wm      = cfg.get("watermark", {})

    text      = payload.get("text") or ""
    entities  = ents_from_json(payload.get("entities"))
    file_id   = payload.get("photo_file_id") or ""
    media_fid = payload.get("media_file_id") or ""
    media_kind = payload.get("media_kind") or ""

    dup_key = (text.strip() or file_id or media_fid)[:300]
    if dup_key:
        dup, when = is_duplicate(dup_key)
        if dup:
            return "duplicate", f"non-Amazon post — {when}"

    body_html = entities_to_html(text, entities) if text else ""

    try:
        if file_id:
            img_bytes = await _get_photo_bytes(context.bot, file_id)
            if img_bytes and wm.get("enabled", True):
                img_bytes = apply_watermark(img_bytes, wm.get("text", "@DealKoti"))
            caption = wrap_plain_post(body_html, cfg, has_image=True) if text else None
            if img_bytes:
                await _send_with_retry(lambda: context.bot.send_photo(
                    chat_id=channel,
                    photo=InputFile(io.BytesIO(img_bytes), filename="post.jpg"),
                    caption=caption,
                    parse_mode=ParseMode.HTML if caption else None,
                    reply_markup=build_final_markup(cfg),
                    disable_notification=silent,
                ))
            else:
                await _send_with_retry(lambda: context.bot.send_photo(
                    chat_id=channel, photo=file_id, caption=caption,
                    parse_mode=ParseMode.HTML if caption else None,
                    reply_markup=build_final_markup(cfg),
                    disable_notification=silent,
                ))

        elif media_fid:
            caption = wrap_plain_post(body_html, cfg, has_image=True) if text else None
            sender = {
                "document":   context.bot.send_document,
                "video":      context.bot.send_video,
                "animation":  context.bot.send_animation,
                "video_note": context.bot.send_video_note,
            }.get(media_kind, context.bot.send_document)
            kwargs = {
                "chat_id": channel,
                "reply_markup": build_final_markup(cfg),
                "disable_notification": silent,
            }
            if media_kind != "video_note":
                kwargs["caption"] = caption
                kwargs["parse_mode"] = ParseMode.HTML if caption else None
            key = {"document": "document", "video": "video",
                   "animation": "animation", "video_note": "video_note"}.get(
                       media_kind, "document")
            kwargs[key] = media_fid
            await _send_with_retry(lambda: sender(**kwargs))

        else:
            if not body_html.strip():
                return "error", "khali post"
            await _send_with_retry(lambda: context.bot.send_message(
                chat_id=channel,
                text=wrap_plain_post(body_html, cfg, has_image=False),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=build_final_markup(cfg),
                disable_notification=silent,
            ))

        if dup_key:
            mark_posted(dup_key)
        return "posted", "non-Amazon post"
    except Exception as e:
        logger.error(f"post_other fail: {e}")
        return "error", str(e)[:120]


# =============================================================================
# QUEUE FLUSH — hourly job aur manual, dono yahi use karte hain
# =============================================================================
def _interleave(amz_sorted: list, others: list) -> list:
    """Amazon deals discount order mein, non-Amazon beech mein bikhre hue."""
    if not others:
        return amz_sorted
    if not amz_sorted:
        return others
    out  = []
    step = max(1, len(amz_sorted) // (len(others) + 1))
    oi   = 0
    for i, item in enumerate(amz_sorted):
        out.append(item)
        if oi < len(others) and (i + 1) % step == 0:
            out.append(others[oi])
            oi += 1
    out.extend(others[oi:])
    return out


async def flush_queue(app, reason: str = "hourly"):
    """Queue khali karo — fresh price laakar, sort karke, ek-ek post karke."""
    if _flush_lock.locked():
        logger.info(f"Flush ({reason}) skip — pichhli batch chal rahi hai")
        return

    async with _flush_lock:
        cfg = load_config()
        channel = cfg.get("channel", "").strip()
        if not channel:
            logger.warning("Flush skip — channel set nahi hai")
            return

        purged = queue_purge_old(QUEUE_MAX_AGE_HOURS)
        items  = queue_fetch_all()
        if not items:
            if reason == "hourly":
                logger.info("Hourly flush — queue khali")
            return

        class _Ctx:
            bot = app.bot
        context = _Ctx()

        logger.info(f"Flush ({reason}): {len(items)} items, {purged} purane drop")

        amz_items = [i for i in items if i["kind"] == "amazon" and i.get("asin")]
        oth_items = [i for i in items if i["kind"] == "other"]

        # ── Fresh data — 10-10 ke batch mein ──────────────────────────────
        dead = 0
        fresh = {}
        if amz_items:
            fresh = await get_products_by_asins([i["asin"] for i in amz_items])

        ready = []
        for it in amz_items:
            p = fresh.get(it["asin"])
            if not p or not p.get("title"):
                queue_delete(it["id"])
                dead += 1
                continue
            if p.get("deal_ends") == "khatam":
                queue_delete(it["id"])
                dead += 1
                continue
            it["product"] = p
            ready.append(it)

        # Discount ke hisaab se — sabse acchi pehle
        ready.sort(key=lambda x: int(x["product"].get("discount_pct") or 0), reverse=True)

        ordered = _interleave(ready, oth_items)

        posted = dupes = errors = 0
        titles = []

        for idx, it in enumerate(ordered):
            try:
                if it["kind"] == "amazon":
                    status, detail, _ = await post_amazon_product(
                        context, it["product"], cfg)
                else:
                    status, detail = await post_other(context, it["payload"], cfg)
            except Exception as e:
                logger.error(f"Flush item fail: {e}")
                status, detail = "error", str(e)[:80]

            if status == "posted":
                posted += 1
                queue_delete(it["id"])
                if it["kind"] == "amazon" and len(titles) < 8:
                    titles.append(detail)
            elif status == "duplicate":
                dupes += 1
                queue_delete(it["id"])
            else:
                errors += 1
                if queue_bump_tries(it["id"]) >= MAX_POST_TRIES:
                    queue_delete(it["id"])
                    logger.warning(f"Queue item {it['id']} {MAX_POST_TRIES} baar fail — drop")

            if idx < len(ordered) - 1:
                await asyncio.sleep(POST_GAP_SECONDS)

        # ── Summary ───────────────────────────────────────────────────────
        lines = [f"📤 <b>Batch bhej di</b> ({reason})\n"]
        lines.append(f"✅ Post  : <b>{posted}</b>")
        for t in titles:
            lines.append(f"   • {html_lib.escape(t[:50])}")
        if dupes:
            lines.append(f"🔁 Duplicate skip : {dupes}")
        if dead:
            lines.append(f"💀 Deal khatam / data nahi : {dead}")
        if errors:
            lines.append(f"❌ Fail (wapas queue mein) : {errors}")
        if purged:
            lines.append(f"🗑️ {QUEUE_MAX_AGE_HOURS}h se purane drop : {purged}")
        lines.append(f"\n📢 <code>{html_lib.escape(channel)}</code>")

        if ADMIN_ID:
            try:
                await app.bot.send_message(
                    chat_id=ADMIN_ID, text="\n".join(lines),
                    parse_mode=ParseMode.HTML, disable_web_page_preview=True)
            except Exception as e:
                logger.error(f"Summary DM fail: {e}")


async def hourly_job(context: ContextTypes.DEFAULT_TYPE):
    cfg = load_config()
    if not cfg.get("park_post"):
        return
    await flush_queue(context.application, reason="hourly")


# =============================================================================
# AMAZON LINK CLASSIFICATION
# =============================================================================
async def classify_amazon_urls(urls: list) -> dict:
    """ASIN check PEHLE — search se nikla product link product hi hai."""
    products, searches, unknown = [], [], []
    seen = set()
    for url in urls:
        try:
            resolved = await resolve_amazon_url(url)
        except Exception as e:
            logger.error(f"Resolve fail ({url[:50]}): {e}")
            resolved = url
        asin = extract_asin(resolved) or extract_asin(url)
        if asin:
            if asin in seen:
                continue
            seen.add(asin)
            products.append({"url": url, "resolved": resolved, "asin": asin})
        elif is_amazon_search_url(resolved) or is_amazon_search_url(url):
            searches.append(url)
        else:
            unknown.append(url)
    return {"products": products, "searches": searches, "unknown": unknown}


# =============================================================================
# CORE PROCESSOR
# =============================================================================
def _msg_payload(msg, text: str, entities) -> dict:
    """Non-Amazon post ko queue mein rakhne layak banao."""
    p = {"text": text, "entities": ents_to_json(entities)}
    if msg.photo:
        p["photo_file_id"] = msg.photo[-1].file_id
    elif msg.document:
        p["media_file_id"] = msg.document.file_id
        p["media_kind"]    = "document"
    elif msg.video:
        p["media_file_id"] = msg.video.file_id
        p["media_kind"]    = "video"
    elif msg.animation:
        p["media_file_id"] = msg.animation.file_id
        p["media_kind"]    = "animation"
    elif msg.video_note:
        p["media_file_id"] = msg.video_note.file_id
        p["media_kind"]    = "video_note"
    return p


async def process_and_post(context, msg, notify, cfg=None,
                           source_tag: str = "", allow_park: bool = False):
    if msg.caption is not None:
        raw_plain    = msg.caption or ""
        raw_entities = list(msg.caption_entities or [])
        has_photo    = True
    elif msg.text:
        raw_plain    = msg.text or ""
        raw_entities = list(msg.entities or [])
        has_photo    = False
    else:
        raw_plain, raw_entities = "", []
        has_photo = bool(msg.photo)

    all_urls    = extract_urls(raw_plain)
    amazon_urls = get_amazon_urls(all_urls)

    if not raw_plain.strip() and not all_urls and not has_photo and not (
            msg.document or msg.video or msg.animation or msg.video_note):
        await notify("⚠️ Message mein koi text ya link nahi mila.")
        return

    if cfg is None:
        cfg = load_config()
    channel = cfg.get("channel", "").strip()
    if not channel:
        await notify("⚠️ <b>Channel set nahi hua!</b>\n/setchannel karo pehle.",
                     parse_mode=ParseMode.HTML)
        return

    parking = allow_park and cfg.get("park_post", False)

    try:
        cleanup_old_entries()
    except Exception:
        pass

    # ==========================================================================
    # AMAZON
    # ==========================================================================
    if amazon_urls:
        wait_msg = await notify("⏳ Amazon links check ho rahe hain...")
        buckets  = await classify_amazon_urls(amazon_urls)
        products = buckets["products"]
        searches = buckets["searches"]
        unknown  = buckets["unknown"]

        if not products and not unknown:
            await _edit_or_notify(
                wait_msg, notify,
                f"🚫 <b>Skip!</b> Sirf search/deals page mile ({len(searches)}) — "
                f"kuch post nahi kiya.", parse_mode=ParseMode.HTML)
            return

        if products:
            if len(products) > MAX_PER_MESSAGE:
                await notify(f"⚠️ {len(products)} products — pehle "
                             f"{MAX_PER_MESSAGE} liye.")
                products = products[:MAX_PER_MESSAGE]

            # ── PARK MODE ─────────────────────────────────────────────────
            if parking:
                added = sum(1 for p in products if queue_add_amazon(p["asin"]))
                skip  = len(products) - added
                amz_n, oth_n = queue_counts()
                nxt = (datetime.now() + timedelta(hours=1)).replace(minute=0)
                lines = [f"🅿️ <b>{added} deal queue mein daal di.</b>"]
                if skip:
                    lines.append(f"🔁 {skip} pehle se queue mein thi.")
                if searches:
                    lines.append(f"🚫 {len(searches)} search page ignore.")
                lines.append(f"\n📋 Queue: {amz_n} Amazon + {oth_n} other")
                lines.append(f"🕐 Agli batch: <b>{nxt.strftime('%H:00')}</b>")
                await _edit_or_notify(wait_msg, notify, "\n".join(lines),
                                      parse_mode=ParseMode.HTML)
                return

            # ── INSTANT ───────────────────────────────────────────────────
            await _delete_quiet(wait_msg)
            fetched = await get_products_by_asins([p["asin"] for p in products])

            posted, dupes, nodata, errors = [], [], [], []
            note, all_skipped = "", set()

            live = [fetched[p["asin"]] for p in products if p["asin"] in fetched]
            live.sort(key=lambda x: int(x.get("discount_pct") or 0), reverse=True)
            nodata = [p["asin"] for p in products if p["asin"] not in fetched]

            for i, prod in enumerate(live):
                status, detail, n = await post_amazon_product(context, prod, cfg)
                if status == "posted":
                    posted.append(detail)
                    note = note or n
                    _, sk = build_amazon_caption(
                        prod, prod.get("affiliate_link", ""), cfg,
                        has_image=bool(n))
                    all_skipped.update(sk)
                elif status == "duplicate":
                    dupes.append(detail)
                else:
                    errors.append(detail)
                if i < len(live) - 1:
                    await asyncio.sleep(POST_GAP_SECONDS)

            # Single product + data nahi mila → purana fallback
            if len(products) == 1 and not posted and nodata:
                asin  = products[0]["asin"]
                short = make_affiliate_url(asin)
                cp, ce = remove_footer(raw_plain, raw_entities)
                cp, ce = replace_url_keep_entities(cp, ce, products[0]["url"], short)
                body   = entities_to_html(cp, ce)
                try:
                    await _send_with_retry(lambda: context.bot.send_message(
                        chat_id=channel,
                        text=wrap_plain_post(body, cfg, has_image=False),
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                        reply_markup=build_final_markup(cfg, asin=asin),
                        disable_notification=cfg.get("silent", True),
                    ))
                    await notify(
                        "✅ <b>Post ho gaya!</b>\n"
                        "⚠️ Amazon data nahi mila — original text affiliate "
                        f"link ke saath bheja.\n📢 <code>"
                        f"{html_lib.escape(channel)}</code>" + source_tag,
                        parse_mode=ParseMode.HTML, disable_web_page_preview=True)
                except Exception as e:
                    await notify(f"❌ <b>Post fail!</b>\n<code>"
                                 f"{html_lib.escape(str(e))}</code>",
                                 parse_mode=ParseMode.HTML)
                return

            bell = "🔕 Silent" if cfg.get("silent", True) else "🔔 Loud"
            lines = []
            if len(posted) == 1 and not dupes and not nodata and not errors:
                lines.append("✅ <b>Amazon Deal Post Ho Gaya!</b>")
                lines.append(f"🖼️ Image: {note}" if note
                             else "🖼️ Image nahi — text post kiya.")
            else:
                lines.append(f"✅ <b>{len(posted)} deal post ho gayi!</b>"
                             if posted else "⚠️ <b>Koi deal post nahi hui.</b>")
                for t in posted[:8]:
                    lines.append(f"   • {html_lib.escape(t[:50])}")
                if note:
                    lines.append(f"🖼️ Image: {note}")
            if dupes:
                lines.append(f"\n🔁 {len(dupes)} duplicate skip")
            if nodata:
                lines.append(f"⚠️ {len(nodata)} ka data nahi mila")
            if errors:
                lines.append(f"❌ {len(errors)} fail")
            if searches:
                lines.append(f"🚫 {len(searches)} search page ignore")
            if all_skipped:
                names = ", ".join(FIELD_LABELS.get(k, k) for k in all_skipped)
                lines.append(f"\n✂️ Caption bhar gayi, ye fields chhoot gaye: "
                             f"<b>{html_lib.escape(names)}</b>\n"
                             f"<i>/amz_post se kuch band kar do.</i>")
            lines.append(f"\n🔔 {bell}")
            lines.append(f"📢 <code>{html_lib.escape(channel)}</code>")
            if source_tag:
                lines.append(source_tag.strip())
            await notify("\n".join(lines), parse_mode=ParseMode.HTML,
                         disable_web_page_preview=True)
            return

        # ── Sirf unknown Amazon links ─────────────────────────────────────
        await _delete_quiet(wait_msg)
        cp, ce = remove_footer(raw_plain, raw_entities)
        cp, ce = await replace_amazon_links(cp, ce, unknown)
        body   = entities_to_html(cp, ce)
        if parking:
            queue_add_other({"text": cp, "entities": ents_to_json(ce)})
            await notify("🅿️ Queue mein daal diya (Amazon product nahi pehchana).",
                         parse_mode=ParseMode.HTML)
            return
        try:
            await _send_with_retry(lambda: context.bot.send_message(
                chat_id=channel,
                text=wrap_plain_post(body, cfg, has_image=False),
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=build_final_markup(cfg),
                disable_notification=cfg.get("silent", True),
            ))
            note = f"\n🚫 {len(searches)} search page ignore." if searches else ""
            await notify("✅ <b>Post ho gaya!</b>\n⚠️ Amazon product link "
                         "pehchana nahi — text post kiya." + note +
                         f"\n📢 <code>{html_lib.escape(channel)}</code>" + source_tag,
                         parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e:
            await notify(f"❌ <b>Post fail!</b>\n<code>"
                         f"{html_lib.escape(str(e))}</code>",
                         parse_mode=ParseMode.HTML)
        return

    # ==========================================================================
    # NON-AMAZON
    # ==========================================================================
    cp, ce  = remove_footer(raw_plain, raw_entities)
    payload = _msg_payload(msg, cp, ce)

    if parking:
        queue_add_other(payload)
        amz_n, oth_n = queue_counts()
        nxt = (datetime.now() + timedelta(hours=1)).replace(minute=0)
        await notify(
            f"🅿️ <b>Queue mein daal diya.</b>\n\n"
            f"📋 Queue: {amz_n} Amazon + {oth_n} other\n"
            f"🕐 Agli batch: <b>{nxt.strftime('%H:00')}</b>",
            parse_mode=ParseMode.HTML)
        return

    status, detail = await post_other(context, payload, cfg)
    bell = "🔕 Silent" if cfg.get("silent", True) else "🔔 Loud"
    if status == "posted":
        await notify(f"✅ <b>Post ho gaya!</b>\n🔔 {bell}\n"
                     f"📢 <code>{html_lib.escape(channel)}</code>" + source_tag,
                     parse_mode=ParseMode.HTML)
    elif status == "duplicate":
        await notify(f"⚠️ <b>Duplicate!</b> {html_lib.escape(detail)} — skip kiya.",
                     parse_mode=ParseMode.HTML)
    else:
        await notify(f"❌ <b>Post fail!</b>\n<code>{html_lib.escape(detail)}</code>",
                     parse_mode=ParseMode.HTML)


# =============================================================================
# ENTRY POINTS
# =============================================================================
async def handle_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin DM — hamesha turant post, park mode ignore."""
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

    await process_and_post(context, msg, notify, allow_park=False)


async def _offer_source_setup(context, chat):
    if chat.id in _offered_channels:
        return
    _offered_channels.add(chat.id)
    await dm_admin(
        context,
        f"📥 <b>Naye channel se post aayi</b>\n\n"
        f"Naam : <b>{html_lib.escape(chat.title or 'Channel')}</b>\n"
        f"ID   : <code>{chat.id}</code>\n\n"
        f"Isse draft channel banana hai?",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Haan", callback_data=f"srcset_{chat.id}"),
            InlineKeyboardButton("❌ Nahi", callback_data="cancel"),
        ]]))


async def handle_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.channel_post
    if not msg:
        return
    if _is_own_message(msg, context.bot.id):
        return

    cfg    = load_config()
    source = (cfg.get("source_channel") or "").strip()
    target = (cfg.get("channel") or "").strip()

    if target and chat_matches(msg.chat, target):
        return
    if not source:
        await _offer_source_setup(context, msg.chat)
        return
    if not chat_matches(msg.chat, source):
        return
    if same_channel(source, target):
        await dm_admin(context,
                       "⚠️ <b>Draft aur post channel same hai!</b> Post skip ki.",
                       parse_mode=ParseMode.HTML)
        return

    src_name   = msg.chat.title or source
    source_tag = f"\n📥 Source: <b>{html_lib.escape(src_name)}</b>"

    async def notify(text, **kwargs):
        try:
            sent = await msg.reply_text(text + SELF_MARKER,
                                        disable_notification=True, **kwargs)
            return _remember_own(sent)
        except Exception as e:
            logger.error(f"Draft reply fail: {e} — DM pe bhej raha hoon")
            return await dm_admin(context, text, **kwargs)

    logger.info(f"Draft post pakda: {msg.chat.id} / {msg.message_id}")
    await process_and_post(context, msg, notify, cfg=cfg,
                           source_tag=source_tag, allow_park=True)


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

    async def show(text, kb=None):
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb)
        except Exception:
            pass

    # ── Cancel ────────────────────────────────────────────────────────────
    if data == "cancel":
        if context.user_data is not None:
            context.user_data.clear()
        await show("❌ Band kar diya.")
        return

    # ── Draft channel quick-set ───────────────────────────────────────────
    if data.startswith("srcset_"):
        chat_id = data.split("_", 1)[1]
        cfg     = load_config()
        if same_channel(chat_id, cfg.get("channel", "")):
            await show("⚠️ Ye to post channel hi hai! Draft alag hona chahiye.")
            return
        cfg["source_channel"] = chat_id
        if not save_config(cfg):
            await show("❌ Save nahi hua, dobara try karo.")
            return
        await show(f"✅ <b>Draft channel set!</b>\n📥 <code>{chat_id}</code>\n\n"
                   f"Ab yahan post karo — status reply wahin milega.")
        return

    # ── Silent ────────────────────────────────────────────────────────────
    if data == "silent_toggle":
        cfg = load_config()
        cfg["silent"] = not cfg.get("silent", True)
        save_config(cfg)
        await show(_silent_status_text(cfg["silent"]), _silent_kb(cfg["silent"]))
        return

    # ── /amz_post ─────────────────────────────────────────────────────────
    if data == "amz_mode":
        cfg = load_config()
        cfg["amz_detailed"] = not cfg.get("amz_detailed", True)
        save_config(cfg)
        await show(_amz_status_text(cfg), _amz_kb(cfg))
        return

    if data.startswith("amzf_"):
        key = data.split("_", 1)[1]
        cfg = load_config()
        f   = cfg.setdefault("amz_fields", {})
        f[key] = not f.get(key, False)
        save_config(cfg)
        await show(_amz_status_text(cfg), _amz_kb(cfg))
        return

    # ── /park_post ────────────────────────────────────────────────────────
    if data == "park_toggle":
        cfg = load_config()
        cfg["park_post"] = not cfg.get("park_post", False)
        save_config(cfg)
        amz_n, oth_n = queue_counts()
        await show(_park_status_text(cfg, amz_n, oth_n),
                   _park_kb(cfg, amz_n + oth_n))
        if not cfg["park_post"] and (amz_n + oth_n):
            context.application.create_task(
                flush_queue(context.application, reason="park off"))
        return

    if data == "park_flush":
        amz_n, oth_n = queue_counts()
        total = amz_n + oth_n
        if not total:
            await show("📋 Queue khali hai.")
            return
        mins = int((total * POST_GAP_SECONDS) // 60) + 1
        await show(f"📤 <b>{total} post bhej raha hoon</b> — "
                   f"~{mins} minute lagega.\n\n"
                   f"<i>Telegram ki limit ke hisaab se gap rakhna padta hai. "
                   f"Ho jaane pe summary bhej dunga.</i>")
        context.application.create_task(
            flush_queue(context.application, reason="manual"))
        return

    if data == "park_clear":
        n = queue_clear()
        cfg = load_config()
        await show(f"🗑️ <b>{n} post queue se hata diye.</b>",
                   _park_kb(cfg, 0))
        return

    # ── Header / Footer ───────────────────────────────────────────────────
    if data.startswith("hf_"):
        _, kind, act = data.split("_", 2)
        cfg = load_config()
        d   = cfg.setdefault(kind, {})
        if act == "toggle":
            d["enabled"] = not d.get("enabled", False)
            save_config(cfg)
            await show(_hf_status_text(kind, d), _hf_kb(kind, d))
        elif act == "text":
            context.user_data["action"] = f"hf_wait_{kind}"
            await show(f"✏️ <b>{kind.title()} ka naya text</b> type karo "
                       f"(max 120 character).\n\n"
                       f"<i>Hatana hai to <code>-</code> bhej do.</i>")
        return

    # ── Watermark ─────────────────────────────────────────────────────────
    if data == "wm_toggle":
        cfg = load_config()
        wm  = cfg.setdefault("watermark", {"enabled": True, "text": "@DealKoti"})
        wm["enabled"] = not wm.get("enabled", True)
        save_config(cfg)
        await show(_watermark_status_text(wm), _watermark_kb(wm))
        return

    if data == "wm_set_text":
        context.user_data["action"] = "wm_wait_text"
        await show("✏️ Naya watermark text type karo (max 30 character):")
        return

    if data == "wm_confirm_text":
        new_text = context.user_data.pop("wm_pending_text", None)
        context.user_data.pop("action", None)
        if new_text:
            cfg = load_config()
            cfg.setdefault("watermark", {})["text"] = new_text
            save_config(cfg)
        await show(f"✅ Watermark text set: <code>"
                   f"{html_lib.escape(new_text or '@DealKoti')}</code>")
        return

    # ── /setbutton ────────────────────────────────────────────────────────
    if data == "sb_main":
        btns = load_config().get("buttons", {})
        await show("🎛️ <b>Button Settings</b>\n\n" + _setbutton_status_text(btns),
                   _setbutton_main_kb(btns))
        return

    if data == "sb_cart":
        cart = load_config().get("buttons", {}).get("cart", {})
        await show(_cart_detail_text(cart), _cart_detail_kb(cart))
        return

    if data == "sb_cart_toggle":
        cfg  = load_config()
        cart = cfg.setdefault("buttons", {}).setdefault("cart", {})
        cart["enabled"] = not cart.get("enabled", False)
        save_config(cfg)
        await show(_cart_detail_text(cart), _cart_detail_kb(cart))
        return

    if data == "sb_cart_rename":
        context.user_data["action"]  = "sb_wait_label"
        context.user_data["sb_key"]  = "cart"
        await show("📝 Add-to-Cart button ka naya naam type karo (max 20):")
        return

    if data in ("sb_btn1", "sb_btn2"):
        key = data.split("_")[1]
        btn = load_config().get("buttons", {}).get(key, {})
        await show(_btn_detail_text(key, btn), _setbutton_detail_kb(key, btn))
        return

    if data in ("sb_btn1_toggle", "sb_btn2_toggle"):
        key  = data.split("_")[1]
        cfg  = load_config()
        btn  = cfg.setdefault("buttons", {}).setdefault(key, {})
        btn["enabled"] = not btn.get("enabled", False)
        save_config(cfg)
        await show(_btn_detail_text(key, btn), _setbutton_detail_kb(key, btn))
        return

    if data in ("sb_btn1_rename", "sb_btn2_rename"):
        key = data.split("_")[1]
        context.user_data["action"] = "sb_wait_label"
        context.user_data["sb_key"] = key
        await show(f"📝 Button {key[-1]} ka naya naam type karo (max 20):")
        return

    if data in ("sb_btn1_link", "sb_btn2_link"):
        key = data.split("_")[1]
        context.user_data["action"] = "sb_wait_link"
        context.user_data["sb_key"] = key
        await show(f"🔗 Button {key[-1]} ka link type karo "
                   f"(https:// ya t.me/ se shuru):")
        return

    if data == "sb_confirm":
        key   = context.user_data.pop("sb_key", "btn1")
        val   = context.user_data.pop("sb_pending", None)
        field = context.user_data.pop("sb_field", None)
        context.user_data.pop("action", None)
        cfg   = load_config()
        btn   = cfg.setdefault("buttons", {}).setdefault(key, {})
        if val and field:
            btn[field] = val
            save_config(cfg)
        if key == "cart":
            await show("✅ <b>Saved!</b>\n\n" + _cart_detail_text(btn),
                       _cart_detail_kb(btn))
        else:
            await show("✅ <b>Saved!</b>\n\n" + _btn_detail_text(key, btn),
                       _setbutton_detail_kb(key, btn))
        return

    if data == "hf_confirm":
        kind = context.user_data.pop("hf_kind", "header")
        val  = context.user_data.pop("hf_pending", None)
        context.user_data.pop("action", None)
        cfg  = load_config()
        d    = cfg.setdefault(kind, {})
        if val is not None:
            d["text"] = val
            if val:
                d["enabled"] = True
            save_config(cfg)
        await show(_hf_status_text(kind, d), _hf_kb(kind, d))
        return


# =============================================================================
# TEXT INPUT
# =============================================================================
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return
    action = context.user_data.get("action")
    text   = (update.message.text or "").strip()
    if not action:
        return

    reply = update.message.reply_text

    if action == "wait_channel_id":
        if not text:
            await reply("⚠️ Channel ID khali nahi ho sakta.")
            return
        cfg = load_config()
        if same_channel(text, cfg.get("source_channel", "")):
            await reply("⚠️ Ye draft channel hai! Post channel alag hona chahiye.")
            return
        cfg["channel"] = text
        ok = save_config(cfg)
        context.user_data.clear()
        await reply(f"{'✅' if ok else '❌'} <b>Post channel "
                    f"{'set ho gaya' if ok else 'save nahi hua'}!</b>\n"
                    f"📢 <code>{html_lib.escape(text)}</code>",
                    parse_mode=ParseMode.HTML)
        return

    if action == "wait_source_id":
        if not text:
            await reply("⚠️ Channel ID khali nahi ho sakta.")
            return
        cfg = load_config()
        if same_channel(text, cfg.get("channel", "")):
            await reply("⚠️ Ye post channel hai! Draft alag hona chahiye, "
                        "warna bot apni hi post uthata rahega.")
            return
        cfg["source_channel"] = text
        ok = save_config(cfg)
        context.user_data.clear()
        await reply(f"{'✅' if ok else '❌'} <b>Draft channel "
                    f"{'set ho gaya' if ok else 'save nahi hua'}!</b>\n"
                    f"📥 <code>{html_lib.escape(text)}</code>\n\n"
                    f"<i>Bot ko is channel mein admin banana zaroori hai.</i>",
                    parse_mode=ParseMode.HTML)
        return

    if action == "wm_wait_text":
        if len(text) > 30:
            await reply("⚠️ Max 30 character.")
            return
        context.user_data["wm_pending_text"] = text
        context.user_data["action"] = None
        await reply(f"📋 Preview: <code>{html_lib.escape(text)}</code>\n\nSave?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Save", callback_data="wm_confirm_text"),
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
                    ]]))
        return

    if action.startswith("hf_wait_"):
        kind = action.replace("hf_wait_", "")
        if len(text) > 120:
            await reply("⚠️ Max 120 character.")
            return
        val = "" if text == "-" else text
        context.user_data["hf_pending"] = val
        context.user_data["hf_kind"]    = kind
        context.user_data["action"]     = None
        prev = html_lib.escape(val) if val else "<i>(khali — band ho jayega)</i>"
        await reply(f"📋 <b>{kind.title()} preview:</b>\n\n{prev}\n\nSave?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Save", callback_data="hf_confirm"),
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
                    ]]))
        return

    if action == "sb_wait_label":
        if len(text) > 20:
            await reply("⚠️ Naam max 20 character.")
            return
        context.user_data["sb_pending"] = text
        context.user_data["sb_field"]   = "label"
        context.user_data["action"]     = None
        await reply(f"📋 Preview: <b>{html_lib.escape(text)}</b>\n\nSave?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Save", callback_data="sb_confirm"),
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
                    ]]))
        return

    if action == "sb_wait_link":
        if text.startswith("t.me/"):
            text = "https://" + text
        if not text.startswith(("https://", "http://")):
            await reply("⚠️ Valid link daalo (https:// ya t.me/ se shuru).")
            return
        context.user_data["sb_pending"] = text
        context.user_data["sb_field"]   = "url"
        context.user_data["action"]     = None
        await reply(f"📋 Preview: <code>{html_lib.escape(text)}</code>\n\nSave?",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Save", callback_data="sb_confirm"),
                        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
                    ]]))
        return


# =============================================================================
# MAIN
# =============================================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable set nahi hai!")
    if ADMIN_ID == 0:
        raise ValueError("ADMIN_ID environment variable set nahi hai ya invalid hai!")

    init_db()

    async def post_init(app):
        await app.bot.set_my_commands([
            ("start",        "ℹ️ Bot ki info"),
            ("help",         "📖 Saari commands"),
            ("status",       "📊 Poora status"),
            ("setchannel",   "📢 Post channel"),
            ("setsource",    "📥 Draft channel"),
            ("amz_post",     "🛍️ Amazon post ki details on/off"),
            ("park_post",    "🅿️ Hourly batch ya instant"),
            ("queue",        "📋 Queue dekho / bhejo"),
            ("silent",       "🔔 Notification silent ya loud"),
            ("header",       "🔝 Post ka header"),
            ("footer",       "🔚 Post ka footer"),
            ("watermark",    "🖼️ Watermark on/off + text"),
            ("setbutton",    "🎛️ Buttons + Add to Cart"),
            ("testamz",      "🧪 Amazon API test"),
            ("exportconfig", "💾 Config backup"),
        ])
        logger.info("Commands register ho gayi.")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .rate_limiter(AIORateLimiter(
            overall_max_rate=25, overall_time_period=1,
            group_max_rate=19, group_time_period=60,
            max_retries=3,
        ))
        .post_init(post_init)
        .build()
    )

    dm = filters.ChatType.PRIVATE
    for name, fn in [
        ("start", cmd_start), ("help", cmd_help), ("status", cmd_status),
        ("setchannel", cmd_setchannel), ("setsource", cmd_setsource),
        ("amz_post", cmd_amz_post), ("park_post", cmd_park_post),
        ("queue", cmd_queue), ("silent", cmd_silent),
        ("header", cmd_header), ("footer", cmd_footer),
        ("watermark", cmd_watermark), ("setbutton", cmd_setbutton),
        ("testamz", cmd_testamz), ("exportconfig", cmd_exportconfig),
    ]:
        app.add_handler(CommandHandler(name, fn, filters=dm))

    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(MessageHandler(
        filters.UpdateType.MESSAGE & filters.ChatType.PRIVATE & ~filters.COMMAND,
        handle_deal))

    app.add_handler(MessageHandler(
        filters.UpdateType.CHANNEL_POST, handle_channel_post))

    # Hourly — ghante ke top pe (1:00, 2:00, 3:00...)
    if app.job_queue:
        delay = _next_hour_delay()
        app.job_queue.run_repeating(hourly_job, interval=3600, first=delay,
                                    name="hourly_flush")
        logger.info(f"Hourly flush job laga diya — pehla {delay/60:.0f} minute mein")
    else:
        logger.error("JobQueue nahi mili! requirements.txt mein "
                     "python-telegram-bot[job-queue] chahiye.")

    logger.info("DealsKoti Bot start ho raha hai...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "channel_post", "callback_query"],
    )


if __name__ == "__main__":
    main()
