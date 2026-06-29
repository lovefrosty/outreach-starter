#!/usr/bin/env python3
"""
c2_scraper.py — C2 Website Scraper node.

Fetches the lead's homepage + common sub-pages, then extracts:
  - On-site emails (regex, deduped, image-asset false-positives dropped)
  - Owner name (heuristic scan near ownership keywords on /about or /team)
  - Processor detection (fingerprint scan → sets `processor` + `tech_signals`)
  - Testimonials / reviews found on site → `reviews` table

Input stage:  'pulled'
Output stage: 'scraped'

Stdlib only. Never crashes out of process().
"""

import sys
import os
import re
import json
import html
import ssl
import time
import hashlib
import logging
import http.client
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urljoin, urlparse

# Small-business sites routinely have expired/self-signed certs. We are only
# reading public marketing HTML (no credentials), so an unverified retry is
# the correct trade-off — otherwise we lose a large fraction of real leads.
_UNVERIFIED_SSL = ssl._create_unverified_context()

# ── Path boilerplate (importable by orchestrator AND runnable standalone) ────
_PIPE = Path(__file__).resolve().parents[1]   # pipeline/
sys.path.insert(0, str(_PIPE))                # import ledger
sys.path.insert(0, str(_PIPE / "sources"))    # import base (for extract_domain)

import ledger as L
from base import extract_domain  # noqa: E402

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [c2_scraper] %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("c2_scraper")

# ── Constants ────────────────────────────────────────────────────────────────
TIMEOUT = 10          # seconds per request
MAX_BYTES = 1_048_576  # 1 MB cap on response body
CACHE_DIR = os.environ.get("C2_HTML_CACHE_DIR", "").strip()
CACHE_TTL_SECONDS = int(os.environ.get("C2_HTML_CACHE_TTL_SECONDS", "604800"))
_CACHE_MISS_SENTINEL = "__C2_FETCH_FAILED__"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# Sub-paths to try after the homepage. Dynamic discovery below adds sitemap and
# same-domain links; these fixed paths are just the cheap baseline.
SUBPATHS = [
    "/contact", "/about", "/team", "/staff", "/our-team", "/about-us",
    "/contact-us", "/locations", "/location", "/hours", "/menu",
]
MAX_DISCOVERED_URLS = 10

# ── Processor fingerprints ────────────────────────────────────────────────────
# Each entry: list of lowercase substrings that, if found in page HTML,
# indicate that processor.  First/strongest match sets `processor`; ALL
# matches end up in `tech_signals`.
PROCESSOR_FINGERPRINTS = [
    ("Stripe",            ["js.stripe.com", "checkout.stripe.com", "buy.stripe.com", "stripe(", "data-stripe"]),
    ("Square",            ["squareup.com", "square.site", "square-", "web.squarecdn.com", "sqpaymentform"]),
    ("Toast",             ["toasttab.com", "order.toasttab.com", "toast-", "toast mobile order"]),
    ("Clover",            ["clover.com", "clover-", "powered by clover"]),
    ("Shopify Payments",  ["cdn.shopify.com", "shopify.checkout", "shopifycdn"]),
    ("Lightspeed",        ["lightspeed", "order-anywhere", "order anywhere"]),
    ("PayPal",            ["paypal.com/sdk", "paypalobjects", "paypal.me", "paypal zettle"]),
    ("Braintree",         ["braintreegateway"]),
    ("Authorize.net",     ["authorize.net"]),
    ("Heartland",         ["heartland"]),
    ("SpotOn",            ["spoton.com"]),
    ("TouchBistro",       ["touchbistro"]),
    ("Revel",             ["revelsystems", "revel systems"]),
    ("Upserve",           ["upserve"]),
    ("PrimeRx",           ["primerx"]),
    ("BestRx",            ["bestrx"]),
    ("Liberty",           ["liberty software", "libertysoftware.com"]),
    ("PioneerRx",         ["pioneerrx"]),
    ("Rx30",              ["rx30"]),
    ("QS/1",              ["qs/1", "qs1"]),
    ("Computer-Rx",       ["computer-rx", "computer rx"]),
    ("CDK",               ["cdk simplepay", "cdk epayments"]),
    ("Dealertrack",       ["dealertrack payment"]),
    ("Tekion",            ["tekion pay"]),
    ("Kimoby",            ["kimoby"]),
    ("DealerPay",         ["dealerpay"]),
    ("Aloha",             ["aloha"]),
]

# ── Email regex ───────────────────────────────────────────────────────────────
# Matches standard email patterns.  We post-filter obvious false positives.
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)

# Extensions that indicate the "email" is actually an image path or similar
_IMAGE_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|ico|bmp|tiff?|avif)$", re.I
)

# Generic / role-based local parts we DON'T want as candidates from site
# (they're too noisy; C4 can still try them as patterns).
_GENERIC_LOCAL = frozenset([
    "info", "contact", "hello", "support", "admin", "sales",
    "noreply", "no-reply", "webmaster", "postmaster", "abuse",
    "privacy", "legal", "billing", "careers", "jobs", "press",
    "media", "team", "service", "help", "feedback",
    # role inboxes common on restaurant/SMB sites
    "office", "events", "event", "reservations", "reservation",
    "order", "orders", "catering", "marketing", "accounting",
    "accounts", "hr", "general", "booking", "bookings",
])

# ── Owner-name heuristic ──────────────────────────────────────────────────────
# Pattern: "Owner/Founder/etc. [is/,/-] Firstname Lastname" or
#          "Firstname Lastname [,/-] Owner/Founder/etc."
# We look in about/team pages; fall back to homepage text.
#
# IMPORTANT: do NOT use re.I on these patterns.  re.I turns [A-Z] into [a-zA-Z],
# which makes the name pattern match common lowercase words.  The ownership
# keywords are written in lowercase so they match regardless of case in text that
# has been lowercased before matching (see _extract_owner below).
_OWNERSHIP_WORDS = r"(?:owner|founder|co-founder|president|proprietor|principal|managing\s+(?:partner|director)|ceo|chief\s+executive)"
_NAME_PAT = r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}"   # 2–4 Title-Case words

# We scan the ORIGINAL (mixed-case) text but lower-case only the keyword part
# by keeping two separate patterns: one for keyword-then-name, one for name-then-keyword.
# The keyword half uses (?i) inline flag so it is case-insensitive; the name half
# stays case-sensitive so [A-Z] truly requires a capital letter.
_OWNER_BEFORE = re.compile(
    r"(?i:\b" + _OWNERSHIP_WORDS + r"\b)[\s:,\-–]{0,4}(" + _NAME_PAT + r")",
    re.DOTALL,
)
_OWNER_AFTER = re.compile(
    r"\b(" + _NAME_PAT + r")[\s,\-–]{0,4}(?i:\b" + _OWNERSHIP_WORDS + r"\b)",
    re.DOTALL,
)
# Also catch "Name is the Owner/Role" — e.g. "Maria Rodriguez is the President"
_OWNER_IS = re.compile(
    r"\b(" + _NAME_PAT + r")\s+(?:is\s+(?:the\s+)?)(?i:\b" + _OWNERSHIP_WORDS + r"\b)",
    re.DOTALL,
)

# ── Testimonial/review heuristic ─────────────────────────────────────────────
# Look for blockquote, .testimonial, .review divs, or paragraph clusters near
# star/rating indicators.  Best-effort: grab any quoted text >40 chars.
_REVIEW_RE = re.compile(
    r'(?:<blockquote[^>]*>|class=["\'][^"\']*(?:testimonial|review|quote)[^"\']*["\'][^>]*>)'
    r'.*?<(?:/blockquote|p|div)',
    re.I | re.DOTALL,
)
_QUOTED_TEXT_RE = re.compile(r'"([^"]{40,300})"')
_HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
_MAILTO_RE = re.compile(r"""href=["']mailto:([^"'?]+)""", re.I)
_JSONLD_RE = re.compile(
    r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.I | re.DOTALL,
)
_LOC_RE = re.compile(r"<loc>\s*([^<]+)\s*</loc>", re.I)
_DISCOVERY_WORDS = (
    "contact", "about", "team", "staff", "people", "owner", "location",
    "hours", "menu", "order", "catering", "events", "reservations",
    "careers", "jobs", "employment", "hiring", "faq", "pricing", "plans",
    "book", "booking", "schedule", "service", "financing", "portal",
)
_DOCUMENT_EXT_RE = re.compile(r"\.(?:pdf|docx?|xlsx?)(?:$|\?)", re.I)
_SOCIAL_DOMAINS = {
    "facebook.com": "social:facebook",
    "instagram.com": "social:instagram",
    "linkedin.com": "social:linkedin",
    "twitter.com": "social:twitter",
    "x.com": "social:x",
    "youtube.com": "social:youtube",
    "tiktok.com": "social:tiktok",
}
_SOCIAL_URL_PREFIX = "social_url:"
_PUBLIC_DOC_URL_PREFIX = "public_doc_url:"

# ── Utility helpers ───────────────────────────────────────────────────────────

def _fetch_page(url: str):
    """
    Fetch `url`, return decoded text (up to MAX_BYTES).
    Returns None on any error.  Never raises.
    """
    cache_path = None
    if CACHE_DIR:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_path = Path(CACHE_DIR) / f"{digest}.html"
        try:
            if cache_path.exists() and time.time() - cache_path.stat().st_mtime <= CACHE_TTL_SECONDS:
                cached = cache_path.read_text(errors="replace")
                return None if cached == _CACHE_MISS_SENTINEL else cached
        except OSError:
            cache_path = None

    # Try verified TLS first; on ANY SSL problem retry once with an unverified
    # context (expired/self-signed certs are common on small-biz sites).
    for ctx in (None, _UNVERIFIED_SSL):
        try:
            req = Request(url, headers={"User-Agent": USER_AGENT})
            kw = {"timeout": TIMEOUT}
            if ctx is not None:
                kw["context"] = ctx
            with urlopen(req, **kw) as resp:
                raw = resp.read(MAX_BYTES)
                ct = resp.headers.get_content_charset("utf-8") if hasattr(resp.headers, "get_content_charset") else "utf-8"
            text = raw.decode(ct, errors="replace")
            if cache_path is not None:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(text)
                except OSError:
                    pass
            return text
        except ssl.SSLError as exc:
            log.debug("fetch %s SSL → %s (retry unverified)", url, exc)
            continue
        except (URLError, HTTPError, OSError, ValueError, http.client.InvalidURL) as exc:
            # URLError often wraps the real SSLError in .reason
            if ctx is None and isinstance(getattr(exc, "reason", None), ssl.SSLError):
                continue
            log.debug("fetch %s → %s", url, exc)
            if cache_path is not None:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(_CACHE_MISS_SENTINEL)
                except OSError:
                    pass
            return None
    if cache_path is not None:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(_CACHE_MISS_SENTINEL)
        except OSError:
            pass
    return None


def _strip_tags(text: str) -> str:
    """Crude tag stripper — no parser needed for SMB sites."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _build_urls(website: str) -> list[str]:
    """Return list of URLs to try: homepage + common sub-paths."""
    if not website:
        return []
    parsed = urlparse(website)
    if not parsed.scheme:
        website = "https://" + website
    # Always try https first, then http if we have no scheme
    base = website.rstrip("/")
    return [base] + [base + p for p in SUBPATHS]


def _same_root(url: str, domain: str) -> bool:
    host = urlparse(url).netloc.lower()
    return bool(host and _root_domain(host) == _root_domain(domain))


def _interesting_path(url: str) -> bool:
    path = (urlparse(url).path or "").lower()
    return any(word in path for word in _DISCOVERY_WORDS)


def _discover_same_domain_links(html_text: str, base_url: str, domain: str) -> list[str]:
    """Find useful same-domain links from fetched HTML. No crawling yet."""
    out = []
    seen = set()
    for href in _HREF_RE.findall(html_text or ""):
        if href.startswith(("#", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, html.unescape(href)).split("#", 1)[0].rstrip("/")
        if any(char.isspace() for char in url):
            continue
        if not url.startswith(("http://", "https://")):
            continue
        if not _same_root(url, domain) or not _interesting_path(url):
            continue
        if url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= MAX_DISCOVERED_URLS:
            break
    return out


def _sitemap_urls(website: str, domain: str) -> list[str]:
    """Fetch /sitemap.xml and return useful same-domain URLs."""
    parsed = urlparse(website if urlparse(website).scheme else "https://" + website)
    base = f"{parsed.scheme}://{parsed.netloc}"
    sitemap = _fetch_page(base.rstrip("/") + "/sitemap.xml")
    if not sitemap:
        return []
    out = []
    seen = set()
    for raw in _LOC_RE.findall(sitemap):
        url = html.unescape(raw.strip()).split("#", 1)[0].rstrip("/")
        if _same_root(url, domain) and _interesting_path(url) and url not in seen:
            seen.add(url)
            out.append(url)
        if len(out) >= MAX_DISCOVERED_URLS:
            break
    return out


# Personal webmail that plausibly belongs to a small business owner.
_WEBMAIL = {"gmail.com", "yahoo.com", "aol.com", "hotmail.com", "outlook.com",
            "icloud.com", "comcast.net", "verizon.net", "me.com", "msn.com", "live.com"}
# Template placeholders + analytics/error-tracking + builder demo addresses.
_PLACEHOLDER_EMAILS = {
    "user@domain.com", "your@email.com", "youremail@domain.com", "email@example.com",
    "name@example.com", "john@doe.com", "john.doe@example.com", "example@example.com",
    "yourname@email.com", "firstname.lastname@example.com", "sentry@sentry.io",
    "info@yourdomain.com", "email@domain.com", "name@domain.com",
}
_PLACEHOLDER_DOMAINS = {
    "domain.com", "example.com", "example.org", "example.net", "email.com",
    "sentry.io", "sentry.wixpress.com", "wixpress.com", "yourdomain.com",
    "yoursite.com", "test.com", "sentry-next.wixpress.com",
}
_HEX_LOCAL_RE = re.compile(r"^[0-9a-f]{16,}$")   # Sentry-style DSN local parts


def _root_domain(d: str) -> str:
    d = (d or "").lower().lstrip(".")
    if d.startswith("www."):
        d = d[4:]
    parts = d.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else d


def _extract_emails(html_text: str, domain: str) -> list[str]:
    """
    Deduplicated, JUNK-FILTERED on-site emails. Drops: image false-positives,
    role/generic addresses, template placeholders (user@domain.com), analytics
    DSNs (hex@sentry.io), and — critically — third-party domains. An on-site
    email is only kept if its domain MATCHES the business domain or is personal
    webmail; vendor/platform/hotel addresses (hyatt.com, getforky.com…) are not
    this business's contact. Own-domain emails rank ahead of webmail.
    """
    found = _EMAIL_RE.findall(html_text)
    found.extend(html.unescape(x).strip() for x in _MAILTO_RE.findall(html_text or ""))
    biz_root = _root_domain(domain) if domain else ""
    seen = {}
    for email in found:
        email = email.lower().strip(".,;:\"'()")
        if _IMAGE_EXT_RE.search(email):
            continue
        local, _, edomain = email.partition("@")
        if "." not in edomain:
            continue
        if local in _GENERIC_LOCAL:
            continue
        if email in _PLACEHOLDER_EMAILS or edomain in _PLACEHOLDER_DOMAINS:
            continue
        if _HEX_LOCAL_RE.match(local):
            continue
        eroot = _root_domain(edomain)
        if eroot in _WEBMAIL:
            pass                                   # plausibly the owner's webmail
        elif biz_root and eroot == biz_root:
            pass                                   # on their own domain — best
        else:
            continue                               # third-party — not their contact
        seen[email] = True
    # own-domain first, webmail second
    return sorted(seen.keys(),
                  key=lambda e: 0 if (biz_root and _root_domain(e.split("@", 1)[1]) == biz_root) else 1)


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _jsonld_signals(all_html: str) -> dict:
    """Extract conservative Organization/Person facts from JSON-LD."""
    emails, people, same_as, org_types = [], [], [], []
    for raw in _JSONLD_RE.findall(all_html or ""):
        text = html.unescape(raw).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        for obj in _walk_json(data):
            typ = obj.get("@type") or obj.get("type")
            types = typ if isinstance(typ, list) else [typ]
            types = [str(t) for t in types if t]
            org_types.extend(types)
            email = obj.get("email")
            if isinstance(email, str):
                emails.append(email)
            name = obj.get("name")
            if name and any(t.lower() in {"person", "founder", "employee"} for t in types):
                people.append(str(name))
            for key in ("founder", "employee", "member"):
                value = obj.get(key)
                for child in _walk_json(value):
                    child_name = child.get("name") if isinstance(child, dict) else None
                    if child_name:
                        people.append(str(child_name))
            same = obj.get("sameAs")
            if isinstance(same, str):
                same_as.append(same)
            elif isinstance(same, list):
                same_as.extend(str(x) for x in same if x)
    return {
        "emails": sorted(set(e.strip().lower() for e in emails if e)),
        "people": sorted(set(p.strip() for p in people if p)),
        "same_as": sorted(set(same_as)),
        "types": sorted(set(org_types)),
    }


def _normalize_social_url(url: str) -> str:
    url = html.unescape((url or "").strip())
    if url.startswith("//"):
        url = "https:" + url
    return url.split("#", 1)[0].rstrip("/")


def _host_matches_domain(host: str, domain: str) -> bool:
    host = (host or "").lower().lstrip(".")
    domain = (domain or "").lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def _social_links(all_html: str, jsonld: dict) -> list[str]:
    """Return compact social URL facts from page anchors and JSON-LD sameAs."""
    candidates = []
    candidates.extend(jsonld.get("same_as") or [])
    candidates.extend(_HREF_RE.findall(all_html or ""))

    out = []
    seen = set()
    for raw in candidates:
        url = _normalize_social_url(str(raw))
        if not url.startswith(("http://", "https://")):
            continue
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if parsed.path in ("", "/"):
            continue
        for domain, label in _SOCIAL_DOMAINS.items():
            if _host_matches_domain(host, domain):
                platform = label.split(":", 1)[1]
                fact = f"{_SOCIAL_URL_PREFIX}{platform}:{url}"
                if fact not in seen:
                    seen.add(fact)
                    out.append(fact)
                break
    return out[:12]


def _public_document_links(all_html: str, base_url: str, domain: str) -> list[str]:
    """Return useful same-domain public document URLs linked from scraped pages."""
    out = []
    seen = set()
    for href in _HREF_RE.findall(all_html or ""):
        url = urljoin(base_url, html.unescape(href)).split("#", 1)[0].rstrip("/")
        if not url.startswith(("http://", "https://")):
            continue
        if not _DOCUMENT_EXT_RE.search(url):
            continue
        if not _same_root(url, domain):
            continue
        fact = f"{_PUBLIC_DOC_URL_PREFIX}{url}"
        if fact not in seen:
            seen.add(fact)
            out.append(fact)
        if len(out) >= 8:
            break
    return out


def _site_signals(all_html: str, jsonld: dict) -> list[str]:
    """
    Detect public website signals C3 can consume.

    This function only records facts visible in public HTML. For example,
    "call to pay" can be scraped as a public hint, but it does not prove the
    business has a private manual reconciliation problem. That deeper workflow
    claim belongs to research, a call, or a statement review.
    """
    lower = (all_html or "").lower()
    signals = []
    checks = {
        "online_ordering": ["order online", "online ordering", "toasttab.com", "chownow", "olo.com"],
        "delivery": ["doordash", "uber eats", "grubhub", "delivery"],
        "third_party_ordering": ["doordash", "uber eats", "grubhub", "chownow", "olo.com", "menufy", "bentobox", "owner.com"],
        "reservations": ["opentable", "resy", "reservation"],
        "gift_cards": ["gift card", "giftcard"],
        "separate_loyalty_or_gift_card": ["third party gift card", "gift up", "toast gift card", "square gift card", "loyalty program"],
        "ecommerce": ["shopify", "woocommerce", "cart", "checkout"],
        "multi_location": ["locations", "multiple locations", "visit us at"],
        "catering_or_events": ["catering", "private events", "events"],
        "hiring_or_careers": ["careers", "jobs", "employment", "we're hiring", "we are hiring"],
        "appointments_or_booking": ["book now", "book online", "schedule service", "schedule now", "request appointment", "appointments"],
        "contact_form": ["contact us", "get in touch", "<form", "type=\"submit\"", "type='submit'"],
        "pricing_or_plans": ["pricing", "plans", "membership", "subscriptions", "monthly plan"],
        "financing": ["financing", "apply now", "affirm", "klarna", "afterpay", "carecredit", "synchrony"],
        "customer_portal_or_account": ["customer portal", "client portal", "my account", "account login", "portal login"],
        "sms_or_text_channel": ["text us", "sms", "message us", "text to pay", "text for a quote"],
        "faq_or_help_center": ["frequently asked questions", "faq", "help center", "support center"],
        # Sales qualification signals. These do not prove pain by themselves,
        # but they help Outreach decide whether to ask for a call, statement
        # review, or more research.
        "table_or_qr_pay": ["qr code", "scan to pay", "pay at the table", "tableside", "table-side", "mobile order and pay", "handheld"],
        "payment_link": ["buy.stripe.com", "checkout.stripe.com", "paypal.me", "payment link", "pay invoice", "pay online"],
        "public_manual_payment_hint": ["call to pay", "pay by phone", "pdf invoice", "print invoice"],
        "pharmacy_compliance_payments": ["fsa", "hsa", "iias", "sigis", "prime rx", "primerx", "bestrx", "liberty software", "pioneerrx", "rxlocal"],
        "pharmacy_stack": ["prime rx", "primerx", "bestrx", "liberty software", "pioneerrx", "rxlocal", "digital pharmacist", "rx30", "qs/1", "computer-rx"],
        "dealership_service_payments": ["service lane", "repair order", " ro ", "text to pay", "cdk simplepay", "dealertrack payment", "tekion pay", "kimoby", "dealerpay"],
        "dealer_dms_or_payments": ["cdk simplepay", "cdk epayments", "dealertrack payment", "tekion pay", "kimoby", "dealerpay", "routeone", "reynolds and reynolds"],
        "restaurant_vertical_stack": ["toasttab.com", "order.toasttab.com", "square for restaurants", "lightspeed restaurant", "spoton restaurant", "touchbistro", "revelsystems", "upserve", "aloha"],
    }
    for name, needles in checks.items():
        if any(n in lower for n in needles):
            signals.append(name)
    for fact in _social_links(all_html, jsonld):
        platform = fact.split(":", 2)[1]
        label = f"social:{platform}"
        if label not in signals:
            signals.append(label)
        signals.append(fact)
    return signals


def _detect_processors(all_html: str) -> tuple[str, list[str]]:
    """
    Scan all fetched HTML for processor fingerprints.
    Returns (primary_processor, [all_matched_processors]).
    primary = first/strongest match (list order in PROCESSOR_FINGERPRINTS).
    """
    lower = all_html.lower()
    matched = []
    for name, fingerprints in PROCESSOR_FINGERPRINTS:
        for fp in fingerprints:
            if fp in lower:
                if name not in matched:
                    matched.append(name)
                break  # one fingerprint hit per processor is enough
    primary = matched[0] if matched else ""
    return primary, matched


def _extract_owner(pages: dict[str, str]) -> str:
    """
    Try to extract an owner/founder name from the scraped pages.
    Priority: /about, /team, /about-us, then homepage.
    Returns the best candidate or "".
    """
    # Order: prefer about/team pages first
    priority = ["/about", "/team", "/about-us", "home"]
    ordered_texts = []
    for key in priority:
        if key in pages:
            ordered_texts.append(_strip_tags(pages[key]))
    # Also add any remaining pages
    for k, v in pages.items():
        if k not in priority:
            ordered_texts.append(_strip_tags(v))

    for text in ordered_texts:
        # Try "Owner: Jane Smith"
        m = _OWNER_BEFORE.search(text)
        if m:
            candidate = m.group(1).strip()
            if len(candidate.split()) >= 2:
                return candidate
        # Try "Jane Smith, Owner"
        m = _OWNER_AFTER.search(text)
        if m:
            candidate = m.group(1).strip()
            if len(candidate.split()) >= 2:
                return candidate
        # Try "Jane Smith is the Owner/President"
        m = _OWNER_IS.search(text)
        if m:
            candidate = m.group(1).strip()
            if len(candidate.split()) >= 2:
                return candidate
    return ""


def _extract_reviews(all_html: str) -> list[str]:
    """
    Best-effort testimonial/review extraction from HTML.
    Returns a list of text strings (each ≥40 chars, de-duped).
    """
    reviews = []
    seen = set()

    # 1. Try blockquote / testimonial / review class containers
    for m in _REVIEW_RE.finditer(all_html):
        text = _strip_tags(m.group(0))
        if len(text) >= 40 and text not in seen:
            reviews.append(text[:500])  # cap at 500 chars
            seen.add(text)

    # 2. Quoted strings ≥40 chars (typical for inline testimonials)
    stripped = _strip_tags(all_html)
    for m in _QUOTED_TEXT_RE.finditer(stripped):
        text = m.group(1).strip()
        if len(text) >= 40 and text not in seen:
            reviews.append(text[:500])
            seen.add(text)

    return reviews[:20]  # cap at 20 per site to avoid noise


# ── Core process function ─────────────────────────────────────────────────────

def process(conn, row) -> bool:
    """
    Scrape the lead's website and advance stage from 'pulled' to 'scraped'.

    Always returns True.  On any error, still advances (with whatever partial
    data was collected) so the lead is never stranded.
    """
    lead_id = row["id"]
    company = row["company"] or ""
    website = row["website"] or ""
    # row["domain"] may not exist in older DB schemas — guard with try/except
    try:
        domain = row["domain"] if row["domain"] else extract_domain(website)
    except (IndexError, KeyError):
        domain = extract_domain(website)

    log.info("scraping lead %s (%s) — %s", lead_id, company, website)

    # ── Fetch all pages ───────────────────────────────────────────────────────
    pages: dict[str, str] = {}    # key → html text
    all_html_parts: list[str] = []

    urls = _build_urls(website)
    if not urls:
        log.warning("lead %s has no website — advancing with empty fields", lead_id)
        L.advance(conn, lead_id, "scraped")
        L.log_cost(conn, "scrape", "website_scrape", 1, 0.0)
        return True

    # Homepage
    homepage_html = _fetch_page(urls[0])
    if homepage_html:
        pages["home"] = homepage_html
        all_html_parts.append(homepage_html)
    else:
        # Try http fallback if https failed
        if urls[0].startswith("https://"):
            fallback = "http://" + urls[0][len("https://"):]
            homepage_html = _fetch_page(fallback)
            if homepage_html:
                pages["home"] = homepage_html
                all_html_parts.append(homepage_html)
    if not homepage_html:
        log.warning("lead %s — homepage unreachable; advancing with empty fields", lead_id)
        L.advance(conn, lead_id, "scraped")
        L.log_cost(conn, "scrape", "website_scrape", 1, 0.0)
        return True

    # Sub-paths + lightweight discovery (contact, about, team, sitemap, etc.)
    discovered = []
    if homepage_html:
        discovered.extend(_discover_same_domain_links(homepage_html, urls[0], domain))
        discovered.extend(_sitemap_urls(urls[0], domain))
    fetch_urls = []
    seen_urls = set()
    for path in SUBPATHS:
        fetch_urls.append(urljoin(urls[0] if urls[0] else website, path))
    fetch_urls.extend(discovered)

    for sub_url in fetch_urls:
        sub_url = sub_url.rstrip("/")
        if sub_url in seen_urls:
            continue
        seen_urls.add(sub_url)
        sub_html = _fetch_page(sub_url)
        if sub_html:
            key = urlparse(sub_url).path or sub_url
            pages[key] = sub_html
            all_html_parts.append(sub_html)
        # Brief pause to be polite
        time.sleep(0.2)

    if not all_html_parts:
        log.warning("lead %s — all pages unreachable; advancing with empty fields", lead_id)
        L.advance(conn, lead_id, "scraped")
        L.log_cost(conn, "scrape", "website_scrape", 1, 0.0)
        return True

    all_html = "\n".join(all_html_parts)

    # ── Extract signals ───────────────────────────────────────────────────────
    jsonld = {"emails": [], "people": [], "same_as": [], "types": []}
    try:
        jsonld = _jsonld_signals(all_html)
        jsonld_email_html = " ".join(jsonld.get("emails") or [])
        emails = sorted(set(_extract_emails(all_html + " " + jsonld_email_html, domain)))
    except Exception as exc:
        log.warning("lead %s email extraction error: %s", lead_id, exc)
        emails = []

    try:
        owner_name = _extract_owner(pages)
        if not owner_name and jsonld.get("people"):
            owner_name = jsonld["people"][0]
    except Exception as exc:
        log.warning("lead %s owner extraction error: %s", lead_id, exc)
        owner_name = ""

    try:
        processor, processors = _detect_processors(all_html)
        tech_signals = processors + _site_signals(all_html, jsonld)
        doc_links = _public_document_links(all_html, urls[0], domain)
        if doc_links:
            if "public_docs" not in tech_signals:
                tech_signals.append("public_docs")
            tech_signals.extend(doc_links)
    except Exception as exc:
        log.warning("lead %s processor detection error: %s", lead_id, exc)
        processor, tech_signals = "", []

    try:
        reviews = _extract_reviews(all_html)
    except Exception as exc:
        log.warning("lead %s review extraction error: %s", lead_id, exc)
        reviews = []

    # ── Persist ───────────────────────────────────────────────────────────────
    try:
        # Set fields on the lead row
        L.set_fields(
            conn, lead_id,
            owner_name=owner_name or None,
            processor=processor or None,
            tech_signals=json.dumps(tech_signals) if tech_signals else None,
        )

        # Add on-site email candidates
        for email in emails:
            try:
                L.add_candidate(
                    conn, lead_id, email,
                    source="onsite",
                    confidence=0.85,
                    rank=0,
                )
            except Exception as exc:
                log.debug("lead %s add_candidate(%s): %s", lead_id, email, exc)

        # Add reviews / testimonials
        for review_text in reviews:
            try:
                L.add_review(conn, lead_id, review_text, rating=None, source="scrape")
            except Exception as exc:
                log.debug("lead %s add_review: %s", lead_id, exc)

        # Advance the stage
        L.advance(conn, lead_id, "scraped")
        L.log_cost(conn, "scrape", "website_scrape", 1, 0.0)

        log.info(
            "lead %s scraped — emails=%d owner=%r processor=%r signals=%s reviews=%d",
            lead_id, len(emails), owner_name, processor, tech_signals, len(reviews),
        )

    except Exception as exc:
        log.error("lead %s persist error: %s — attempting advance anyway", lead_id, exc)
        try:
            L.advance(conn, lead_id, "scraped")
        except Exception as exc2:
            log.error("lead %s advance failed too: %s", lead_id, exc2)

    return True


# ── Dry-run / standalone test ─────────────────────────────────────────────────

def _dry_run_url(url: str) -> None:
    """Scrape `url` and print extracted signals. No DB writes."""
    print(f"\n{'='*60}")
    print(f"URL: {url}")
    print("="*60)

    pages: dict[str, str] = {}
    all_html_parts: list[str] = []

    base = url.rstrip("/")
    domain = extract_domain(url)
    urls_to_try = [base] + [base + p for p in SUBPATHS]

    print(f"Fetching {len(urls_to_try)} pages …")
    homepage_html = _fetch_page(urls_to_try[0])
    if homepage_html:
        pages["home"] = homepage_html
        all_html_parts.append(homepage_html)
        print(f"  home ({len(homepage_html):,} bytes)")
    else:
        # http fallback
        if urls_to_try[0].startswith("https://"):
            fb = "http://" + urls_to_try[0][len("https://"):]
            homepage_html = _fetch_page(fb)
            if homepage_html:
                pages["home"] = homepage_html
                all_html_parts.append(homepage_html)
                print(f"  home via http fallback ({len(homepage_html):,} bytes)")
    if not homepage_html:
        print("  [homepage unreachable; skipping sub-path fetches]")
        return

    discovered = []
    if homepage_html:
        discovered.extend(_discover_same_domain_links(homepage_html, urls_to_try[0], domain))
        discovered.extend(_sitemap_urls(urls_to_try[0], domain))

    fetch_urls = []
    seen_urls = set()
    for path in SUBPATHS:
        fetch_urls.append(urljoin(base + "/", path.lstrip("/")))
    fetch_urls.extend(discovered)

    for sub_url in fetch_urls:
        sub_url = sub_url.rstrip("/")
        if sub_url in seen_urls:
            continue
        seen_urls.add(sub_url)
        sub_html = _fetch_page(sub_url)
        if sub_html:
            key = urlparse(sub_url).path or sub_url
            pages[key] = sub_html
            all_html_parts.append(sub_html)
            print(f"  {key} ({len(sub_html):,} bytes)")
        time.sleep(0.15)

    if not all_html_parts:
        print("  [no pages reachable]")
        return

    all_html = "\n".join(all_html_parts)
    jsonld = _jsonld_signals(all_html)
    jsonld_email_html = " ".join(jsonld.get("emails") or [])
    emails = _extract_emails(all_html + " " + jsonld_email_html, domain)
    owner_name = _extract_owner(pages)
    if not owner_name and jsonld.get("people"):
        owner_name = jsonld["people"][0]
    processor, processors = _detect_processors(all_html)
    tech_signals = processors + _site_signals(all_html, jsonld)
    doc_links = _public_document_links(all_html, base, domain)
    if doc_links:
        if "public_docs" not in tech_signals:
            tech_signals.append("public_docs")
        tech_signals.extend(doc_links)
    reviews = _extract_reviews(all_html)

    print(f"\nEmails found ({len(emails)}):")
    for e in emails:
        print(f"  {e}")

    print(f"\nOwner name: {owner_name or '(not detected)'}")
    print(f"Processor:  {processor or '(none detected)'}")
    print(f"Tech signals: {tech_signals}")
    print(f"Reviews/testimonials found: {len(reviews)}")
    for i, r in enumerate(reviews[:3], 1):
        print(f"  [{i}] {r[:120]}…" if len(r) > 120 else f"  [{i}] {r}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="C2 Website Scraper — standalone test")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scrape test URLs and print extraction results (no DB)")
    parser.add_argument("--url", metavar="URL",
                        help="Scrape a specific URL (implies --dry-run)")
    args = parser.parse_args()

    if args.url:
        _dry_run_url(args.url)
        return

    if args.dry_run:
        # A handful of real small-business sites for smoke-testing
        TEST_URLS = [
            "https://www.squareup.com/us/en",   # known Square fingerprint
            "https://stripe.com",               # known Stripe fingerprint
        ]
        for url in TEST_URLS:
            _dry_run_url(url)
        return

    # If run with no flags, run a quick DB integration test
    print("No flags given. Use --dry-run or --url <url>.")
    print("For DB integration test: LEAD_DB=/tmp/_b.db python3 c2_scraper.py --dry-run")


if __name__ == "__main__":
    main()
