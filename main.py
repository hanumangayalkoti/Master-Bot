import os
import re
import html as html_lib
import json
import logging
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from classifier import detect_category
from amazon_api import (
    is_amazon_url, enrich_amazon_url,
    get_short_affiliate_link, extract_asin
)
from caption import build_amazon_caption

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ALL_CATS = ["fitness", "fashion", "electronics", "home", "skincare"]

URL_REGEX = re.compile(r"(https?://[^\s\]\[<>\"']+)")

FOOTER_LINE_PATTERN = re.compile(
    r'^[-—\s]*(deal\s*from|buy\s*on|shop\s*on|source\s*:|via\s*:|'
    r'brought\s*by|available\s*on|check\s*on|grab\s*on|get\s*it\s*on|'
    r'amazon\s*deal|flipkart\s*deal|meesho\s*deal|deal\s*by|'
    r'posted\s*by|bot\s*by)\b.*$',
    re.IGNORECASE
)


# =============================================================================
# CONFIG
# =============================================================================
def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"groups": [], "folder_link": ""}


def save_config(config):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def is_admin(uid):
    return ADMIN_ID != 0 and uid == ADMIN_ID


# =============================================================================
# URL HELPERS
# =============================================================================
def extract_urls(text: str) -> list:
    return URL_REGEX.findall(text)


def get_amazon_urls(urls: list) -> list:
    return [u for u in urls if is_amazon_url(u)]


def get_non_amazon_urls(urls: list) -> list:
    return [u for u in urls if not is_amazon_url(u)]


async def replace_amazon_links(text: str, urls: list) -> str:
    """
    Message mein sirf Amazon links ko short affiliate links se replace karo.
    Baki sab text same rahega.
    """
    result = text
    for url in urls:
        if is_amazon_url(url):
            short = await get_short_affiliate_link(url)
            result = result.replace(url, short)
    return result


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

    utf16_map = _build_utf16_map(text)
    open_tags  = [""] * len(text)
    close_tags = [""] * len(text)

    sorted_ents = sorted(entities, key=lambda e: (e.offset, -e.length))

    for ent in sorted_ents:
        s_utf16 = ent.offset
        e_utf16 = ent.offset + ent.length
        s = utf16_map[s_utf16] if s_utf16 < len(utf16_map) else s_utf16
        e = utf16_map[e_utf16] if e_utf16 < len(utf16_map) else e_utf16
        if e > len(text):
            continue
        etype = ent.type

        if etype == "url":
            open_tags[s]   = '<b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b>'
        elif etype == "text_link":
            url = html_lib.escape(ent.url or "")
            open_tags[s]   = f'<a href="{url}"><b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b></a>'
        elif etype == "bold":
            open_tags[s]   = '<b>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</b>'
        elif etype == "italic":
            open_tags[s]   = '<i>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</i>'
        elif etype == "underline":
            open_tags[s]   = '<u>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</u>'
        elif etype == "strikethrough":
            open_tags[s]   = '<s>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</s>'
        elif etype == "code":
            open_tags[s]   = '<code>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</code>'
        elif etype == "pre":
            open_tags[s]   = '<pre>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</pre>'
        elif etype == "spoiler":
            open_tags[s]   = '<tg-spoiler>' + open_tags[s]
            close_tags[e-1] = close_tags[e-1] + '</tg-spoiler>'

    result = []
    for i, ch in enumerate(text):
        result.append(open_tags[i])
        result.append(html_lib.escape(ch))
        result.append(close_tags[i])
    return ''.join(result)


# =============================================================================
# MARKUP BUILDER
# =============================================================================
def build_final_markup(original_markup, folder_link: str):
    buttons = []
    if original_markup and original_markup.inline_keyboard:
        buttons.extend(original_markup.inline_keyboard)
    return InlineKeyboardMarkup(buttons) if buttons else None


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


def _cat_toggle_kb(selected, done_cb):
    buttons = []
    for cat in ALL_CATS:
        tick = "✅" if cat in selected else "⬜"
        buttons.append([InlineKeyboardButton(
            f"{tick} {cat.capitalize()}",
            callback_data=f"cat_{cat}"
        )])
    buttons.append([InlineKeyboardButton("💾 Done", callback_data=done_cb)])
    return InlineKeyboardMarkup(buttons)


def _channel_select_kb(group_idx, prefix):
    config  = load_config()
    groups  = config.get("groups", [])
    if group_idx >= len(groups):
        return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel")]])
    buttons = []
    for j, ch_obj in enumerate(groups[group_idx].get("channels", [])):
        ch = ch_obj.get("channel", f"Channel {j+1}")
        buttons.append([InlineKeyboardButton(ch, callback_data=f"{prefix}_{group_idx}_{j}")])
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


# =============================================================================
# COMMANDS
# =============================================================================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "👋 *DealsKoti Bot chalu hai!*\n\n"
        "Deal ka message bhejo — AI category detect karke sahi channel mein post kar dega.\n\n"
        "/help daao sare commands dekhne ke liye.",
        parse_mode="Markdown"
    )


async def cmd_testai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔄 OpenAI API test ho rahi hai...")
    try:
        cat, method, err, kws = await detect_category("mamaearth face scrub for glowing skin")
        if method == "AI":
            await update.message.reply_text(
                f"✅ *AI kaam kar raha hai!*\n\nTest: `mamaearth face scrub`\n"
                f"Category: *{cat}*\nMethod: *AI* 🤖",
                parse_mode="Markdown"
            )
        else:
            kw_str = ", ".join(f"`{k}`" for k in kws) if kws else "koi match nahi"
            await update.message.reply_text(
                f"❌ *AI kaam nahi kar raha!*\n\nMethod: *Keyword fallback* 🔑\n"
                f"Category: *{cat or 'detect nahi hua'}*\nMatched keywords: {kw_str}\n\n"
                f"*AI Error:*\n`{err}`",
                parse_mode="Markdown"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ *Exception aaya:*\n`{e}`", parse_mode="Markdown")


async def cmd_testamz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Amazon API test command."""
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔄 Amazon API test ho rahi hai...")
    try:
        from amazon_api import get_product_by_asin, make_affiliate_url
        test_asin    = "B08N5WRWNW"
        product      = await get_product_by_asin(test_asin)
        short        = make_affiliate_url(test_asin)
        if product and product.get("title"):
            await update.message.reply_text(
                f"✅ *Amazon API kaam kar raha hai!*\n\n"
                f"🏷️ Title: `{product['title'][:80]}`\n"
                f"💰 Actual: `{product.get('actual_price', 'N/A')}`\n"
                f"🏷️ Deal: `{product.get('deal_price', 'N/A')}`\n"
                f"📉 Discount: `{product.get('discount_pct', 0)}%`\n"
                f"🖼️ Image: `{'Mili ✅' if product.get('image_url') else 'Nahi mili ❌'}`\n"
                f"🔗 Short link: `{short}`",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                "⚠️ Amazon API se product data nahi mila. "
                "AMAZON_CLIENT_ID aur AMAZON_CLIENT_SECRET check karo.",
                parse_mode="Markdown"
            )
    except Exception as e:
        await update.message.reply_text(f"❌ Amazon API error:\n`{e}`", parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "📖 *DealsKoti Bot — Sare Commands*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *STATUS*\n"
        "/status — Sare groups, channels aur categories dekho\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔁 *ON / OFF*\n"
        "/manage — Groups ko ON ya OFF karo (buttons se)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "✏️ *EDIT*\n"
        "/editgroup — Channel ki categories badlo\n"
        "/rename — Group ka naam badlo\n"
        "/setfolder — 'Get More Deals' button ka link badlo\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "➕ *ADD*\n"
        "/addgroup — Naya group banao\n"
        "/addchannel — Existing group mein naya channel add karo\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🗑️ *DELETE*\n"
        "/deletegroup — Poora group delete karo\n"
        "/deletechannel — Group ke andar se koi channel hatao\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🧪 *TEST*\n"
        "/testai — OpenAI API test karo\n"
        "/testamz — Amazon PA API test karo\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "ℹ️ *OTHER*\n"
        "/start — Bot ki info\n"
        "/help — Ye poori list",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    config  = load_config()
    groups  = config.get("groups", [])
    folder  = config.get("folder_link", "Set nahi hua")
    lines   = ["⚙️ *Bot Status*\n", f"🔗 Folder: `{folder}`\n"]
    if not groups:
        lines.append("⚠️ Koi group nahi — /addgroup se banao")
    else:
        for i, g in enumerate(groups, 1):
            st = "✅ ON" if g.get("enabled", True) else "❌ OFF"
            lines.append(f"*{i}. {g.get('name','Group')}* — {st}")
            for ch_obj in g.get("channels", []):
                cats = ", ".join(c.capitalize() for c in ch_obj.get("categories", []))
                lines.append(f"   📢 {ch_obj.get('channel','')}")
                lines.append(f"      Categories: {cats or '—'}")
            lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    groups = load_config().get("groups", [])
    if not groups:
        await update.message.reply_text("⚠️ Koi group nahi — /addgroup se banao")
        return
    await update.message.reply_text(
        "🔁 *Groups ON/OFF Karo*\nTap karo toggle karne ke liye:",
        reply_markup=_toggle_keyboard(groups), parse_mode="Markdown"
    )


async def cmd_editgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi — /addgroup se banao")
        return
    await update.message.reply_text(
        "✏️ *Group Edit Karo*\nKaun sa group?",
        reply_markup=_group_select_kb("eg_group"), parse_mode="Markdown"
    )


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi.")
        return
    await update.message.reply_text(
        "✏️ *Rename Karo*\nKaun sa group?",
        reply_markup=_group_select_kb("ren_group"), parse_mode="Markdown"
    )


async def cmd_setfolder(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    context.user_data["action"] = "wait_folder"
    cur = load_config().get("folder_link", "Set nahi hua")
    await update.message.reply_text(
        f"🔗 *Folder Link Badlo*\nCurrent: `{cur}`\n\nNaya link type karo:",
        parse_mode="Markdown"
    )


async def cmd_addgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    context.user_data.clear()
    context.user_data["action"] = "wait_group_name"
    await update.message.reply_text(
        "➕ *Naya Group Banao*\n\nGroup ka naam type karo:",
        parse_mode="Markdown"
    )


async def cmd_addchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Pehle /addgroup se ek group banao.")
        return
    await update.message.reply_text(
        "➕ *Channel Add Karo*\nKaun se group mein?",
        reply_markup=_group_select_kb("ac_group"), parse_mode="Markdown"
    )


async def cmd_deletegroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi.")
        return
    await update.message.reply_text(
        "🗑️ *Group Delete Karo*\nKaun sa group?",
        reply_markup=_group_select_kb("del_group"), parse_mode="Markdown"
    )


async def cmd_deletechannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not load_config().get("groups"):
        await update.message.reply_text("⚠️ Koi group nahi.")
        return
    await update.message.reply_text(
        "🗑️ *Channel Delete Karo*\nKaun se group se?",
        reply_markup=_group_select_kb("dc_group"), parse_mode="Markdown"
    )


# =============================================================================
# CHANNEL POSTER — common helper
# =============================================================================
async def _post_to_channels(
    context, config, category,
    send_fn          # async fn(channel) → None
) -> tuple:
    sent_channels = []
    errors        = []
    for group in config.get("groups", []):
        if not group.get("enabled", True):
            continue
        for ch_obj in group.get("channels", []):
            if category not in ch_obj.get("categories", []):
                continue
            channel = ch_obj.get("channel", "").strip()
            if not channel:
                continue
            try:
                await send_fn(channel)
                sent_channels.append(channel)
            except Exception as e:
                errors.append(f"• `{channel}`: {e}")
    return sent_channels, errors


# =============================================================================
# MAIN DEAL HANDLER
# =============================================================================
async def handle_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    action = context.user_data.get("action")
    if action:
        await handle_text_input(update, context)
        return

    msg = update.message

    # ── Raw text / caption nikalo ──
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

    # ── URLs nikalo ──
    all_urls      = extract_urls(raw_plain)
    amazon_urls   = get_amazon_urls(all_urls)
    has_amazon    = len(amazon_urls) > 0
    single_amazon = len(amazon_urls) == 1 and len(all_urls) == 1

    # ── Agar koi text hi nahi aur Amazon link bhi nahi ──
    if not raw_plain.strip() and not has_amazon:
        await msg.reply_text("⚠️ Message mein koi text ya link nahi mila.")
        return

    config      = load_config()
    final_markup = build_final_markup(msg.reply_markup, config.get("folder_link", ""))

    # ==========================================================================
    # CASE 1: Single Amazon link
    # ==========================================================================
    if single_amazon and has_amazon:
        await msg.reply_text("⏳ Amazon product data fetch ho raha hai...")

        amazon_url = amazon_urls[0]
        product    = await enrich_amazon_url(amazon_url)
        short_link = await get_short_affiliate_link(amazon_url)

        if product and product.get("title"):
            caption_html = build_amazon_caption(product, short_link, raw_plain)
            detect_text  = product.get("title", "") + " " + raw_plain
            logger.info(f"Amazon product mila: {product.get('title', '')[:60]}")
        else:
            logger.warning(f"Amazon API se product nahi mila: {amazon_url[:80]}")
            await msg.reply_text(
                f"⚠️ *Amazon API se data nahi mila*\n"
                f"Link: `{short_link}`\n"
                f"Abhi sirf affiliate link ke saath post ho raha hai.\n\n"
                f"`/testamz` se API check karo.",
                parse_mode="Markdown"
            )
            cleaned_plain, cleaned_entities = remove_footer(raw_plain, raw_entities)
            body_html     = entities_to_html(cleaned_plain, cleaned_entities)
            body_replaced = body_html.replace(html_lib.escape(amazon_url), short_link)
            caption_html  = "🙏Jai Shree Ram Dosto🙏\n\n" + body_replaced
            detect_text   = raw_plain

        # Category detect karo
        try:
            result = await detect_category(detect_text)
            category, method, ai_error, matched_kws = (result + [None, None])[:4] if len(result) < 4 else result
        except Exception as e:
            await msg.reply_text(f"❌ Category detection error:\n`{e}`", parse_mode="Markdown")
            return

        if not category:
            await msg.reply_text(
                "⚠️ Category detect nahi ho saki! Product naam hona chahiye message mein."
            )
            return

        image_url = product.get("image_url", "") if product else ""

        async def send_single_amazon(channel):
            if has_photo:
                await context.bot.copy_message(
                    chat_id=channel,
                    from_chat_id=msg.chat_id,
                    message_id=msg.message_id,
                    caption=caption_html,
                    parse_mode="HTML",
                    reply_markup=final_markup
                )
            elif image_url:
                await context.bot.send_photo(
                    chat_id=channel,
                    photo=image_url,
                    caption=caption_html,
                    parse_mode="HTML",
                    reply_markup=final_markup
                )
            else:
                # ── CHANGE: disable_web_page_preview=True ──
                await context.bot.send_message(
                    chat_id=channel,
                    text=caption_html,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=final_markup
                )

        sent_channels, errors = await _post_to_channels(context, config, category, send_single_amazon)
        await _send_admin_reply(msg, category, method, matched_kws, ai_error, sent_channels, errors)
        return

    # ==========================================================================
    # CASE 2: Multiple links ya Amazon + non-Amazon mix
    # ==========================================================================
    if has_amazon and not single_amazon:
        await msg.reply_text("⏳ Links replace ho rahi hain...")
        updated_plain = await replace_amazon_links(raw_plain, amazon_urls)
    else:
        updated_plain = raw_plain

    # Footer remove + HTML convert
    cleaned_plain, cleaned_entities = remove_footer(updated_plain, raw_entities)
    GREETING  = "🙏Jai Shree Ram Dosto🙏\n\n"
    body_html = entities_to_html(cleaned_plain, cleaned_entities)
    final_html = GREETING + body_html

    # Category detect
    await msg.reply_text("⏳ Category detect ho rahi hai...")
    try:
        result   = await detect_category(raw_plain)
        category, method, ai_error, matched_kws = (list(result) + [None, None])[:4]
    except Exception as e:
        await msg.reply_text(f"❌ Category detection mein error:\n`{e}`", parse_mode="Markdown")
        return

    if not category:
        err_detail = f"\n\n⚠️ AI Error: `{ai_error}`" if ai_error else ""
        await msg.reply_text(
            f"⚠️ Category detect nahi ho saki!\n"
            f"Message mein product naam/brand hona chahiye.{err_detail}",
            parse_mode="Markdown"
        )
        return

    # ==========================================================================
    # CASE 3: No Amazon link — existing behavior
    # ==========================================================================
    async def send_normal(channel):
        if msg.caption is not None:
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                caption=final_html,
                parse_mode="HTML",
                reply_markup=final_markup
            )
        elif msg.text:
            # ── CHANGE: disable_web_page_preview=True ──
            await context.bot.send_message(
                chat_id=channel,
                text=final_html,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=final_markup
            )
        else:
            await context.bot.copy_message(
                chat_id=channel,
                from_chat_id=msg.chat_id,
                message_id=msg.message_id,
                reply_markup=final_markup
            )

    sent_channels, errors = await _post_to_channels(context, config, category, send_normal)
    await _send_admin_reply(msg, category, method, matched_kws, ai_error, sent_channels, errors)


# =============================================================================
# ADMIN REPLY HELPER
# =============================================================================
async def _send_admin_reply(
    msg, category, method, matched_kws, ai_error, sent_channels, errors
):
    cat_emoji = {
        "electronics": "⚡", "fashion": "👗", "fitness": "💪",
        "skincare": "✨", "home": "🏠"
    }.get(category, "🏷️")

    method_emoji = "🤖" if method == "AI" else "🔑"
    kw_line = ""
    if method == "Keyword" and matched_kws:
        kw_line = f"\n🔍 *Matched:* {', '.join(matched_kws[:5])}"

    if sent_channels:
        ch_list = "\n".join(f"  • `{c}`" for c in sent_channels)
        reply = (
            f"✅ *Deal Posted!*\n\n"
            f"{cat_emoji} *Category:* {category.capitalize()}\n"
            f"{method_emoji} *Detected by:* {method}{kw_line}\n\n"
            f"📢 *Sent to:*\n{ch_list}"
        )
    else:
        reply = (
            f"⚠️ *Koi channel nahi mila!*\n\n"
            f"{cat_emoji} *Category:* {category.capitalize()}\n"
            f"{method_emoji} *Detected by:* {method}{kw_line}\n\n"
            f"Is category ke liye koi enabled channel nahi hai.\n"
            f"/editgroup se channels set karo."
        )

    if ai_error:
        reply += f"\n\n⚠️ *AI Error:*\n`{ai_error}`"
    if errors:
        reply += "\n\n❌ *Errors:*\n" + "\n".join(errors)

    await msg.reply_text(reply, parse_mode="Markdown")


# =============================================================================
# CALLBACK HANDLER
# =============================================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
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

    if data.startswith("eg_group_"):
        try:
            gi      = int(data.split("_")[2])
            group   = groups[gi]
            channels = group.get("channels", [])
            if not channels:
                await query.edit_message_text(f"⚠️ '{group.get('name','')}' mein koi channel nahi.")
                return
            context.user_data["edit_group_idx"] = gi
            buttons = []
            for j, ch_obj in enumerate(channels):
                cats = ", ".join(ch_obj.get("categories", []))
                buttons.append([InlineKeyboardButton(
                    f"{ch_obj.get('channel','')} [{cats}]", callback_data=f"eg_ch_{gi}_{j}"
                )])
            buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel")])
            await query.edit_message_text(
                f"✏️ *{group.get('name','')}* — Kaun sa channel edit karo?",
                reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("eg_ch_"):
        try:
            parts  = data.split("_")
            gi, ci = int(parts[2]), int(parts[3])
            ch_obj = groups[gi]["channels"][ci]
            context.user_data["edit_group_idx"]   = gi
            context.user_data["edit_channel_idx"] = ci
            selected = ch_obj.get("categories", [])
            context.user_data["selected_cats"] = list(selected)
            context.user_data["action"] = "editing_cats"
            await query.edit_message_text(
                f"✏️ *{ch_obj.get('channel','')}* — Categories select karo:",
                reply_markup=_cat_toggle_kb(selected, "eg_cat_done"),
                parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("cat_") and context.user_data.get("action") in ("editing_cats", "adding_channel_cats", "adding_group_cats"):
        cat      = data[4:]
        selected = context.user_data.get("selected_cats", [])
        if cat in selected:
            selected.remove(cat)
        else:
            selected.append(cat)
        context.user_data["selected_cats"] = selected
        action  = context.user_data.get("action")
        done_cb = {
            "editing_cats":       "eg_cat_done",
            "adding_channel_cats":"ac_cat_done",
            "adding_group_cats":  "ag_cat_done"
        }.get(action, "eg_cat_done")
        try:
            await query.edit_message_reply_markup(reply_markup=_cat_toggle_kb(selected, done_cb))
        except Exception:
            pass
        return

    if data == "eg_cat_done":
        try:
            gi       = context.user_data.get("edit_group_idx", 0)
            ci       = context.user_data.get("edit_channel_idx", 0)
            selected = context.user_data.get("selected_cats", [])
            config["groups"][gi]["channels"][ci]["categories"] = selected
            save_config(config)
            cats_str = ", ".join(c.capitalize() for c in selected) or "None"
            context.user_data.clear()
            await query.edit_message_text(f"✅ Categories updated!\n\nCategories: {cats_str}")
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("ren_group_"):
        try:
            gi       = int(data.split("_")[2])
            context.user_data["rename_group_idx"] = gi
            context.user_data["action"] = "wait_rename"
            old_name = groups[gi].get("name", "")
            await query.edit_message_text(
                f"✏️ Group *'{old_name}'* ka naya naam type karo:",
                parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("ac_group_"):
        try:
            gi = int(data.split("_")[2])
            context.user_data["add_channel_group_idx"] = gi
            context.user_data["action"] = "wait_channel_name"
            await query.edit_message_text(
                f"➕ *{groups[gi].get('name','')}* mein channel add karo\n\nChannel ID type karo (jaise @mychannel ya -100123456):",
                parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data == "ac_cat_done":
        try:
            gi      = context.user_data.get("add_channel_group_idx", 0)
            ch_name = context.user_data.get("new_channel_name", "")
            selected = context.user_data.get("selected_cats", [])
            config["groups"][gi]["channels"].append({"channel": ch_name, "categories": selected})
            save_config(config)
            cats_str = ", ".join(c.capitalize() for c in selected) or "None"
            context.user_data.clear()
            await query.edit_message_text(
                f"✅ Channel *{ch_name}* add ho gaya!\nCategories: {cats_str}",
                parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data == "ag_cat_done":
        try:
            group_name = context.user_data.get("new_group_name", "New Group")
            ch_name    = context.user_data.get("new_channel_name", "")
            selected   = context.user_data.get("selected_cats", [])
            new_group  = {
                "name": group_name, "enabled": True,
                "channels": [{"channel": ch_name, "categories": selected}]
            }
            config["groups"].append(new_group)
            save_config(config)
            cats_str = ", ".join(c.capitalize() for c in selected) or "None"
            context.user_data.clear()
            await query.edit_message_text(
                f"✅ Group *{group_name}* ban gaya!\n📢 Channel: {ch_name}\nCategories: {cats_str}",
                parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("del_group_"):
        try:
            gi      = int(data.split("_")[2])
            removed = config["groups"].pop(gi)
            save_config(config)
            await query.edit_message_text(f"🗑️ Group *'{removed.get('name','')}'* delete ho gaya.", parse_mode="Markdown")
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("dc_group_"):
        try:
            gi = int(data.split("_")[2])
            context.user_data["dc_group_idx"] = gi
            await query.edit_message_text(
                f"🗑️ *{groups[gi].get('name','')}* — Kaun sa channel delete karo?",
                reply_markup=_channel_select_kb(gi, "dc_ch"),
                parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
        return

    if data.startswith("dc_ch_"):
        try:
            parts  = data.split("_")
            gi, ci = int(parts[2]), int(parts[3])
            removed = config["groups"][gi]["channels"].pop(ci)
            save_config(config)
            await query.edit_message_text(
                f"🗑️ Channel *{removed.get('channel','')}* delete ho gaya.",
                parse_mode="Markdown"
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")
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

    if action == "wait_folder":
        if not text.startswith("http"):
            await update.message.reply_text("⚠️ Valid link daalo (http se shuru hona chahiye).")
            return
        config = load_config()
        config["folder_link"] = text
        save_config(config)
        context.user_data.clear()
        await update.message.reply_text(f"✅ Folder link save ho gaya!\n`{text}`", parse_mode="Markdown")
        return

    if action == "wait_group_name":
        context.user_data["new_group_name"] = text
        context.user_data["action"] = "wait_group_channel"
        await update.message.reply_text(
            f"➕ Group *'{text}'* — Pehla channel ID type karo:\n(jaise @mychannel ya -100123456789)",
            parse_mode="Markdown"
        )
        return

    if action == "wait_group_channel":
        context.user_data["new_channel_name"] = text
        context.user_data["action"] = "adding_group_cats"
        context.user_data["selected_cats"] = []
        await update.message.reply_text(
            f"➕ *{text}* — Is channel ke liye categories select karo:",
            reply_markup=_cat_toggle_kb([], "ag_cat_done"),
            parse_mode="Markdown"
        )
        return

    if action == "wait_channel_name":
        context.user_data["new_channel_name"] = text
        context.user_data["action"] = "adding_channel_cats"
        context.user_data["selected_cats"] = []
        await update.message.reply_text(
            f"➕ *{text}* — Categories select karo:",
            reply_markup=_cat_toggle_kb([], "ac_cat_done"),
            parse_mode="Markdown"
        )
        return

    if action == "wait_rename":
        gi = context.user_data.get("rename_group_idx", 0)
        config = load_config()
        old_name = config["groups"][gi].get("name", "")
        config["groups"][gi]["name"] = text
        save_config(config)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Group rename ho gaya!\n*{old_name}* → *{text}*",
            parse_mode="Markdown"
        )
        return


# =============================================================================
# MAIN
# =============================================================================
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("BOT_TOKEN environment variable set nahi hai!")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("testai",        cmd_testai))
    app.add_handler(CommandHandler("testamz",       cmd_testamz))
    app.add_handler(CommandHandler("help",          cmd_help))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("manage",        cmd_manage))
    app.add_handler(CommandHandler("editgroup",     cmd_editgroup))
    app.add_handler(CommandHandler("rename",        cmd_rename))
    app.add_handler(CommandHandler("setfolder",     cmd_setfolder))
    app.add_handler(CommandHandler("addgroup",      cmd_addgroup))
    app.add_handler(CommandHandler("addchannel",    cmd_addchannel))
    app.add_handler(CommandHandler("deletegroup",   cmd_deletegroup))
    app.add_handler(CommandHandler("deletechannel", cmd_deletechannel))

    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND,
        handle_deal
    ))

    logger.info("DealsKoti Bot start ho raha hai...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
