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
        is_amazon_url, is_amazon_search_url, enrich_amazon_url,
        get_short_affiliate_link, extract_asin, make_affiliate_url,
        _resolve_redirect,
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
    ADMIN_ID           = int(os.getenv("ADMIN_ID", "0"))
    ALL_CATS           = ["fitness", "fashion", "electronics", "home", "skincare"]
    
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
    
    
    def get_non_amazon_urls(urls: list) -> list:
        return [u for u in urls if not is_amazon_url(u)]
    
    
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
        category: str | None = None,
        method: str | None   = None,
        matched_kws: list    = None,
        ai_error: str | None = None,
        sent_channels: list  = None,
        errors: list         = None,
        extra_line: str      = "",
    ):
        cat_emojis = {
            "electronics": "⚡", "fashion": "👗",
            "fitness": "💪", "skincare": "✨", "home": "🏠",
        }
        cat_emoji    = cat_emojis.get(category, "🏷️") if category else ""
        method_emoji = "🤖" if method == "AI" else ("🔑" if method else "")
        kw_line      = ""
        if method == "Keyword" and matched_kws:
            kw_line = f"\n🔍 Matched: {', '.join(matched_kws[:5])}"
    
        lines = [headline]
    
        if category:
            lines.append(f"{cat_emoji} Category: <b>{category.capitalize()}</b> {method_emoji}{kw_line}")
    
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
                "⚠️ Koi channel nahi mila! Is category ke liye koi enabled channel nahi hai.\n"
                "/editgroup se channels set karo."
            )
    
        if ai_error:
            lines.append(f"\n⚠️ AI Error: <code>{html_lib.escape(str(ai_error))}</code>")
        if errors:
            lines.append("\n❌ <b>Errors:</b>")
            for err in errors:
                lines.append(f"  • {html_lib.escape(str(err))}")
    
        text = "\n".join(lines)
        await msg.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
    
    
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
    
    
    def _setbutton_status_text(buttons: dict) -> str:
        """Build the status lines shown in the /setbutton message (outside buttons)."""
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
    
    
    def _setbutton_main_kb() -> InlineKeyboardMarkup:
        """Main /setbutton keyboard — no status inside button labels."""
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ Configure Button 1", callback_data="sb_b1")],
            [InlineKeyboardButton("✏️ Configure Button 2", callback_data="sb_b2")],
            [InlineKeyboardButton("❌ Cancel",             callback_data="cancel")],
        ])
    
    
    def _setbutton_detail_kb(btn_key: str, btn: dict) -> InlineKeyboardMarkup:
        toggle_label = "🟢 Turn ON" if not btn.get("enabled") else "🔴 Turn OFF"
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Rename",      callback_data=f"sb_{btn_key}_rename")],
            [InlineKeyboardButton("🔗 Set Link",    callback_data=f"sb_{btn_key}_link")],
            [InlineKeyboardButton(toggle_label,     callback_data=f"sb_{btn_key}_toggle")],
            [InlineKeyboardButton("⬅️ Back",        callback_data="sb_main")],
        ])
    
    
    def _btn_detail_text(btn_key: str, btn: dict) -> str:
        """Full status text for a single button detail screen."""
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
            "Deal ka message bhejo — AI category detect karke sahi channel mein post kar dega.\n\n"
            "/help daao sare commands dekhne ke liye.",
            parse_mode="HTML"
        )
    
    
    async def cmd_testai(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return
        await update.message.reply_text("🔄 OpenAI API test ho rahi hai...")
        try:
            cat, method, err, kws = await detect_category("mamaearth face scrub for glowing skin")
            if method == "AI":
                await update.message.reply_text(
                    f"✅ <b>AI kaam kar raha hai!</b>\n\n"
                    f"Test: <code>mamaearth face scrub</code>\n"
                    f"Category: <b>{cat}</b>\nMethod: <b>AI 🤖</b>",
                    parse_mode="HTML"
                )
            else:
                kw_str = ", ".join(f"<code>{k}</code>" for k in kws) if kws else "koi match nahi"
                await update.message.reply_text(
                    f"❌ <b>AI kaam nahi kar raha!</b>\n\n"
                    f"Method: <b>Keyword fallback 🔑</b>\n"
                    f"Category: <b>{cat or 'detect nahi hua'}</b>\n"
                    f"Matched keywords: {kw_str}\n\n"
                    f"<b>AI Error:</b>\n<code>{html_lib.escape(str(err))}</code>",
                    parse_mode="HTML"
                )
        except Exception as e:
            await update.message.reply_text(
                f"❌ <b>Exception aaya:</b>\n<code>{html_lib.escape(str(e))}</code>",
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
                    "CREDENTIAL_ID aur CREDENTIAL_SECRET check karo.\n\n"
                    "/testamz se dobara check karo.",
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
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📊 <b>STATUS</b>\n"
            "/status — Sare groups, channels aur categories dekho\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🔁 <b>ON / OFF</b>\n"
            "/manage — Groups ko ON ya OFF karo\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "✏️ <b>EDIT</b>\n"
            "/editgroup — Channel ki categories badlo\n"
            "/rename — Group ka naam badlo\n"
            "/setbutton — Har post ke neeche 2 customisable buttons set karo\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "➕ <b>ADD</b>\n"
            "/addgroup — Naya group banao\n"
            "/addchannel — Existing group mein naya channel add karo\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🗑️ <b>DELETE</b>\n"
            "/deletegroup — Poora group delete karo\n"
            "/deletechannel — Group ke andar se koi channel hatao\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🧪 <b>TEST</b>\n"
            "/testai — OpenAI API test karo\n"
            "/testamz — Amazon Creators API test karo\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💾 <b>BACKUP</b>\n"
            "/exportconfig — Config JSON export karo (backup ke liye)\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "ℹ️ <b>OTHER</b>\n"
            "/start — Bot ki info\n"
            "/help — Ye poori list",
            parse_mode="HTML"
        )
    
    
    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return
        config  = load_config()
        groups  = config.get("groups", [])
        buttons = config.get("buttons", {})
    
        b1 = buttons.get("btn1", {})
        b2 = buttons.get("btn2", {})
    
        lines = [
            "⚙️ <b>Bot Status</b>\n",
            "🎛️ <b>Buttons:</b>",
            f"  Button 1: {'✅ ON' if b1.get('enabled') else '❌ OFF'} — <b>{b1.get('label', '-')}</b>",
            f"  Button 2: {'✅ ON' if b2.get('enabled') else '❌ OFF'} — <b>{b2.get('label', '-')}</b>",
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
                    cats = ", ".join(c.capitalize() for c in ch_obj.get("categories", []))
                    lines.append(f"   📢 {html_lib.escape(ch_obj.get('channel', ''))}")
                    lines.append(f"      Categories: {cats or '—'}")
    
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
            reply_markup=_setbutton_main_kb()
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
        config      = load_config()
        config_json = json.dumps(config, indent=2, ensure_ascii=False)
        await update.message.reply_text(
            f"📦 <b>Config Export (Backup)</b>\n\n"
            f"Is JSON ko copy karke safe jagah save karo.\n"
            f"Naya bot banane ke baad config.json file mein paste karo.\n\n"
            f"<pre>{html_lib.escape(config_json)}</pre>",
            parse_mode="HTML"
        )
    
    
    # =============================================================================
    # CHANNEL POSTER
    # =============================================================================
    async def _post_to_channels(context, config, category, send_fn) -> tuple:
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
                    errors.append(f"{channel}: {e}")
        return sent_channels, errors
    
    
    # =============================================================================
    # MAIN DEAL HANDLER
    # =============================================================================
    async def handle_deal(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            return
    
        # Delegate to text-input handler when a multi-step action is active
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
        single_amazon = len(amazon_urls) == 1 and len(all_urls) == 1
    
        # No content at all
        if not raw_plain.strip() and not all_urls and not has_photo:
            await msg.reply_text("⚠️ Message mein koi text ya link nahi mila.")
            return
    
        # Photo without any text or link — can't categorize
        if not raw_plain.strip() and not all_urls and has_photo:
            await msg.reply_text(
                "⚠️ Photo ke saath product ka naam ya link bhi bhejo — tabhei post ho sakta hai."
            )
            return
    
        config       = load_config()
        final_markup = build_final_markup(config)
    
        # Periodic cleanup of old duplicate entries
        try:
            cleanup_old_entries()
        except Exception:
            pass
    
        # ==========================================================================
        # CASE 1: Single Amazon product link — full enrichment
        # ==========================================================================
        if single_amazon and has_amazon:
            amazon_url = amazon_urls[0]
    
            # Resolve short links before search-page check
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
                        f"⚠️ <b>Yeh deal {dup_time} pehle already post ho chuki hai — skip kiya.</b>\n\n"
                        f"🏷️ {html_lib.escape(title[:80])}",
                        parse_mode="HTML"
                    )
                    return
    
                # FIX: await the async caption builder (was sync before — blocked event loop)
                caption_html = await build_amazon_caption(product, short_link, raw_plain)
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
                # FIX: truncate based on visible text (not HTML tag length)
                caption_html = _safe_truncate(raw_caption, max_visible=1020)
    
            await wait_msg.delete()
    
            detect_text = ((product.get("title", "") + " ") if product else "") + raw_plain
            try:
                result   = await detect_category(detect_text)
                category, method, ai_error, matched_kws = (list(result) + [None, None, None, None])[:4]
            except Exception as e:
                await msg.reply_text(
                    f"❌ Category detection mein error:\n<code>{html_lib.escape(str(e))}</code>",
                    parse_mode="HTML"
                )
                return
    
            if not category:
                await msg.reply_text("⚠️ Category detect nahi ho saki! Product naam hona chahiye message mein.")
                return
    
            async def send_amazon(channel):
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
    
            sent_channels, errors = await _post_to_channels(context, config, category, send_amazon)
    
            if sent_channels and title:
                mark_posted(title)
    
            await _send_admin_reply(
                msg,
                headline      = "✅ <b>Amazon Deal Post Ho Gaya!</b>" if sent_channels else "⚠️ <b>Koi channel nahi mila!</b>",
                category      = category,
                method        = method,
                matched_kws   = matched_kws or [],
                ai_error      = ai_error,
                sent_channels = sent_channels,
                errors        = errors,
                extra_line    = api_note,
            )
            return
    
        # ==========================================================================
        # CASE 2: Multiple links or non-Amazon — normal post with affiliate links
        # ==========================================================================
        if has_amazon and not single_amazon:
            updated_plain = await replace_amazon_links(raw_plain, amazon_urls)
        else:
            updated_plain = raw_plain
    
        cleaned_plain, cleaned_entities = remove_footer(updated_plain, raw_entities)
        GREETING   = "🙏Jai Shree Ram Dosto🙏\n\n"
        body_html  = entities_to_html(cleaned_plain, cleaned_entities)
        final_html = GREETING + body_html
    
        try:
            result   = await detect_category(raw_plain)
            category, method, ai_error, matched_kws = (list(result) + [None, None, None, None])[:4]
        except Exception as e:
            await msg.reply_text(
                f"❌ Category detection mein error:\n<code>{html_lib.escape(str(e))}</code>",
                parse_mode="HTML"
            )
            return
    
        if not category:
            err_detail = f"\n\n⚠️ AI Error: <code>{html_lib.escape(str(ai_error))}</code>" if ai_error else ""
            await msg.reply_text(
                f"⚠️ Category detect nahi ho saki!\n"
                f"Message mein product naam/brand hona chahiye.{err_detail}",
                parse_mode="HTML"
            )
            return
    
        async def send_normal(channel):
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
    
        sent_channels, errors = await _post_to_channels(context, config, category, send_normal)
        await _send_admin_reply(
            msg,
            headline      = "✅ <b>Post Ho Gaya!</b>" if sent_channels else "⚠️ <b>Koi channel nahi mila!</b>",
            category      = category,
            method        = method,
            matched_kws   = matched_kws or [],
            ai_error      = ai_error,
            sent_channels = sent_channels,
            errors        = errors,
        )
    
    
    # =============================================================================
    # CALLBACK HANDLER
    # =============================================================================
    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data   = query.data
        config = load_config()
        groups = config.get("groups", [])
    
        # ── Cancel ──
        if data == "cancel":
            context.user_data.clear()
            try:
                await query.edit_message_text("❌ Cancel ho gaya.")
            except Exception:
                pass
            return
    
        # ── Group toggle ──
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
    
        # ── Edit group ──
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
                    f"✏️ <b>{html_lib.escape(group.get('name',''))}</b> — Kaun sa channel edit karo?",
                    reply_markup=InlineKeyboardMarkup(buttons), parse_mode="HTML"
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
                    f"✏️ <b>{html_lib.escape(ch_obj.get('channel',''))}</b> — Categories select karo:",
                    reply_markup=_cat_toggle_kb(selected, "eg_cat_done"),
                    parse_mode="HTML"
                )
            except Exception as e:
                await query.edit_message_text(f"❌ Error: {e}")
            return
    
        if data.startswith("cat_") and context.user_data.get("action") in (
            "editing_cats", "adding_channel_cats", "adding_group_cats"
        ):
            cat      = data[4:]
            selected = context.user_data.get("selected_cats", [])
            if cat in selected:
                selected.remove(cat)
            else:
                selected.append(cat)
            context.user_data["selected_cats"] = selected
            action  = context.user_data.get("action")
            done_cb = {
                "editing_cats":        "eg_cat_done",
                "adding_channel_cats": "ac_cat_done",
                "adding_group_cats":   "ag_cat_done",
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
                    f"✏️ Group <b>'{html_lib.escape(old_name)}'</b> ka naya naam type karo:",
                    parse_mode="HTML"
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
                    f"➕ <b>{html_lib.escape(groups[gi].get('name',''))}</b> mein channel add karo\n\n"
                    f"Channel ID type karo (jaise @mychannel ya -100123456):",
                    parse_mode="HTML"
                )
            except Exception as e:
                await query.edit_message_text(f"❌ Error: {e}")
            return
    
        if data == "ac_cat_done":
            try:
                gi       = context.user_data.get("add_channel_group_idx", 0)
                ch_name  = context.user_data.get("new_channel_name", "")
                selected = context.user_data.get("selected_cats", [])
                config["groups"][gi]["channels"].append({"channel": ch_name, "categories": selected})
                save_config(config)
                cats_str = ", ".join(c.capitalize() for c in selected) or "None"
                context.user_data.clear()
                await query.edit_message_text(
                    f"✅ Channel <b>{html_lib.escape(ch_name)}</b> add ho gaya!\nCategories: {cats_str}",
                    parse_mode="HTML"
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
                    "channels": [{"channel": ch_name, "categories": selected}],
                }
                config["groups"].append(new_group)
                save_config(config)
                cats_str = ", ".join(c.capitalize() for c in selected) or "None"
                context.user_data.clear()
                await query.edit_message_text(
                    f"✅ Group <b>{html_lib.escape(group_name)}</b> ban gaya!\n"
                    f"📢 Channel: {html_lib.escape(ch_name)}\nCategories: {cats_str}",
                    parse_mode="HTML"
                )
            except Exception as e:
                await query.edit_message_text(f"❌ Error: {e}")
            return
    
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
    
        # ==========================================================================
        # /setbutton callbacks
        # ==========================================================================
        if data == "sb_main":
            cfg     = load_config()
            buttons = cfg.get("buttons", {})
            try:
                await query.edit_message_text(
                    "🎛️ <b>Button Settings</b>\n\n"
                    + _setbutton_status_text(buttons)
                    + "\n\n<i>Kaun sa configure karna hai?</i>",
                    parse_mode="HTML",
                    reply_markup=_setbutton_main_kb()
                )
            except Exception:
                pass
            return
    
        if data in ("sb_b1", "sb_b2"):
            btn_key = data[3:]           # "b1" or "b2"
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
    
        if data in ("sb_b1_toggle", "sb_b2_toggle"):
            btn_key = data[3:5]          # "b1" or "b2"
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
    
        if data in ("sb_b1_rename", "sb_b2_rename"):
            btn_key = data[3:5]
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
    
        if data in ("sb_b1_link", "sb_b2_link"):
            btn_key = data[3:5]
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
    
        if data in ("sb_b1_confirm", "sb_b2_confirm"):
            btn_key  = data[3:5]
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
            # Reload after save so displayed data is always fresh from DB
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
    
        if data in ("sb_b1_cancel_edit", "sb_b2_cancel_edit"):
            btn_key = data[3:5]
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
    
        # ── /setbutton: button rename ──
        if action.endswith("_wait_label"):
            btn_key = context.user_data.get("sb_btn_key", "b1")
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
    
        # ── /setbutton: button link ──
        if action.endswith("_wait_link"):
            btn_key = context.user_data.get("sb_btn_key", "b1")
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
    
        # ── /addgroup ──
        if action == "wait_group_name":
            context.user_data["new_group_name"] = text
            context.user_data["action"] = "wait_group_channel"
            await update.message.reply_text(
                f"➕ Group <b>'{html_lib.escape(text)}'</b> — Pehla channel ID type karo:\n(jaise @mychannel ya -100123456789)",
                parse_mode="HTML"
            )
            return
    
        if action == "wait_group_channel":
            context.user_data["new_channel_name"] = text
            context.user_data["action"] = "adding_group_cats"
            context.user_data["selected_cats"] = []
            await update.message.reply_text(
                f"➕ <b>{html_lib.escape(text)}</b> — Is channel ke liye categories select karo:",
                reply_markup=_cat_toggle_kb([], "ag_cat_done"),
                parse_mode="HTML"
            )
            return
    
        # ── /addchannel ──
        if action == "wait_channel_name":
            context.user_data["new_channel_name"] = text
            context.user_data["action"] = "adding_channel_cats"
            context.user_data["selected_cats"] = []
            await update.message.reply_text(
                f"➕ <b>{html_lib.escape(text)}</b> — Categories select karo:",
                reply_markup=_cat_toggle_kb([], "ac_cat_done"),
                parse_mode="HTML"
            )
            return
    
        # ── /rename ──
        if action == "wait_rename":
            gi = context.user_data.get("rename_group_idx", 0)
            cfg = load_config()
            old_name = cfg["groups"][gi].get("name", "")
            cfg["groups"][gi]["name"] = text
            save_config(cfg)
            context.user_data.clear()
            await update.message.reply_text(
                f"✅ Group rename ho gaya!\n<b>{html_lib.escape(old_name)}</b> → <b>{html_lib.escape(text)}</b>",
                parse_mode="HTML"
            )
            return
    
    
    # =============================================================================
    # MAIN
    # =============================================================================
    def main():
        if not TELEGRAM_BOT_TOKEN:
            raise ValueError("BOT_TOKEN environment variable set nahi hai!")
    
        # Initialize PostgreSQL tables on startup
        try:
            init_db()
        except Exception as e:
            logger.error(f"DB init failed: {e}")
            raise
    
        app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    
        app.add_handler(CommandHandler("start",         cmd_start))
        app.add_handler(CommandHandler("testai",        cmd_testai))
        app.add_handler(CommandHandler("testamz",       cmd_testamz))
        app.add_handler(CommandHandler("help",          cmd_help))
        app.add_handler(CommandHandler("status",        cmd_status))
        app.add_handler(CommandHandler("manage",        cmd_manage))
        app.add_handler(CommandHandler("editgroup",     cmd_editgroup))
        app.add_handler(CommandHandler("rename",        cmd_rename))
        app.add_handler(CommandHandler("setbutton",     cmd_setbutton))
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
    
