
# main.py — Amazon Lead Filter Bot (Final Pro + OCR)
#
# Features
#   • Filters Discord lead posts and DMs you ONLY when they pass:
#       - Eligibility: Yes (or missing if toggle enabled)
#       - ROI >= MIN_ROI (explicit ROI or approximate from Buy/Sell or Was/Now)
#       - No IP/PL/IP Alert
#   • ASIN extraction from:
#       - Amazon URLs in content, embed text, or title link
#       - Plain tokens like B0XXXXXXXX
#   • SAS links prefilled with Buy/Sell (or Was/Now) values
#   • Cross-server relay mapping: /link_channels, /link_clear
#   • Per-guild settings with persistence (config.json):
#       - /set_min_roi, /toggle_dm, /toggle_allow_missing_eligibility
#       - /set_dedupe_hours, /set_log_channel
#   • Diagnostics & status: /diag_last, /status
#   • Optional OCR for image-only posts (screenshots):
#       - Set OCR_PROVIDER=ocrspace (needs OCRSPACE_API_KEY) or OCR_PROVIDER=pytesseract
#   • Utility: /asin_links <asin> [tag] — build product links for multiple marketplaces
#
# Quick start
#   pip install -U discord.py python-dotenv aiohttp
#   # (optional OCR) pip install pillow pytesseract  (and install Tesseract from your OS)
#
#   .env:
#       DISCORD_TOKEN=...
#       FORWARD_USER_ID=123456789012345678
#       MIN_ROI=20
#       WATCH_CHANNEL_IDS=
#       BLOCK_ALERT_KEYWORDS=IP,PL
#       FALLBACK_TO_CHANNEL_ON_DM_FAIL=true
#       OCR_PROVIDER=ocrspace
#       OCRSPACE_API_KEY=YOUR_KEY
#       OCR_LANG=eng
#
# Permissions & Intents
#   • Invite with scopes: bot, applications.commands
#   • Permissions: View Channels, Send Messages, Read Message History, Use Slash Commands
#   • Dev Portal → Bot → Privileged Gateway Intents → enable MESSAGE CONTENT (and Server Members recommended)
#
import os, re, asyncio, logging, urllib.parse, json, time, io
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import aiohttp

VERSION = "1.5.0-final"

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s:%(name)s: %(message)s")
log = logging.getLogger("bot")

# ---------- Intents ----------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

# ---------- Bot ----------
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------- Env ----------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN not found. Put DISCORD_TOKEN=... in your .env")

FORWARD_USER_ID = int(os.getenv("FORWARD_USER_ID", "0"))
GLOBAL_MIN_ROI = float(os.getenv("MIN_ROI", "20"))
BLOCK_ALERTS = {s.strip().lower() for s in os.getenv("BLOCK_ALERT_KEYWORDS", "IP,PL").split(",") if s.strip()}
WATCH_IDS_ENV = [int(s.strip()) for s in os.getenv("WATCH_CHANNEL_IDS", "").split(",") if s.strip().isdigit()]
FALLBACK_TO_CHANNEL_ON_DM_FAIL = os.getenv("FALLBACK_TO_CHANNEL_ON_DM_FAIL", "true").lower() in {"1","true","yes","on"}

# OCR config
OCR_PROVIDER = (os.getenv("OCR_PROVIDER") or "").lower().strip()
OCRSPACE_API_KEY = os.getenv("OCRSPACE_API_KEY", "").strip()
OCR_LANG = os.getenv("OCR_LANG", "eng")

WATCHED_CHANNELS: set[int] = set(WATCH_IDS_ENV)

# ---------- Persistence (config.json) ----------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
DEFAULT_CONFIG = {"guilds":{}, "links":{}}

def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(DEFAULT_CONFIG))
    except Exception as e:
        log.exception("Failed to load config.json: %s", e); return json.loads(json.dumps(DEFAULT_CONFIG))

def save_config(cfg: Dict[str, Any]):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        log.exception("Failed to save config.json: %s", e)

CONFIG = load_config()

def get_guild_settings(guild_id: int) -> Dict[str, Any]:
    g = CONFIG.setdefault("guilds", {})
    return g.setdefault(str(guild_id), {
        "min_roi": GLOBAL_MIN_ROI,
        "dm_enabled": True,
        "allow_missing_eligibility": False,
        "log_channel_id": None,
        "dedupe_hours": 6.0
    })

def set_guild_settings(guild_id: int, **kwargs):
    s = get_guild_settings(guild_id)
    s.update({k:v for k,v in kwargs.items() if v is not None})
    save_config(CONFIG)

def set_channel_link(source_channel_id: int, dest_channel_id: Optional[int]):
    links = CONFIG.setdefault("links", {})
    if dest_channel_id is None:
        links.pop(str(source_channel_id), None)
    else:
        links[str(source_channel_id)] = int(dest_channel_id)
    save_config(CONFIG)

def get_link_destination(source_channel_id: int) -> Optional[int]:
    v = CONFIG.get("links", {}).get(str(source_channel_id))
    return int(v) if v is not None else None

# ---------- Dedupe (runtime) ----------
DEDUP_CACHE: Dict[int, Dict[str, float]] = {}
def should_dedupe(guild_id: int, asin_list: List[str], hours: float) -> List[str]:
    now = time.time(); win = hours*3600.0
    store = DEDUP_CACHE.setdefault(guild_id, {}); keep = []
    for a in asin_list:
        t = store.get(a, 0)
        if now - t >= win:
            keep.append(a); store[a] = now
    return keep

# ---------- Parsing ----------
ROI_RE = re.compile(r"(?:ROI|R\.?O\.?I\.?|Return\s+on\s+Investment|Est(?:imated)?\s*ROI)\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)\s*%", re.IGNORECASE)
ELIG_RE = re.compile(r"(?:Elig(?:ibility)?|Eligible)\s*[:=]?\s*(Yes|No)", re.IGNORECASE)
ALERT_LINE_RE = re.compile(r"Alerts?\s*[:=]?\s*(.*)", re.IGNORECASE)
AMAZON_URL_RE = re.compile(r"https?://(?:www\.)?amazon\.[^\s)>\]]+", re.IGNORECASE)
BLOCK_TOKEN_RE = re.compile(r"\b(ip|pl)\b", re.IGNORECASE)
IP_PHRASES = ["ip alert","ip-alert","ip alert:","ip violation"]

# Extra extractors
ASIN_TOKEN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b", re.IGNORECASE)
BUY_RE  = re.compile(r"\bBuy\s*[:=]\s*[£$€]?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)
SELL_RE = re.compile(r"\bSell\s*[:=]\s*[£$€]?\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)
WAS_RE  = re.compile(r"\bWas\s*[:=]\s*[£$€]?\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)
NOW_RE  = re.compile(r"\bNow\s*[:=]\s*[£$€]?\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)

def extract_asins_from_text(text: str) -> List[str]:
    return [m.group(1).upper() for m in ASIN_TOKEN_RE.finditer(text or "")]

def extract_asins_from_embeds(embeds: List[discord.Embed]) -> List[str]:
    found = []
    for e in embeds:
        parts = []
        if e.title: parts.append(e.title)
        if e.description: parts.append(e.description)
        for f in (e.fields or []): parts.append(f"{f.name}: {f.value}")
        if e.footer and e.footer.text: parts.append(e.footer.text)
        blob = "\n".join(parts)

        # ASIN tokens
        found.extend(extract_asins_from_text(blob))

        # Amazon links inside text
        for u in AMAZON_URL_RE.findall(blob or ""):
            a = extract_asin_from_url(u)
            if a: found.append(a)

        # Title hyperlink (embed.url)
        try:
            if e.url:
                a = extract_asin_from_url(str(e.url))
                if a: found.append(a)
        except Exception:
            pass

    # de-dup preserve order
    out, seen = [], set()
    for a in found:
        if a not in seen:
            seen.add(a); out.append(a)
    return out

def extract_asin_from_url(url: str) -> Optional[str]:
    try:
        parsed = urllib.parse.urlparse(url); path = parsed.path
        m = re.search(r"/dp/([A-Z0-9]{10})(?:/|$)", path, re.IGNORECASE)
        if not m: m = re.search(r"/gp/(?:product|aw/d)/([A-Z0-9]{10})(?:/|$)", path, re.IGNORECASE)
        if m: return m.group(1).upper()
        qs = urllib.parse.parse_qs(parsed.query or "")
        asin_vals = qs.get("asin") or qs.get("ASIN")
        if asin_vals:
            cand = (asin_vals[0] or "").strip().upper()
            if re.fullmatch(r"[A-Z0-9]{10}", cand):
                return cand
    except Exception:
        pass
    return None

def build_sas_url(asin: str, cost: Optional[float]=None, sale: Optional[float]=None, source_url: Optional[str]=None) -> str:
    base = "https://sas.selleramp.com/sas/lookup"
    params = {"asin": asin}
    if cost is not None: params["sas_cost_price"] = f"{cost:.2f}"
    if sale is not None: params["sas_sale_price"] = f"{sale:.2f}"
    if source_url: params["source_url"] = source_url
    return f"{base}?{urllib.parse.urlencode(params)}"

def approximate_roi_from_buy_sell(text: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (roi, buy, sell) using either Buy/Sell or Was/Now (as fallback)."""
    mb = BUY_RE.search(text or ""); ms = SELL_RE.search(text or "")
    if mb and ms:
        try:
            buy = float(mb.group(1)); sell = float(ms.group(1))
            if buy > 0:
                return round((sell-buy)/buy*100,2), buy, sell
        except Exception: pass
    mw = WAS_RE.search(text or ""); mn = NOW_RE.search(text or "")
    if mw and mn:
        try:
            was = float(mw.group(1)); now = float(mn.group(1))
            if now > 0:
                return round((was-now)/now*100,2), now, was
        except Exception: pass
    return None, None, None

@dataclass
class LeadDecision:
    eligible: Optional[bool]
    roi: Optional[float]
    has_block_alert: bool
    ok: bool
    reason: str

def parse_text_block(text: str) -> Tuple[Optional[bool], Optional[float], bool]:
    eligible = None; roi = None; has_block = False
    m = ELIG_RE.search(text); 
    if m: eligible = (m.group(1).strip().lower()=="yes")
    m = ROI_RE.search(text); 
    if m: roi = float(m.group(1))
    for line in text.splitlines():
        am = ALERT_LINE_RE.search(line)
        if am and BLOCK_TOKEN_RE.search(am.group(1).lower()):
            has_block=True; break
    if not has_block and any(p in text.lower() for p in IP_PHRASES): has_block=True
    return eligible, roi, has_block

def evaluate_message_text(text: str, min_roi: float, allow_missing_eligibility: bool) -> LeadDecision:
    eligible, roi, has_block = parse_text_block(text)
    if roi is None:
        approx, _, _ = approximate_roi_from_buy_sell(text); roi = approx
    ok=True; reasons: List[str]=[]
    if eligible is False:
        ok=False; reasons.append("Eligibility = No")
    if eligible is None and not allow_missing_eligibility:
        ok=False; reasons.append("Eligibility not found")
    if roi is None:
        ok=False; reasons.append("ROI not found")
    elif roi < min_roi:
        ok=False; reasons.append(f"ROI {roi}% < {min_roi}%")
    if has_block:
        ok=False; reasons.append("Blocked (IP/PL/IP Alert)")
    return LeadDecision(eligible, roi, has_block, ok, "; ".join(reasons) if reasons else "Pass")

async def send_log(guild: discord.Guild, message: str):
    gs = get_guild_settings(guild.id); ch_id = gs.get("log_channel_id")
    if not ch_id: return
    ch = guild.get_channel(int(ch_id)) or await bot.fetch_channel(int(ch_id))
    try: await ch.send(message[:1900])
    except Exception as e: log.warning("log send failed: %s", e)

async def forward_good_lead(msg: discord.Message, roi: float, extra: str, dm_enabled: bool):
    summary = f"Eligibility: Yes | ROI: {roi}% | Channel: #{getattr(msg.channel,'name',msg.channel.id)}"
    if extra: summary += "\n" + extra
    did = False
    if dm_enabled and FORWARD_USER_ID:
        try:
            u = await bot.fetch_user(FORWARD_USER_ID)
            dm = await u.create_dm(); await dm.send(f"**Approved Lead**\n{summary}\nJump: {msg.jump_url}")
            did = True
        except discord.Forbidden:
            if FALLBACK_TO_CHANNEL_ON_DM_FAIL: await msg.reply(f"Approved lead →\n{summary}"); did=True
        except Exception: pass
    if not did and not dm_enabled:
        await msg.reply(f"Approved lead →\n{summary}"); did=True
    await send_log(msg.guild, f"✅ Approved in <#{msg.channel.id}> (ROI {roi}%). {msg.jump_url}")
    # Relay
    dest_id = get_link_destination(msg.channel.id)
    if dest_id:
        dest = bot.get_channel(dest_id)
        if dest:
            emb = discord.Embed(title="Approved Lead (Relayed)", description=f"From **{msg.guild.name}** #{getattr(msg.channel,'name',msg.channel.id)}", color=0x00AAFF)
            emb.add_field(name="Summary", value=summary[:1024], inline=False)
            emb.add_field(name="Jump to Source", value=msg.jump_url, inline=False)
            try: await dest.send(embed=emb)
            except Exception as e: log.warning("relay failed: %s", e)

# ---------- OCR ----------
async def ocr_image_from_url(session: aiohttp.ClientSession, url: str) -> str:
    """Return extracted text from an image URL using configured OCR provider. Returns '' on failure."""
    if not OCR_PROVIDER: return ""
    try:
        if OCR_PROVIDER == "ocrspace" and OCRSPACE_API_KEY:
            api = "https://api.ocr.space/parse/imageurl"
            data = {"apikey": OCRSPACE_API_KEY, "url": url, "OCREngine": 2, "isOverlayRequired": False, "language": OCR_LANG}
            async with session.post(api, data=data, timeout=30) as r:
                js = await r.json()
                if js.get("IsErroredOnProcessing"): return ""
                results = js.get("ParsedResults") or []
                if results: return results[0].get("ParsedText") or ""
                return ""
        elif OCR_PROVIDER == "pytesseract":
            try:
                from PIL import Image
                import pytesseract
            except Exception:
                log.warning("pytesseract not available")
                return ""
            async with session.get(url) as resp:
                b = await resp.read()
            im = Image.open(io.BytesIO(b))
            return pytesseract.image_to_string(im)
    except Exception as e:
        log.warning("OCR error: %s", e)
    return ""

# ---------- Events ----------
@bot.event
async def on_ready():
    try: synced = await bot.tree.sync(); log.info("Synced %d commands", len(synced))
    except Exception as e: log.exception("sync failed: %s", e)
    log.info("Logged in as %s (ID: %s) | v%s", bot.user, bot.user.id, VERSION)
    if WATCHED_CHANNELS: log.info("Watching channels: %s", ", ".join(map(str, WATCHED_CHANNELS)))

@bot.event
async def on_message(message: discord.Message):
    if message.author.id == bot.user.id: return
    if WATCHED_CHANNELS and message.channel.id not in WATCHED_CHANNELS: return

    # Build text blob from content + embeds
    parts = [message.content or ""]
    for e in message.embeds:
        if e.title: parts.append(e.title)
        if e.description: parts.append(e.description)
        for f in (e.fields or []): parts.append(f"{f.name}: {f.value}")
        if e.footer and e.footer.text: parts.append(e.footer.text)

    blob = "\n".join([p for p in parts if p])

    # If blob empty & images exist → OCR
    if not blob.strip() and message.attachments:
        async with aiohttp.ClientSession() as session:
            for att in message.attachments:
                if att.content_type and att.content_type.startswith("image"):
                    txt = await ocr_image_from_url(session, att.url)
                    if txt:
                        blob += "\n" + txt

    gs = get_guild_settings(message.guild.id)
    min_roi = float(gs.get("min_roi", GLOBAL_MIN_ROI))
    dm_enabled = bool(gs.get("dm_enabled", True))
    allow_missing_elig = bool(gs.get("allow_missing_eligibility", False))
    dedupe_hours = float(gs.get("dedupe_hours", 6.0))

    urls = AMAZON_URL_RE.findall(blob)

    # Collect ASINs
    asin_candidates = []
    for u in urls:
        a = extract_asin_from_url(u)
        if a: asin_candidates.append(a)
    asin_candidates.extend(extract_asins_from_embeds(message.embeds))
    asin_candidates.extend(extract_asins_from_text(blob))

    seen=set(); asin_list=[]
    for a in asin_candidates:
        if a not in seen:
            seen.add(a); asin_list.append(a)

    approx_roi, buy_val, sell_val = approximate_roi_from_buy_sell(blob)

    # Build SAS lines
    asin_lines=[]
    for asin in asin_list:
        sas = build_sas_url(asin, cost=buy_val, sale=sell_val, source_url=(urls[0] if urls else None))
        part = f"- **{asin}**"
        if urls: part += f"\n  Amazon: {urls[0]}"
        part += f"\n  SAS: {sas}"
        asin_lines.append(part)

    decision = evaluate_message_text(blob, min_roi=min_roi, allow_missing_eligibility=allow_missing_elig)

    # Dedupe
    new_asins = should_dedupe(message.guild.id, asin_list, dedupe_hours) if asin_list else asin_list

    if decision.ok and decision.roi is not None:
        if asin_list and not new_asins:
            await send_log(message.guild, f"🟨 Dedupe skip in <#{message.channel.id}> — ASINs within {dedupe_hours}h: {', '.join(asin_list)}")
            return
        if asin_list and new_asins:
            filtered = []
            for asin, line in zip(asin_list, asin_lines):
                if asin in new_asins: filtered.append(line)
            extra = ("**Links**:\n" + "\n".join(filtered)) if filtered else ""
        else:
            extra = ("**Links**:\n" + "\n".join(asin_lines)) if asin_lines else ""
        await forward_good_lead(message, decision.roi, extra, dm_enabled=dm_enabled)

    await bot.process_commands(message)

# ---------- Commands ----------
@bot.tree.command(name="watch_add", description="Add a channel to the watch list (current if omitted)")
@app_commands.describe(channel="Channel to watch; defaults to current")
async def watch_add(interaction: discord.Interaction, channel: Optional[discord.TextChannel]=None):
    ch = channel or interaction.channel
    WATCHED_CHANNELS.add(ch.id)
    await interaction.response.send_message(f"Now watching <#{ch.id}>.", ephemeral=True)

@bot.tree.command(name="watch_remove", description="Remove a channel from the watch list")
async def watch_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    WATCHED_CHANNELS.discard(channel.id)
    await interaction.response.send_message(f"Stopped watching <#{channel.id}>.", ephemeral=True)

@bot.tree.command(name="watch_list", description="List watched channels")
async def watch_list(interaction: discord.Interaction):
    if not WATCHED_CHANNELS:
        await interaction.response.send_message("No channels are being watched.", ephemeral=True); return
    names = []
    for cid in WATCHED_CHANNELS:
        c = interaction.client.get_channel(cid); names.append(f"<#{cid}>" if c else str(cid))
    await interaction.response.send_message("Watching: " + ", ".join(names), ephemeral=True)

@bot.tree.command(name="settings", description="Show current filter & relay settings")
async def settings(interaction: discord.Interaction):
    gs = get_guild_settings(interaction.guild.id)
    link = get_link_destination(interaction.channel.id)
    await interaction.response.send_message(
        f"MIN_ROI (guild): {gs.get('min_roi')}%\n"
        f"DM enabled: {gs.get('dm_enabled')}\n"
        f"Allow missing Eligibility: {gs.get('allow_missing_eligibility')}\n"
        f"Dedupe hours: {gs.get('dedupe_hours')}\n"
        f"Log channel: {('<#'+str(gs.get('log_channel_id'))+'>') if gs.get('log_channel_id') else 'None'}\n"
        f"Fallback to channel on DM fail: {FALLBACK_TO_CHANNEL_ON_DM_FAIL}\n"
        f"Relay link (this channel): {('<#'+str(link)+'>') if link else 'None'}",
        ephemeral=True
    )

@bot.tree.command(name="set_min_roi", description="Set MIN_ROI for this server (guild)")
@app_commands.describe(value="Minimum ROI percentage (e.g., 20 for 20%)")
async def set_min_roi(interaction: discord.Interaction, value: app_commands.Range[float, 0, 100]):
    set_guild_settings(interaction.guild.id, min_roi=float(value))
    await interaction.response.send_message(f"Set MIN_ROI for this server to {value}%.", ephemeral=True)

@bot.tree.command(name="toggle_dm", description="Turn DM notifications on or off for this server")
@app_commands.describe(enabled="True to DM you; False for channel-only notifications")
async def toggle_dm(interaction: discord.Interaction, enabled: bool):
    set_guild_settings(interaction.guild.id, dm_enabled=bool(enabled))
    await interaction.response.send_message(f"DM notifications set to: {enabled}", ephemeral=True)

@bot.tree.command(name="toggle_allow_missing_eligibility", description="Allow pass when Eligibility is missing (uses ROI + Alerts only)")
@app_commands.describe(enabled="true or false")
async def toggle_allow_missing_eligibility(interaction: discord.Interaction, enabled: bool):
    set_guild_settings(interaction.guild.id, allow_missing_eligibility=bool(enabled))
    await interaction.response.send_message(f"Allow missing Eligibility set to: {enabled}", ephemeral=True)

@bot.tree.command(name="set_dedupe_hours", description="Skip re-sending same ASIN within N hours (per guild)")
@app_commands.describe(hours="Number of hours to dedupe (e.g., 6)")
async def set_dedupe_hours(interaction: discord.Interaction, hours: app_commands.Range[float, 0, 168]):
    set_guild_settings(interaction.guild.id, dedupe_hours=float(hours))
    await interaction.response.send_message(f"Dedupe window set to {hours} hours.", ephemeral=True)

@bot.tree.command(name="set_log_channel", description="Set a log channel for approvals and dedupe notices")
async def set_log_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    set_guild_settings(interaction.guild.id, log_channel_id=int(channel.id))
    await interaction.response.send_message(f"Log channel set to <#{channel.id}>.", ephemeral=True)

@bot.tree.command(name="link_channels", description="Link this channel to a destination channel for relay")
@app_commands.describe(source="Source (defaults to current)", destination="Destination channel to forward approved leads into")
async def link_channels(interaction: discord.Interaction, destination: discord.TextChannel, source: Optional[discord.TextChannel]=None):
    src = source or interaction.channel
    set_channel_link(src.id, destination.id)
    await interaction.response.send_message(f"Linked <#{src.id}> → <#{destination.id}> for approved-lead relay.", ephemeral=True)

@bot.tree.command(name="link_clear", description="Clear relay link for this channel")
async def link_clear(interaction: discord.Interaction, channel: Optional[discord.TextChannel]=None):
    ch = channel or interaction.channel
    set_channel_link(ch.id, None)
    await interaction.response.send_message(f"Cleared relay link for <#{ch.id}>.", ephemeral=True)

@bot.tree.command(name="test_dm", description="Send me a test DM to verify DM delivery")
async def test_dm(interaction: discord.Interaction):
    try:
        await interaction.user.send("✅ DM test from your AmazonLeadFilterBot worked!")
        await interaction.response.send_message("Sent you a DM. Check your inbox!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I couldn't DM you. Enable 'Allow DMs from server members' and try again.", ephemeral=True)

@bot.tree.command(name="sas_link", description="Build a SellerAmp link for a given ASIN")
@app_commands.describe(asin="The 10-character ASIN")
async def sas_link(interaction: discord.Interaction, asin: str):
    asin = asin.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
        await interaction.response.send_message("Please provide a valid 10-character ASIN.", ephemeral=True); return
    url = build_sas_url(asin)
    await interaction.response.send_message(f"**SAS:** {url}", ephemeral=True)

# -------- New utility: /asin_links --------
MARKETS = {
    "US": "https://www.amazon.com/dp/{asin}",
    "UK": "https://www.amazon.co.uk/dp/{asin}",
    "DE": "https://www.amazon.de/dp/{asin}",
    "FR": "https://www.amazon.fr/dp/{asin}",
    "IT": "https://www.amazon.it/dp/{asin}",
    "ES": "https://www.amazon.es/dp/{asin}",
    "CA": "https://www.amazon.ca/dp/{asin}",
    "AU": "https://www.amazon.com.au/dp/{asin}",
    "JP": "https://www.amazon.co.jp/dp/{asin}",
    "IN": "https://www.amazon.in/dp/{asin}",
}

@bot.tree.command(name="asin_links", description="Build Amazon product links for multiple regions")
@app_commands.describe(asin="10-character ASIN", tag="Optional affiliate tag (e.g., mytag-20)")
async def asin_links(interaction: discord.Interaction, asin: str, tag: Optional[str] = None):
    asin = asin.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
        await interaction.response.send_message("Please provide a valid 10-character ASIN.", ephemeral=True); return
    lines = []
    for region, tmpl in MARKETS.items():
        u = tmpl.format(asin=asin)
        if tag:
            sep = "&" if "?" in u else "?"
            u = f"{u}{sep}tag={tag}"
        lines.append(f"{region}: {u}")
    out = "\n".join(lines)
    await interaction.response.send_message(out[:1900], ephemeral=True)

@bot.tree.command(name="ping", description="Latency check")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"Pong! {round(bot.latency*1000)} ms", ephemeral=True)

@bot.tree.command(name="diag_last", description="Diagnose parsing of the most recent message in this channel")
async def diag_last(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    msg=None
    async for m in channel.history(limit=25):
        if m.author.bot: continue
        msg=m; break
    if msg is None:
        await interaction.followup.send("No recent user message found to diagnose.", ephemeral=True); return

    parts=[msg.content or ""]
    for e in msg.embeds:
        if e.title: parts.append(e.title)
        if e.description: parts.append(e.description)
        for f in (e.fields or []): parts.append(f"{f.name}: {f.value}")
        if e.footer and e.footer.text: parts.append(e.footer.text)
    blob="\n".join([p for p in parts if p])

    gs = get_guild_settings(interaction.guild.id)
    decision = evaluate_message_text(blob, min_roi=float(gs.get("min_roi", GLOBAL_MIN_ROI)), allow_missing_eligibility=bool(gs.get("allow_missing_eligibility", False)))

    urls = AMAZON_URL_RE.findall(blob)
    asin_candidates=[]
    for u in urls:
        a=extract_asin_from_url(u)
        if a: asin_candidates.append(a)
    asin_candidates.extend(extract_asins_from_embeds(msg.embeds))
    asin_candidates.extend(extract_asins_from_text(blob))
    seen=set(); asin_list=[]
    for a in asin_candidates:
        if a not in seen: seen.add(a); asin_list.append(a)
    approx_roi, buy_val, sell_val = approximate_roi_from_buy_sell(blob)
    asin_lines=[f"- {a}\n  SAS: {build_sas_url(a, cost=buy_val, sale=sell_val, source_url=(urls[0] if urls else None))}" for a in asin_list]

    report=[
        "**Diagnostics**",
        f"Eligible parsed: {decision.eligible}",
        f"ROI parsed: {decision.roi}",
        f"Blocked alert: {decision.has_block_alert}",
        f"OK to send: {decision.ok}",
        f"Reason: {decision.reason}",
        "",
        f"Guild MIN_ROI: {gs.get('min_roi')}% | DM: {gs.get('dm_enabled')} | AllowMissingEligibility: {gs.get('allow_missing_eligibility')} | Dedupe: {gs.get('dedupe_hours')}h",
        f"OCR: provider={OCR_PROVIDER or 'disabled'} lang={OCR_LANG}",
        f"Approx ROI (Buy/Sell or Was/Now): {approx_roi}",
        "",
        f"ASINs: {', '.join(asin_list) if asin_list else '(none)'}",
        *(asin_lines if asin_lines else ["(No SAS links)"]),
        "",
        f"Message link: {msg.jump_url}"
    ]
    out="\n".join(report)
    if len(out)>1900: out=out[:1900]+"\n...(truncated)"
    await interaction.followup.send(out, ephemeral=True)

@bot.tree.command(name="status", description="Bot status & configuration overview")
async def status_cmd(interaction: discord.Interaction):
    links = CONFIG.get("links", {})
    watching = ", ".join(f"<#{cid}>" for cid in WATCHED_CHANNELS) if WATCHED_CHANNELS else "none"
    intents_status = f"message_content={bot.intents.message_content}, members={bot.intents.members}"
    gs = get_guild_settings(interaction.guild.id)
    lines=[
        f"**Amazon Lead Filter Bot v{VERSION}**",
        f"Intents: {intents_status}",
        f"Watching (global): {watching}",
        f"Guild MIN_ROI: {gs.get('min_roi')}% | DM: {gs.get('dm_enabled')} | AllowMissingEligibility: {gs.get('allow_missing_eligibility')} | Dedupe: {gs.get('dedupe_hours')}h",
        f"OCR: provider={OCR_PROVIDER or 'disabled'} lang={OCR_LANG}",
        f"Log channel: {('<#'+str(gs.get('log_channel_id'))+'>') if gs.get('log_channel_id') else 'None'}",
        f"Config path: `{CONFIG_PATH}`",
        f"Relay links mapped: {len(links)}"
    ]
    count=0
    for k,v in links.items():
        lines.append(f"• <#{k}> → <#{v}>"); count+=1
        if count>=5: break
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

# ---------- Run ----------
if __name__ == "__main__":
    try: bot.run(TOKEN)
    except KeyboardInterrupt: log.info("Shutting down…")
