import re
import time
import random
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse, unquote

USER_AGENTS = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

PLATFORM_TITLE_SELECTORS = {
    "amazon":   ["#productTitle", "#title span", ".product-title-word-break"],
    "flipkart": [".B_NuCI", "h1.yhB1nd", "span.B_NuCI", "h1._6EBuvT"],
    "shopsy":   [".B_NuCI", "h1"],
    "myntra":   ["h1.pdp-name", ".pdp-title", "h1"],
    "meesho":   ["h1", ".product-name", "[class*='ProductName']", "[class*='product-title']"],
    "nykaa":    ["h1.css-1gc4x7i", ".product-title", "h1"],
    "ajio":     ["h1.prod-name", ".prod-desc", "h1"],
    "jiomart":  ["h1.product-name", ".product-title", "h1"],
}

PLATFORM_PRICE_SELECTORS = {
    "amazon":   [".a-price-whole", "#priceblock_ourprice", "#priceblock_dealprice",
                 ".apexPriceToPay span.a-offscreen", "#corePriceDisplay_desktop_feature_div .a-offscreen"],
    "flipkart": ["._30jeq3._16Jk6d", "._30jeq3", "div[class*='_30jeq3']"],
    "myntra":   [".pdp-price strong", ".pdp-mrp", "[class*='price']"],
    "meesho":   ["h4", "[class*='Price']", "[class*='price']"],
    "nykaa":    [".css-111z9ua", "[class*='price']", "[class*='Price']"],
    "ajio":     [".prod-sp", ".price", "[class*='price']"],
}

TITLE_SUFFIXES = [
    " - Amazon.in", "| Amazon.in", "- Amazon.in", "- Amazon",
    "| Flipkart.com", "- Flipkart.com", "- Flipkart",
    "| Shopsy", "- Shopsy",
    "| Myntra", "- Myntra",
    "| Meesho", "- Meesho",
    "| Nykaa", "- Nykaa",
    "| AJIO", "- AJIO",
    "| JioMart", "- JioMart",
    "Buy Online", ": Buy", "- Buy",
]

PRICE_REGEX = re.compile(
    r"(?:₹|Rs\.?|INR)\s*(\d[\d,]*(?:\.\d{1,2})?)"
    r"|(\d[\d,]*(?:\.\d{1,2})?)\s*(?:₹|Rs\.?|INR)",
    re.IGNORECASE,
)

JUNK_TITLES = {
    "amazon.in", "amazon", "flipkart", "flipkart.com", "shopsy", "shopsy.in",
    "myntra", "meesho", "ajio", "nykaa", "jiomart", "snapdeal",
    "buy online", "online shopping", "best price", "shop online",
}


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }


def _platform_key(url: str) -> str:
    host = urlparse(url).netloc.lower()
    for key in PLATFORM_TITLE_SELECTORS:
        if key in host:
            return key
    return "generic"


def _title_from_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        path = unquote(parsed.path)
        parts = [
            p for p in path.split("/")
            if p and len(p) > 5
            and not re.match(r'^[A-Z0-9]{8,12}$', p)
            and p.lower() not in ('dp', 'p', 's', 'b', 'gp', 'ref', 'product',
                                   'item', 'buy', 'store', 'seller', 'shop')
        ]
        if parts:
            best = max(parts, key=len)
            title = best.replace("-", " ").replace("_", " ")
            title = re.sub(r'\s+', ' ', title).strip()
            if len(title) > 8:
                return title.title()
    except Exception:
        pass
    return ""


def _clean_title(title: str) -> str:
    for suffix in TITLE_SUFFIXES:
        title = title.replace(suffix, "").strip()
    title = re.sub(r'\s+', ' ', title).strip()
    return title


def _is_junk(title: str) -> bool:
    if not title:
        return True
    t = title.lower().strip()
    return t in JUNK_TITLES or len(t) < 6


def _extract_price(soup: BeautifulSoup, platform_key: str) -> str:
    selectors = PLATFORM_PRICE_SELECTORS.get(platform_key, [])
    for sel in selectors:
        try:
            el = soup.select_one(sel)
            if el:
                text = el.get_text(strip=True)
                m = PRICE_REGEX.search(text)
                if m:
                    amount = (m.group(1) or m.group(2)).replace(",", "")
                    if amount.isdigit() and 10 <= int(amount) <= 500000:
                        return f"₹{m.group(1) or m.group(2)}"
        except Exception:
            continue
    full_text = soup.get_text(" ", strip=True)
    for g1, g2 in PRICE_REGEX.findall(full_text)[:5]:
        raw = g1 or g2
        clean = raw.replace(",", "")
        if clean.isdigit() and 10 <= int(clean) <= 500000:
            return f"₹{raw}"
    return ""


def scrape_url(url: str, retries: int = 2) -> tuple:
    url_title = _title_from_url(url)
    platform_key = _platform_key(url)

    for attempt in range(retries + 1):
        try:
            if attempt > 0:
                time.sleep(1.5 * attempt)

            session = requests.Session()
            res = session.get(url, headers=_get_headers(), timeout=14, allow_redirects=True)
            final_url = res.url

            if final_url != url:
                better = _title_from_url(final_url)
                if better:
                    url_title = better
                platform_key = _platform_key(final_url)

            soup = BeautifulSoup(res.text, "html.parser")
            title = ""

            og_title = soup.find("meta", property="og:title")
            if og_title:
                title = og_title.get("content", "").strip()

            if not title:
                selectors = PLATFORM_TITLE_SELECTORS.get(platform_key, []) + [
                    "h1.product-title", ".product-name", "h1"
                ]
                for sel in selectors:
                    el = soup.select_one(sel)
                    if el:
                        t = el.get_text(strip=True)
                        if t and len(t) > 5:
                            title = t
                            break

            if not title and soup.title:
                title = (soup.title.string or "").strip()

            title = _clean_title(title)

            if _is_junk(title):
                title = url_title

            price = _extract_price(soup, platform_key)

            desc = ""
            og_desc = soup.find("meta", property="og:description")
            if og_desc:
                desc = og_desc.get("content", "").strip()
            if not desc:
                meta_desc = soup.find("meta", attrs={"name": "description"})
                if meta_desc:
                    desc = meta_desc.get("content", "").strip()

            url_text = final_url.replace("-", " ").replace("/", " ").replace("_", " ")
            price_text = f"price {price}" if price else ""
            combined = f"{title} {desc} {url_text} {price_text}".lower()

            return title.strip(), combined, price

        except Exception:
            continue

    url_text = url.replace("-", " ").replace("/", " ").replace("_", " ")
    combined = f"{url_title} {url_text}".lower()
    return url_title, combined, ""


def scrape_all_urls(urls: list) -> tuple:
    all_text = ""
    main_title = ""
    main_price = ""

    for url in urls:
        title, page_text, price = scrape_url(url)
        if title and not main_title:
            main_title = title
        if price and not main_price:
            main_price = price
        all_text += " " + page_text

    return main_title.strip(), all_text.strip(), main_price.strip()
