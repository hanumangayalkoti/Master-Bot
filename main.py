import os
import re
import io
import html as html_lib
import logging
import urllib.parse
import aiohttp
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from amazon_api import (
    is_amazon_url, is_amazon_search_url, enrich_amazon_url,
    get_short_affiliate_link, _resolve_redirect,
)
from database import is_duplicate, mark_posted, cleanup_old_entries
from storage import load_config, save_config, init_db

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

URL_REGEX = re.compile(r"(https?://[^\s\]\[<>\"']+)")

FOOTER_LINE_PATTERN = re.compile(
    r'^[-—\s]*(deal\s*from|buy\s*on|shop\s*on|source\s*:|via\s*:|'
    r'brought\s*by|available\s*on|check\s*on|grab\s*on|get\s*it\s*on|'
    r'amazon\s*deal|flipkart\s*deal|meesho\s*deal|deal\s*by|'
    r'posted\s*by|bot\s*by)\b.*$',
    re.IGNORECASE
)


# =============================================================================
# WATERMARK HELPERS
# =============================================================================
async def _download_image(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception as e:
        logger.error(f"Image download failed: {e}")
    return None


def _apply_watermark(image_bytes: bytes, text: str) -> bytes:
    """Apply text watermark at bottom-right corner. Returns original bytes if Pillow fails."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        font_size = max(14, img.width // 28)
        try:
            font = ImageFont.truetype(
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size
            )
        except Exception:
            try:
                font = ImageFont.truetype("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

        draw  = ImageDraw.Draw(img)
        bbox  = draw.textbbox((0, 0), text, font=font)
        tw    = bbox[2] - bbox[0]
        th    = bbox[3] - bbox[1]
        pad   = 8
        mar   = 10
        x     = img.width  - tw - pad * 2 - mar
        y     = img.height - th - pad * 2 - mar

        overlay      = Image.new("RGBA", img.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        overlay_draw.rectangle(
            [x, y, x + tw + pad * 2, y + th + pad * 2],
            fill=(0, 0, 0, 150),
        )
        img  = Image.alpha_composite(img, overlay)
        draw = ImageDraw.Draw(img)
        draw.text((x + pad, y + pad), text, font=font, fill=(255, 255, 255, 255))

        img_rgb = img.convert("RGB")
        buf = io.BytesIO()
        img_rgb.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as e:
        logger.error(f"Watermark apply failed: {e}")
        return image_bytes  # return original on failure


# =============================================================================
# HELPERS
# =============================================================================
def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID


def extract_urls(text: str) -> list:
    return URL_REGEX.findall(text) if text else []


def get_amazon_urls(urls: list) -> list:
    return [u for u in urls if is_amazon_url(u)]


async def replace_amazon_links(text: str, urls: list) -> str:
    result = text
    for url in urls:
        if is_amazon_url(url):
            short = await get_short_affiliate_link(url)
            result = result.replace(url, short)
    return result


def _truncate_caption(text: str, max_len: int = 1020) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


# =============================================================================
# AMAZON CAPTION BUILDER  (replaces AI caption — no OpenAI needed)
# =============================================================================
def build_amazon_caption(product: dict, short_link: str) -> str:
    title        = product.get("title", "")
    deal_price   = product.get("deal_price", "")
    actual_price = product.get("actual_price", "")
    discount     = product.get("discount_pct", 0)
    savings      = product.get("savings", "")
    rating       = product.get("rating", "")
    reviews      = product.get("review_count", "")

    lines = ["🙏Jai Shree Ram Dosto🙏\n"]

    if title:
        lines.append(f"<b>{html_lib.escape(title)}</b>\n")

    if deal_price:
        price_line = f"💰 <b>Price: {html_lib.escape(deal_price)}</b>"
        if actual_price and discount:
            price_line += f"  <s>{html_lib.escape(actual_price)}</s>  🔥 {discount}% OFF"
        elif actual_price:
            price_line += f"  <s>{html_lib.escape(actual_price)}</s>"
        lines.append(price_line)

    if savings:
        lines.append(f"💵 Bachao: <b>{html_lib.escape(savings)}</b>")

    if rating or reviews:
        star_line = ""
        if rating:
            star_line += f"⭐ {html_lib.escape(str(rating))}/5"
        if reviews:
            star_line += f"  ({html_lib.escape(str(reviews))} reviews)"
        if star_line:
            lines.append(star_line)

    lines.append(f'\n🔗 <b><a href="{html_lib.escape(short_link)}">{html_lib.escape(short_link)}</a></b>')
    return _truncate_caption("\n".join(lines), 1020)


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
# TEXT → HTML CONVERTER  (unchanged)
# =============================================================================
def _py_to_utf16_len(text: str) -> int:
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


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
        if e > len(text):
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
# ADMIN REPLY HELPERS
# =============================================================================
def _channel_html_link(channel: str) -> str:
    ch = channel.strip()
    if ch.startswith("@"):
        name = ch[1:]
        return f'<a href="https://t.me/{name}">{ch}</a>'
    return f"<code>{ch}</code>"


async def _send_admin_reply(
    msg,
    headline: str,
    sent_channels: list = None,
    errors: list        = None,
    extra_line: str     = "",
):
    lines = [headline]

    if extra_line:
        lines.append(extra_line)

    if sent_channels:
        lines.append("")
        lines.append("📢 <b>Post kiya:</b>")
        for ch in sent_channels:
            lines.append(f"  • {_channel_html_link(ch)}")
    elif sent_channels is not None:
        lines.append("")
        lines.append(
            "⚠️ Koi channel nahi mila!\n"
            "/addgroup se channel set karo."
        )

    if errors:
        lines.append("\n❌ <b>Errors:</b>")
        for err in errors:
            lines.append(f"  • {html_lib.escape(str(err))}")

    await msg.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
    )


# =============================================================================
# UI HELPERS
# =============================================================================
def _toggle_keyboard(groups):
    buttons = []
    for i, g in enumerate(groups):
        status = "✅ ON" if g.get("enabled", True) else "❌ OFF"
        buttons.append([InlineKeyboardButton(
            f"{status} — {g.get('name', 'Group')}",
            callback_data=f"toggle_{i}"
        )])
    buttons.append([InlineKeyboardButton("💾 Save & Done", callback_data="toggle_done")])
    return InlineKeyboardMarkup(buttons)


def _group_select_kb(prefix):
    config = load_config()
    buttons = []
    for i, g in enumerate(config.get("groups", [])):
        buttons.append([InlineKeyboardButton(
            g.get("name", f"Group {i+1}"),
            callback_data=f"{prefix}_{i}"
        )])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def _channel_select_kb(group_idx, prefix):
    config = load_config()
    groups = config.get("groups", [])
    if group_idx >= len(groups):
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    buttons = []
    for j, ch_obj in enumerate(groups[group_idx].get("channels", [])):
        ch = ch_obj.get("channel", f"Channel {j+1}")
        buttons.append([InlineKeyboardButton(ch, callback_data=f"{prefix}_{group_idx}_{j}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def _setbutton_status_text(buttons: dict) -> str:
    b1 = buttons.get("btn1", {})
    b2 = buttons.get("btn2", {})
    b1_status = "✅ ON" if b1.get("enabled") else "❌ OFF"
    b2_status = "✅ ON" if b2.get("enabled") else "❌ OFF"
    b1_label  = b1.get("label", "Button 1")
    b2_label  = b2.get("label", "Button 2")
    b1_url    = b1.get("url") or "—"
    b2_url    = b2.get("url") or "—"
    return (
        f"📌 <b>Button 1</b> — {b1_status}\n"
        f"   Naam: {html_lib.escape(b1_label)}\n"
        f"   Link: <code>{html_lib.escape(b1_url)}</code>\n\n"
        f"📌 <b>Button 2</b> — {b2_status}\n"
        f"   Naam: {html_lib.escape(b2_label)}\n"
        f"   Link: <code>{html_lib.escape(b2_url)}</code>"
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
        f"📝 Naam: <b>{html_lib.escape(label)}</b>\n"
        f"🔗 Link: <code>{html_lib.escape(url)}</code>\n"
        f"Status: {status}"
    )


def _watermark_status_text(wm: dict) -> str:
    enabled = wm.get("enabled", False)
    text    = wm.get("text", "") or "—"
    status  = "✅ ON" if enabled else "❌ OFF"
    return (
        f"🖼️ <b>Watermark Settings</b>\n\n"
        f"Status: <b>{status}</b>\n"
        f"Text: <b>{html_lib.escape(text)}</b>\n\n"
        f"<i>Position: Bottom Right | Style: White text on dark background</i>"
    )


def _watermark_kb(wm: dict) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Turn OFF" if wm.get("enabled") else "🟢 Turn ON"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label,   callback_data="wm_toggle")],
        [InlineKeyboardButton("✏️ Set Text",  callback_data="wm_set_text")],
        [InlineKeyboardButton("❌ Cancel",    callback_data="cancel")],
    ])


# =============================================================================
# COMMANDS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 <b>DealsKoti Bot chalu hai!</b>\n\n"
        "Deal ka message bhejo — bot set channels mein post kar dega.\n\n"
        "/help daao sare commands dekhne ke liye.",
        parse_mode="HTML"
    )


async def cmd_testamz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔄 Amazon Creators API test ho rahi hai...")
    try:
        from amazon_api import get_product_by_asin, make_affiliate_url
        test_asin = "B08N5WRWNW"
        product   = await get_product_by_asin(test_asin)
        short     = make_affiliate_url(test_asin)
        if product and product.get("title"):
            await update.message.reply_text(
                f"✅ <b>Amazon Creators API kaam kar raha hai!</b>\n\n"
                f"🏷️ Title: <code>{product['title'][:80]}</code>\n"
                f"💰 Deal Price: <b>{product.get('deal_price', 'N/A')}</b>\n"
                f"📉 Discount: <b>{product.get('discount_pct', 0)}%</b>\n"
                f"⭐ Rating: <b>{product.get('rating', 'N/A')}</b>\n"
                f"👥 Reviews: <b>{product.get('review_count', 'N/A')}</b>\n"
                f"🖼️ Image: <b>{'Mili ✅' if product.get('image_url') else 'Nahi mili ❌'}</b>\n"
                f"🔗 Affiliate link: <code>{short}</code>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                "⚠️ Amazon API se product data nahi mila.\n"
                "CREDENTIAL_ID aur CREDENTIAL_SECRET check karo.",
                parse_mode="HTML"
            )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Amazon API error:\n<code>{html_lib.escape(str(e))}</code>",
            parse_mode="HTML"
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 <b>DealsKoti Bot — Sare Commands</b>\n\n"
        "📊 /status — Groups, channels aur settings dekho\n"
        "🔁 /manage — Groups ON/OFF karo\n"
        "✏️ /editgroup — Group ke inline buttons toggle karo\n"
        "✏️ /rename — Group naam badlo\n"
        "🎛️ /setbutton — Post ke neeche buttons set karo\n"
        "🖼️ /setwatermark — Image watermark settings\n"
        "➕ /addgroup — Naya group banao\n"
        "➕ /addchannel — Group mein channel add karo\n"
        "🗑️ /deletegroup — Group delete karo\n"
        "🗑️ /deletechannel — Channel hatao\n"
        "🧪 /testamz — Amazon API test karo\n"
        "💾 /exportconfig — Config backup karo\n"
        "ℹ️ /start — Bot ki info",
        parse_mode="HTML"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config  = load_config()
    groups  = config.get("groups", [])
    buttons = config.get("buttons", {})
    wm      = config.get("watermark", {})

    b1        = buttons.get("btn1", {})
    b2        = buttons.get("btn2", {})
    wm_status = "✅ ON" if wm.get("enabled") else "❌ OFF"
    wm_text   = wm.get("text", "") or "—"

    lines = [
        "⚙️ <b>Bot Status</b>\n",
        "🎛️ <b>Buttons:</b>",
        f"  Button 1: {'✅ ON' if b1.get('enabled') else '❌ OFF'} — <b>{html_lib.escape(b1.get('label', '-'))}</b>",
        f"  Button 2: {'✅ ON' if b2.get('enabled') else '❌ OFF'} — <b>{html_lib.escape(b2.get('label', '-'))}</b>",
        "",
        "🖼️ <b>Watermark:</b>",
        f"  Status: {wm_status}  |  Text: <b>{html_lib.escape(wm_text)}</b>",
        "",
    ]

    if not groups:
        lines.append("⚠️ Koi group nahi — /addgroup se banao")
    else:
        lines.append("📢 <b>Groups:</b>")
        for i, g in enumerate(groups, 1):
            st = "✅ ON" if g.get("enabled", True) else "❌ OFF"
            lines.append(f"\n<b>{i}. {html_lib.escape(g.get('name', 'Group'))}</b> — {st}")
            for ch_obj in g.get("channels", []):
                lines.append(f"   📢 {html_lib.escape(ch_obj.get('channel', ''))}")

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    groups = load_config().get("groups", [])
    if not groups:
        await update.message.reply_text("⚠️ Koi group nahi — /addgroup se banao")
        return
    await update.message.reply_text(
        "🔁 <b>Groups ON/OFF Karo</b>\nTap karo toggle karne ke liye:",
        reply_markup=_toggle_keyboard(groups), parse_mode="HTML"
    )


async def cmd_editgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi — /addgroup se banao")
        return
    await update.message.reply_text(
        "✏️ <b>Group Edit Karo</b>\nKaun sa group?",
        reply_markup=_group_select_kb("eg_group"), parse_mode="HTML"
    )


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi.")
        return
    await update.message.reply_text(
        "✏️ <b>Rename Karo</b>\nKaun sa group?",
        reply_markup=_group_select_kb("ren_group"), parse_mode="HTML"
    )


async def cmd_setbutton(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
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


async def cmd_setwatermark(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config = load_config()
    wm     = config.get("watermark", {})
    await update.message.reply_text(
        _watermark_status_text(wm),
        parse_mode="HTML",
        reply_markup=_watermark_kb(wm)
    )


async def cmd_addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    context.user_data["action"] = "wait_group_name"
    await update.message.reply_text(
        "➕ <b>Naya Group Banao</b>\n\nGroup ka naam type karo:",
        parse_mode="HTML"
    )


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Pehle /addgroup se ek group banao.")
        return
    await update.message.reply_text(
        "➕ <b>Channel Add Karo</b>\nKaun se group mein?",
        reply_markup=_group_select_kb("ac_group"), parse_mode="HTML"
    )


async def cmd_deletegroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi.")
        return
    await update.message.reply_text(
        "🗑️ <b>Group Delete Karo</b>\nKaun sa group?",
        reply_markup=_group_select_kb("del_group"), parse_mode="HTML"
    )


async def cmd_deletechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi.")
        return
    await update.message.reply_text(
        "🗑️ <b>Channel Delete Karo</b>\nKaun se group se?",
        reply_markup=_group_select_kb("dc_group"), parse_mode="HTML"
    )


async def cmd_exportconfig(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    import json
    config      = load_config()
    config_json = json.dumps(config, indent=2, ensure_ascii=False)
    await update.message.reply_text(
        f"📦 <b>Config Export (Backup)</b>\n\n"
        f"Is JSON ko copy karke safe jagah save karo.\n\n"
        f"<pre>{html_lib.escape(config_json)}</pre>",
        parse_mode="HTML"
    )


# =============================================================================
# CHANNEL POSTER  (no category filter — posts to all enabled channels)
# =============================================================================
async def _post_to_channels(context, config, send_fn, base_markup=None) -> tuple:
    sent_channels = []
    errors        = []
    for group in config.get("groups", []):
        if not group.get("enabled", True):
            continue
        show_buttons = group.get("show_buttons", True)
        group_markup = base_markup if show_buttons else None
        for ch_obj in group.get("channels", []):
            channel = ch_obj.get("channel", "").strip()
            if not channel:
                continue
            try:
                await send_fn(channel, group_markup)
                sent_channels.append(channel)
            except Exception as e:
                errors.append(f"{channel}: {e}")
    return sent_channels, errors


# =============================================================================
# MAIN DEAL HANDLER
# =============================================================================
async def handle_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    action = context.user_data.get("action")
    if action:
        await handle_text_input(update, context)
        return

    msg = update.message

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
        has_photo    = bool(msg.photo or msg.document or msg.video)

    all_urls      = extract_urls(raw_plain)
    amazon_urls   = get_amazon_urls(all_urls)
    has_amazon    = len(amazon_urls) > 0
    single_amazon = len(amazon_urls) == 1

    if not raw_plain.strip() and not all_urls and not has_photo:
        await msg.reply_text("⚠️ Message mein koi text ya link nahi mila.")
        return

    if not raw_plain.strip() and not all_urls and has_photo:
        await msg.reply_text("⚠️ Photo ke saath product ka naam ya link bhi bhejo.")
        return

    config       = load_config()
    final_markup = build_final_markup(config)
    wm_config    = config.get("watermark", {})
    wm_enabled   = wm_config.get("enabled", False) and bool(wm_config.get("text", "").strip())
    wm_text      = wm_config.get("text", "").strip()

    try:
        cleanup_old_entries()
    except Exception:
        pass

    # ==========================================================================
    # CASE 1: Single Amazon product link — fetch original image + watermark
    # ==========================================================================
    if single_amazon and has_amazon:
        amazon_url = amazon_urls[0]

        resolved_url = amazon_url
        if "amzn.to" in amazon_url or "amzn.in" in amazon_url or "link.amazon.com" in amazon_url or "link.amazon.in" in amazon_url:
            resolved_url = await _resolve_redirect(amazon_url)

        if is_amazon_search_url(resolved_url):
            await msg.reply_text(
                "❌ <b>Yeh Amazon search page ka link hai — post nahi kiya.</b>\n\n"
                "Kisi specific product ka link bhejo 😊",
                parse_mode="HTML"
            )
            return

        wait_msg = await msg.reply_text("⏳ Amazon product data fetch ho raha hai...")

        product    = await enrich_amazon_url(amazon_url)
        short_link = await get_short_affiliate_link(amazon_url)

        await wait_msg.delete()

        if product and product.get("title"):
            title        = product["title"]
            dup, dup_time = is_duplicate(title)
            if dup:
                await msg.reply_text(
                    f"⚠️ <b>Yeh deal {dup_time} already post ho chuki hai — skip kiya.</b>\n\n"
                    f"🏷️ {html_lib.escape(title[:80])}",
                    parse_mode="HTML"
                )
                return
            caption_html = build_amazon_caption(product, short_link)
            image_url    = product.get("image_url", "")
            api_note     = ""
        else:
            title        = None
            image_url    = ""
            api_note     = "⚠️ Amazon API se data nahi mila — sirf affiliate link ke saath post kiya."
            cleaned_plain, cleaned_entities = remove_footer(raw_plain, raw_entities)
            body_html    = entities_to_html(cleaned_plain, cleaned_entities)
            escaped_url  = html_lib.escape(amazon_url)
            body_html    = body_html.replace(escaped_url, f'<a href="{short_link}">{short_link}</a>')
            caption_html = _truncate_caption("🙏Jai Shree Ram Dosto🙏\n\n" + body_html, 1020)

        # Download Amazon image once; apply watermark if enabled
        image_bytes = None
        if image_url:
            if wm_enabled:
                raw_bytes = await _download_image(image_url)
                if raw_bytes:
                    image_bytes = _apply_watermark(raw_bytes, wm_text)
            # image_bytes=None → will fall back to sending URL directly (no watermark)

        async def send_amazon(channel, markup):
            if image_bytes:
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=image_bytes,
                    caption=caption_html,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            elif image_url:
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=image_url,
                    caption=caption_html,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            else:
                await context.bot.send_message(
                    chat_id=channel,
                    text=caption_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=markup,
                )

        sent_channels, errors = await _post_to_channels(
            context, config, send_amazon, base_markup=final_markup
        )

        if sent_channels and title:
            mark_posted(title)

        await _send_admin_reply(
            msg,
            headline      = "✅ <b>Amazon Deal Post Ho Gaya!</b>" if sent_channels else "⚠️ <b>Koi channel nahi mila!</b>",
            sent_channels = sent_channels,
            errors        = errors,
            extra_line    = api_note,
        )
        return

    # ==========================================================================
    # CASE 2: Multiple Amazon links  →  text only (no image)
    # CASE 2: Non-Amazon             →  forwarded image with optional watermark
    # ==========================================================================
    cleaned_plain, cleaned_entities = remove_footer(raw_plain, raw_entities)

    if has_amazon:
        updated_plain = await replace_amazon_links(cleaned_plain, amazon_urls)
    else:
        updated_plain = cleaned_plain

    # Duplicate check
    dup_key = raw_plain.strip()[:300]
    if dup_key:
        dup, dup_time = is_duplicate(dup_key)
        if dup:
            await msg.reply_text(
                f"⚠️ <b>Yeh message {dup_time} already post ho chuka hai — skip kiya.</b>",
                parse_mode="HTML"
            )
            return

    GREETING   = "🙏Jai Shree Ram Dosto🙏\n\n"
    body_html  = entities_to_html(updated_plain, cleaned_entities)
    final_html = GREETING + body_html

    # For NON-Amazon posts with photo: apply watermark once before loop
    # For MULTIPLE Amazon links with photo: skip image entirely
    photo_bytes_wm = None
    if not has_amazon and has_photo and msg.photo and wm_enabled:
        try:
            photo_file     = await context.bot.get_file(msg.photo[-1].file_id)
            raw_photo      = bytes(await photo_file.download_as_bytearray())
            photo_bytes_wm = _apply_watermark(raw_photo, wm_text)
        except Exception as e:
            logger.error(f"Photo watermark error: {e}")

    async def send_normal(channel, markup):
        if photo_bytes_wm:
            # Non-Amazon with watermark applied
            await context.bot.send_photo(
                chat_id=channel,
                photo=photo_bytes_wm,
                caption=final_html,
                parse_mode="HTML",
                reply_markup=markup,
            )
        elif has_amazon:
            # Multiple Amazon links → text only, no image
            await context.bot.send_message(
                chat_id=channel,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        elif msg.caption is not None:
            # Non-Amazon image post, watermark OFF → copy original
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=final_html,
                parse_mode="HTML",
                reply_markup=markup,
            )
        elif msg.text:
            # Plain text post
            await context.bot.send_message(
                chat_id=channel,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=markup,
            )
        else:
            # Other media (document, video, etc.)
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                reply_markup=markup,
            )

    sent_channels, errors = await _post_to_channels(
        context, config, send_normal, base_markup=final_markup
    )

    if sent_channels and dup_key:
        mark_posted(dup_key)

    await _send_admin_reply(
        msg,
        headline      = "✅ <b>Post Ho Gaya!</b>" if sent_channels else "⚠️ <b>Koi channel nahi mila!</b>",
        sent_channels = sent_channels,
        errors        = errors,
    )


# =============================================================================
# CALLBACK HANDLER
# =============================================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query.from_user or not is_admin(query.from_user.id):
        await query.answer("Unauthorized.")
        return
    await query.answer()
    data   = query.data
    config = load_config()
    groups = config.get("groups", [])

    if data == "cancel":
        context.user_data.clear()
        try:
            await query.edit_message_text("❌ Cancel ho gaya.")
        except Exception:
            pass
        return

    # --- Group toggle ---
    if data.startswith("toggle_") and data != "toggle_done":
        try:
            idx = int(data.split("_")[1])
            if idx < len(groups):
                groups[idx]["enabled"] = not groups[idx].get("enabled", True)
                config["groups"] = groups
                save_config(config)
            await query.edit_message_reply_markup(reply_markup=_toggle_keyboard(groups))
        except Exception as e:
            logger.error(f"Toggle error: {e}")
        return

    if data == "toggle_done":
        lines = [("✅ ON" if g.get("enabled") else "❌ OFF") + f" — {g.get('name','')}" for g in groups]
        try:
            await query.edit_message_text("✅ Saved!\n\n" + "\n".join(lines))
        except Exception:
            pass
        return

    # --- Edit group (show channels + show_buttons toggle) ---
    if data.startswith("eg_group_"):
        try:
            gi           = int(data.split("_")[2])
            group        = groups[gi]
            channels     = group.get("channels", [])
            show_buttons = group.get("show_buttons", True)
            btn_status   = "✅ ON" if show_buttons else "❌ OFF"
            ch_lines     = "\n".join(
                f"  • {html_lib.escape(ch_obj.get('channel', ''))}"
                for ch_obj in channels
            ) or "  (koi channel nahi)"
            await query.edit_message_text(
                f"✏️ <b>{html_lib.escape(group.get('name',''))}</b>\n\n"
                f"📢 Channels:\n{ch_lines}\n\n"
                f"🎛️ Inline Buttons: <b>{btn_status}</b>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"🎛️ Inline Buttons: {btn_status}", callback_data=f"gb_toggle_{gi}"
                    )],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
                ]),
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("gb_toggle_"):
        try:
            gi           = int(data.split("_")[2])
            current      = config["groups"][gi].get("show_buttons", True)
            config["groups"][gi]["show_buttons"] = not current
            save_config(config)
            show_buttons = not current
            btn_status   = "✅ ON" if show_buttons else "❌ OFF"
            group        = config["groups"][gi]
            channels     = group.get("channels", [])
            ch_lines     = "\n".join(
                f"  • {html_lib.escape(ch_obj.get('channel', ''))}"
                for ch_obj in channels
            ) or "  (koi channel nahi)"
            await query.edit_message_text(
                f"✅ <b>Saved!</b>\n\n"
                f"✏️ <b>{html_lib.escape(group.get('name',''))}</b>\n\n"
                f"📢 Channels:\n{ch_lines}\n\n"
                f"🎛️ Inline Buttons: <b>{btn_status}</b>",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        f"🎛️ Inline Buttons: {btn_status}", callback_data=f"gb_toggle_{gi}"
                    )],
                    [InlineKeyboardButton("❌ Cancel", callback_data="cancel")],
                ]),
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Rename group ---
    if data.startswith("ren_group_"):
        try:
            gi       = int(data.split("_")[2])
            context.user_data["rename_group_idx"] = gi
            context.user_data["action"] = "wait_rename"
            old_name = groups[gi].get("name", "")
            await query.edit_message_text(
                f"✏️ Group <b>'{html_lib.escape(old_name)}'</b> ka naya naam type karo:",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Add channel to group ---
    if data.startswith("ac_group_"):
        try:
            gi = int(data.split("_")[2])
            context.user_data["add_channel_group_idx"] = gi
            context.user_data["action"] = "wait_channel_name"
            await query.edit_message_text(
                f"➕ <b>{html_lib.escape(groups[gi].get('name',''))}</b> mein channel add karo\n\n"
                f"Channel ID type karo (jaise @mychannel ya -100123456):",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Delete group ---
    if data.startswith("del_group_"):
        try:
            gi      = int(data.split("_")[2])
            removed = config["groups"].pop(gi)
            save_config(config)
            await query.edit_message_text(
                f"🗑️ Group <b>'{html_lib.escape(removed.get('name',''))}'</b> delete ho gaya.",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Delete channel ---
    if data.startswith("dc_group_"):
        try:
            gi = int(data.split("_")[2])
            context.user_data["dc_group_idx"] = gi
            await query.edit_message_text(
                f"🗑️ <b>{html_lib.escape(groups[gi].get('name',''))}</b> — Kaun sa channel delete karo?",
                reply_markup=_channel_select_kb(gi, "dc_ch"),
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("dc_ch_"):
        try:
            parts   = data.split("_")
            gi, ci  = int(parts[2]), int(parts[3])
            removed = config["groups"][gi]["channels"].pop(ci)
            save_config(config)
            await query.edit_message_text(
                f"🗑️ Channel <b>{html_lib.escape(removed.get('channel',''))}</b> delete ho gaya.",
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    # --- Watermark callbacks ---
    if data == "wm_toggle":
        try:
            wm              = config.setdefault("watermark", {})
            wm["enabled"]   = not wm.get("enabled", False)
            config["watermark"] = wm
            save_config(config)
            await query.edit_message_text(
                _watermark_status_text(wm),
                parse_mode="HTML",
                reply_markup=_watermark_kb(wm)
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data == "wm_set_text":
        context.user_data["action"] = "wait_wm_text"
        try:
            await query.edit_message_text(
                "✏️ <b>Watermark Text Daalo</b>\n\n"
                "Channel naam ya koi bhi text type karo (max 30 characters):\n"
                "<i>Example: @YourChannel</i>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # --- /setbutton callbacks ---
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
                f"📝 <b>Button {btn_key[-1]}</b> ka naya naam type karo:\n(max 20 characters)",
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
                f"🔗 <b>Button {btn_key[-1]}</b> ka link type karo:\n(https:// ya t.me/ se shuru hona chahiye)",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    if data in ("sb_btn1_confirm", "sb_btn2_confirm"):
        btn_key  = data.split("_")[1]
        new_val  = context.user_data.pop(f"sb_{btn_key}_pending", None)
        field    = context.user_data.pop(f"sb_{btn_key}_field",   None)
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
        context.user_data.pop(f"sb_{btn_key}_field",   None)
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
# TEXT INPUT HANDLER  (multi-step flows)
# =============================================================================
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    action = context.user_data.get("action")
    text   = (update.message.text or "").strip()

    if not action:
        return

    # --- Watermark text ---
    if action == "wait_wm_text":
        if len(text) > 30:
            await update.message.reply_text("⚠️ Text max 30 characters hona chahiye.")
            return
        cfg             = load_config()
        wm              = cfg.setdefault("watermark", {})
        wm["text"]      = text
        cfg["watermark"] = wm
        save_config(cfg)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Watermark text save ho gaya: <b>{html_lib.escape(text)}</b>",
            parse_mode="HTML"
        )
        return

    # --- Button label ---
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

    # --- Button link ---
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

    # --- New group flow ---
    if action == "wait_group_name":
        context.user_data["new_group_name"] = text
        context.user_data["action"] = "wait_group_channel"
        await update.message.reply_text(
            f"➕ Group <b>'{html_lib.escape(text)}'</b> — Pehla channel ID type karo:\n"
            f"(jaise @mychannel ya -100123456789)",
            parse_mode="HTML"
        )
        return

    if action == "wait_group_channel":
        group_name = context.user_data.get("new_group_name", "New Group")
        new_group  = {
            "name": group_name, "enabled": True,
            "channels": [{"channel": text}],
        }
        cfg = load_config()
        cfg["groups"].append(new_group)
        save_config(cfg)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Group <b>{html_lib.escape(group_name)}</b> ban gaya!\n"
            f"📢 Channel: {html_lib.escape(text)}",
            parse_mode="HTML"
        )
        return

    # --- Add channel to existing group ---
    if action == "wait_channel_name":
        gi         = context.user_data.get("add_channel_group_idx", 0)
        cfg        = load_config()
        cfg["groups"][gi]["channels"].append({"channel": text})
        save_config(cfg)
        group_name = cfg["groups"][gi].get("name", "")
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Channel <b>{html_lib.escape(text)}</b> add ho gaya!\n"
            f"Group: {html_lib.escape(group_name)}",
            parse_mode="HTML"
        )
        return

    # --- Rename group ---
    if action == "wait_rename":
        gi       = context.user_data.get("rename_group_idx", 0)
        cfg      = load_config()
        old_name = cfg["groups"][gi].get("name", "")
        cfg["groups"][gi]["name"] = text
        save_config(cfg)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Group rename ho gaya!\n"
            f"<b>{html_lib.escape(old_name)}</b> → <b>{html_lib.escape(text)}</b>",
            parse_mode="HTML"
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

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("testamz",       cmd_testamz))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("manage",        cmd_manage))
    app.add_handler(CommandHandler("editgroup",     cmd_editgroup))
    app.add_handler(CommandHandler("rename",        cmd_rename))
    app.add_handler(CommandHandler("setbutton",     cmd_setbutton))
    app.add_handler(CommandHandler("setwatermark",  cmd_setwatermark))
    app.add_handler(CommandHandler("addgroup",      cmd_addgroup))
    app.add_handler(CommandHandler("addchannel",    cmd_addchannel))
    app.add_handler(CommandHandler("deletegroup",   cmd_deletegroup))
    app.add_handler(CommandHandler("deletechannel", cmd_deletechannel))
    app.add_handler(CommandHandler("exportconfig",  cmd_exportconfig))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        handle_deal
    ))

    logger.info("DealsKoti Bot start ho raha hai...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
