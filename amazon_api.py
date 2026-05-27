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

# PARTNER_TAG: Railway variable se aata hai — koi hardcoded default nahi
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

ASIN_PAT = re.compile(r"/(?:dp|gp/product|exec/obidos/ASIN|o/ASIN)/([A-Za-z0-9]{10})")

_token_cache: dict = {"token": None, "expires_at": None}

PRODUCT_RESOURCES = [
    "images.primary.large",
    "images.primary.medium",
    "itemInfo.title",
    "offersV2.listings.price",
    "offersV2.listings.availability",
    "offersV2.listings.condition",
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

    # v3.x (LWA) → JSON body  |  v2.x (Cognito) → form-encoded
    # Source: Amazon Creators API SDK oauth2_token_manager.py
    is_lwa = CREDENTIAL_VERSION.startswith("3.")
    token_payload = {
        "grant_type":    "client_credentials",
        "client_id":     CREDENTIAL_ID,
        "client_secret": CREDENTIAL_SECRET,
        "scope":         SCOPE,
    }

    try:
        async with aiohttp.ClientSession() as session:
            if is_lwa:
                req = session.post(
                    token_url,
                    json=token_payload,                          # LWA needs JSON
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=15),
                )
            else:
                req = session.post(
                    token_url,
                    data=token_payload,                          # Cognito needs form-encoded
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


def extract_asin(url: str) -> str | None:
    url = url.strip()
    if re.fullmatch(r"[A-Za-z0-9]{10}", url):
        return url.upper()
    m = ASIN_PAT.search(url)
    if m:
        return m.group(1).upper()
    q = re.search(r"[?&]ASIN=([A-Za-z0-9]{10})", url)
    if q:
        return q.group(1).upper()
    return None


def is_amazon_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return "amazon" in host or "amzn" in host


def is_amazon_search_url(url: str) -> bool:
    markers = ["/s?", "/s/", "field-keywords", "/b?", "node=", "/deals", "/gp/browse"]
    return any(m in url for m in markers)


def _strip_tag_param(url: str) -> str:
    """
    Kisi bhi existing affiliate tag= parameter ko URL se remove karo.
    Ensures sirf ek hi clean tag lagta hai — purana kisi aur ka tag nahi rahega.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        params.pop("tag", None)
        new_query = urllib.parse.urlencode(params, doseq=True)
        return urllib.parse.urlunparse(parsed._replace(query=new_query))
    except Exception:
        return url


def make_affiliate_url(asin: str) -> str:
    """
    Clean affiliate URL — sirf ASIN aur PARTNER_TAG, kuch aur nahi.
    MARKETPLACE env var se domain aata hai.
    """
    base = f"https://{MARKETPLACE}/dp/{asin}"
    if PARTNER_TAG:
        return f"{base}?tag={PARTNER_TAG}"
    return base


async def _resolve_redirect(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
            ) as resp:
                return str(resp.url)
    except Exception:
        return url


async def get_short_affiliate_link(url: str) -> str:
    """
    Amazon URL ko clean affiliate link mein convert karo.
    - ASIN milne pe: ekdum clean URL (https://domain/dp/ASIN?tag=TAG)
    - ASIN nahi milne pe: existing tag strip karke sirf apna tag add karo
    """
    asin = extract_asin(url)
    if not asin:
        resolved = await _resolve_redirect(url)
        asin = extract_asin(resolved)

    if asin:
        # Best case: clean ASIN-based URL
        return make_affiliate_url(asin)

    # Fallback: strip any old tag, add ours
    cleaned = _strip_tag_param(url)
    if PARTNER_TAG:
        sep = "&" if "?" in cleaned else "?"
        return f"{cleaned}{sep}tag={PARTNER_TAG}"
    return cleaned


def _parse_item(item: dict) -> dict:
    result: dict = {}

    title_data    = item.get("itemInfo", {}).get("title", {})
    result["title"] = (title_data.get("displayValue", "") if title_data else "").strip()

    img_primary = item.get("images", {}).get("primary", {})
    img = img_primary.get("large") or img_primary.get("medium") or img_primary.get("small") or {}
    result["image_url"] = img.get("url", "") if img else ""

    result["deal_price"]    = ""
    result["actual_price"]  = ""
    result["deal_amount"]   = 0.0
    result["actual_amount"] = 0.0
    result["discount_pct"]  = 0
    result["savings"]       = ""

    listings = item.get("offersV2", {}).get("listings", [])
    if listings:
        listing   = listings[0]
        price_obj = listing.get("price", {})
        money     = price_obj.get("money", {})
        if money:
            result["deal_price"]  = money.get("displayAmount", "")
            result["deal_amount"] = float(money.get("amount", 0) or 0)

        savings_obj = price_obj.get("savings", {})
        sav_money   = savings_obj.get("money", {})
        if sav_money:
            sav_amt = float(sav_money.get("amount", 0) or 0)
            result["savings"] = sav_money.get("displayAmount", "")
            if sav_amt and result["deal_amount"]:
                mrp_amt                 = result["deal_amount"] + sav_amt
                result["actual_amount"] = mrp_amt
                result["actual_price"]  = f"₹{mrp_amt:,.0f}"

        pct = savings_obj.get("percentage")
        if pct is not None:
            result["discount_pct"] = int(pct)
        elif result["deal_amount"] and result["actual_amount"]:
            try:
                result["discount_pct"] = round(
                    (result["actual_amount"] - result["deal_amount"]) / result["actual_amount"] * 100
                )
            except Exception:
                pass

    cr   = item.get("customerReviews", {})
    star = cr.get("starRating", {})
    result["rating"]       = str(star.get("value", "")).strip() if star else ""
    count                  = cr.get("count")
    result["review_count"] = f"{count:,}" if isinstance(count, int) else str(count or "")

    return result


async def get_product_by_asin(asin: str) -> dict | None:
    token = await _get_token()
    if not token:
        return None

    payload = {
        "partnerTag": PARTNER_TAG,
        "itemIds":    [asin],
        "resources":  PRODUCT_RESOURCES,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                ITEMS_EP,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "x-marketplace": MARKETPLACE,
                    "Content-Type":  "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 403:
                    _token_cache["token"]      = None
                    _token_cache["expires_at"] = None
                    logger.error("Amazon API 403 — token invalidated")
                    return None
                if resp.status not in (200, 206):
                    body = await resp.text()
                    logger.error(f"GetItems {resp.status}: {body[:300]}")
                    return None
                data  = await resp.json()
                items = data.get("itemsResult", {}).get("items", [])
                if items:
                    parsed = _parse_item(items[0])
                    parsed["asin"]           = asin
                    parsed["affiliate_link"] = make_affiliate_url(asin)
                    logger.info(f"Creators API product mila: {asin}")
                    return parsed
                errors = data.get("errors", [])
                msg    = errors[0].get("message", "") if errors else "Product not found"
                logger.warning(f"ASIN {asin} — {msg}")
                return None
    except Exception as e:
        logger.error(f"GetItems call fail: {e}")
        return None


async def enrich_amazon_url(url: str) -> dict | None:
    resolved = url
    if "amzn.to" in url or "amzn.in" in url:
        resolved = await _resolve_redirect(url)

    asin = extract_asin(resolved)
    if not asin:
        asin = extract_asin(url)
    if asin:
        return await get_product_by_asin(asin)
    logger.warning(f"ASIN nahi mila: {url[:80]}")
    return None
