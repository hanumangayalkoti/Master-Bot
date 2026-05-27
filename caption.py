import os
import re
import aiohttp

_TAG_RE = re.compile(r"<[^>]+>")

PLATFORM_EMOJIS = {
    "Amazon":           "🛒",
    "Flipkart":         "🛍️",
    "Shopsy":           "🛍️",
    "Myntra":           "👗",
    "Ajio":             "👔",
    "Meesho":           "🏷️",
    "Nykaa":            "💄",
    "Snapdeal":         "🎯",
    "JioMart":          "🛒",
    "TataCliq":         "🛒",
    "Croma":            "🔌",
    "Reliance Digital": "🔌",
}


def _visible_len(html_text: str) -> int:
    """Telegram caption limit counts visible characters, not HTML tags."""
    return len(_TAG_RE.sub("", html_text))


def _safe_truncate(html_text: str, max_visible: int = 1020) -> str:
    """
    Truncate caption so visible text <= max_visible chars.
    Preserves the last line (buy link) when truncating.
    """
    if _visible_len(html_text) <= max_visible:
        return html_text

    lines = html_text.rsplit("\n", 1)
    if len(lines) == 2:
        body, last_line = lines
        last_visible = _visible_len(last_line) + 1  # +1 for the newline
        body_limit = max_visible - last_visible - 3
        body_plain = _TAG_RE.sub("", body)
        if body_limit > 20:
            return body_plain[:body_limit] + "...\n" + last_line

    plain = _TAG_RE.sub("", html_text)
    return plain[:max_visible - 3] + "..."


def _shorten_amazon_title(title: str, max_words: int = 8) -> str:
    if not title:
        return ""
    words = title.split()
    if len(words) <= max_words:
        return f"🔥 {title}"
    return f"🔥 {' '.join(words[:max_words])}..."


async def _ai_short_title(product_title: str, original_message: str) -> str | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        prompt = (
            "You are a deals bot for an Indian Telegram channel. "
            "Given an Amazon product title, create a SHORT catchy deal headline in Hinglish "
            "(max 10 words). Use fire/urgency emojis. "
            "Examples: '🔥 boAt Earbuds — Sirf ₹699!' or '⚡ Sony TV 55\" — Best Price Ever!'\n\n"
            "Return ONLY the headline. No explanation, no JSON."
        )
        context_msg = (
            f"Product Title: {product_title[:200]}\n"
            f"Original Message: {original_message[:200]}"
        )
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user",   "content": context_msg},
            ],
            "max_tokens": 60,
            "temperature": 0.5,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type":  "application/json",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    data   = await resp.json()
                    result = data["choices"][0]["message"]["content"].strip()
                    return result if result else None
    except Exception:
        return None


async def build_amazon_caption(
    product: dict,
    short_link: str,
    original_message: str = "",
) -> str:
    title        = product.get("title", "").strip()
    actual_price = product.get("actual_price", "").strip()
    deal_price   = product.get("deal_price", "").strip()
    discount_pct = product.get("discount_pct", 0)
    savings      = product.get("savings", "").strip()
    rating       = product.get("rating", "").strip()
    review_count = product.get("review_count", "").strip()

    ai_title = await _ai_short_title(title, original_message) if title else None
    display_title = ai_title or _shorten_amazon_title(title) or "🔥 Hot Deal!"

    lines = []
    lines.append("🙏Jai Shree Ram Dosto🙏")
    lines.append("")
    lines.append(f"<b>{display_title}</b>")
    lines.append("")

    if actual_price and deal_price and actual_price != deal_price:
        lines.append(f"💰 MRP:               <s>{actual_price}</s>")
        lines.append(f"🏷️ Deal Price:        <b>{deal_price}</b>")
        if savings:
            lines.append(f"💵 You Save:         <b>{savings}</b>")
    elif deal_price:
        lines.append(f"🏷️ Price: <b>{deal_price}</b>")
    elif actual_price:
        lines.append(f"🏷️ Price: <b>{actual_price}</b>")

    if discount_pct and int(discount_pct) > 0:
        lines.append(f"📉 Discount:          <b>{discount_pct}% OFF</b>")

    if rating or review_count:
        rating_line = ""
        if rating:
            rating_line = f"⭐ Rating: <b>{rating}/5</b>"
        if review_count:
            rating_line += f"  |  👥 <b>{review_count}</b> reviews"
        if rating_line:
            lines.append(rating_line)

    lines.append("")
    if short_link:
        lines.append(f'🛒 <b><a href="{short_link}">Buy Now →</a></b>')
    else:
        lines.append("🛒 <b>Buy Now</b> (link unavailable)")

    caption = "\n".join(lines)
    return _safe_truncate(caption, max_visible=1020)
