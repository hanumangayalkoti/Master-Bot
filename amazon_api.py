import os
import re
import logging
import aiohttp
import urllib.parse
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

CREDENTIAL_ID      = os.getenv("CREDENTIAL_ID", "")
CREDENTIAL_SECRET  = os.getenv("CREDENTIAL_SECRET", "")
CREDENTIAL_VERSION = os.getenv("CREDENTIAL_VERSION", "3.2")
MARKETPLACE        = os.getenv("MARKETPLACE", "www.amazon.in")

PARTNER_TAG = os.getenv("PARTNER_TAG", "")
if not PARTNER_TAG:
    logger.warning("PARTNER_TAG env var set nahi hai! Affiliate links mein tag nahi hoga.")

VERSION_TOKEN_URLS = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
    "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}

SCOPE    = "creatorsapi::default" if CREDENTIAL_VERSION.startswith("3.") else "creatorsapi/default"
API_BASE = "https://creatorsapi.amazon"
ITEMS_EP = f"{API_BASE}/catalog/v1/getItems"

# Ek call mein max 10 ASIN — Amazon ki limit
MAX_ASINS_PER_CALL = 10

ASIN_PAT = re.compile(r"/(?:dp|gp/product|exec/obidos/ASIN|o/ASIN)/([A-Za-z0-9]{10})")

NEEDS_REDIRECT = ("amzn.to", "amzn.in", "amzn.eu", "amzn.asia", "a.co", "link.amazon")

SEARCH_MARKERS = (
    "/s?", "/s/", "/search",
    "field-keywords", "keywords=", "k=",
    "/b?", "/b/", "node=",
    "/deals", "/gp/goldbox", "/goldbox",
    "/gp/browse", "/gp/search",
    "/gp/bestsellers", "/bestsellers", "/gp/new-releases", "/gp/movers-and-shakers",
    "/stores/", "/shop/", "/brand/",
    "/gcx/", "/events/", "/promotion", "/hz/",
    "/gp/most-wished-for", "/international-shopping",
)

_token_cache: dict = {"token": None, "expires_at": None}

PRODUCT_RESOURCES = [
    "images.primary.large",
    "images.primary.medium",
    "itemInfo.title",
    "itemInfo.features",
    "itemInfo.byLineInfo",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
    "offersV2.listings.dealDetails",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.isBuyBoxWinner",
    "browseNodeInfo.websiteSalesRank",
    "browseNodeInfo.browseNodes",
    "customerReviews.count",
    "customerReviews.starRating",
]


async def _get_token() -> str | None:
    now = datetime.now()
    if _token_cache["token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not CREDENTIAL_ID or not CREDENTIAL_SECRET:
        logger.error("CREDENTIAL_ID ya CREDENTIAL_SECRET set nahi hai")
        return None

    token_url = VERSION_TOKEN_URLS.get(CREDENTIAL_VERSION)
    if not token_url:
        logger.error(f"Unsupported CREDENTIAL_VERSION: {CREDENTIAL_VERSION}")
        return None

    is_lwa = CREDENTIAL_VERSION.startswith("3.")
    payload = {
        "grant_type":    "client_credentials",
        "client_id":     CREDENTIAL_ID,
        "client_secret": CREDENTIAL_SECRET,
        "scope":         SCOPE,
    }

    try:
        async with aiohttp.ClientSession() as session:
            if is_lwa:
                req = session.post(
                    token_url, json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            else:
                req = session.post(
                    token_url, data=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            async with req as resp:
                if resp.status == 200:
                    data       = await resp.json()
                    token      = data.get("access_token")
                    expires_in = data.get("expires_in", 3600)
                    _token_cache["token"]      = token
                    _token_cache["expires_at"] = now + timedelta(seconds=expires_in - 60)
                    logger.info("Amazon Creators API token mila!")
                    return token
                body = await resp.text()
                logger.error(f"Token error {resp.status}: {body[:300]}")
                return None
    except Exception as e:
        logger.error(f"Token fetch fail: {e}")
        return None


# =============================================================================
# URL HELPERS
# =============================================================================
def extract_asin(url: str) -> str | None:
    if not url:
        return None
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9]{10}", url):
        return url.upper()
    m = ASIN_PAT.search(url)
    if m:
        return m.group(1).upper()
    q = re.search(r"[?&]ASIN=([A-Za-z0-9]{10})", url)
    if q:
        return q.group(1).upper()
    try:
        path = urllib.parse.urlparse(url).path.strip("/")
    except Exception:
        return None
    if re.fullmatch(r"[A-Za-z0-9]{10}", path):
        return path.upper()
    return None


def is_amazon_url(url: str) -> bool:
    """'amazon'/'amzn' poora domain label hona chahiye — fake domains pakde nahi jayenge."""
    try:
        host = urllib.parse.urlparse(url).netloc.lower().split(":")[0].strip(".")
    except Exception:
        return False
    if not host:
        return False
    if host in ("a.co", "www.a.co"):
        return True
    return any(lbl in ("amazon", "amzn") for lbl in host.split("."))


def is_amazon_search_url(url: str) -> bool:
    """Search/browse/deals page hai ya nahi. Caller pehle extract_asin() try kare."""
    if not url:
        return False
    low = url.lower()
    try:
        parsed = urllib.parse.urlparse(low)
        path   = parsed.path or ""
        query  = parsed.query or ""
    except Exception:
        path, query = low, ""

    for marker in SEARCH_MARKERS:
        if marker.endswith("="):
            if re.search(r"[?&]" + re.escape(marker), "?" + query):
                return True
        elif marker.startswith("/"):
            if marker.rstrip("?") in path or marker in low:
                return True
        elif marker in low:
            return True
    return False


def needs_redirect(url: str) -> bool:
    return any(d in url for d in NEEDS_REDIRECT)


def _strip_tag_param(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        params.pop("tag", None)
        return urllib.parse.urlunparse(
            parsed._replace(query=urllib.parse.urlencode(params, doseq=True))
        )
    except Exception:
        return url


def make_affiliate_url(asin: str) -> str:
    base = f"https://{MARKETPLACE}/dp/{asin}"
    return f"{base}?tag={PARTNER_TAG}" if PARTNER_TAG else base


def make_cart_url(asin: str) -> str:
    """
    Add-to-Cart link. Cart mein daalne se attribution window
    24 ghante se 89 din tak badh jaati hai.
    """
    if not asin:
        return ""
    return (
        f"https://{MARKETPLACE}/gp/aws/cart/add.html"
        f"?AssociateTag={urllib.parse.quote(PARTNER_TAG)}"
        f"&ASIN.1={asin}&Quantity.1=1"
    )


async def _resolve_redirect(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
            ) as resp:
                return str(resp.url)
    except Exception:
        return url


async def resolve_amazon_url(url: str) -> str:
    if needs_redirect(url):
        return await _resolve_redirect(url)
    return url


async def get_short_affiliate_link(url: str) -> str:
    asin = extract_asin(url)
    if not asin:
        asin = extract_asin(await _resolve_redirect(url))
    if asin:
        return make_affiliate_url(asin)
    cleaned = _strip_tag_param(url)
    if PARTNER_TAG:
        sep = "&" if "?" in cleaned else "?"
        return f"{cleaned}{sep}tag={PARTNER_TAG}"
    return cleaned


# =============================================================================
# RESPONSE PARSING
# =============================================================================
def _parse_item(item: dict) -> dict:
    r: dict = {}

    info = item.get("itemInfo") or {}

    title_data = info.get("title") or {}
    r["title"] = (title_data.get("displayValue") or "").strip()

    img_primary = (item.get("images") or {}).get("primary") or {}
    img = (img_primary.get("large") or img_primary.get("medium")
           or img_primary.get("small") or {})
    r["image_url"] = (img or {}).get("url", "") or ""

    # ── Brand ──────────────────────────────────────────────────────────────
    byline = info.get("byLineInfo") or {}
    brand  = (byline.get("brand") or {}).get("displayValue", "")
    if not brand:
        brand = (byline.get("manufacturer") or {}).get("displayValue", "")
    r["brand"] = (brand or "").strip()

    # ── Features ───────────────────────────────────────────────────────────
    feats = (info.get("features") or {}).get("displayValues") or []
    r["features"] = [f for f in feats if f]

    # ── Price block ────────────────────────────────────────────────────────
    r["deal_price"]    = ""
    r["actual_price"]  = ""
    r["deal_amount"]   = 0.0
    r["actual_amount"] = 0.0
    r["discount_pct"]  = 0
    r["savings"]       = ""
    r["stock_note"]    = ""
    r["seller"]        = ""
    r["deal_badge"]    = ""
    r["deal_ends"]     = ""

    listings = (item.get("offersV2") or {}).get("listings") or []
    if listings:
        listing   = listings[0]
        price_obj = listing.get("price") or {}
        money     = price_obj.get("money") or {}
        if money:
            r["deal_price"]  = money.get("displayAmount", "") or ""
            try:
                r["deal_amount"] = float(money.get("amount", 0) or 0)
            except (TypeError, ValueError):
                r["deal_amount"] = 0.0

        savings_obj = price_obj.get("savings") or {}
        sav_money   = savings_obj.get("money") or {}
        if sav_money:
            try:
                sav_amt = float(sav_money.get("amount", 0) or 0)
            except (TypeError, ValueError):
                sav_amt = 0.0
            r["savings"] = sav_money.get("displayAmount", "") or ""
            if sav_amt and r["deal_amount"]:
                mrp = r["deal_amount"] + sav_amt
                r["actual_amount"] = mrp
                r["actual_price"]  = f"₹{mrp:,.0f}"

        pct = savings_obj.get("percentage")
        if pct is not None:
            try:
                r["discount_pct"] = int(pct)
            except (TypeError, ValueError):
                pass
        elif r["deal_amount"] and r["actual_amount"]:
            try:
                r["discount_pct"] = round(
                    (r["actual_amount"] - r["deal_amount"]) / r["actual_amount"] * 100
                )
            except Exception:
                pass

        # ── Stock ──────────────────────────────────────────────────────────
        avail = listing.get("availability") or {}
        msg   = (avail.get("message") or "").strip()
        maxq  = avail.get("maxOrderQuantity")
        if msg:
            r["stock_note"] = msg
        elif isinstance(maxq, int) and 0 < maxq <= 10:
            r["stock_note"] = f"Sirf {maxq} bache hain"

        # ── Seller ─────────────────────────────────────────────────────────
        merch = listing.get("merchantInfo") or {}
        r["seller"] = (merch.get("name") or "").strip()

        # ── Deal details ───────────────────────────────────────────────────
        deal = listing.get("dealDetails") or {}
        badge = (deal.get("badge") or deal.get("dealBadge")
                 or deal.get("accessType") or "")
        if isinstance(badge, dict):
            badge = badge.get("displayValue", "") or badge.get("label", "")
        badge = str(badge or "").replace("_", " ").strip()
        if badge:
            r["deal_badge"] = badge.title() if badge.isupper() else badge

        ends = deal.get("endTime") or deal.get("endDate") or ""
        if ends:
            r["deal_ends"] = _humanise_deal_end(str(ends))

        if deal.get("percentClaimed") is not None and not r["deal_ends"]:
            try:
                claimed = int(deal["percentClaimed"])
                if claimed > 0:
                    r["deal_ends"] = f"{claimed}% claimed"
            except (TypeError, ValueError):
                pass

    # ── Sales rank ─────────────────────────────────────────────────────────
    bni  = item.get("browseNodeInfo") or {}
    rank = bni.get("websiteSalesRank") or {}
    r["sales_rank"] = ""
    if rank:
        num = rank.get("salesRank")
        cat = (rank.get("contextFreeName") or rank.get("displayName") or "").strip()
        if num and cat:
            r["sales_rank"] = f"#{num:,} in {cat}"

    nodes = bni.get("browseNodes") or []
    r["category"] = ""
    if nodes:
        r["category"] = (nodes[0].get("contextFreeName")
                         or nodes[0].get("displayName") or "").strip()

    # ── Reviews ────────────────────────────────────────────────────────────
    cr   = item.get("customerReviews") or {}
    star = cr.get("starRating") or {}
    r["rating"] = str(star.get("value", "")).strip() if star else ""
    count       = cr.get("count")
    r["review_count"] = f"{count:,}" if isinstance(count, int) else str(count or "")

    # ── Amazon ka apna tagged link (hamare banaye se behtar) ───────────────
    r["detail_url"] = item.get("detailPageURL", "") or ""

    return r


def _humanise_deal_end(raw: str) -> str:
    """ISO timestamp ko 'X ghante baaki' mein badlo."""
    try:
        cleaned = raw.replace("Z", "+00:00")
        end     = datetime.fromisoformat(cleaned)
        if end.tzinfo:
            from datetime import timezone
            now = datetime.now(timezone.utc)
        else:
            now = datetime.now()
        secs = (end - now).total_seconds()
        if secs <= 0:
            return "khatam"
        hours = int(secs // 3600)
        mins  = int((secs % 3600) // 60)
        if hours >= 1:
            return f"{hours} ghante baaki"
        return f"{mins} minute baaki"
    except Exception:
        return ""


# =============================================================================
# API CALLS
# =============================================================================
async def _call_get_items(asins: list) -> dict:
    """Ek call, max 10 ASIN. Returns {asin: parsed_product}."""
    token = await _get_token()
    if not token:
        return {}

    payload = {
        "itemIds":    asins,
        "itemIdType": "ASIN",
        "marketplace": MARKETPLACE,
        "partnerTag": PARTNER_TAG,
        "resources":  PRODUCT_RESOURCES,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ITEMS_EP, json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-marketplace": MARKETPLACE,
                    "Content-Type":  "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=25),
            ) as resp:
                if resp.status == 403:
                    _token_cache["token"]      = None
                    _token_cache["expires_at"] = None
                    logger.error("Amazon API 403 — token invalidated")
                    return {}
                if resp.status not in (200, 206):
                    body = await resp.text()
                    logger.error(f"GetItems {resp.status}: {body[:300]}")
                    return {}
                data = await resp.json()
    except Exception as e:
        logger.error(f"GetItems call fail: {e}")
        return {}

    # Docs do naam dikhate hain — dono accept karo, warna chup-chaap fail hoga
    container = data.get("itemsResult") or data.get("itemResults") or {}
    items     = container.get("items") or []

    out = {}
    for item in items:
        asin = (item.get("asin") or "").upper()
        if not asin:
            continue
        parsed = _parse_item(item)
        parsed["asin"] = asin
        # Saaf link — ?tag=... bas. Amazon ka detailPageURL mein
        # linkCode/th/psc jaisa kachra hota hai, wo nahi chahiye.
        parsed["affiliate_link"] = make_affiliate_url(asin)
        parsed["cart_link"]      = make_cart_url(asin)
        out[asin] = parsed

    for err in (data.get("errors") or []):
        logger.warning(f"GetItems error: {err.get('code')} — {err.get('message', '')[:120]}")

    return out


async def get_products_by_asins(asins: list) -> dict:
    """Kai ASIN ka data lao — 10-10 ke batch mein. Returns {asin: product}."""
    uniq = []
    seen = set()
    for a in asins:
        a = (a or "").upper()
        if a and a not in seen:
            seen.add(a)
            uniq.append(a)

    result = {}
    for i in range(0, len(uniq), MAX_ASINS_PER_CALL):
        chunk = uniq[i:i + MAX_ASINS_PER_CALL]
        result.update(await _call_get_items(chunk))
    logger.info(f"Amazon API: {len(uniq)} ASIN maange, {len(result)} mile")
    return result


async def get_product_by_asin(asin: str) -> dict | None:
    got = await get_products_by_asins([asin])
    return got.get((asin or "").upper())


async def enrich_amazon_url(url: str) -> dict | None:
    resolved = await resolve_amazon_url(url)
    asin = extract_asin(resolved) or extract_asin(url)
    if asin:
        return await get_product_by_asin(asin)
    logger.warning(f"ASIN nahi mila: {url[:80]}")
    return None
