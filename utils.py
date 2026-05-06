import re
from urllib.parse import urlparse

URL_REGEX = re.compile(r'https?://[^\s\]\[<>"\']+')

PLATFORM_MAP = {
    "amazon.in":          "Amazon",
    "amazon.com":         "Amazon",
    "amzn.in":            "Amazon",
    "amzn.to":            "Amazon",
    "flipkart.com":       "Flipkart",
    "fkrt.it":            "Flipkart",
    "dl.flipkart.com":    "Flipkart",
    "shopsy.in":          "Shopsy",
    "myntra.com":         "Myntra",
    "ajio.com":           "Ajio",
    "meesho.com":         "Meesho",
    "nykaa.com":          "Nykaa",
    "nykaafashion.com":   "Nykaa",
    "snapdeal.com":       "Snapdeal",
    "jiomart.com":        "JioMart",
    "tatacliq.com":       "TataCliq",
    "croma.com":          "Croma",
    "reliancedigital.in": "Reliance Digital",
}


def extract_urls(text: str) -> list:
    if not text:
        return []
    return URL_REGEX.findall(text)


def detect_platform(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().replace("www.", "")
        for domain, name in PLATFORM_MAP.items():
            if domain in host:
                return name
    except Exception:
        pass
    return "Other"
