# main.py — Amazon Lead Filter Bot (Discord.py 2.x)
# -------------------------------------------------
# What this version adds:
# - Persistent watch list (config.json): /watch_add, /watch_remove, /watch_list, /watch_add_all, /watch_all, /watch_clear
# - Parses ANY lead by first rewriting message+embeds to plain text; OCR fallback for images (optional)
# - Robust Keepa fetch (handles dict/list shapes, stats.current.*, CSV fallbacks, multi-domain tries)
# - ROI field shows % PLUS (Sell £X, Buy £Y); ASIN is shown clearly
# - Your rules: Eligibility=Yes (unless allowed), Profit ≥ £5, ROI ≥ 8%, and reject if IP/PL
# - Tools: /settings /set_min_profit /set_min_roi /set_default_buy /set_allow_unknown_elig /diag_asin /calc_asin
# - Context menu: “Show Plain Text” (right-click a message → Apps)
#
# Setup:
#   pip install -U discord.py python-dotenv aiohttp
#   Create .env (same folder):
#       DISCORD_TOKEN=YOUR_BOT_TOKEN
#       FORWARD_USER_ID=YOUR_USER_ID
#       KEEPA_KEY=YOUR_KEEPA_KEY
#       KEEPA_DOMAIN=GB            # accepts GB/UK/US/DE or number; bot tries UK→DE→US fallback
#       MIN_PROFIT=5
#       MIN_ROI=8
#       ALLOW_UNKNOWN_ELIG=false
#       DEFAULT_BUY=10
#       OCRSPACE_KEY=YOUR_OCR_SPACE_API_KEY   # optional; leave blank to disable OCR
#
# In Discord Developer Portal → Bot: enable "Message Content Intent".

import os, re, json, asyncio, logging
import urllib.parse
from typing import Optional, Tuple, List, Dict

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
import aiohttp
from aiohttp import web

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s")
log = logging.getLogger("bot")

# ---------------- Env ----------------
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
FORWARD_USER_ID = int(os.getenv("FORWARD_USER_ID", "0"))
FORWARD_CHANNEL_ID = int(os.getenv("FORWARD_CHANNEL_ID", "0"))

MIN_PROFIT = float(os.getenv("MIN_PROFIT", "5"))
MIN_ROI = float(os.getenv("MIN_ROI", "8"))
ALLOW_UNKNOWN_ELIG = os.getenv("ALLOW_UNKNOWN_ELIG", "false").lower() in {"1","true","yes","on"}

DEFAULT_BUY = float(os.getenv("DEFAULT_BUY", "0"))

KEEPA_KEY = os.getenv("KEEPA_KEY")
KEEPA_DOMAIN_RAW = (os.getenv("KEEPA_DOMAIN") or "GB").strip()

OCRSPACE_KEY = (os.getenv("OCRSPACE_KEY") or "").strip()
RAINFOREST_KEY = os.getenv("RAINFOREST_KEY")
RAINFOREST_PRIME_ONLY = os.getenv("RAINFOREST_PRIME_ONLY", "false").lower() in {"1","true","yes","on"}
RAINFOREST_FREE_SHIP_ONLY = os.getenv("RAINFOREST_FREE_SHIP_ONLY", "false").lower() in {"1","true","yes","on"}
RAINFOREST_CONDITION_NEW_ONLY = os.getenv("RAINFOREST_CONDITION_NEW_ONLY", "false").lower() in {"1","true","yes","on"}
RAINFOREST_SHOW_DIFFERENT_ASINS = os.getenv("RAINFOREST_SHOW_DIFFERENT_ASINS", "false").lower() in {"1","true","yes","on"}
RAINFOREST_MIN_PRICE = os.getenv("RAINFOREST_MIN_PRICE")
RAINFOREST_MAX_PRICE = os.getenv("RAINFOREST_MAX_PRICE")

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ---------------- Config persistence ----------------
def _default_config() -> Dict:
    return {
        "watch_all": False,
        "watched_channels": []  # list[int]
    }
    
def load_config() -> Dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            if not isinstance(cfg, dict):
                return _default_config()
            cfg.setdefault("watch_all", False)
            cfg.setdefault("watched_channels", [])
            return cfg
    except Exception:
        return _default_config()

def save_config(cfg: Dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.warning("Failed to write config: %s", e)

CFG = load_config()

# ---------------- Discord ----------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------- Regex & helpers ----------------
ASIN_RE = re.compile(r"\b([A-Z0-9]{10})\b", re.I)
ASIN_LABEL_RE = re.compile(r"\bASIN[:\s-]*([A-Z0-9]{10})\b", re.I)
ASIN_LABEL_FLEX_RE = re.compile(r"\bASIN[:\s-]*([A-Z0-9\-\s]{10,})\b", re.I)
AMAZON_URL_RE = re.compile(r"amazon\.(?:com|co\.uk|de|fr|it|es|ca|co\.jp|in|com\.mx|com\.br|com\.au|nl)/.*?/(?:dp|gp/product|product)/([A-Z0-9]{10})", re.I)
B0_ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b", re.I)
B0_ASIN_FLEX_RE = re.compile(r"\bB\s*0[\s\-A-Z0-9]{8}\b", re.I)
ROI_RE = re.compile(r"\bROI[:\s]([0-9]+(?:\.[0-9]+)?)\s%?", re.I)
ELIG_RE = re.compile(r"\bEligibl\w*[:\s-]*([Yy]es|[Nn]o|Unknown)\b")
MONEY_RE = re.compile(r"£\s*([0-9]+(?:\.[0-9]{1,2})?)")

# Common words that might accidentally match ASIN regex (10 chars, alphanumeric)
INVALID_ASIN_WORDS = {
    "ATTACHMENT", "DOWNLOAD", "REGISTER", "SUBSCRIPT", "VALIDATIO", 
    "AUTHORIZE", "DOCUMENT", "TEMPORARY", "PERMANENT", "AVAILABLE",
    "UNAVAILAB", "PURCHASED", "COMPLETED", "CANCELLED", "RECEIVED",
    "DELIVERED", "SHIPPING", "PROCESSED", "ACTIVATED", "DISABLED"
}

def is_valid_asin(asin: str) -> bool:
    """Validate that a string is a real ASIN, not a common word."""
    if not asin or len(asin) != 10:
        return False
    asin_upper = asin.upper()
    if asin_upper in INVALID_ASIN_WORDS:
        return False
    has_letter = any(c.isalpha() for c in asin_upper)
    has_digit = any(c.isdigit() for c in asin_upper)
    if not (has_letter and has_digit):
        return False
    return True

def normalize_asin(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()

def money(n: Optional[float]) -> str:
    return "—" if n is None else f"£{n:.2f}"

def pct(n: Optional[float]) -> str:
    return "—" if n is None else f"{n:.2f}%"

def parse_domain_candidates(raw: str) -> List[int]:
    mapping = {
        "US": 1, "COM": 1,
        "UK": 2, "GB": 2, "CO.UK": 2,
        "DE": 3, "GERMANY": 3,
        "FR": 4, "FRANCE": 4,
        "JP": 5, "CO.JP": 5, "JAPAN": 5,
        "CA": 6, "CANADA": 6,
        "IT": 7, "ITALY": 7,
        "ES": 8, "SPAIN": 8,
        "IN": 9, "INDIA": 9,
        "MX": 10, "MEXICO": 10,
        "BR": 11, "BRAZIL": 11,
        "AU": 12, "AUSTRALIA": 12,
        "NL": 15, "NETHERLANDS": 15,
    }
    
    # All available Keepa domains in priority order
    all_domains = [2, 3, 1, 4, 7, 8, 15, 6, 5, 9, 10, 12, 11]  # UK, DE, US, FR, IT, ES, NL, CA, JP, IN, MX, AU, BR
    
    cands: List[int] = []
    
    # If user specified a domain, try it first
    if raw.isdigit():
        try: 
            user_domain = int(raw)
            if user_domain in all_domains:
                cands.append(user_domain)
        except: 
            pass
    else:
        user_domain = mapping.get(raw.upper())
        if user_domain and user_domain in all_domains:
            cands.append(user_domain)
    
    # If no user domain or not found, default to UK first
    if not cands:
        cands.append(2)
    
    # Add ALL other domains as fallbacks to maximize chance of finding price
    for d in all_domains:
        if d not in cands:
            cands.append(d)
    
    return cands

KEEPA_DOMAINS_TO_TRY = parse_domain_candidates(KEEPA_DOMAIN_RAW)

def amazon_url_for_domain(asin: str, domain_id: int) -> str:
    host = {
        1: "www.amazon.com",          # US
        2: "www.amazon.co.uk",        # UK
        3: "www.amazon.de",           # Germany
        4: "www.amazon.fr",           # France
        5: "www.amazon.co.jp",        # Japan
        6: "www.amazon.ca",           # Canada
        7: "www.amazon.it",           # Italy
        8: "www.amazon.es",           # Spain
        9: "www.amazon.in",           # India
        10: "www.amazon.com.mx",      # Mexico
        11: "www.amazon.com.br",      # Brazil
        12: "www.amazon.com.au",      # Australia
        15: "www.amazon.nl",          # Netherlands
    }.get(domain_id, "www.amazon.co.uk")
    return f"https://{host}/dp/{asin}"

async def try_dm(user_id: int, content: str = "", embed: Optional[discord.Embed] = None) -> bool:
    if not user_id: return False
    try:
        u = await bot.fetch_user(user_id)
        if embed: await u.send(content=content, embed=embed)
        else:     await u.send(content)
        return True
    except Exception as e:
        log.info("DM failed: %s", e)
        return False

def extract_from_text(txt: str):
    asin = None; buy=None; sell=None; roi=None; elig=None
    # Try to find ASIN - look for all matches and validate them
    asin_matches = ASIN_RE.findall(txt)
    log.debug(f"Found {len(asin_matches)} potential ASIN matches: {asin_matches}")
    for match in asin_matches:
        candidate = match.upper()
        if is_valid_asin(candidate):
            asin = candidate
            log.info(f"✓ Valid ASIN found: {asin}")
            break
        else:
            log.debug(f"✗ Rejected invalid ASIN candidate: {candidate}")
    
    # Also check for ASIN in Amazon URLs (more reliable)
    url_match = AMAZON_URL_RE.search(txt)
    if url_match:
        url_asin = url_match.group(1).upper()
        if is_valid_asin(url_asin):
            asin = url_asin
            log.info(f"✓ Found ASIN from Amazon URL: {asin}")
    
    # Check explicit ASIN label
    if not asin:
        label_match = ASIN_LABEL_RE.search(txt)
        if label_match:
            label_asin = label_match.group(1).upper()
            if is_valid_asin(label_asin):
                asin = label_asin
                log.info(f"✓ Found ASIN from label: {asin}")
    if not asin:
        label_flex = ASIN_LABEL_FLEX_RE.search(txt)
        if label_flex:
            cand = normalize_asin(label_flex.group(1))
            if len(cand) == 10 and is_valid_asin(cand):
                asin = cand
                log.info(f"✓ Found ASIN from flexible label: {asin}")
    
    # Prefer typical B0-prefixed ASINs if still missing
    if not asin:
        b0_match = B0_ASIN_RE.search(txt)
        if b0_match:
            b0_asin = b0_match.group(1).upper()
            if is_valid_asin(b0_asin):
                asin = b0_asin
                log.info(f"✓ Found B0 ASIN: {asin}")
    if not asin:
        b0_flex = B0_ASIN_FLEX_RE.search(txt)
        if b0_flex:
            cand = normalize_asin(b0_flex.group(0))
            if len(cand) == 10 and is_valid_asin(cand):
                asin = cand
                log.info(f"✓ Found flexible B0 ASIN: {asin}")

    mb = re.search(r"\b(Buy|Cost)\s*[:=]\s*£?\s*([0-9]+(?:\.[0-9]+)?)", txt, re.I)
    if mb: buy = float(mb.group(2))
    ms = re.search(r"\b(Sell|Sale|SP|Price)\s*[:=]\s*£?\s*([0-9]+(?:\.[0-9]+)?)", txt, re.I)
    if ms: sell = float(ms.group(2))

    m = ROI_RE.search(txt)
    if m: roi = float(m.group(1))
    m = ELIG_RE.search(txt)
    if m: elig = m.group(1).capitalize()

    has_ip_pl = bool(
        re.search(r"\bip(?:\s+alert)?\b", txt, re.I) or
        re.search(r"\bprivate\s+label\b", txt, re.I) or
        re.search(r"\bpl\b", txt, re.I)
    )
    return asin, buy, sell, roi, elig, bool(has_ip_pl)

def compute_profit_roi(buy: Optional[float], sell: Optional[float]) -> Tuple[Optional[float], Optional[float]]:
    if buy is None or sell is None:
        return None, None
    profit = round(sell - buy, 2)
    roi = round((profit / buy) * 100.0, 2) if buy > 0 else None
    return profit, roi

# ---------------- Message → Plain text ----------------
def message_to_plaintext(msg: discord.Message) -> str:
    """
    Rewrites a Discord message (content + embeds) into plain text our regex can parse.
    Includes titles, descriptions, fields, footers, authors, links, image URLs, and attachment names.
    """
    parts: List[str] = []

    if msg.content:
        parts.append(msg.content)

    for e in msg.embeds:
        if e.title:
            parts.append(e.title)
        if e.author and e.author.name:
            parts.append(f"By: {e.author.name}")
        if e.description:
            parts.append(e.description)
        for f in e.fields:
            name = (f.name or "").strip()
            val = (f.value or "").strip()
            if name or val:
                parts.append(f"{name}\n{val}" if name else val)
        if e.footer and e.footer.text:
            parts.append(e.footer.text)
        if e.url:
            parts.append(e.url)
        if e.image and e.image.url:
            parts.append(f"[image] {e.image.url}")
        if e.thumbnail and e.thumbnail.url:
            parts.append(f"[image] {e.thumbnail.url}")

    for a in msg.attachments:
        parts.append(f"[attachment] {a.filename}")
        # OCR fallback will pull text from image URLs directly; we don't download here.

    text = "\n".join(p for p in parts if p and p.strip())
    text = re.sub(r"[•·▪▶]", "-", text)  # normalize bullet chars
    text = re.sub(r"\u200b", "", text)   # zero-width
    return text

# ---------------- OCR Fallback (optional) ----------------
async def ocr_try_extract_from_images(message: discord.Message) -> str:
    """Use OCR.space to extract text from images (if OCRSPACE_KEY is set)."""
    if not OCRSPACE_KEY:
        return ""

    urls: List[str] = []
    for e in message.embeds:
        if e.image and e.image.url: urls.append(e.image.url)
        if e.thumbnail and e.thumbnail.url: urls.append(e.thumbnail.url)
    for a in message.attachments:
        if a.content_type and a.content_type.startswith("image/"):
            urls.append(a.url)
        else:
            # crude fallback by extension if content_type missing
            if a.filename.lower().endswith((".png",".jpg",".jpeg",".webp",".bmp",".gif")):
                urls.append(a.url)

    if not urls:
        return ""

    ocr_texts: List[str] = []
    api = "https://api.ocr.space/parse/imageurl"
    timeout = aiohttp.ClientTimeout(total=45)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for u in urls[:3]:  # keep it light
            try:
                payload = {
                    "apikey": OCRSPACE_KEY,
                    "url": u,
                    "language": "eng",
                    "isOverlayRequired": "false",
                    "OCREngine": "2",
                }
                async with session.post(api, data=payload) as r:
                    js = await r.json(content_type=None)
                parsed = js.get("ParsedResults") or []
                for pr in parsed:
                    t = pr.get("ParsedText") or ""
                    if t.strip():
                        ocr_texts.append(t)
            except Exception as e:
                log.info("OCR error for %s: %s", u, e)

    return "\n".join(ocr_texts)

# ---------------- Keepa (very robust) ----------------
async def keepa_fetch(session: aiohttp.ClientSession, asin: str) -> Tuple[Optional[float], Optional[str], Optional[str], str, Optional[int]]:
    """
    Returns (sell_price, brand, title, diag_text, domain_used).
    Tries multiple domains. Pulls price from stats, stats.current and csv fallbacks.
    Handles dict/list shapes and weird returns.
    """
    if not KEEPA_KEY:
        return None, None, None, "Keepa disabled: KEEPA_KEY not set.", None

    def cents(v):
        return round(v / 100.0, 2) if isinstance(v, (int, float)) and v > 0 else None

    async def fetch_once(domain: int):
        url = "https://api.keepa.com/product"
        params = {"key": KEEPA_KEY, "domain": domain, "asin": asin, "history": 1, "stats": 90}
        try:
            async with session.get(url, params=params, timeout=25) as r:
                status = r.status
                raw = await r.text()

            try:
                js = json.loads(raw)
            except Exception:
                return None, None, None, f"Invalid JSON domain={domain} status={status}: {raw[:200]}", None

            while isinstance(js, list) and js:
                js = js[0]
            if not isinstance(js, dict):
                return None, None, None, f"Unexpected JSON type {type(js).__name__} domain={domain}", None

            products = js.get("products")
            if products is None:
                return None, None, None, f"No 'products' key in response domain={domain}", None
            if isinstance(products, list):
                if not products:
                    return None, None, None, f"Empty products list domain={domain}", None
                p = products[0]
            elif isinstance(products, dict):
                p = products
            else:
                return None, None, None, f"Invalid 'products' type={type(products).__name__} domain={domain}", None

            if not isinstance(p, dict):
                return None, None, None, f"Bad product data type={type(p).__name__} domain={domain}", None

            stats = p.get("stats") or {}
            if isinstance(stats, list): stats = {}
            current = stats.get("current") if isinstance(stats, dict) else None
            if not isinstance(current, dict): current = {}

            csv = p.get("csv") or {}
            if isinstance(csv, list): csv = {}

            price = None
            # 1) stats.*
            for key in ("buyBoxPrice", "buyBoxShipping", "newPrice", "amazonPrice"):
                price = cents(stats.get(key))
                if price: break
            # 2) stats.current.*
            if not price:
                for key in ("buyBoxPrice", "buyBoxShipping", "newPrice", "amazonPrice"):
                    price = cents(current.get(key))
                    if price: break
            if not price and isinstance(csv, dict):
                def last_price_from_series(arr):
                    if not isinstance(arr, list) or not arr:
                        return None
                    if isinstance(arr[-1], (int, float)):
                        v = arr[-1]
                        if 0 < v <= 500000:
                            return cents(v)
                    for i in range(len(arr) - 1, -1, -2):
                        x = arr[i]
                        if isinstance(x, (int, float)) and 0 < x <= 500000:
                            return cents(x)
                    return None
                preferred = []
                for k, arr in csv.items():
                    name = str(k).lower()
                    if isinstance(arr, list) and any(t in name for t in ("buy", "box", "bb", "amazon", "new")):
                        preferred.append(arr)
                for series in preferred:
                    p2 = last_price_from_series(series)
                    if p2:
                        price = p2
                        break
                if not price:
                    for arr in csv.values():
                        p2 = last_price_from_series(arr)
                        if p2:
                            price = p2
                            break

            if not price:
                top_level_candidates = [p.get("listPrice"), (current.get("listPrice") if isinstance(current, dict) else None)]
                for v in top_level_candidates:
                    price = cents(v)
                    if price:
                        break

            brand = (p.get("brand") or "").strip()
            title = (p.get("title") or "").strip()
            note = f"OK domain={domain} status={status}" if (price or brand or title) else f"No usable price domain={domain}"
            return price, brand, title, note, domain

        except asyncio.TimeoutError:
            return None, None, None, f"Timeout domain={domain}", None
        except Exception as e:
            return None, None, None, f"Exception domain={domain}: {e}", None

    diags = []
    domain_names = {1: "US", 2: "UK", 3: "DE", 4: "FR", 5: "JP", 6: "CA", 7: "IT", 8: "ES", 9: "IN", 10: "MX", 11: "BR", 12: "AU", 15: "NL"}
    
    log.info(f"Trying {len(KEEPA_DOMAINS_TO_TRY)} Keepa domains for ASIN {asin}: {[domain_names.get(d, d) for d in KEEPA_DOMAINS_TO_TRY]}")
    
    best_brand = None
    best_title = None
    best_domain_info = None

    for d in KEEPA_DOMAINS_TO_TRY:
        price, brand, title, note, used = await fetch_once(d)
        diags.append(note)
        if price is not None:
            log.info(f"✓ Found price on {domain_names.get(d, d)} domain: {price}")
            if brand and not best_brand:
                best_brand = brand
            if title and not best_title:
                best_title = title
            return price, (best_brand or brand), (best_title or title), " | ".join(diags), used
        if (brand or title) and best_domain_info is None:
            best_brand = brand or best_brand
            best_title = title or best_title
            best_domain_info = used
    
    log.warning(f"✗ No price found on any of {len(KEEPA_DOMAINS_TO_TRY)} domains")
    if best_brand or best_title:
        return None, best_brand, best_title, " | ".join(diags), best_domain_info
    return None, None, None, " | ".join(diags), None

async def rainforest_fetch(session: aiohttp.ClientSession, asin: str) -> Tuple[Optional[float], Optional[str], Optional[str], str, Optional[int]]:
    if not RAINFOREST_KEY:
        return None, None, None, "Rainforest disabled: RAINFOREST_KEY not set.", None

    def parse_price_obj(po):
        if not isinstance(po, dict):
            return None
        v = po.get("value")
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
        raw = po.get("raw")
        if isinstance(raw, str):
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)", raw)
            if m:
                try:
                    return float(m.group(1))
                except:
                    return None
        return None

    async def fetch_once(domain_id: int):
        host = amazon_url_for_domain(asin, domain_id).split("/")[2]
        domain = host.replace("www.", "")
        url = "https://api.rainforestapi.com/request"
        try:
            price = None
            brand = None
            title = None

            page = 1
            total_pages = None
            candidates = []
            while page <= 5 and (total_pages is None or page <= total_pages):
                params_off = {
                    "api_key": RAINFOREST_KEY,
                    "type": "offers",
                    "asin": asin,
                    "amazon_domain": domain,
                    "page": page,
                }
                if RAINFOREST_PRIME_ONLY:
                    params_off["offers_prime"] = "true"
                if RAINFOREST_FREE_SHIP_ONLY:
                    params_off["offers_free_shipping"] = "true"
                if RAINFOREST_CONDITION_NEW_ONLY:
                    params_off["offers_condition_new"] = "true"
                if RAINFOREST_SHOW_DIFFERENT_ASINS:
                    params_off["show_different_asins"] = "true"
                if RAINFOREST_MIN_PRICE:
                    params_off["min_price"] = RAINFOREST_MIN_PRICE
                if RAINFOREST_MAX_PRICE:
                    params_off["max_price"] = RAINFOREST_MAX_PRICE

                async with session.get(url, params=params_off, timeout=25) as r_off:
                    raw_off = await r_off.text()
                try:
                    js_off = json.loads(raw_off)
                except Exception:
                    break
                ri = js_off.get("request_info") if isinstance(js_off, dict) else None
                if isinstance(ri, dict) and ri.get("success") is False:
                    msg = ri.get("message") or "Rainforest API error"
                    return None, None, None, f"Rainforest error: {msg}", None
                offers = js_off.get("offers") or []
                for item in offers:
                    pval = parse_price_obj((item or {}).get("price"))
                    if pval:
                        candidates.append(pval)
                pagination = js_off.get("pagination") or {}
                tp = pagination.get("total_pages")
                total_pages = tp if isinstance(tp, int) else None
                if not offers:
                    break
                page += 1

            if candidates:
                price = min(candidates)

            params_prod = {"api_key": RAINFOREST_KEY, "type": "product", "asin": asin, "amazon_domain": domain}
            async with session.get(url, params=params_prod, timeout=25) as r_prod:
                raw_prod = await r_prod.text()
            try:
                js_prod = json.loads(raw_prod)
            except Exception:
                js_prod = {}
            ri2 = js_prod.get("request_info") if isinstance(js_prod, dict) else None
            if not (isinstance(ri2, dict) and ri2.get("success") is False):
                p = js_prod.get("product") if isinstance(js_prod, dict) else None
                if isinstance(p, dict):
                    brand = (p.get("brand") or "").strip() or brand
                    title = (p.get("title") or "").strip() or title
            note = "OK" if (price or brand or title) else f"No usable price domain={domain_id}"
            return price, brand, title, note, domain_id
        except asyncio.TimeoutError:
            return None, None, None, f"Timeout domain={domain_id}", None
        except Exception as e:
            return None, None, None, f"Exception domain={domain_id}: {e}", None

    diags = []
    for d in KEEPA_DOMAINS_TO_TRY:
        price, brand, title, note, used = await fetch_once(d)
        diags.append(note)
        if price is not None or (brand or title):
            return price, brand, title, " | ".join(diags), used
    return None, None, None, " | ".join(diags), None

async def product_fetch(session: aiohttp.ClientSession, asin: str) -> Tuple[Optional[float], Optional[str], Optional[str], str, Optional[int]]:
    if RAINFOREST_KEY:
        sp, b, t, d, u = await rainforest_fetch(session, asin)
        if sp is not None or (b or t):
            return sp, b, t, d, u
    return await keepa_fetch(session, asin)

# ---------------- Decisions ----------------
def decide(eligibility: Optional[str], profit: Optional[float], roi: Optional[float], ip_pl: bool) -> Tuple[bool, str]:
    if ip_pl:
        return False, "Blocked (IP/Private Label)"
    if eligibility:
        if eligibility.lower().startswith("n"):
            return False, "Eligibility: No"
        if eligibility.lower().startswith("u") and not ALLOW_UNKNOWN_ELIG:
            return False, "Eligibility unknown (disabled)"
    else:
        if not ALLOW_UNKNOWN_ELIG:
            return False, "Eligibility missing (disabled)"
    if profit is None or profit < MIN_PROFIT:
        return False, f"Profit {'missing' if profit is None else '< min'}"
    if roi is None or roi < MIN_ROI:
        return False, f"ROI {'missing' if roi is None else '< min'}"
    return True, "Approved"

# ---------------- Lead handling ----------------
async def handle_lead_message(message: discord.Message):
    # Respect watch-all / watched
    watch_all = CFG.get("watch_all", False)
    watched = set(CFG.get("watched_channels", []))
    
    # If watch_all is disabled AND there's a watch list AND this channel isn't in it, skip
    if not watch_all and watched and message.channel.id not in watched:
        log.debug(f"Skipping channel #{message.channel.name} (not in watch list)")
        return
    
    # If watch_all is disabled AND the watch list is empty, warn once but still process
    if not watch_all and not watched:
        log.warning(f"Processing message but no channels are watched! Use /watch_add or /watch_all")

    # 1) Plain-text rewrite
    txt = message_to_plaintext(message)

    # 2) Extract from text first
    asin, buy, sell, roi, elig, ip_pl = extract_from_text(txt)

    # 3) OCR fallback if critical fields missing
    if (buy is None or sell is None or roi is None or (not asin)):
        ocr_text = await ocr_try_extract_from_images(message)
        if ocr_text:
            asin2, buy2, sell2, roi2, elig2, ip_pl2 = extract_from_text(ocr_text)
            asin = asin or asin2
            if buy is None:  buy = buy2
            if sell is None: sell = sell2
            if roi is None:  roi = roi2
            if not elig:     elig = elig2
            ip_pl = ip_pl or ip_pl2

    if not asin:
        log.debug(f"No valid ASIN found in message from #{message.channel.name}, skipping")
        return
    
    # Double-check ASIN is valid before processing
    if not is_valid_asin(asin):
        log.warning(f"Invalid ASIN detected: {asin} - skipping message from #{message.channel.name}")
        return
    
    log.info(f"✓ Found ASIN: {asin} in #{message.channel.name}")
    log.info(f"  Initial data - Buy: {buy} | Sell: {sell} | ROI: {roi} | Elig: {elig}")

    # Always fetch Keepa data for ASIN (for Sell price, Brand, Title)
    # NOTE: Keepa only provides SELL prices, not BUY prices
    keepa_diag = ""
    domain_used: Optional[int] = None
    keepa_brand = None
    keepa_title = None
    keepa_sell = None
    
    log.info(f"  Fetching product data for {asin}...")
    async with aiohttp.ClientSession() as session:
        ks, brand, title, keepa_diag, domain_used = await product_fetch(session, asin)
        keepa_sell = ks
        if brand:
            keepa_brand = brand
        if title:
            keepa_title = title
        log.info(f"  Data result - Sell: {keepa_sell} | Brand: {keepa_brand} | Title: {keepa_title[:50] if keepa_title else None} | Domain: {domain_used}")
    
    # Use Keepa sell price if we don't have one from message
    if sell is None or sell <= 0:
        if keepa_sell:
            sell = keepa_sell
            log.info(f"  Using Sell price from Keepa: {sell}")
        else:
            log.warning(f"  No Sell price found in message OR Keepa")
    else:
        log.info(f"  Using Sell price from message: {sell} (Keepa had: {keepa_sell})")

    # Use DEFAULT_BUY if no Buy price in message
    if buy is None:
        if DEFAULT_BUY > 0:
            buy = DEFAULT_BUY
            log.info(f"  Using DEFAULT_BUY from .env: {buy}")
        else:
            log.warning(f"  No Buy price found in message AND DEFAULT_BUY is not set - ROI cannot be calculated")

    # Calculate profit and ROI from Buy + Sell
    profit, roi_calc = compute_profit_roi(buy, sell)
    if roi is None:
        roi = roi_calc
    
    log.info(f"  Final values - Buy: {buy} | Sell: {sell} | Profit: {profit} | ROI: {roi}")

    ok, reason = decide(elig, profit, roi, ip_pl)
    log.info(f"  Decision: {'✅ APPROVED' if ok else '❌ REJECTED'} - {reason}")

    # Build links (use domain used if we have it; else first candidate)
    amz_domain = domain_used if domain_used is not None else KEEPA_DOMAINS_TO_TRY[0]
    amz_url = amazon_url_for_domain(asin, amz_domain)
    sas_url = f"https://sas.selleramp.com/sas/lookup?asin={asin}&sas_cost_price={(buy or 0):.2f}&source_url={urllib.parse.quote(amz_url, safe='')}"

    # Build embed title with product info if available
    embed_title = keepa_title[:100] + "..." if keepa_title and len(keepa_title) > 100 else (keepa_title or f"ASIN: {asin}")
    
    embed = discord.Embed(
        title=("✅ Approved Lead" if ok else "❌ Not Approved"),
        description=f"**ASIN:** `{asin}`" + (f"\n**Brand:** {keepa_brand}" if keepa_brand else ""),
        color=0x18a558 if ok else 0xC23B22
    )
    
    # Add product title if we have it from Keepa
    if keepa_title:
        embed.add_field(name="Product", value=keepa_title[:200] + "..." if len(keepa_title) > 200 else keepa_title, inline=False)
    
    embed.add_field(name="Buy", value=money(buy), inline=True)
    embed.add_field(name="Sell", value=money(sell), inline=True)
    embed.add_field(name="Profit", value=money(profit), inline=True)

    # ROI field shows percent + (Sell, Buy)
    roi_str = pct(roi)
    if buy is not None and sell is not None:
        roi_str += f"  (Sell {money(sell)}, Buy {money(buy)})"
    elif buy is None:
        roi_str += "  (Buy price missing)"
    elif sell is None:
        roi_str += "  (Sell price missing)"
    embed.add_field(name="ROI", value=roi_str, inline=False)

    embed.add_field(name="Eligibility", value=(elig or "Unknown"), inline=True)
    embed.add_field(name="Links", value=f"[Amazon]({amz_url}) • [SAS]({sas_url})", inline=False)
    
    # Footer with data sources
    footer_parts = []
    if keepa_diag:
        footer_parts.append(f"Keepa: {keepa_diag}")
    if buy and DEFAULT_BUY > 0 and buy == DEFAULT_BUY:
        footer_parts.append("Buy from DEFAULT_BUY")
    if sell and keepa_sell and sell == keepa_sell:
        footer_parts.append("Sell from Keepa")
    if footer_parts:
        embed.set_footer(text=" • ".join(footer_parts))

    await message.reply(embed=embed, mention_author=False)
    if ok:
        sent = await try_dm(FORWARD_USER_ID, f"Lead from #{message.channel.name}", embed)
        if not sent:
            await try_send_channel(FORWARD_CHANNEL_ID, f"Lead from #{message.channel.name}", embed)

# ---------------- Health Check Server (for Digital Ocean/cloud platforms) ----------------
HEALTH_CHECK_PORT = int(os.getenv("PORT", "8080"))
health_check_app = None
health_check_runner = None

async def health_check_handler(request):
    """Simple health check endpoint that returns 200 OK if bot is running."""
    return web.json_response({
        "status": "healthy",
        "bot": {
            "ready": bot.is_ready() if bot else False,
            "user": str(bot.user) if bot and bot.user else None
        }
    }, status=200)

async def start_health_check_server():
    """Start the HTTP health check server on port 8080."""
    global health_check_app, health_check_runner
    try:
        health_check_app = web.Application()
        health_check_app.router.add_get("/", health_check_handler)
        health_check_app.router.add_get("/health", health_check_handler)
        
        runner = web.AppRunner(health_check_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HEALTH_CHECK_PORT)
        await site.start()
        health_check_runner = runner
        log.info(f"✓ Health check server started on port {HEALTH_CHECK_PORT}")
    except Exception as e:
        log.warning(f"Failed to start health check server: {e}")

# ---------------- Events ----------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        log.info("Synced %d commands", len(synced))
    except Exception as e:
        log.exception("Slash sync failed: %s", e)
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)
    log.info("Keepa domains: %s", KEEPA_DOMAINS_TO_TRY)
    log.info("Watch-all: %s | Watched: %s", CFG.get("watch_all", False), CFG.get("watched_channels", []))
    
    # Start health check server for cloud deployments
    await start_health_check_server()

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    await handle_lead_message(message)
    await bot.process_commands(message)

# ---------------- Slash commands ----------------
@bot.tree.command(name="watch_add", description="Start watching this channel (or pick another)")
async def watch_add(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    ch = channel or interaction.channel
    watched = set(CFG.get("watched_channels", []))
    watched.add(ch.id)
    CFG["watched_channels"] = list(watched)
    save_config(CFG)
    await interaction.response.send_message(f"Now watching <#{ch.id}>.", ephemeral=True)

@bot.tree.command(name="watch_remove", description="Stop watching a channel")
async def watch_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    watched = set(CFG.get("watched_channels", []))
    watched.discard(channel.id)
    CFG["watched_channels"] = list(watched)
    save_config(CFG)
    await interaction.response.send_message(f"Stopped watching <#{channel.id}>.", ephemeral=True)

@bot.tree.command(name="watch_list", description="List watched channels")
async def watch_list(interaction: discord.Interaction):
    watched = CFG.get("watched_channels", [])
    wa = CFG.get("watch_all", False)
    if wa:
        txt = "Watching *all channels*."
    elif not watched:
        txt = "No watched channels."
    else:
        names=[]
        for cid in watched:
            c = interaction.client.get_channel(cid)
            names.append(f"<#{cid}>" if c else str(cid))
        txt = "Watching: " + ", ".join(names)
    await interaction.response.send_message(txt, ephemeral=True)

@bot.tree.command(name="watch_all", description="Toggle watching ALL channels (on/off)")
async def watch_all(interaction: discord.Interaction, on: bool):
    CFG["watch_all"] = bool(on)
    save_config(CFG)
    await interaction.response.send_message(f"Watch-all set to *{CFG['watch_all']}*.", ephemeral=True)

@bot.tree.command(name="watch_add_all", description="Add ALL text channels in this server to the watch list")
async def watch_add_all(interaction: discord.Interaction):
    watched = set(CFG.get("watched_channels", []))
    for ch in interaction.guild.text_channels:
        watched.add(ch.id)
    CFG["watched_channels"] = list(watched)
    save_config(CFG)
    await interaction.response.send_message(f"Added *{len(interaction.guild.text_channels)}* channels to watch list.", ephemeral=True)

@bot.tree.command(name="watch_clear", description="Clear the watched channel list (keeps watch-all setting)")
async def watch_clear(interaction: discord.Interaction):
    CFG["watched_channels"] = []
    save_config(CFG)
    await interaction.response.send_message("Cleared watched channels.", ephemeral=True)

@bot.tree.command(name="settings", description="Show current filter & watch settings")
async def settings_cmd(interaction: discord.Interaction):
    watched = CFG.get("watched_channels", [])
    await interaction.response.send_message(
        f"*Filters*\n"
        f"- Eligibility required: {'Yes' if not ALLOW_UNKNOWN_ELIG else 'No (unknown allowed)'}\n"
        f"- Min Profit: {money(MIN_PROFIT)}\n"
        f"- Min ROI: {MIN_ROI:.2f}%\n"
        f"- Default Buy: {money(DEFAULT_BUY) if DEFAULT_BUY else '—'}\n"
        f"*Data Sources*\n"
        f"- Rainforest key set: {'Yes' if bool(RAINFOREST_KEY) else 'No'}\n"
        f"- Keepa domain input: {KEEPA_DOMAIN_RAW} → trying {KEEPA_DOMAINS_TO_TRY}\n"
        f"- Keepa key set: {'Yes' if bool(KEEPA_KEY) else 'No'}\n"
        f"*Watch*\n"
        f"- Watch-all: {CFG.get('watch_all', False)}\n"
        f"- Watched: {watched if watched else '[]'}",
        ephemeral=True
    )   

@bot.tree.command(name="set_min_profit", description="Set minimum profit (£)")
async def set_min_profit_cmd(interaction: discord.Interaction, value: float):
    global MIN_PROFIT
    MIN_PROFIT = float(value)
    await interaction.response.send_message(f"Min profit set to {money(MIN_PROFIT)}", ephemeral=True)

@bot.tree.command(name="set_min_roi", description="Set minimum ROI (%)")
async def set_min_roi_cmd(interaction: discord.Interaction, value: float):
    global MIN_ROI
    MIN_ROI = float(value)
    await interaction.response.send_message(f"Min ROI set to {MIN_ROI:.2f}%", ephemeral=True)

@bot.tree.command(name="set_default_buy", description="Set default buy price for ASIN-only leads")
async def set_default_buy_cmd(interaction: discord.Interaction, value: float):
    global DEFAULT_BUY
    DEFAULT_BUY = float(value)
    await interaction.response.send_message(f"Default buy set to {money(DEFAULT_BUY)}", ephemeral=True)

@bot.tree.command(name="set_allow_unknown_elig", description="Allow or block unknown eligibility")
async def set_allow_unknown_elig_cmd(interaction: discord.Interaction, allow: bool):
    global ALLOW_UNKNOWN_ELIG
    ALLOW_UNKNOWN_ELIG = bool(allow)
    await interaction.response.send_message(f"Allow unknown eligibility: {ALLOW_UNKNOWN_ELIG}", ephemeral=True)

@bot.tree.command(name="diag_asin", description="Check product price/brand/title and show diagnostics")
@app_commands.describe(asin="10-char ASIN", buy="Override buy price (optional)")
async def diag_asin(interaction: discord.Interaction, asin: str, buy: Optional[float] = None):
    asin = asin.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
        await interaction.response.send_message("Provide a valid 10-character ASIN.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        sell, brand, title, diag, used = await product_fetch(session, asin)
    amz_url = amazon_url_for_domain(asin, used if used is not None else KEEPA_DOMAINS_TO_TRY[0])
    effective_buy = buy if buy is not None else (DEFAULT_BUY if DEFAULT_BUY > 0 else None)
    profit = roi = None
    if effective_buy is not None and sell is not None:
        profit, roi = compute_profit_roi(effective_buy, sell)
    await interaction.followup.send(
        f"ASIN: *{asin}*\n"
        f"Brand: {brand or '—'}\n"
        f"Title: {title or '—'}\n"
        f"Sell: {money(sell)} | Buy: {money(effective_buy)}\n"
        f"Profit: {money(profit)} | ROI: {pct(roi)}\n"
        f"Amazon: {amz_url}\n"
        f"Diag: {diag}",
        ephemeral=True
    )

@bot.tree.command(name="calc_asin", description="Fetch Keepa price and compute ROI/Profit (uses default buy unless provided)")
@app_commands.describe(asin="10-char ASIN", buy="Override buy price (defaults to DEFAULT_BUY)")
async def calc_asin(interaction: discord.Interaction, asin: str, buy: Optional[float] = None):
    asin = asin.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
        await interaction.response.send_message("Provide a valid 10-character ASIN.", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)
    async with aiohttp.ClientSession() as session:
        sell, brand, title, diag, used = await product_fetch(session, asin)
    effective_buy = buy if buy is not None else (DEFAULT_BUY if DEFAULT_BUY > 0 else None)
    profit = roi = None
    if effective_buy is not None and sell is not None:
        profit, roi = compute_profit_roi(effective_buy, sell)
    amz_url = amazon_url_for_domain(asin, used if used is not None else KEEPA_DOMAINS_TO_TRY[0])
    await interaction.followup.send(
        f"ASIN: *{asin}*\n"
        f"Brand: {brand or '—'}\n"
        f"Title: {title or '—'}\n"
        f"Sell: {money(sell)} | Buy used: {money(effective_buy)}\n"
        f"Profit: {money(profit)} | ROI: {pct(roi)}\n"
        f"Amazon: {amz_url}\n"
        f"Diag: {diag}",
        ephemeral=True
    )

# ------------- Context menu: Show Plain Text (right-click a message → Apps) -----
@bot.tree.context_menu(name="Show Plain Text")
async def show_plain_ctx(interaction: discord.Interaction, message: discord.Message):
    txt = message_to_plaintext(message)
    preview = txt if len(txt) <= 1800 else txt[:1800] + "\n…(truncated)…"
    await interaction.response.send_message(f"text\n{preview}\n", ephemeral=True)

# ---------------- Run ----------------
if __name__ == "__main__":
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN missing in .env")
    bot.run(TOKEN)
async def try_send_channel(channel_id: int, content: str = "", embed: Optional[discord.Embed] = None) -> bool:
    if not channel_id: return False
    try:
        ch = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if embed: await ch.send(content=content, embed=embed)
        else:     await ch.send(content)
        return True
    except Exception as e:
        log.info("Channel send failed: %s", e)
        return False
