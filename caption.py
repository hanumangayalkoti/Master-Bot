import os
import re
import json
import urllib.parse
import aiohttp

ASSOCIATE_TAG = os.getenv("PARTNER_TAG", "dealskoti-21")

PRICE_REGEX = re.compile(
    r"(?:₹|Rs\.?|INR)\s*(\d[\d,]*(?:\.\d{1,2})?)"
    r"|(\d[\d,]*(?:\.\d{1,2})?)\s*(?:₹|Rs\.?|INR)",
    re.IGNORECASE,
)

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


def _extract_price_from_text(text: str) -> str:
    m = PRICE_REGEX.search(text)
    if m:
        return f"₹{m.group(1) or m.group(2)}"
    return ""


def _visible_len(html_text: str) -> int:
    """Telegram caption limit counts visible characters, not HTML tags."""
    return len(_TAG_RE.sub("", html_text))


def _safe_truncate(html_text: str, max_visible: int = 1020) -> str:
    """
    Truncate caption so visible text <= max_visible chars.
    If it's already within limit, return as-is.
    If over limit, strip all HTML tags and truncate plain text.
    """
    if _visible_len(html_text) <= max_visible:
        return html_text
    plain = _TAG_RE.sub("", html_text)
    return plain[:max_visible - 3] + "..."


async def _ai_short_title(product_title: str, original_message: str) -> str | None:
    """
    Async AI title generation — uses aiohttp (non-blocking).
    Falls back to None if OPENAI_API_KEY not set or request fails.
    """
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


async def _ai_title_and_price(
    message_text: str, title: str, scraped_text: str, platform: str
) -> tuple:
    """Async AI title+price extraction — uses aiohttp (non-blocking)."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None, None
    try:
        context_msg = (
            f"Platform: {platform}\n"
            f"Message: {message_text[:300]}\n"
            f"Product Title: {title[:200]}\n"
            f"Product Details: {scraped_text[:400]}"
        )
        prompt = (
            "You are a deals bot assistant for an Indian Telegram channel. "
            "Given product info, return a JSON with exactly 2 keys:\n"
            "1. 'title': A short urgent deal headline in Hinglish (max 12 words). "
            "Use fire/urgency emojis. Example: '🔥 boAt Earbuds — Sirf ₹699! Abhi Khareed lo 🏃'\n"
            "2. 'price': Extract the price as '₹NUMBER' format. "
            "If price not found, return null.\n\n"
            "Return ONLY valid JSON. Example:\n"
            '{"title": "🔥 Sony Headphones — ₹1299 mein! Limited Stock ⚡", "price": "₹1299"}'
        )
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user",   "content": context_msg},
            ],
            "max_tokens": 80,
            "temperature": 0.4,
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
                    data = await resp.json()
                    raw  = data["choices"][0]["message"]["content"].strip()
                    raw  = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
                    parsed = json.loads(raw)
                    return parsed.get("title"), parsed.get("price")
    except Exception:
        return None, None


async def build_amazon_caption(
    product: dict,
    short_link: str,
    original_message: str = "",
) -> str:
    """
    Build a rich HTML caption for an Amazon product.
    Now async so the AI title call doesn't block the event loop.
    """
    title        = product.get("title", "").strip()
    actual_price = product.get("actual_price", "").strip()
    deal_price   = product.get("deal_price", "").strip()
    discount_pct = product.get("discount_pct", 0)
    savings      = product.get("savings", "").strip()
    rating       = product.get("rating", "").strip()
    review_count = product.get("review_count", "").strip()

    ai_title      = await _ai_short_title(title, original_message) if title else None
    display_title = ai_title or (f"🔥 {title}" if title else "🔥 Hot Deal!")

    lines = []
    lines.append("🙏Jai Shree Ram Dosto🙏")
    lines.append("")
    lines.append(f"<b>{display_title}</b>")
    lines.append("")

    if actual_price and deal_price and actual_price != deal_price:
        lines.append(f"💰 Actual Price:      <s>{actual_price}</s>")
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
    lines.append(f'🛒 <b><a href="{short_link}">Buy Now →</a></b>')

    caption = "\n".join(lines)

    # Truncate based on VISIBLE text length (Telegram limit = 1024 visible chars for captions)
    return _safe_truncate(caption, max_visible=1020)


def build_caption(
    content: str,
    platform: str,
    title: str = "",
    scraped_text: str = "",
    scraped_price: str = "",
    single_link: bool = True,
) -> str:
    emoji = PLATFORM_EMOJIS.get(platform, "🔥")

    body = content.strip()
    body = "\n".join(line.strip() for line in body.splitlines() if line.strip())

    if single_link:
        price = scraped_price or _extract_price_from_text(content)

        lines = []
        if price:
            lines.append(f"💰 Price — {price}")
            lines.append("")
        lines.append(body)
        if platform and platform != "Other":
            lines.append(f"\n🏪 From {platform}")
        return "\n".join(lines)

    header = (
        f"{emoji} <b>{platform} Deals</b>\n\n"
        if platform and platform != "Other"
        else "🔥 <b>Hot Deals!</b>\n\n"
    )
    return header + body
