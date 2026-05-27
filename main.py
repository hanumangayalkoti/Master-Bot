import os
import re
import aiohttp

_TAG_RE = re.compile(r"<[^>]+>")


def _visible_len(html_text: str) -> int:
    return len(_TAG_RE.sub("", html_text))


def _safe_truncate(html_text: str, max_visible: int = 1020) -> str:
    if _visible_len(html_text) <= max_visible:
        return html_text
    lines = html_text.rsplit("\n", 1)
    if len(lines) == 2:
        body, last_line = lines
        last_visible = _visible_len(last_line) + 1
        body_limit = max_visible - last_visible - 3
        body_plain = _TAG_RE.sub("", body)
        if body_limit > 20:
            return body_plain[:body_limit] + "...\n" + last_line
    plain = _TAG_RE.sub("", html_text)
    return plain[:max_visible - 3] + "..."


def _fallback_title(title: str) -> str:
    """Clean product title when AI is unavailable — max 8 words, no emojis."""
    if not title:
        return "Hot Deal"
    words = title.split()
    if len(words) <= 8:
        return title
    return " ".join(words[:8]) + "..."


async def _ai_short_title(product_title: str) -> str | None:
    """
    Generate a clean English product title (5-9 words).
    NO price, NO discount %, NO rupee amounts, NO urgency words.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        prompt = (
            "You are a product title formatter for an Indian deals Telegram channel.\n\n"
            "Given the full Amazon product title, return a SHORT, CLEAN English title (5 to 9 words).\n\n"
            "Rules:\n"
            "- English only\n"
            "- 5 to 9 words\n"
            "- Include: Brand + Product Type + Key Feature (color/size/material if relevant)\n"
            "- NO price, NO discount %, NO rupee symbol, NO words like 'Sirf', 'Only', 'Best Deal'\n"
            "- NO emojis\n"
            "- NO punctuation at end\n\n"
            "Examples:\n"
            "Input: 'Symbol Men's Jacket Padded Full Sleeve Winter Wear Quilted 82% Off'\n"
            "Output: Symbol Men Full-Sleeve Quilted Puffer Jacket\n\n"
            "Input: 'boAt Airdopes 141 Bluetooth Truly Wireless in Ear Earbuds with 42H Playtime'\n"
            "Output: boAt Airdopes 141 Wireless Earbuds 42H Battery\n\n"
            "Input: 'Mamaearth Vitamin C Face Wash for Glowing Skin with Vitamin C & Turmeric'\n"
            "Output: Mamaearth Vitamin C Turmeric Glow Face Wash\n\n"
            "Return ONLY the title. Nothing else."
        )
        payload = {
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user",   "content": product_title[:400]},
            ],
            "max_tokens": 40,
            "temperature": 0.3,
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
                    # Reject if AI slipped in a rupee sign or % (price in title)
                    if result and "₹" not in result and "%" not in result:
                        return result
    except Exception:
        pass
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

    ai_title      = await _ai_short_title(title) if title else None
    display_title = ai_title or _fallback_title(title) or "Hot Deal"

    lines = []
    lines.append("🙏Jai Shree Ram Dosto🙏")
    lines.append("")
    lines.append(f"🔥 <b>{display_title}</b>")
    lines.append("")

    if actual_price and deal_price and actual_price != deal_price:
        lines.append(f"💰 MRP:          <s>{actual_price}</s>")
        lines.append(f"🏷️ Buy At:        <b>{deal_price}</b>")
        if savings:
            lines.append(f"💵 You Save:     <b>{savings}</b>")
    elif deal_price:
        lines.append(f"🏷️ Buy At: <b>{deal_price}</b>")
    elif actual_price:
        lines.append(f"💰 Price: <b>{actual_price}</b>")

    try:
        disc = int(discount_pct)
    except (ValueError, TypeError):
        disc = 0
    if disc > 0:
        lines.append(f"📉 Discount:      <b>{disc}% OFF</b>")

    if rating:
        lines.append(f"⭐ Rating:        <b>{rating}/5</b>")
    if review_count:
        lines.append(f"👥 Reviews:       <b>{review_count}</b>")

    lines.append("")
    if short_link:
        lines.append(f'🔗 <b><a href="{short_link}">{short_link}</a></b>')

    caption = "\n".join(lines)
    return _safe_truncate(caption, max_visible=1020)
