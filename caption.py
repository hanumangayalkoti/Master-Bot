import re
import html as html_lib

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
    """Clean product title — max 8 words."""
    if not title:
        return "Hot Deal"
    words = title.split()
    if len(words) <= 8:
        return title
    return " ".join(words[:8]) + "..."


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

    display_title = _fallback_title(title) or "Hot Deal"

    lines = []
    lines.append("🙏Jai Shree Ram Dosto🙏")
    lines.append("")
    lines.append(f"🔥 <b>{html_lib.escape(display_title)}</b>")
    lines.append("")

    if actual_price and deal_price and actual_price != deal_price:
        lines.append(f"🏷️ MRP:      <s>{html_lib.escape(actual_price)}</s>")
        lines.append(f"💰 Buy At:   <b>{html_lib.escape(deal_price)}</b>")
        if savings:
            lines.append(f"💵 You Save: <b>{html_lib.escape(savings)}</b>")
    elif deal_price:
        lines.append(f"💰 Buy At: <b>{html_lib.escape(deal_price)}</b>")
    elif actual_price:
        lines.append(f"💰 Price: <b>{html_lib.escape(actual_price)}</b>")

    try:
        disc = int(discount_pct)
    except (ValueError, TypeError):
        disc = 0
    if disc > 0:
        lines.append(f"📉 Discount:  <b>{disc}% OFF</b>")

    if rating:
        lines.append(f"⭐ Rating:   <b>{html_lib.escape(rating)}/5</b>")
    if review_count:
        lines.append(f"👥 Reviews:  <b>{html_lib.escape(review_count)}</b>")

    lines.append("")
    if short_link:
        safe_link = html_lib.escape(short_link)
        lines.append(f'🔗 <b><a href="{safe_link}">{safe_link}</a></b>')

    caption = "\n".join(lines)
    return _safe_truncate(caption, max_visible=1020)
