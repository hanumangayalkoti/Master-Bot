import os
import re
import html as html_lib
import logging
import urllib.parse
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
from caption import build_amazon_caption, _safe_truncate, _TAG_RE
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
# SETBUTTON UI HELPERS
# =============================================================================
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


# =============================================================================
# COMMANDS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 <b>DealsKoti Bot chalu hai!</b>\n\n"
        "Deal ka message bhejo — bot automatically channel mein post kar dega.\n\n"
        "/help daao sare commands dekhne ke liye.",
        parse_mode="HTML"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 <b>DealsKoti Bot — Sare Commands</b>\n\n"
        "📊 /status — Channel aur buttons ka status dekho\n"
        "📢 /setchannel — Post karne wala channel set karo\n"
        "🎛️ /setbutton — Post ke neeche buttons set karo\n"
        "🧪 /testamz — Amazon API test karo\n"
        "💾 /exportconfig — Config backup karo\n"
        "ℹ️ /start — Bot ki info",
        parse_mode="HTML"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config  = load_config()
    channel = config.get("channel", "") or "Set nahi hua (/setchannel se set karo)"
    buttons = config.get("buttons", {})

    b1 = buttons.get("btn1", {})
    b2 = buttons.get("btn2", {})

    lines = [
        "⚙️ <b>Bot Status</b>\n",
        f"📢 <b>Channel:</b> <code>{html_lib.escape(channel)}</code>\n",
        "🎛️ <b>Buttons:</b>",
        f"  Button 1: {'✅ ON' if b1.get('enabled') else '❌ OFF'} — <b>{html_lib.escape(b1.get('label', '-'))}</b>",
        f"  Button 2: {'✅ ON' if b2.get('enabled') else '❌ OFF'} — <b>{html_lib.escape(b2.get('label', '-'))}</b>",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_setchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    context.user_data["action"] = "wait_channel_id"
    config = load_config()
    current = config.get("channel", "") or "Set nahi hua"
    await update.message.reply_text(
        f"📢 <b>Channel Set Karo</b>\n\n"
        f"Current: <code>{html_lib.escape(current)}</code>\n\n"
        f"Naya channel ID type karo (jaise @mychannel ya -100123456789):",
        parse_mode="HTML"
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
    channel      = config.get("channel", "").strip()
    final_markup = build_final_markup(config)

    if not channel:
        await msg.reply_text(
            "⚠️ <b>Channel set nahi hua!</b>\n/setchannel se channel set karo.",
            parse_mode="HTML"
        )
        return

    try:
        cleanup_old_entries()
    except Exception:
        pass

    # ==========================================================================
    # CASE 1: Single Amazon product link — full enrichment
    # ==========================================================================
    if single_amazon:
        amazon_url = amazon_urls[0]

        resolved_url = amazon_url
        if "amzn.to" in amazon_url or "amzn.in" in amazon_url:
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

        if product and product.get("title"):
            title = product["title"]

            dup, dup_time = is_duplicate(title)
            if dup:
                await wait_msg.edit_text(
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
            raw_caption  = "🙏Jai Shree Ram Dosto🙏\n\n" + body_html
            caption_html = _safe_truncate(raw_caption, max_visible=1020)

        await wait_msg.delete()

        # Post to channel
        try:
            if has_photo:
                await context.bot.copy_message(
                    chat_id=channel,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=caption_html,
                    parse_mode="HTML",
                    reply_markup=final_markup,
                )
            elif image_url:
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=image_url,
                    caption=caption_html,
                    parse_mode="HTML",
                    reply_markup=final_markup,
                )
            else:
                await context.bot.send_message(
                    chat_id=channel,
                    text=caption_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=final_markup,
                )

            if title:
                mark_posted(title)

            reply_lines = ["✅ <b>Amazon Deal Post Ho Gaya!</b>"]
            if api_note:
                reply_lines.append(api_note)
            reply_lines.append(f"\n📢 Posted to: <code>{html_lib.escape(channel)}</code>")
            await msg.reply_text("\n".join(reply_lines), parse_mode="HTML", disable_web_page_preview=True)

        except Exception as e:
            await msg.reply_text(
                f"❌ <b>Post nahi ho saka!</b>\n<code>{html_lib.escape(str(e))}</code>",
                parse_mode="HTML"
            )
        return

    # ==========================================================================
    # CASE 2: Multiple links or non-Amazon — normal post with affiliate links
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

    # Post to channel
    try:
        if msg.caption is not None:
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=final_html,
                parse_mode="HTML",
                reply_markup=final_markup,
            )
        elif msg.text:
            await context.bot.send_message(
                chat_id=channel,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=final_markup,
            )
        else:
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                reply_markup=final_markup,
            )

        if dup_key:
            mark_posted(dup_key)

        await msg.reply_text(
            f"✅ <b>Post Ho Gaya!</b>\n📢 <code>{html_lib.escape(channel)}</code>",
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    except Exception as e:
        await msg.reply_text(
            f"❌ <b>Post nahi ho saka!</b>\n<code>{html_lib.escape(str(e))}</code>",
            parse_mode="HTML"
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
    data = query.data

    if data == "cancel":
        context.user_data.clear()
        try:
            await query.edit_message_text("❌ Cancel ho gaya.")
        except Exception:
            pass
        return

    # /setbutton callbacks
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
# TEXT INPUT HANDLER (multi-step flows)
# =============================================================================
async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
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
        cfg["channel"] = text
        save_config(cfg)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Channel set ho gaya!\n📢 <code>{html_lib.escape(text)}</code>",
            parse_mode="HTML"
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

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("setchannel",  cmd_setchannel))
    app.add_handler(CommandHandler("setbutton",   cmd_setbutton))
    app.add_handler(CommandHandler("testamz",     cmd_testamz))
    app.add_handler(CommandHandler("exportconfig", cmd_exportconfig))

    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        handle_deal
    ))

    logger.info("DealsKoti Bot start ho raha hai...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
