import os
import re
import json
import urllib.parse
import openai

EARNKARO_BASE = "https://fktr.in/0MCTP0G"
URL_REGEX = r"(https?://[^\s\]\[<>\"']+)"

PRICE_REGEX = re.compile(
    r"(?:₹|Rs\.?|INR)\s*(\d[\d,]*(?:\.\d{1,2})?)"
    r"|(\d[\d,]*(?:\.\d{1,2})?)\s*(?:₹|Rs\.?|INR)",
    re.IGNORECASE,
)

PLATFORM_EMOJIS = {
    "Amazon":          "🛒",
    "Flipkart":        "🛍️",
    "Shopsy":          "🛍️",
    "Myntra":          "👗",
    "Ajio":            "👔",
    "Meesho":          "🏷️",
    "Nykaa":           "💄",
    "Snapdeal":        "🎯",
    "JioMart":         "🛒",
    "TataCliq":        "🛒",
    "Croma":           "🔌",
    "Reliance Digital":"🔌",
}


def _affiliate_url(url: str) -> str:
    encoded = urllib.parse.quote(url, safe="")
    return f"{EARNKARO_BASE}?redirect={encoded}"


def _extract_price_from_text(text: str) -> str:
    m = PRICE_REGEX.search(text)
    if m:
        return f"₹{m.group(1) or m.group(2)}"
    return ""


def _ai_title_and_price(message_text: str, title: str, scraped_text: str, platform: str) -> tuple:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None, None
    try:
        client = openai.OpenAI(api_key=api_key)
        context = (
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
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user",   "content": context},
            ],
            max_tokens=80,
            temperature=0.4,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        return data.get("title"), data.get("price")
    except Exception:
        return None, None


def build_caption(
    content: str,
    platform: str,
    title: str = "",
    scraped_text: str = "",
    scraped_price: str = "",
    single_link: bool = True,
) -> str:
    emoji = PLATFORM_EMOJIS.get(platform, "🔥")

    def replace_url(m):
        return _affiliate_url(m.group(1))

    body = re.sub(URL_REGEX, replace_url, content)
    body = "\n".join(line.strip() for line in body.strip().splitlines() if line.strip())

    if single_link:
        price = scraped_price or _extract_price_from_text(content)
        ai_title, ai_price = _ai_title_and_price(content, title, scraped_text, platform)
        if not price and ai_price:
            price = ai_price

        lines = []
        if ai_title:
            lines.append(ai_title)
            lines.append("")
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
