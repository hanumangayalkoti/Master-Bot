import os
import re
import logging
import aiohttp
import urllib.parse
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

AMAZON_CLIENT_ID     = os.getenv("AMAZON_CLIENT_ID", "")
AMAZON_CLIENT_SECRET = os.getenv("AMAZON_CLIENT_SECRET", "")
ASSOCIATE_TAG        = "dealskoti-21"
PAAPI_HOST           = "webservices.amazon.in"
LWA_TOKEN_URL        = "https://api.amazon.com/auth/o2/token"

_token_cache = {"token": None, "expires_at": None}


# =============================================================================
# AUTH — OAuth token
# =============================================================================
async def _get_access_token() -> str | None:
    global _token_cache
    now = datetime.utcnow()
    if _token_cache["token"] and _token_cache["expires_at"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    if not AMAZON_CLIENT_ID or not AMAZON_CLIENT_SECRET:
        logger.error("AMAZON_CLIENT_ID ya AMAZON_CLIENT_SECRET set nahi hai")
        return None

    try:
        async with aiohttp.ClientSession() as session:
            data = {
                "grant_type":    "client_credentials",
                "client_id":     AMAZON_CLIENT_ID,
                "client_secret": AMAZON_CLIENT_SECRET,
                "scope":         "productads",
            }
            async with session.post(
                LWA_TOKEN_URL, data=data, timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    token      = result.get("access_token")
                    expires_in = result.get("expires_in", 3600)
                    _token_cache["token"]      = token
                    _token_cache["expires_at"] = now + timedelta(seconds=expires_in - 60)
                    logger.info("Amazon OAuth token mila!")
                    return token
                body = await resp.text()
                logger.error(f"Token error {resp.status}: {body[:300]}")
                return None
    except Exception as e:
        logger.error(f"Token fetch fail: {e}")
        return None


# =============================================================================
# HELPERS
# =============================================================================
def extract_asin(url: str) -> str | None:
    patterns = [
        r"/dp/([A-Z0-9]{10})",
        r"/gp/product/([A-Z0-9]{10})",
        r"ASIN=([A-Z0-9]{10})",
        r"%2Fdp%2F([A-Z0-9]{10})",
    ]
    for p in patterns:
        m = re.search(p, url)
        if m:
            return m.group(1)
    return None


def is_amazon_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).netloc.lower()
    return "amazon" in host or "amzn" in host


def make_affiliate_url(asin: str) -> str:
    return f"https://www.amazon.in/dp/{asin}?tag={ASSOCIATE_TAG}"


async def make_short_link(long_url: str) -> str:
    """TinyURL se short link banao."""
    try:
        encoded = urllib.parse.quote(long_url, safe="")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://tinyurl.com/api-create.php?url={encoded}",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    short = (await resp.text()).strip()
                    if short.startswith("http"):
                        return short
    except Exception as e:
        logger.warning(f"TinyURL fail, long URL use kar raha hoon: {e}")
    return long_url


async def get_short_affiliate_link(url: str) -> str:
    """
    Amazon URL → affiliate URL → TinyURL short link.
    ASIN nahi mila to original URL return karo.
    """
    asin = extract_asin(url)
    if not asin:
        try:
            resolved = await _resolve_redirect(url)
            asin = extract_asin(resolved)
        except Exception:
            pass
    if asin:
        affiliate = make_affiliate_url(asin)
        return await make_short_link(affiliate)
    return url


async def _resolve_redirect(url: str) -> str:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"User-Agent": "Mozilla/5.0"},
            ) as resp:
                return str(resp.url)
    except Exception:
        return url


# =============================================================================
# PA API — Product data
# =============================================================================
def _parse_item(item: dict) -> dict:
    result = {}

    result["title"] = (
        item.get("ItemInfo", {}).get("Title", {}).get("DisplayValue", "")
    )

    listings = item.get("Offers", {}).get("Listings", [])
    if listings:
        listing   = listings[0]
        price_obj = listing.get("Price", {})
        basis_obj = listing.get("SavingBasis", {})
        saving_obj = listing.get("Saving", {})

        result["deal_price"]   = price_obj.get("DisplayAmount", "")
        result["deal_amount"]  = price_obj.get("Amount", 0)
        result["actual_price"] = basis_obj.get("DisplayAmount", "") or result["deal_price"]
        result["actual_amount"]= basis_obj.get("Amount", 0) or result["deal_amount"]

        perc = saving_obj.get("Percentage", 0)
        if not perc and result["actual_amount"] and result["deal_amount"]:
            try:
                perc = round(
                    (result["actual_amount"] - result["deal_amount"])
                    / result["actual_amount"] * 100
                )
            except Exception:
                perc = 0
        result["discount_pct"] = perc
    else:
        result["deal_price"] = result["actual_price"] = ""
        result["deal_amount"] = result["actual_amount"] = 0
        result["discount_pct"] = 0

    images = item.get("Images", {}).get("Primary", {})
    result["image_url"] = images.get("Large", {}).get("URL", "")

    reviews = item.get("CustomerReviews", {})
    result["rating"]       = reviews.get("StarRating", {}).get("DisplayValue", "")
    result["review_count"] = reviews.get("Count", {}).get("DisplayValue", "")

    result["asin"] = item.get("ASIN", "")
    return result


async def get_product_by_asin(asin: str) -> dict | None:
    token = await _get_access_token()
    if not token:
        return None

    headers = {
        "Authorization":         f"Bearer {token}",
        "Content-Type":          "application/json",
        "x-amzn-associate-tag": ASSOCIATE_TAG,
    }
    payload = {
        "ItemIds": [asin],
        "Resources": [
            "ItemInfo.Title",
            "Offers.Listings.Price",
            "Offers.Listings.SavingBasis",
            "Offers.Listings.Saving",
            "Images.Primary.Large",
            "CustomerReviews.StarRating",
            "CustomerReviews.Count",
        ],
        "PartnerTag":  ASSOCIATE_TAG,
        "PartnerType": "Associates",
        "Marketplace": "www.amazon.in",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://{PAAPI_HOST}/paapi5/getitems",
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 200:
                    data  = await resp.json()
                    items = data.get("ItemsResult", {}).get("Items", [])
                    if items:
                        logger.info(f"PA API product mila: ASIN {asin}")
                        return _parse_item(items[0])
                    logger.warning(f"ASIN {asin} — koi item nahi mila")
                    return None
                body = await resp.text()
                logger.error(f"GetItems {resp.status}: {body[:300]}")
                return None
    except Exception as e:
        logger.error(f"GetItems call fail: {e}")
        return None


async def enrich_amazon_url(url: str) -> dict | None:
    """URL → ASIN → PA API product data."""
    asin = extract_asin(url)
    if not asin:
        resolved = await _resolve_redirect(url)
        asin = extract_asin(resolved)
    if asin:
        return await get_product_by_asin(asin)
    logger.warning(f"ASIN nahi mila: {url[:80]}")
    return None
