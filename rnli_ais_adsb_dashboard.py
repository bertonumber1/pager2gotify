#!/usr/bin/env python3
"""RNLI & AIS/ADS-B Dashboard — Scottish lifeboat, maritime & aircraft monitoring.  Port 8083."""

import asyncio, json, logging, math, os, re, shutil, signal, socket
import sqlite3, subprocess, sys, threading, time, urllib.parse, urllib.request
import cloudscraper as _cloudscraper
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse

# ── paths / meta ───────────────────────────────────────────────────────────────
_HERE       = Path(__file__).parent
_VERSION    = "1.0.0"
_PORT       = 8083
_DB_PATH    = _HERE / "incidents.db"
_LOG_FILE   = _HERE / "rnli_ais_adsb_dashboard.log"
_SETTINGS_F = _HERE / "settings.json"
_START_TIME = time.time()

# ── credentials (loaded from secrets.json — gitignored, never committed) ──────
# Copy secrets.example.json → secrets.json and fill in your own values.
_SECRETS_F = _HERE / "secrets.json"
try:
    _SECRETS = json.loads(_SECRETS_F.read_text())
except FileNotFoundError:
    sys.exit(f"FATAL: {_SECRETS_F} not found — copy secrets.example.json to "
             "secrets.json and fill in your credentials.")

TELEGRAM_BOT_TOKEN = _SECRETS.get("telegram_bot_token", "")
TELEGRAM_CHAT_ID   = _SECRETS.get("telegram_chat_id", "")
GOTIFY_URL         = _SECRETS.get("gotify_url", "http://localhost:8088")
GOTIFY_TOKEN       = _SECRETS.get("gotify_token", "")

# Worldtides API — register free at https://www.worldtides.info/developer
# Free tier: 5000 calls/month.  One call per refresh = ~52/year.
WORLDTIDES_KEY  = _SECRETS.get("worldtides_key", "")

# AISStream.io — real-time WebSocket AIS feed (wss://stream.aisstream.io/v0/stream)
AISSTREAM_KEY   = _SECRETS.get("aisstream_key", "")
APRS_FI_KEY     = _SECRETS.get("aprs_fi_key", "")
# Bounding box: all of Scotland + all of Ireland  [[latSW, lonSW], [latNE, lonNE]]
AISSTREAM_BOX   = [[51.0, -11.0], [61.0, -1.5]]
TIDE_LAT        = 55.9587     # Greenock / River Clyde
TIDE_LON        = -4.7656
TIDE_CACHE_SECS = 6 * 3600    # re-fetch no more than every 6 hours

# ── default settings (overridable from web UI → saved to settings.json) ───────
_DEFAULTS = {
    "telegram_enabled": True,
    "gotify_enabled":   True,
}


# ── AIS watch rules (mirrors ais_watch.py) ─────────────────────────────────────
AIS_SSE_URL          = "http://localhost:8100/api/sse"
AIS_BROADCAST_THROTTLE = 30    # seconds between position broadcasts per vessel
AIS_ALERT_DEDUP      = 1800    # seconds between repeat alerts for same MMSI
AIS_VESSEL_TTL       = 1200    # seconds before a silent vessel is expired (20 min)

AIS_SHIP_TYPES = {
    31: ("Towing/salvage",   5),
    35: ("Military ops",     7),
    51: ("SAR vessel",       8),
    55: ("Law enforcement",  7),
    58: ("Medical transport",6),
}
AIS_NAME_RULES = [
    ("HELENSBURGH", "RNLI Helensburgh",       10),
    ("RNLI",        "RNLI",                    9),
    ("LIFEBOAT",    "Lifeboat",                9),
    ("G-MC",        "HM Coastguard SAR",       9),  # Bristow SAR helicopters G-MCxx
    ("RESCUE",      "SAR Helicopter",          8),
    ("COASTGUARD",  "Coastguard",              8),
    ("SAR",         "SAR",                     8),
    ("HMS ",        "Royal Navy",              7),
    ("BORDER",      "Border Force",            7),
    ("POLICE",      "Police",                  7),
    ("HMC ",        "HMRC / Customs",          7),
    ("INTERCEPTOR", "Law enforcement",         7),
]
AIS_MMSI_RULES: dict = {
    # RNLI stations — Firth of Clyde & West Scotland
    232010167: ("RNLI Helensburgh — Angus and Muriel Mackay (B-903)",    10),
    232003706: ("RNLI Largs — Spirit of Largs (B-852)",                   10),
    232006660: ("RNLI Troon — Mora Edith MacDonald (Severn class)",        10),
    232003491: ("RNLI Girvan",                                             10),
    232003478: ("RNLI Arran — The Lady of Hilbre (Arran/Lamlash)",         10),
    232002543: ("RNLI Campbeltown",                                        10),
    232005827: ("RNLI Port Askaig (Islay)",                                10),
    232004532: ("RNLI Tobermory",                                          10),
    # HM Coastguard Search & Rescue vessels
    232004610: ("HMCG Clyde — SAR vessel",                                  9),
    232006800: ("HMCG Greenock MRU",                                        9),
    # Scottish Ambulance Service helicopter pads (AIS transponders)
    232009999: ("Scottish Ambulance Air Ambulance",                         8),
}
AIS_NOTIFY_LABELS = {
    "RNLI Helensburgh", "RNLI Largs", "RNLI Troon", "RNLI Girvan",
    "RNLI Arran", "RNLI Campbeltown", "RNLI Port Askaig", "RNLI Tobermory",
    "RNLI", "Lifeboat",
    "HM Coastguard SAR", "SAR Helicopter", "Coastguard",
    "HMCG Clyde — SAR vessel", "HMCG Greenock MRU",
    "SAR vessel", "SAR",
    "Royal Navy", "Military ops",
    "Police", "Law enforcement",
    "DISTRESS: AIS-SART", "DISTRESS: MOB Beacon", "DISTRESS: EPIRB",
}

AIS_LABEL_COLORS = {
    "DISTRESS: AIS-SART":   "#ff2222",
    "DISTRESS: MOB Beacon": "#ff2222",
    "DISTRESS: EPIRB":      "#ff2222",
    "RNLI Helensburgh":   "#3fb950",
    "RNLI":               "#3fb950",
    "Lifeboat":           "#3fb950",
    "HM Coastguard SAR":  "#f97316",
    "SAR Helicopter":     "#f97316",
    "Coastguard":         "#f97316",
    "SAR vessel":         "#3b82f6",
    "SAR":                "#3b82f6",
    "Royal Navy":         "#6366f1",
    "Military ops":       "#ef4444",
    "Border Force":       "#a855f7",
    "Police":             "#a855f7",
    "HMRC / Customs":     "#a855f7",
    "Law enforcement":    "#a855f7",
    "Towing/salvage":     "#eab308",
    "Medical transport":  "#f43f5e",
}

# ── ADS-B watch rules ──────────────────────────────────────────────────────────
ADSB_JSON_PATH         = Path("/run/readsb/aircraft.json")
ADSB_DB_PATH           = Path("/usr/local/share/tar1090/git-db/db")
ADSB_POLL_SECS         = 5
ADSB_BROADCAST_THROTTLE = 10
ADSB_ALERT_DEDUP       = 1800
ADSB_TTL               = 300    # remove aircraft silent >5 min
ADSB_TRACK_MIN_MOVE    = 0.0003
ADSB_TRACK_MIN_SECS    = 20
ADSB_TRACK_TTL_DAYS    = 7
ADSB_PHOTO_CACHE_TTL   = 30 * 86400

# Callsign prefix → (label, priority)
ADSB_CALLSIGN_RULES = [
    ("RESCUE",   "HM Coastguard SAR",   9),
    ("SAR",      "SAR",                 8),
    ("COAST",    "Coastguard",          8),
    ("SHREK",    "HM Coastguard SAR",   9),  # MCA fixed-wing callsign
    ("RRR",      "Rescue",              9),
    ("POLICE",   "Police",              7),
    ("BORDER",   "Border Force",        7),
    ("HELIMED",  "Air Ambulance",       7),
    ("SCOTAMB",  "Scottish Air Ambulance", 7),
    ("AMBUL",    "Air Ambulance",       7),
    ("NAVY",     "Royal Navy",          7),
    ("ATLAS",    "Military",            7),
    ("RAFAIR",   "RAF",                 7),
    ("ASCOT",    "RAF",                 7),  # RAF transport callsign
    ("VIPER",    "RAF",                 7),
    ("TARTAN",   "Military",            7),
    ("RECCE",    "Military",            7),
]

# Registration prefix → (label, priority)
ADSB_REG_RULES = [
    ("G-MC",  "HM Coastguard SAR",   9),  # Bristow/Babcock S-92/AW189 SAR fleet
    ("ZJ",    "Military",            8),
    ("ZH",    "Military",            8),
    ("ZK",    "Military",            8),
    ("ZM",    "Military",            8),
    ("ZA",    "Military",            8),
    ("ZB",    "Military",            8),
    ("ZC",    "Military",            8),
    ("ZD",    "Military",            8),
    ("ZE",    "Military",            8),
    ("ZF",    "Military",            8),
    ("ZG",    "Military",            8),
    ("ZP",    "Military",            8),
]

ADSB_NOTIFY_LABELS = {
    "HM Coastguard SAR", "SAR", "Coastguard", "Rescue",
    "Air Ambulance", "Scottish Air Ambulance",
    "Royal Navy", "Military", "RAF", "Army Air Corps",
    "Police", "Border Force",
}

ADSB_LABEL_COLORS = {
    "HM Coastguard SAR":       "#f97316",
    "SAR":                     "#f97316",
    "Coastguard":              "#f97316",
    "Rescue":                  "#f97316",
    "Air Ambulance":           "#f43f5e",
    "Scottish Air Ambulance":  "#f43f5e",
    "Royal Navy":              "#6366f1",
    "Military":                "#ef4444",
    "RAF":                     "#3b82f6",
    "Army Air Corps":          "#22c55e",
    "Police":                  "#a855f7",
    "Border Force":            "#a855f7",
    "Emergency":               "#ff0000",
}

# ── settings ───────────────────────────────────────────────────────────────────
def _load_cfg() -> dict:
    s = dict(_DEFAULTS)
    if _SETTINGS_F.exists():
        try: s.update(json.loads(_SETTINGS_F.read_text()))
        except Exception: pass
    return s

def _save_cfg(s: dict):
    _SETTINGS_F.write_text(json.dumps(s, indent=2))

cfg = _load_cfg()

# ── logging ────────────────────────────────────────────────────────────────────
def _setup_logging():
    fmt  = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
    root = logging.getLogger()
    # Only configure once — guard against uvicorn re-entry
    if any(isinstance(h, logging.FileHandler) for h in root.handlers):
        return
    root.handlers.clear()
    fh = logging.FileHandler(_LOG_FILE)
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh): h.setFormatter(fmt)
    root.setLevel(logging.INFO)
    root.addHandler(fh)
    root.addHandler(sh)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").propagate = False

_setup_logging()
log = logging.getLogger("dashboard")

# ── DB ─────────────────────────────────────────────────────────────────────────
from contextlib import contextmanager

@contextmanager
def _db():
    """Yield a connection that commits on success, rolls back on error, and
    ALWAYS closes — leaked connections held stale read snapshots and caused
    'database table is locked' during track cleanup."""
    c = sqlite3.connect(_DB_PATH, check_same_thread=False)
    try:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=5000")  # wait for concurrent writers instead of erroring
        with c:      # transaction: commit / rollback
            yield c
    finally:
        c.close()

def _init_db():
    with _db() as c:
        c.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS ais_track (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            mmsi    INTEGER NOT NULL,
            ts      INTEGER NOT NULL,
            lat     REAL NOT NULL,
            lon     REAL NOT NULL,
            speed   REAL,
            heading REAL
        );
        CREATE INDEX IF NOT EXISTS idx_ais_mmsi_ts ON ais_track(mmsi, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_ais_ts      ON ais_track(ts DESC);
        CREATE TABLE IF NOT EXISTS ais_photos (
            mmsi      INTEGER PRIMARY KEY,
            photo_url TEXT,
            fetched   INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watched_vessels (
            mmsi  INTEGER PRIMARY KEY,
            name  TEXT,
            added INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tides (
            id      INTEGER PRIMARY KEY CHECK (id=1),
            fetched INTEGER NOT NULL,
            data    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS aircraft_photos (
            icao_hex  TEXT PRIMARY KEY,
            photo_url TEXT,
            thumb_url TEXT,
            fetched   INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS adsb_track (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            icao_hex  TEXT NOT NULL,
            ts        INTEGER NOT NULL,
            lat       REAL NOT NULL,
            lon       REAL NOT NULL,
            alt       INTEGER,
            speed     REAL,
            track     REAL
        );
        CREATE INDEX IF NOT EXISTS idx_adsb_hex_ts ON adsb_track(icao_hex, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_adsb_ts     ON adsb_track(ts DESC);
        CREATE TABLE IF NOT EXISTS aircraft_intel (
            icao_hex    TEXT PRIMARY KEY,
            registration TEXT,
            icao_type   TEXT,
            type_desc   TEXT,
            manufacturer TEXT,
            owner       TEXT,
            label       TEXT,
            priority    INTEGER,
            first_seen  INTEGER,
            last_seen   INTEGER,
            alert_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS vessel_intel (
            mmsi        INTEGER PRIMARY KEY,
            name        TEXT,
            ship_type   INTEGER,
            label       TEXT,
            priority    INTEGER,
            first_seen  INTEGER,
            last_seen   INTEGER,
            alert_count INTEGER DEFAULT 0
        );
        """)

_init_db()

def _haversine_nm(lat1, lon1, lat2, lon2) -> float:
    R = 3440.065
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

# ── Aircraft DB (tar1090 offline registration / type lookup) ──────────────────
import gzip as _gzip

_adsb_db_cache: dict = {}
_adsb_db_lock  = threading.Lock()
_adsb_mil_ranges: list = []   # [(start_int, end_int), ...]

def _load_adsb_db():
    global _adsb_mil_ranges
    try:
        fp = ADSB_DB_PATH / "ranges.js"
        if fp.exists():
            with _gzip.open(fp, "rt") as f:
                data = json.load(f)
            _adsb_mil_ranges = [
                (int(s, 16), int(e, 16))
                for s, e in data.get("military", [])
            ]
            log.info(f"ADS-B: loaded {len(_adsb_mil_ranges)} military hex ranges")
    except Exception as ex:
        log.warning(f"ADS-B DB ranges: {ex}")
    # Pre-warm UK civil and Irish aircraft DBs
    for prefix in ("40", "48"):
        _adsb_db_prefix(prefix)
    log.info("ADS-B: aircraft DB ready")

def _adsb_db_prefix(prefix: str) -> dict:
    prefix = prefix.upper()
    with _adsb_db_lock:
        if prefix in _adsb_db_cache:
            return _adsb_db_cache[prefix]
    fp = ADSB_DB_PATH / f"{prefix}.js"
    if not fp.exists():
        with _adsb_db_lock:
            _adsb_db_cache[prefix] = {}
        return {}
    try:
        with _gzip.open(fp, "rt") as f:
            d = json.load(f)
        with _adsb_db_lock:
            _adsb_db_cache[prefix] = d
        return d
    except Exception as ex:
        log.debug(f"ADS-B DB load {prefix}: {ex}")
        with _adsb_db_lock:
            _adsb_db_cache[prefix] = {}
        return {}

def _adsb_lookup_db(hex_code: str) -> dict:
    """Look up registration/type from local tar1090 DB. Returns {} if not found."""
    hx = hex_code.upper().zfill(6)
    for prefix_len in (3, 2):
        prefix = hx[:prefix_len]
        suffix = hx[prefix_len:]
        db = _adsb_db_prefix(prefix)
        entry = db.get(suffix)
        if entry and isinstance(entry, list) and entry[0] and entry[0] not in ("TWR",):
            return {
                "registration": entry[0] or "",
                "icao_type":    entry[1] or "" if len(entry) > 1 else "",
                "flags":        entry[2] or "" if len(entry) > 2 else "",
                "type_desc":    entry[3] or "" if len(entry) > 3 else "",
            }
    return {}

def _is_military_hex(hex_code: str) -> bool:
    try:
        h = int(hex_code, 16)
        return any(s <= h <= e for s, e in _adsb_mil_ranges)
    except ValueError:
        return False

_load_adsb_db()

# ── AIS NMEA helpers ───────────────────────────────────────────────────────────
def _ais_nmea_bits(payload):
    bits = []
    for c in payload:
        v = ord(c) - 48
        if v > 40: v -= 8
        for i in range(5, -1, -1): bits.append((v >> i) & 1)
    return bits

def _ais_ubits(bits, start, n):
    v = 0
    for i in range(n): v = (v << 1) | bits[start + i]
    return v

def _ais_sbits(bits, start, n):
    v = _ais_ubits(bits, start, n)
    if v & (1 << (n - 1)): v -= (1 << n)
    return v

def _ais_ship_type(nmea_list):
    try:
        s = next((x for x in nmea_list if x.startswith("!AI")), "")
        payload = s.split(",")[5] if s else ""
        if not payload: return None
        bits = _ais_nmea_bits(payload)
        mt = _ais_ubits(bits, 0, 6)
        if mt == 5 and len(bits) >= 240:
            return _ais_ubits(bits, 232, 8)
        if mt == 24 and len(bits) >= 48 and _ais_ubits(bits, 38, 2) == 1:
            return _ais_ubits(bits, 40, 8)
    except Exception: pass
    return None

def _ais_position(nmea_list):
    try:
        s = next((x for x in nmea_list if x.startswith("!AI")), "")
        payload = s.split(",")[5] if s else ""
        if not payload: return None
        bits = _ais_nmea_bits(payload)
        mt = _ais_ubits(bits, 0, 6)
        if mt in (1, 2, 3) and len(bits) >= 116:
            lon = _ais_sbits(bits, 61, 28) / 600000.0
            lat = _ais_sbits(bits, 89, 27) / 600000.0
        elif mt == 18 and len(bits) >= 112:
            lon = _ais_sbits(bits, 57, 28) / 600000.0
            lat = _ais_sbits(bits, 85, 27) / 600000.0
        else: return None
        if abs(lon) >= 181 or abs(lat) >= 91: return None
        return lat, lon
    except Exception: pass
    return None

def _ais_motion(nmea_list):
    """Decode SOG / COG / true heading from position reports (types 1/2/3, 18).
    Returns dict with any of speed (kn), cog (deg), heading (deg) — or None."""
    try:
        s = next((x for x in nmea_list if x.startswith("!AI")), "")
        payload = s.split(",")[5] if s else ""
        if not payload: return None
        bits = _ais_nmea_bits(payload)
        mt = _ais_ubits(bits, 0, 6)
        if mt in (1, 2, 3) and len(bits) >= 137:
            sog, cog, hdg = (_ais_ubits(bits, 50, 10),
                             _ais_ubits(bits, 116, 12),
                             _ais_ubits(bits, 128, 9))
        elif mt == 18 and len(bits) >= 133:
            sog, cog, hdg = (_ais_ubits(bits, 46, 10),
                             _ais_ubits(bits, 112, 12),
                             _ais_ubits(bits, 124, 9))
        else: return None
        return {"speed":   sog / 10.0 if sog != 1023 else None,
                "cog":     cog / 10.0 if cog != 3600 else None,
                "heading": hdg        if hdg != 511  else None}
    except Exception: pass
    return None

def _ais_match(mmsi, name, nmea):
    # Distress beacons: dedicated MMSI prefixes (ITU-R M.585) — always top priority
    ms = str(mmsi)
    if ms.startswith("970"): return "DISTRESS: AIS-SART", 10
    if ms.startswith("972"): return "DISTRESS: MOB Beacon", 10
    if ms.startswith("974"): return "DISTRESS: EPIRB", 10
    if mmsi in AIS_MMSI_RULES: return AIS_MMSI_RULES[mmsi]
    upper = (name or "").upper()
    for kw, lbl, pri in AIS_NAME_RULES:
        if kw in upper: return lbl, pri
    st = _ais_ship_type(nmea)
    if st and st in AIS_SHIP_TYPES: return AIS_SHIP_TYPES[st]
    return None, None

# ── local photo cache ──────────────────────────────────────────────────────────
_PHOTOS_DIR = Path(__file__).parent / "photos"
_PHOTOS_DIR.mkdir(exist_ok=True)
_PHOTO_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

def _cache_photo(url, fname: str):
    """Download url into photos/<fname> once; return the local /photos/ URL.
    Falls back to the original hotlink URL on any failure. Never raises."""
    if not url:
        return None
    if url.startswith("/photos/"):
        return url                                    # already local
    path = _PHOTOS_DIR / fname
    try:
        if not path.exists() or path.stat().st_size == 0:
            req  = urllib.request.Request(url, headers={"User-Agent": _PHOTO_UA})
            data = urllib.request.urlopen(req, timeout=15).read()
            # sanity: only store real images
            if len(data) < 2000 or not (data[:3] == b"\xff\xd8\xff"
                                        or data[:8] == b"\x89PNG\r\n\x1a\n"
                                        or data[:4] == b"RIFF"):
                return url
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.rename(path)
        return f"/photos/{fname}"
    except Exception as e:
        log.debug(f"photo cache {fname}: {e}")
        return url

# ── Shipspotting.com photo scraper (cloudscraper bypasses Cloudflare) ──────────
_ss_scraper: object = None
_ss_scraper_lock = threading.Lock()

def _get_ss_scraper():
    global _ss_scraper
    if _ss_scraper is None:
        with _ss_scraper_lock:
            if _ss_scraper is None:
                _ss_scraper = _cloudscraper.create_scraper(
                    browser={"browser": "chrome", "platform": "windows", "mobile": False}
                )
    return _ss_scraper

_SS_PHOTO_RE = re.compile(
    r'https://www\.shipspotting\.com/photos/middle/[0-9/]+\.jpg', re.I
)

def _shipspotting_photo(ship_name: str) -> str | None:
    """Return first shipspotting.com photo URL for a vessel name. Never raises."""
    if not ship_name or len(ship_name) < 3:
        return None
    try:
        scraper = _get_ss_scraper()
        url = (f"https://www.shipspotting.com/photos"
               f"?shipName={urllib.parse.quote(ship_name)}&shipNameSearchMode=begins")
        r = scraper.get(url, timeout=15)
        m = _SS_PHOTO_RE.search(r.text)
        return m.group(0) if m else None
    except Exception as e:
        log.debug(f"Shipspotting photo {ship_name}: {e}")
        return None

# ── Planespotters.net photo API ────────────────────────────────────────────────
_PS_UA = "AisAdsbDashboard/1.0 (+mailto:adsbgreenock@gmail.com)"

def _planespotters_photo(hex_code: str, reg: str = "") -> tuple:
    """Return (photo_url, thumb_url) from planespotters.net, or (None, None). Never raises."""
    endpoints = [f"hex/{hex_code.upper()}"]
    if reg:
        endpoints.append(f"reg/{urllib.parse.quote(reg)}")
    for ep in endpoints:
        try:
            req = urllib.request.Request(
                f"https://api.planespotters.net/pub/photos/{ep}",
                headers={"User-Agent": _PS_UA}
            )
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            photos = data.get("photos", [])
            if photos:
                p = photos[0]
                large = (p.get("thumbnail_large") or {}).get("src")
                thumb = (p.get("thumbnail") or {}).get("src")
                return large or thumb, thumb or large
        except Exception as e:
            log.debug(f"Planespotters {ep}: {e}")
    return None, None

_VF_PHOTO_RE = re.compile(
    r'<img[^>]+src="(https://static\.vesselfinder\.net/ship-photo/[^"]+)"',
    re.IGNORECASE,
)

def _vf_detail_url(mmsi: int, name: str = "", imo = None) -> str:
    """Build VesselFinder vessel detail URL (server-rendered, has photo)."""
    slug = re.sub(r'[^A-Z0-9]+', '-', (name or "").upper()).strip('-')
    if slug and imo:
        return f"https://www.vesselfinder.com/vessels/{slug}-IMO-{imo}-MMSI-{mmsi}"
    elif slug:
        return f"https://www.vesselfinder.com/vessels/{slug}-MMSI-{mmsi}"
    return f"https://www.vesselfinder.com/?mmsi={mmsi}"

def _ais_fetch_vessel_info(mmsi: int) -> dict:
    """Return dict with vessel_url, photo_url, and local data from AIS-catcher. Never raises."""
    result: dict = {"vessel_url": f"https://www.vesselfinder.com/?mmsi={mmsi}",
                    "photo_url": None, "local": {}}

    # Enrich from in-memory AIS vessel store (fastest, always available)
    with _ais_lock:
        cached = _ais_vessels.get(mmsi)
    if cached:
        result["local"] = cached
    else:
        # Fallback: try AIS-catcher ships endpoint
        try:
            req  = urllib.request.Request("http://localhost:8100/ships.json")
            data = json.loads(urllib.request.urlopen(req, timeout=5).read())
            for ship in data.get("ships", []):
                if ship.get("mmsi") == mmsi:
                    result["local"] = ship
                    break
        except Exception as e:
            log.debug(f"AIS local lookup {mmsi}: {e}")

    loc  = result["local"]
    name = (loc.get("name") or loc.get("shipname") or "").strip()
    imo  = loc.get("imo")
    if not name:
        # Vessel no longer tracked live — fall back to the intel history name
        try:
            with _db() as c:
                row = c.execute("SELECT name FROM vessel_intel WHERE mmsi=?", (mmsi,)).fetchone()
                if row and row["name"]: name = row["name"]
        except Exception:
            pass
    detail_url = _vf_detail_url(mmsi, name, imo)
    result["vessel_url"] = detail_url

    # Check photo cache (re-fetch after 30 days)
    _PHOTO_CACHE_TTL = 30 * 86400
    now = int(time.time())
    try:
        with _db() as c:
            row = c.execute("SELECT photo_url, fetched FROM ais_photos WHERE mmsi=?", (mmsi,)).fetchone()
            if row and (now - row["fetched"]) < _PHOTO_CACHE_TTL:
                result["photo_url"] = _cache_photo(row["photo_url"], f"ship_{mmsi}.jpg")
                return result
    except Exception as e:
        log.debug(f"AIS photo cache read {mmsi}: {e}")

    # Primary: scrape VesselFinder — keyed by MMSI so the photo is for the RIGHT vessel.
    # (shipspotting.com disabled 2026-07-09: their ?shipName= search now ignores the
    #  query and returns "latest uploads", so every vessel got the same wrong photo)
    if not result["photo_url"]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-GB,en;q=0.9",
        }
        try:
            req  = urllib.request.Request(detail_url, headers=headers)
            html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
            m = _VF_PHOTO_RE.search(html)
            if m:
                result["photo_url"] = m.group(1)
        except Exception as e:
            log.debug(f"AIS photo VesselFinder {detail_url}: {e}")

    # Mirror the photo to local disk so it survives remote sites blocking us
    if result["photo_url"]:
        result["photo_url"] = _cache_photo(result["photo_url"], f"ship_{mmsi}.jpg")

    # Cache the result (even None = no photo, avoids re-scraping)
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO ais_photos(mmsi,photo_url,fetched) VALUES(?,?,?) "
                "ON CONFLICT(mmsi) DO UPDATE SET photo_url=excluded.photo_url, fetched=excluded.fetched",
                (mmsi, result["photo_url"], now)
            )
    except Exception as e:
        log.debug(f"AIS photo cache write {mmsi}: {e}")

    return result

def _ais_intel_update(v: dict):
    """Persist alerted vessels into vessel_intel (mirrors aircraft_intel)."""
    try:
        now = int(time.time())
        with _db() as c:
            c.execute("""
                INSERT INTO vessel_intel(mmsi,name,ship_type,label,priority,
                                         first_seen,last_seen,alert_count)
                VALUES(?,?,?,?,?,?,?,1)
                ON CONFLICT(mmsi) DO UPDATE SET
                  name       = CASE WHEN excluded.name != '' THEN excluded.name
                                    ELSE vessel_intel.name END,
                  ship_type  = COALESCE(excluded.ship_type, vessel_intel.ship_type),
                  label      = excluded.label,
                  priority   = excluded.priority,
                  last_seen  = excluded.last_seen,
                  alert_count = vessel_intel.alert_count + 1
            """, (v["mmsi"], v.get("name") or "", v.get("ship_type"),
                  v.get("label"), v.get("priority"), now, now))
    except Exception as e:
        log.debug(f"vessel_intel update {v.get('mmsi')}: {e}")

# ── APRS-IS live feed (all stations over Scotland, incl. LoRa APRS) ────────────
_APRS_CALL   = _SECRETS.get("aprs_callsign", "N0CALL")
_APRS_HOST   = "euro.aprs2.net"
_APRS_PORT   = 14580
_APRS_FILTER = "r/56.5/-4.5/400"    # 400 km radius — covers all of Scotland
_APRS_EXPIRE = 7200                 # drop stations not heard for 2 h
_aprs_is_stations: dict = {}        # callsign → station dict
_aprs_is_lock   = threading.Lock()
_aprs_is_status = "disconnected"
_aprs_is_last_bc: dict = {}         # callsign → last SSE broadcast ts

def _aprs_passcode(callsign: str) -> int:
    """Standard APRS-IS passcode hash for a callsign."""
    call = callsign.split("-")[0].upper()
    h = 0x73E2
    for i, c in enumerate(call):
        h ^= ord(c) << (8 if i % 2 == 0 else 0)
    return h & 0x7FFF

def _aprs_is_reader():
    global _aprs_is_status
    try:
        import aprslib
    except ImportError:
        log.warning("aprslib not installed — APRS-IS live feed disabled")
        return
    while True:
        s = None
        try:
            s = socket.create_connection((_APRS_HOST, _APRS_PORT), timeout=15)
            s.settimeout(90)
            s.recv(512)     # server banner
            s.sendall((f"user {_APRS_CALL} pass {_aprs_passcode(_APRS_CALL)} "
                       f"vers ais-adsb-dash 1.0 filter {_APRS_FILTER}\r\n").encode())
            _aprs_is_status = "connected"
            log.info(f"APRS-IS connected: {_APRS_HOST} as {_APRS_CALL}, filter {_APRS_FILTER}")
            buf = b""
            last_expire = time.time()
            while True:
                data = s.recv(4096)
                if not data:
                    raise ConnectionError("server closed connection")
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    txt = line.decode("utf-8", errors="replace").strip()
                    if not txt or txt.startswith("#"):
                        continue
                    try:
                        p = aprslib.parse(txt)
                    except Exception:
                        continue
                    lat, lon = p.get("latitude"), p.get("longitude")
                    if lat is None or lon is None:
                        continue
                    call = p.get("from") or ""
                    dest = p.get("to") or ""
                    path = " ".join(p.get("path") or [])
                    now  = int(time.time())
                    st = {
                        "call":    call,
                        "lat":     round(lat, 5),
                        "lon":     round(lon, 5),
                        "symbol":  p.get("symbol") or "",
                        "table":   p.get("symbol_table") or "",
                        "comment": str(p.get("comment") or p.get("status") or "")[:120],
                        "speed":   p.get("speed"),
                        "course":  p.get("course"),
                        "alt":     p.get("altitude"),
                        "lora":    dest.startswith("APLR") or "APLR" in path,
                        "last_ts": now,
                    }
                    with _aprs_is_lock:
                        _aprs_is_stations[call] = st
                    if now - _aprs_is_last_bc.get(call, 0) >= 20:
                        _aprs_is_last_bc[call] = now
                        _broadcast({"type": "aprs_station", **st})
                if time.time() - last_expire > 300:
                    last_expire = time.time()
                    cutoff = time.time() - _APRS_EXPIRE
                    with _aprs_is_lock:
                        gone = [c for c, v in _aprs_is_stations.items()
                                if v["last_ts"] < cutoff]
                        for c in gone:
                            del _aprs_is_stations[c]
                    for c in gone:
                        _aprs_is_last_bc.pop(c, None)
                        _broadcast({"type": "aprs_remove", "call": c})
        except Exception as e:
            _aprs_is_status = "disconnected"
            log.warning(f"APRS-IS error: {e} — reconnecting in 30s")
            try:
                if s: s.close()
            except Exception:
                pass
            time.sleep(30)

# ── community ADS-B feed (adsb.fi / adsb.lol / airplanes.live) ─────────────────
# All three run readsb-compatible v2 APIs. We feed these networks from this Pi,
# so pulling the aggregate view back is fair use. Sources are rotated so each
# gets one request every ~90s (guides ask for ≤1 req/s).
_COMMUNITY_ADSB_SOURCES = [
    ("adsb.fi",        "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}"),
    ("adsb.lol",       "https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{dist}"),
    ("airplanes.live", "https://api.airplanes.live/v2/point/{lat}/{lon}/{dist}"),
]
_COMMUNITY_ADSB_LAT    = 55.95
_COMMUNITY_ADSB_LON    = -4.76
_COMMUNITY_ADSB_DIST   = 250          # nm (API max)
_COMMUNITY_ADSB_PERIOD = 30           # seconds between polls (rotating source)
_community_aircraft: dict = {}        # hex → aircraft dict (network-seen only)

def _community_adsb_poll():
    src_i = 0
    time.sleep(25)
    while True:
        name, tmpl = _COMMUNITY_ADSB_SOURCES[src_i % len(_COMMUNITY_ADSB_SOURCES)]
        src_i += 1
        try:
            url = tmpl.format(lat=_COMMUNITY_ADSB_LAT, lon=_COMMUNITY_ADSB_LON,
                              dist=_COMMUNITY_ADSB_DIST)
            req  = urllib.request.Request(url, headers={"User-Agent": _PS_UA})
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            acs  = data.get("ac") or data.get("aircraft") or []
            now  = int(time.time())
            with _adsb_lock:
                local = set(_adsb_aircraft.keys())
            for ac in acs:
                hx = (ac.get("hex") or "").upper().strip()
                lat, lon = ac.get("lat"), ac.get("lon")
                if not hx or len(hx) > 7 or lat is None or hx in local:
                    continue      # local receiver always wins
                reg      = (ac.get("r") or "").strip()
                callsign = (ac.get("flight") or "").strip()
                squawk   = ac.get("squawk") or ""
                itype    = (ac.get("t") or "").strip()
                category = ac.get("category") or ""
                # label for display colouring only — community aircraft never notify
                label, priority = _adsb_match(hx, reg, callsign, squawk, itype, category)
                alt = ac.get("alt_baro")
                _community_aircraft[hx] = {
                    "hex": hx, "registration": reg, "icao_type": itype,
                    "type_desc": (ac.get("desc") or ""), "flight": callsign,
                    "label": label, "priority": priority,
                    "lat": lat, "lon": lon,
                    "alt_baro": alt if isinstance(alt, (int, float)) else 0,
                    "gs": ac.get("gs"), "track": ac.get("track"),
                    "category": category, "squawk": squawk,
                    "emergency": ac.get("emergency", "none"),
                    "is_helicopter": _adsb_is_heli(itype, category),
                    "is_military": _is_military_hex(hx),
                    "last_ts": now, "community": True, "source": name,
                }
                _broadcast({"type": "adsb_aircraft", **_community_aircraft[hx]})
            # expire community aircraft not refreshed recently or now seen locally
            for hx in list(_community_aircraft):
                if _community_aircraft[hx]["last_ts"] < now - 120 or hx in local:
                    del _community_aircraft[hx]
                    _broadcast({"type": "adsb_remove", "hex": hx})
        except Exception as e:
            log.debug(f"community adsb {name}: {e}")
        time.sleep(_COMMUNITY_ADSB_PERIOD)

# ── feed watchdog ──────────────────────────────────────────────────────────────
_WD_STALE_SECS = 300
_wd_healthy    = {"ais": True, "adsb": True}
_wd_bad_since: dict = {}

def _watchdog():
    """Notify via Telegram/Gotify when a receiver feed dies (and when it recovers)."""
    names = {"ais": "AIS feed (AIS-catcher/Airspy)", "adsb": "ADS-B feed (readsb/RTL-SDR)"}
    time.sleep(120)   # grace period after startup
    while True:
        try:
            checks = {"ais": _ais_status == "connected"}
            try:
                checks["adsb"] = (time.time() - os.path.getmtime("/run/readsb/aircraft.json")) < _WD_STALE_SECS
            except Exception:
                checks["adsb"] = False
            for k, ok in checks.items():
                if ok:
                    _wd_bad_since.pop(k, None)
                    if not _wd_healthy[k]:
                        _wd_healthy[k] = True
                        title = f"✅ {names[k]} recovered"
                        log.warning(f"WATCHDOG: {title}")
                        _send_telegram(title)
                        _send_gotify(title, "Feed is flowing again.", priority=6)
                else:
                    bad_t = _wd_bad_since.setdefault(k, time.time())
                    if _wd_healthy[k] and time.time() - bad_t >= _WD_STALE_SECS:
                        _wd_healthy[k] = False
                        title = f"⚠ {names[k]} DOWN"
                        body  = (f"No data for {_WD_STALE_SECS//60}+ minutes. "
                                 "Check the receiver bar and the USB dongle.")
                        log.warning(f"WATCHDOG: {title}")
                        _send_telegram(f"{title}\n\n{body}")
                        _send_gotify(title, body, priority=9)
        except Exception as e:
            log.debug(f"watchdog: {e}")
        time.sleep(60)

# ── AIS vessel store ───────────────────────────────────────────────────────────
_ais_vessels: dict  = {}   # mmsi → vessel dict
_ais_status         = "disconnected"
_ais_lock           = threading.Lock()
_ais_last_pos: dict = {}    # mmsi → epoch, throttle SSE broadcasts
_ais_alert_dedup: dict = {} # mmsi/key → epoch, dedup alert events
_ais_track_last: dict = {}  # mmsi → {lat, lon, ts}, throttle track storage

# ── watched vessels (user-added, stored in DB) ─────────────────────────────────
_watched_lock  = threading.Lock()
_watched_mmsi: set = set()

def _load_watched():
    with _db() as c:
        rows = c.execute("SELECT mmsi FROM watched_vessels").fetchall()
    with _watched_lock:
        _watched_mmsi.update(r["mmsi"] for r in rows)

_load_watched()

AIS_TRACK_MIN_MOVE = 0.00045  # ~50m in degrees lat (approx, good enough)
AIS_TRACK_MIN_SECS = 60       # minimum seconds between stored points per vessel
AIS_TRACK_TTL_DAYS = 14

def _ais_track_store(mmsi: int, lat: float, lon: float, speed, heading):
    """Store a track point if vessel moved enough and enough time has elapsed."""
    prev = _ais_track_last.get(mmsi)
    now  = time.time()
    if prev:
        if now - prev["ts"] < AIS_TRACK_MIN_SECS:
            return
        dlat = abs(lat - prev["lat"])
        dlon = abs(lon - prev["lon"]) * 0.56  # cos(56°) approx for Clyde area
        if dlat < AIS_TRACK_MIN_MOVE and dlon < AIS_TRACK_MIN_MOVE:
            return
    _ais_track_last[mmsi] = {"lat": lat, "lon": lon, "ts": now}
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO ais_track(mmsi,ts,lat,lon,speed,heading) VALUES(?,?,?,?,?,?)",
                (mmsi, int(now), lat, lon, speed, heading)
            )
    except Exception as e:
        log.debug(f"AIS track insert {mmsi}: {e}")

def _ais_track_cleanup():
    """Delete track points older than TTL; run periodically."""
    cutoff = int(time.time()) - AIS_TRACK_TTL_DAYS * 86400
    for attempt in range(3):
        try:
            with _db() as c:
                deleted = c.execute("DELETE FROM ais_track WHERE ts < ?", (cutoff,)).rowcount
            if deleted:
                # checkpoint AFTER commit — inside the DELETE's transaction it
                # fails with "database table is locked" (the old 6-hourly warning)
                with _db() as c:
                    c.execute("PRAGMA wal_checkpoint(PASSIVE)")
                log.info(f"AIS track cleanup: removed {deleted} old points")
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(30)
            else:
                log.warning(f"AIS track cleanup error: {e}")

def _ais_vessel_expiry():
    """Background thread: remove vessels silent for >AIS_VESSEL_TTL seconds."""
    while True:
        time.sleep(60)
        now = int(time.time())
        expired = []
        with _ais_lock:
            for mmsi, v in list(_ais_vessels.items()):
                if now - v.get("last_ts", 0) > AIS_VESSEL_TTL:
                    expired.append(mmsi)
                    del _ais_vessels[mmsi]
        for mmsi in expired:
            _broadcast({"type": "ais_vessel_remove", "mmsi": mmsi})
        if expired:
            log.info(f"AIS expiry: removed {len(expired)} stale vessels")

threading.Thread(target=_ais_vessel_expiry, daemon=True).start()

def _ais_cleanup_loop():
    """Background thread: cleanup shortly after startup then every 6 hours."""
    # let _init_db DDL and thread startup settle — first-run DELETE used to race
    # them and die with SQLITE_LOCKED ("database table is locked")
    time.sleep(60)
    _ais_track_cleanup()
    while True:
        time.sleep(6 * 3600)
        _ais_track_cleanup()

threading.Thread(target=_ais_cleanup_loop, daemon=True).start()

def _ais_shiptype_sync():
    """Periodically pull all vessels from ships.json — enriches ship type AND adds
    community vessels (group_mask!=1) that our antenna didn't hear directly."""
    while True:
        try:
            req  = urllib.request.Request("http://localhost:8100/api/ships_full.json")
            data = json.loads(urllib.request.urlopen(req, timeout=5).read())
            now  = int(time.time())
            to_broadcast = []
            with _ais_lock:
                for ship in data.get("ships", []):
                    mmsi = ship.get("mmsi")
                    if not mmsi:
                        continue
                    is_community = ship.get("group_mask", 1) != 1
                    ex = _ais_vessels.get(mmsi)
                    if ex:
                        st = ship.get("shiptype")
                        if st is not None:
                            ex["ship_type"] = st
                        if is_community and now - ex.get("last_ts", 0) > 300:
                            if ship.get("lat") is not None:
                                ex["lat"] = ship["lat"]
                                ex["lon"] = ship.get("lon")
                            if ship.get("speed") is not None:
                                ex["speed"] = ship["speed"]
                            ex["community"] = True
                            ex["last_ts"] = now
                            to_broadcast.append(dict(ex))
                    elif is_community and ship.get("lat") is not None:
                        label, priority = _ais_match(mmsi,
                            (ship.get("shipname") or "").strip(), [])
                        vessel = {
                            "mmsi":      mmsi,
                            "name":      (ship.get("shipname") or "").strip(),
                            "lat":       ship.get("lat"),
                            "lon":       ship.get("lon"),
                            "ship_type": ship.get("shiptype"),
                            "channel":   "C",
                            "last_ts":   now,
                            "label":     label,
                            "priority":  priority,
                            "heading":   ship.get("heading"),
                            "cog":       ship.get("cog"),
                            "speed":     ship.get("speed"),
                            "community": True,
                        }
                        _ais_vessels[mmsi] = vessel
                        to_broadcast.append(dict(vessel))
            for v in to_broadcast:
                if now - _ais_last_pos.get(v["mmsi"], 0) >= AIS_BROADCAST_THROTTLE:
                    _ais_last_pos[v["mmsi"]] = now
                    _broadcast({"type": "ais_vessel", **v})
        except Exception:
            pass
        time.sleep(10)

threading.Thread(target=_ais_shiptype_sync, daemon=True).start()

def _process_ais(ev: dict):
    mmsi = ev.get("mmsi")
    if not mmsi: return
    name  = (ev.get("shipname") or "").strip()
    nmea  = ev.get("nmea") or []
    ts    = int(ev.get("timestamp") or time.time())
    chan  = ev.get("channel", "?")
    pos   = _ais_position(nmea)
    stype = _ais_ship_type(nmea) or ev.get("shiptype")
    label, priority = _ais_match(mmsi, name, nmea)

    heading = ev.get("heading")
    cog     = ev.get("cog")
    speed   = ev.get("speed")
    # AIS-catcher SSE events carry raw NMEA only — decode motion from the payload
    motion = _ais_motion(nmea)
    if motion:
        if heading is None: heading = motion["heading"]
        if cog     is None: cog     = motion["cog"]
        if speed   is None: speed   = motion["speed"]
    # heading 511 = not available in AIS spec
    if heading is not None and (heading > 359 or heading < 0): heading = None

    with _ais_lock:
        ex = _ais_vessels.get(mmsi, {})
        vessel = {
            "mmsi":      mmsi,
            "name":      name or ex.get("name", ""),
            "lat":       pos[0] if pos else ex.get("lat"),
            "lon":       pos[1] if pos else ex.get("lon"),
            "ship_type": stype if stype is not None else ex.get("ship_type"),
            "channel":   chan,
            "last_ts":   ts,
            "label":     label or ex.get("label"),
            "priority":  priority or ex.get("priority"),
            "heading":   heading if heading is not None else ex.get("heading"),
            "cog":       cog     if cog     is not None else ex.get("cog"),
            "speed":     speed   if speed   is not None else ex.get("speed"),
        }
        _ais_vessels[mmsi] = vessel

    now = time.time()
    if pos:
        _ais_track_store(mmsi, pos[0], pos[1],
                         vessel.get("speed"), vessel.get("heading"))
        if now - _ais_last_pos.get(mmsi, 0) >= AIS_BROADCAST_THROTTLE:
            _ais_last_pos[mmsi] = now
            _broadcast({"type": "ais_vessel", **vessel})

    if label and now - _ais_alert_dedup.get(mmsi, 0) >= AIS_ALERT_DEDUP:
        _ais_alert_dedup[mmsi] = now
        _ais_intel_update(vessel)
        _broadcast({"type": "ais_alert", **vessel})
        log.info(f"AIS ALERT [{label}] {vessel['name'] or mmsi}  MMSI:{mmsi}"
                 + (f"  pos={vessel['lat']:.4f},{vessel['lon']:.4f}" if pos else ""))
        def _notify_ais(v=vessel):
            if v.get("label") not in AIS_NOTIFY_LABELS:
                return
            info  = _ais_fetch_vessel_info(v["mmsi"])
            local = info.get("local", {})
            disp  = v["name"] or str(v["mmsi"])
            title = f"AIS: {disp}"
            ts_str = time.strftime("%H:%M:%S", time.localtime(v.get("last_ts") or time.time()))
            lines = [v["label"], f"MMSI: {v['mmsi']}"]
            if local.get("callsign"):    lines.append(f"Callsign: {local['callsign']}")
            if local.get("imo"):         lines.append(f"IMO: {local['imo']}")
            if local.get("destination"): lines.append(f"Destination: {local['destination']}")
            spd = local.get("speed")
            if spd is not None:          lines.append(f"Speed: {spd:.1f} kn")
            lines.append(f"Channel: {v.get('channel','?')}  ·  {ts_str}")
            if v.get("lat") is not None and v.get("lon") is not None:
                lat, lon = v["lat"], v["lon"]
                lines.append(
                    f"\nMap: https://www.openstreetmap.org/?mlat={lat:.6f}"
                    f"&mlon={lon:.6f}#map=14/{lat:.6f}/{lon:.6f}"
                )
            lines.append(f"VesselFinder: {info['vessel_url']}")
            body = "\n".join(lines)
            if info.get("photo_url"):
                _send_telegram_photo(f"🚢 {title}\n\n{body}", info["photo_url"])
            else:
                _send_telegram(f"🚢 {title}\n\n{body}")
            _send_gotify(f"AIS: {disp} — {v['label']}", body, v.get("priority", 5))
        threading.Thread(target=_notify_ais, daemon=True).start()

    # watched vessel notification (user-added via UI, not auto-matched)
    with _watched_lock:
        is_watched = mmsi in _watched_mmsi
    watch_key = f"w_{mmsi}"
    if is_watched and not label and pos and now - _ais_alert_dedup.get(watch_key, 0) >= 1800:
        _ais_alert_dedup[watch_key] = now
        _broadcast({"type": "ais_alert", **vessel, "label": "Watched"})
        log.info(f"AIS WATCHED {vessel['name'] or mmsi}  MMSI:{mmsi}")
        def _notify_watched(v=vessel):
            info  = _ais_fetch_vessel_info(v["mmsi"])
            local = info.get("local", {})
            disp  = v["name"] or str(v["mmsi"])
            title = f"👁 Watched: {disp}"
            ts_str = time.strftime("%H:%M:%S", time.localtime(v.get("last_ts") or time.time()))
            lines = [f"MMSI: {v['mmsi']}"]
            if local.get("callsign"):    lines.append(f"Callsign: {local['callsign']}")
            if local.get("destination"): lines.append(f"Destination: {local['destination']}")
            spd = local.get("speed")
            if spd is not None:          lines.append(f"Speed: {spd:.1f} kn")
            lines.append(f"Channel: {v.get('channel','?')}  ·  {ts_str}")
            if v.get("lat") is not None and v.get("lon") is not None:
                lat, lon = v["lat"], v["lon"]
                lines.append(
                    f"\nMap: https://www.openstreetmap.org/?mlat={lat:.6f}"
                    f"&mlon={lon:.6f}#map=14/{lat:.6f}/{lon:.6f}"
                )
            body = "\n".join(lines)
            if info.get("photo_url"):
                _send_telegram_photo(f"🚢 {title}\n\n{body}", info["photo_url"])
            else:
                _send_telegram(f"🚢 {title}\n\n{body}")
            _send_gotify(title, body, 4)
        threading.Thread(target=_notify_watched, daemon=True).start()

def _ais_reader():
    global _ais_status
    log.info(f"AIS watcher starting — {AIS_SSE_URL}")
    while True:
        try:
            req  = urllib.request.Request(
                AIS_SSE_URL,
                headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"})
            resp = urllib.request.urlopen(req, timeout=90)
            _ais_status = "connected"
            _broadcast({"type": "ais_status", "connected": True,
                        "count": len(_ais_vessels)})
            log.info("AIS SSE connected")
            buf: dict = {}
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("data:"):
                    try: buf["data"] = json.loads(line[5:].strip())
                    except Exception: pass
                elif line == "":
                    if "data" in buf: _process_ais(buf["data"])
                    buf = {}
        except Exception as e:
            if _ais_status != "disconnected":
                log.warning(f"AIS SSE error: {e}")
            _ais_status = "disconnected"
            _broadcast({"type": "ais_status", "connected": False,
                        "count": len(_ais_vessels)})
            time.sleep(15)

# ── notifications ──────────────────────────────────────────────────────────────
def _send_telegram(text):
    if not cfg.get("telegram_enabled"): return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id":TELEGRAM_CHAT_ID,"text":text,
                                       "disable_web_page_preview":"true"}).encode()
        urllib.request.urlopen(urllib.request.Request(url,data=data,method="POST"),timeout=10).read()
    except Exception as e:
        log.warning(f"Telegram error: {e}")

def _send_gotify(title, body, priority=5):
    if not cfg.get("gotify_enabled"): return
    try:
        url  = f"{GOTIFY_URL.rstrip('/')}/message?token={urllib.parse.quote(GOTIFY_TOKEN)}"
        data = urllib.parse.urlencode({"title":title,"message":body,"priority":priority}).encode()
        req  = urllib.request.Request(url,data=data,method="POST")
        req.add_header("Content-Type","application/x-www-form-urlencoded")
        urllib.request.urlopen(req,timeout=10).read()
    except Exception as e:
        log.warning(f"Gotify error: {e}")

def _send_telegram_photo(caption: str, photo_url: str):
    """Send a Telegram photo message; falls back to text with URL on failure."""
    if not cfg.get("telegram_enabled"): return
    try:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID,
            "photo":   photo_url,
            "caption": caption,
        }).encode()
        urllib.request.urlopen(
            urllib.request.Request(url, data=data, method="POST"), timeout=12
        ).read()
        return
    except Exception as e:
        log.debug(f"Telegram sendPhoto failed ({e}), falling back to sendMessage")
    _send_telegram(caption + f"\n\nPhoto: {photo_url}")

# ── SSE broadcast ──────────────────────────────────────────────────────────────
_sse_clients: list = []
_sse_lock = threading.Lock()
_ev_loop: asyncio.AbstractEventLoop = None

def _broadcast(event: dict):
    if not _ev_loop: return
    data = json.dumps(event)
    with _sse_lock:
        for q in list(_sse_clients):
            try: _ev_loop.call_soon_threadsafe(q.put_nowait, data)
            except Exception: pass

# ── FastAPI ────────────────────────────────────────────────────────────────────
def _process_aisstream_msg(msg: dict):
    """Merge a single AISStream.io message into the vessel dict and broadcast."""
    mtype = msg.get("MessageType")
    meta  = msg.get("MetaData", {})
    mmsi  = meta.get("MMSI")
    if not mmsi:
        return
    mmsi = int(mmsi)
    now  = int(time.time())

    if mtype == "PositionReport":
        pr  = msg.get("Message", {}).get("PositionReport", {})
        lat = pr.get("Latitude")
        lon = pr.get("Longitude")
        if lat is None or lon is None:
            return
        sog = pr.get("Sog")
        cog = pr.get("Cog")
        hdg = pr.get("TrueHeading")
        if hdg is not None and (hdg > 359 or hdg < 0):
            hdg = None
        name = (meta.get("ShipName") or "").strip()
        with _ais_lock:
            ex = _ais_vessels.get(mmsi)
            # don't overwrite a vessel we heard locally in the last 3 min
            if ex and not ex.get("community") and now - ex.get("last_ts", 0) < 180:
                if name and not ex.get("name"):
                    ex["name"] = name
                return
            label, priority = _ais_match(mmsi, name or (ex or {}).get("name", ""), [])
            vessel = {
                "mmsi":      mmsi,
                "name":      name or (ex or {}).get("name", ""),
                "lat":       lat,
                "lon":       lon,
                "ship_type": (ex or {}).get("ship_type"),
                "channel":   "S",   # S = aisstream
                "last_ts":   now,
                "label":     label or (ex or {}).get("label"),
                "priority":  priority or (ex or {}).get("priority"),
                "heading":   hdg,
                "cog":       cog,
                "speed":     sog,
                "community": True,
            }
            _ais_vessels[mmsi] = vessel
        if now - _ais_last_pos.get(mmsi, 0) >= AIS_BROADCAST_THROTTLE:
            _ais_last_pos[mmsi] = now
            _broadcast({"type": "ais_vessel", **vessel})

    elif mtype == "ShipStaticData":
        sd      = msg.get("Message", {}).get("ShipStaticData", {})
        stype   = sd.get("Type")
        name    = (sd.get("Name") or meta.get("ShipName") or "").strip()
        callsign= (sd.get("CallSign") or "").strip()
        dest    = (sd.get("Destination") or "").strip()
        draught = sd.get("MaximumStaticDraught")
        imo     = sd.get("ImoNumber")
        eta_raw = sd.get("Eta") or {}
        eta_str = None
        if eta_raw.get("Month") and eta_raw.get("Day"):
            eta_str = f"{eta_raw.get('Day'):02d}/{eta_raw.get('Month'):02d} {eta_raw.get('Hour',0):02d}:{eta_raw.get('Minute',0):02d}"
        with _ais_lock:
            ex = _ais_vessels.get(mmsi)
            if ex:
                if name:     ex["name"]     = name
                if stype is not None: ex["ship_type"] = stype
                if callsign: ex["callsign"] = callsign
                if dest:     ex["destination"] = dest
                if draught:  ex["draught"]  = draught
                if imo:      ex["imo"]       = imo
                if eta_str:  ex["eta"]       = eta_str


def _aisstream_reader():
    """Background thread: stream real-time AIS positions from AISStream.io via WebSocket."""
    import asyncio as _aio

    async def _run():
        sub = json.dumps({
            "APIKey":             AISSTREAM_KEY,
            "BoundingBoxes":      [AISSTREAM_BOX],
            "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
        })
        while True:
            try:
                import websockets as _ws
                async with _ws.connect(
                    "wss://stream.aisstream.io/v0/stream",
                    ping_interval=20, ping_timeout=20,
                ) as ws:
                    await ws.send(sub)
                    log.info("AISStream: connected")
                    async for raw in ws:
                        try:
                            _process_aisstream_msg(json.loads(raw))
                        except Exception:
                            pass
            except Exception as e:
                log.warning(f"AISStream error: {e} — reconnecting in 15s")
                await _aio.sleep(15)

    _aio.run(_run())


# ── ADS-B vessel store ─────────────────────────────────────────────────────────
_adsb_aircraft: dict    = {}   # icao_hex → aircraft dict
_adsb_lock              = threading.Lock()
_adsb_last_pos: dict    = {}   # icao_hex → epoch
_adsb_alert_dedup: dict = {}   # icao_hex/key → epoch
_adsb_track_last: dict  = {}   # icao_hex → {lat,lon,ts}

def _adsb_match(hex_code: str, reg: str, callsign: str,
                squawk: str, icao_type: str, category: str):
    """Return (label, priority) for this aircraft, or (None, None)."""
    reg      = (reg or "").upper()
    callsign = (callsign or "").upper().strip()
    # Registration rules (highest specificity)
    for prefix, lbl, pri in ADSB_REG_RULES:
        if reg.startswith(prefix):
            return lbl, pri
    # Callsign rules
    for kw, lbl, pri in ADSB_CALLSIGN_RULES:
        if kw in callsign:
            return lbl, pri
    # Military hex range
    if _is_military_hex(hex_code):
        return "Military", 8
    return None, None

def _adsb_is_heli(icao_type: str, category: str) -> bool:
    if category == "A7":
        return True
    if icao_type and icao_type[0].upper() == "H":
        return True
    # Common SAR helicopter ICAO type codes
    return (icao_type or "").upper() in {
        "S92","AW89","AW01","EC35","EC45","EC55","AS65","AS32","A139","A189",
        "S61","S76","R44","R66","BK17","BO10","NH90","EH10","LYNX","WILD"
    }

def _adsb_fetch_info(hex_code: str) -> dict:
    """Fetch aircraft info from adsbdb.com and cache photo in DB. Never raises."""
    result: dict = {"registration": "", "type": "", "manufacturer": "",
                    "owner": "", "photo_url": None, "thumb_url": None}
    now = int(time.time())

    # Check photo cache first (skip network if fresh)
    photo_cached = False
    try:
        with _db() as c:
            row = c.execute(
                "SELECT photo_url, thumb_url, fetched FROM aircraft_photos WHERE icao_hex=?",
                (hex_code.upper(),)
            ).fetchone()
        if row and (now - row["fetched"]) < ADSB_PHOTO_CACHE_TTL:
            hx = hex_code.upper()
            result["photo_url"] = _cache_photo(row["photo_url"], f"ac_{hx}.jpg")
            result["thumb_url"] = _cache_photo(row["thumb_url"], f"ac_{hx}_t.jpg")
            photo_cached = True
    except Exception: pass

    # Fetch registration/type from adsbdb.com (metadata only — free, no key)
    reg = ""
    try:
        req = urllib.request.Request(
            f"https://api.adsbdb.com/v0/aircraft/{hex_code.upper()}",
            headers={"User-Agent": _PS_UA}
        )
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        ac   = data.get("response", {}).get("aircraft", {})
        result["registration"]  = ac.get("registration", "")
        result["type"]          = ac.get("type", "")
        result["manufacturer"]  = ac.get("manufacturer", "")
        result["owner"]         = ac.get("registered_owner", "")
        result["country"]       = ac.get("registered_owner_country_name", "")
        reg = result["registration"]
    except Exception as e:
        log.debug(f"adsbdb {hex_code}: {e}")

    # Fetch photo from planespotters.net if not cached
    if not photo_cached:
        photo_url, thumb_url = _planespotters_photo(hex_code, reg)
        hx = hex_code.upper()
        result["photo_url"] = _cache_photo(photo_url, f"ac_{hx}.jpg")
        result["thumb_url"] = _cache_photo(thumb_url or photo_url, f"ac_{hx}_t.jpg")
        # Cache result (even None — prevents hammering the API)
        try:
            with _db() as c:
                c.execute(
                    "INSERT INTO aircraft_photos(icao_hex,photo_url,thumb_url,fetched) VALUES(?,?,?,?) "
                    "ON CONFLICT(icao_hex) DO UPDATE SET photo_url=excluded.photo_url,"
                    "thumb_url=excluded.thumb_url,fetched=excluded.fetched",
                    (hex_code.upper(), result.get("photo_url"), result.get("thumb_url"), now)
                )
        except Exception as e:
            log.debug(f"aircraft_photos cache write {hex_code}: {e}")

    return result

def _adsb_track_store(hex_code: str, lat: float, lon: float,
                      alt, speed, track, ts: int):
    prev = _adsb_track_last.get(hex_code)
    if prev:
        if ts - prev["ts"] < ADSB_TRACK_MIN_SECS:
            return
        if (abs(lat - prev["lat"]) < ADSB_TRACK_MIN_MOVE and
                abs(lon - prev["lon"]) < ADSB_TRACK_MIN_MOVE):
            return
    _adsb_track_last[hex_code] = {"lat": lat, "lon": lon, "ts": ts}
    try:
        with _db() as c:
            c.execute(
                "INSERT INTO adsb_track(icao_hex,ts,lat,lon,alt,speed,track)"
                " VALUES(?,?,?,?,?,?,?)",
                (hex_code.upper(), ts, lat, lon, alt, speed, track)
            )
    except Exception as e:
        log.debug(f"adsb_track insert {hex_code}: {e}")

def _adsb_track_cleanup():
    cutoff = int(time.time()) - ADSB_TRACK_TTL_DAYS * 86400
    for attempt in range(3):
        try:
            with _db() as c:
                deleted = c.execute(
                    "DELETE FROM adsb_track WHERE ts < ?", (cutoff,)
                ).rowcount
                if deleted:
                    log.info(f"ADS-B track cleanup: removed {deleted} old points")
            return
        except Exception as e:
            if attempt < 2:
                time.sleep(30)
            else:
                log.warning(f"ADS-B track cleanup: {e}")

def _adsb_cleanup_loop():
    time.sleep(75)   # same startup-race guard as _ais_cleanup_loop (offset from it)
    _adsb_track_cleanup()
    while True:
        time.sleep(6 * 3600)
        _adsb_track_cleanup()

threading.Thread(target=_adsb_cleanup_loop, daemon=True).start()

def _adsb_intel_update(aircraft: dict, info: dict):
    """Persist enriched data into aircraft_intel table."""
    hx  = aircraft["hex"].upper()
    now = int(time.time())
    try:
        with _db() as c:
            c.execute("""
            INSERT INTO aircraft_intel(icao_hex,registration,icao_type,type_desc,
                manufacturer,owner,label,priority,first_seen,last_seen,alert_count)
            VALUES(?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(icao_hex) DO UPDATE SET
                registration=COALESCE(excluded.registration, registration),
                icao_type   =COALESCE(excluded.icao_type,    icao_type),
                type_desc   =COALESCE(excluded.type_desc,    type_desc),
                manufacturer=COALESCE(excluded.manufacturer, manufacturer),
                owner       =COALESCE(excluded.owner,        owner),
                label       =COALESCE(excluded.label,        label),
                priority    =MAX(excluded.priority, priority),
                last_seen   =excluded.last_seen,
                alert_count =alert_count + 1
            """, (hx,
                  aircraft.get("registration") or info.get("registration") or "",
                  aircraft.get("icao_type") or "",
                  aircraft.get("type_desc") or info.get("type") or "",
                  info.get("manufacturer") or "",
                  info.get("owner") or "",
                  aircraft.get("label") or "",
                  aircraft.get("priority") or 0,
                  now, now))
    except Exception as e:
        log.debug(f"aircraft_intel update {hx}: {e}")

def _cross_correlate():
    """Check for co-located AIS SAR vessels + ADS-B SAR aircraft — possible major SAR event."""
    now = time.time()
    with _ais_lock:
        sar_vessels = [v for v in _ais_vessels.values()
                       if v.get("label") and v.get("lat") is not None
                       and now - v.get("last_ts", 0) < 1800]
    with _adsb_lock:
        sar_aircraft = [a for a in _adsb_aircraft.values()
                        if a.get("label") and a.get("lat") is not None
                        and now - a.get("last_ts", 0) < 600]
    for vessel in sar_vessels:
        for aircraft in sar_aircraft:
            dist = _haversine_nm(vessel["lat"], vessel["lon"],
                                 aircraft["lat"], aircraft["lon"])
            if dist > 15:
                continue
            key = f"corr_{vessel['mmsi']}_{aircraft['hex']}"
            if now - _adsb_alert_dedup.get(key, 0) < 3600:
                continue
            _adsb_alert_dedup[key] = now
            log.info(f"CORRELATION: {vessel.get('name',vessel['mmsi'])} + "
                     f"{aircraft.get('registration',aircraft['hex'])} "
                     f"within {dist:.1f}nm")
            _broadcast({
                "type":        "correlation_alert",
                "vessel_name": vessel.get("name") or str(vessel["mmsi"]),
                "vessel_label":vessel.get("label"),
                "aircraft_reg":aircraft.get("registration") or aircraft["hex"],
                "aircraft_label":aircraft.get("label"),
                "distance_nm": round(dist, 1),
                "lat":         vessel["lat"],
                "lon":         vessel["lon"],
            })
            def _notify_corr(v=vessel, a=aircraft, d=dist):
                vn  = v.get("name") or str(v["mmsi"])
                an  = a.get("registration") or a.get("flight") or a["hex"]
                title = f"⚠ SAR Co-location: {vn} + {an}"
                body  = (f"Vessel: {vn} [{v['label']}]\n"
                         f"Aircraft: {an} [{a['label']}]\n"
                         f"Distance: {d:.1f} nm\n")
                if v.get("lat"):
                    body += (f"Map: https://www.openstreetmap.org/?mlat={v['lat']:.5f}"
                             f"&mlon={v['lon']:.5f}#map=12/{v['lat']:.5f}/{v['lon']:.5f}")
                _send_telegram(f"⚠ {title}\n\n{body}")
                _send_gotify(title, body, 9)
            threading.Thread(target=_notify_corr, daemon=True).start()

def _process_adsb_aircraft(ac: dict, now: int):
    hex_code = (ac.get("hex") or "").upper()
    if not hex_code or len(hex_code) > 7:
        return
    lat   = ac.get("lat")
    lon   = ac.get("lon")
    seen  = float(ac.get("seen", 0))
    if seen > 60:
        return  # stale entry in the JSON

    db_info   = _adsb_lookup_db(hex_code)
    reg       = db_info.get("registration", "")
    icao_type = db_info.get("icao_type", "")
    type_desc = db_info.get("type_desc", "")
    callsign  = (ac.get("flight") or "").strip()
    squawk    = ac.get("squawk") or ""
    category  = ac.get("category") or ""
    is_heli   = _adsb_is_heli(icao_type, category)
    is_mil    = _is_military_hex(hex_code)
    emrg      = ac.get("emergency", "none")
    emrg_sq   = squawk in ("7700", "7600", "7500")

    label, priority = _adsb_match(hex_code, reg, callsign, squawk, icao_type, category)
    if emrg_sq and not label:
        label, priority = f"EMERGENCY {squawk}", 10

    aircraft = {
        "hex":           hex_code,
        "registration":  reg,
        "icao_type":     icao_type,
        "type_desc":     type_desc,
        "flight":        callsign,
        "label":         label,
        "priority":      priority,
        "lat":           lat,
        "lon":           lon,
        "alt_baro":      ac.get("alt_baro"),
        "gs":            ac.get("gs"),
        "track":         ac.get("track") or ac.get("calc_track"),
        "category":      category,
        "squawk":        squawk,
        "emergency":     emrg,
        "is_helicopter": is_heli,
        "is_military":   is_mil,
        "last_ts":       now,
    }
    with _adsb_lock:
        _adsb_aircraft[hex_code] = aircraft

    if lat is not None and lon is not None:
        _adsb_track_store(hex_code, lat, lon,
                          ac.get("alt_baro"), ac.get("gs"),
                          aircraft["track"], now)
        if now - _adsb_last_pos.get(hex_code, 0) >= ADSB_BROADCAST_THROTTLE:
            _adsb_last_pos[hex_code] = now
            _broadcast({"type": "adsb_aircraft", **aircraft})

    # Alert on first match or after dedup window
    if label and now - _adsb_alert_dedup.get(hex_code, 0) >= ADSB_ALERT_DEDUP:
        _adsb_alert_dedup[hex_code] = now
        _broadcast({"type": "adsb_alert", **aircraft})
        log.info(f"ADSB ALERT [{label}] {reg or callsign or hex_code}"
                 + (f"  pos={lat:.4f},{lon:.4f}" if lat else ""))
        if label in ADSB_NOTIFY_LABELS or emrg_sq:
            def _notify_adsb(a=aircraft):
                info  = _adsb_fetch_info(a["hex"])
                _adsb_intel_update(a, info)
                disp  = (a.get("registration") or info.get("registration")
                         or a.get("flight") or a["hex"])
                ts_str = time.strftime("%H:%M:%S", time.localtime(now))
                title  = f"✈ ADS-B: {disp}"
                lines  = [a["label"] or ""]
                if info.get("type"):         lines.append(f"Type: {info['type']}")
                if info.get("manufacturer"): lines.append(f"Maker: {info['manufacturer']}")
                if info.get("owner"):        lines.append(f"Owner: {info['owner']}")
                if a.get("flight"):          lines.append(f"Callsign: {a['flight']}")
                lines.append(f"Squawk: {a.get('squawk') or '?'}")
                if a.get("alt_baro") is not None:
                    lines.append(f"Alt: {a['alt_baro']:,} ft")
                if a.get("gs") is not None:
                    lines.append(f"Speed: {a['gs']:.0f} kn")
                lines.append(f"Time: {ts_str}")
                if a.get("lat") is not None:
                    alat, alon = a["lat"], a["lon"]
                    lines.append(
                        f"\nMap: https://www.openstreetmap.org/?mlat={alat:.6f}"
                        f"&mlon={alon:.6f}#map=13/{alat:.6f}/{alon:.6f}"
                    )
                body  = "\n".join(lines)
                photo = info.get("photo_url")
                if photo:
                    _send_telegram_photo(f"✈ {title}\n\n{body}", photo)
                else:
                    _send_telegram(f"✈ {title}\n\n{body}")
                _send_gotify(f"ADS-B: {disp} — {a['label']}", body,
                             a.get("priority") or 5)
            threading.Thread(target=_notify_adsb, daemon=True).start()

    # Separate fast alert for emergency squawk
    if emrg_sq:
        emrg_key = f"esq_{hex_code}"
        if now - _adsb_alert_dedup.get(emrg_key, 0) >= 600:
            _adsb_alert_dedup[emrg_key] = now
            def _notify_emrg(a=aircraft):
                disp  = a.get("registration") or a.get("flight") or a["hex"]
                title = f"🚨 EMERGENCY SQUAWK {a['squawk']}: {disp}"
                body  = (f"Registration: {a.get('registration') or '?'}\n"
                         f"Callsign: {a.get('flight') or '?'}\n"
                         f"Squawk: {a['squawk']}\n"
                         f"Alt: {a.get('alt_baro') or '?'} ft\n"
                         f"Speed: {a.get('gs') or '?'} kn")
                if a.get("lat"):
                    body += (f"\nMap: https://www.openstreetmap.org/?mlat={a['lat']:.6f}"
                             f"&mlon={a['lon']:.6f}#map=13/{a['lat']:.6f}/{a['lon']:.6f}")
                _send_telegram(f"🚨 {title}\n\n{body}")
                _send_gotify(title, body, 10)
            threading.Thread(target=_notify_emrg, daemon=True).start()

def _adsb_reader():
    log.info("ADS-B reader starting — polling /run/readsb/aircraft.json")
    _corr_counter = 0
    while True:
        try:
            data     = json.loads(ADSB_JSON_PATH.read_bytes())
            now      = int(time.time())
            aircraft = data.get("aircraft", data.get("ac", []))
            seen_set = set()
            for ac in aircraft:
                hx = (ac.get("hex") or "").upper()
                if hx:
                    seen_set.add(hx)
                    _process_adsb_aircraft(ac, now)
            # Expire stale aircraft
            with _adsb_lock:
                stale = [h for h, a in _adsb_aircraft.items()
                         if now - a.get("last_ts", 0) > ADSB_TTL]
            for hx in stale:
                with _adsb_lock:
                    _adsb_aircraft.pop(hx, None)
                _broadcast({"type": "adsb_remove", "hex": hx})
            # Run cross-correlation every 60s
            _corr_counter += 1
            if _corr_counter >= (60 // ADSB_POLL_SECS):
                _corr_counter = 0
                threading.Thread(target=_cross_correlate, daemon=True).start()
        except Exception as e:
            log.debug(f"ADS-B reader: {e}")
        time.sleep(ADSB_POLL_SECS)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _ev_loop
    _ev_loop = asyncio.get_running_loop()
    threading.Thread(target=_ais_reader, daemon=True).start()
    threading.Thread(target=_aisstream_reader, daemon=True).start()
    threading.Thread(target=_adsb_reader, daemon=True).start()
    threading.Thread(target=_watchdog, daemon=True).start()
    threading.Thread(target=_community_adsb_poll, daemon=True).start()
    threading.Thread(target=_aprs_is_reader, daemon=True).start()
    yield

app = FastAPI(title="AIS-ADSB DASHBOARD", lifespan=_lifespan)

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(_HTML)

@app.get("/ais-icons.png")
async def ais_icons():
    p = _HERE / "icons.png"
    return FileResponse(str(p), media_type="image/png")

@app.get("/leaflet.js")
async def leaflet_js():
    return FileResponse(str(_HERE / "leaflet.js"), media_type="application/javascript")

@app.get("/leaflet.css")
async def leaflet_css():
    return FileResponse(str(_HERE / "leaflet.css"), media_type="text/css")

@app.get("/photos/{fname}")
async def photos(fname: str):
    if not re.fullmatch(r"[A-Za-z0-9_.\-]+\.jpg", fname):
        return JSONResponse({"detail": "bad name"}, status_code=400)
    p = _PHOTOS_DIR / fname
    if not p.exists():
        return JSONResponse({"detail": "not found"}, status_code=404)
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})

@app.get("/api/health")
async def health():
    return {"ok":True,"version":_VERSION,"uptime":int(time.time()-_START_TIME)}

@app.get("/api/events")
async def sse(request: Request):
    q = asyncio.Queue(maxsize=300)
    with _sse_lock: _sse_clients.append(q)
    with _ais_lock:
        ais_snap = list(_ais_vessels.values())
    init = {"type":"init","ais_connected":_ais_status=="connected","ais_vessels":ais_snap}
    async def gen():
        try:
            yield f"data: {json.dumps(init)}\n\n"
            while True:
                if await request.is_disconnected(): break
                try:
                    data = await asyncio.wait_for(q.get(), timeout=20.0)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            with _sse_lock:
                try: _sse_clients.remove(q)
                except ValueError: pass
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

# ── local SDR receiver control (AIS-catcher on Airspy, readsb on RTL-SDR) ──────
_RECEIVERS = {
    "ais":  {"service": "ais-catcher.service", "label": "AIS (Airspy)"},
    "adsb": {"service": "readsb.service",      "label": "ADS-B (RTL-SDR)"},
}

def _rx_state(service: str) -> dict:
    try:
        out = subprocess.run(
            ["systemctl", "show", service,
             "--property=ActiveState,SubState,ExecMainStartTimestamp"],
            capture_output=True, text=True, timeout=5).stdout
        props = dict(l.split("=", 1) for l in out.strip().splitlines() if "=" in l)
        return {"active": props.get("ActiveState", "unknown"),
                "sub":    props.get("SubState", ""),
                "since":  props.get("ExecMainStartTimestamp", "")}
    except Exception as e:
        return {"active": "unknown", "sub": "", "since": "", "error": str(e)}

@app.get("/api/receivers")
async def api_receivers():
    def _snap():
        out = {}
        for name, rx in _RECEIVERS.items():
            out[name] = {**rx, **_rx_state(rx["service"])}
        with _ais_lock:
            vessels = list(_ais_vessels.values())
        out["ais"]["connected"] = _ais_status == "connected"
        out["ais"]["count"]     = sum(1 for v in vessels if not v.get("community"))
        with _adsb_lock:
            out["adsb"]["count"] = len(_adsb_aircraft)
        try:
            out["adsb"]["data_age"] = round(
                time.time() - os.path.getmtime("/run/readsb/aircraft.json"), 1)
        except Exception:
            out["adsb"]["data_age"] = None
        return out
    return await asyncio.get_event_loop().run_in_executor(None, _snap)

@app.post("/api/receivers/{name}/{action}")
async def api_receiver_ctl(name: str, action: str):
    rx = _RECEIVERS.get(name)
    if not rx or action not in ("start", "stop", "restart"):
        return JSONResponse({"ok": False, "error": "unknown receiver or action"},
                            status_code=400)
    def _ctl():
        r = subprocess.run(["sudo", "-n", "systemctl", action, rx["service"]],
                           capture_output=True, text=True, timeout=30)
        ok = r.returncode == 0
        if ok:
            log.info(f"Receiver control: systemctl {action} {rx['service']}")
        else:
            log.warning(f"Receiver control failed: systemctl {action} "
                        f"{rx['service']}: {r.stderr.strip()}")
        time.sleep(1.0)   # let systemd settle before reporting the new state
        return {"ok": ok, "error": "" if ok else r.stderr.strip(),
                "state": _rx_state(rx["service"])}
    return await asyncio.get_event_loop().run_in_executor(None, _ctl)

@app.get("/api/settings")
async def api_get_settings():
    return {k:cfg[k] for k in _DEFAULTS}

@app.post("/api/settings")
async def api_save_settings(request: Request):
    body = await request.json()
    for k,v in body.items():
        if k in _DEFAULTS: cfg[k]=v
    _save_cfg({k:cfg[k] for k in _DEFAULTS})
    return {"ok":True}

@app.get("/api/ais/vessels")
async def api_ais_vessels():
    with _ais_lock:
        return list(_ais_vessels.values())

@app.get("/api/ais/vessel-info/{mmsi}")
async def api_ais_vessel_info(mmsi: int):
    import asyncio
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _ais_fetch_vessel_info, mmsi)
    return info

@app.get("/api/ais/watched")
async def api_ais_watched():
    with _db() as c:
        rows = c.execute("SELECT mmsi, name, added FROM watched_vessels ORDER BY added DESC").fetchall()
    return [{"mmsi": r["mmsi"], "name": r["name"], "added": r["added"]} for r in rows]

@app.post("/api/ais/watch/{mmsi}")
async def api_ais_watch(mmsi: int, request: Request):
    body = await request.json()
    name = (body.get("name") or "").strip()
    now  = int(time.time())
    with _db() as c:
        c.execute(
            "INSERT INTO watched_vessels(mmsi,name,added) VALUES(?,?,?) "
            "ON CONFLICT(mmsi) DO UPDATE SET name=excluded.name",
            (mmsi, name, now)
        )
    with _watched_lock:
        _watched_mmsi.add(mmsi)
    log.info(f"Watching MMSI {mmsi} ({name})")
    return {"ok": True}

@app.delete("/api/ais/watch/{mmsi}")
async def api_ais_unwatch(mmsi: int):
    with _db() as c:
        c.execute("DELETE FROM watched_vessels WHERE mmsi=?", (mmsi,))
    with _watched_lock:
        _watched_mmsi.discard(mmsi)
    log.info(f"Unwatched MMSI {mmsi}")
    return {"ok": True}

def _fetch_tides_sync() -> dict:
    """Fetch 7-day tide extremes from Worldtides API or return cached data."""
    now = int(time.time())
    with _db() as c:
        row = c.execute("SELECT fetched, data FROM tides WHERE id=1").fetchone()
    if row and now - row["fetched"] < TIDE_CACHE_SECS:
        return {"cached": True, "fetched": row["fetched"], **json.loads(row["data"])}
    if not WORLDTIDES_KEY:
        return {"error": "No Worldtides API key set — add it to WORLDTIDES_KEY in rnli_ais_adsb_dashboard.py"}
    url = (
        f"https://www.worldtides.info/api/v3?extremes&datum=LAT"
        f"&start={now}&length=604800&lat={TIDE_LAT}&lon={TIDE_LON}&key={WORLDTIDES_KEY}"
    )
    try:
        req  = urllib.request.Request(url, headers={"User-Agent": "AisAdsbDashboard/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read().decode())
        if data.get("status", 0) != 200:
            return {"error": data.get("error", f"API status {data.get('status')}")}
        payload = {"extremes": data.get("extremes", []), "station": data.get("station", "")}
        with _db() as c:
            c.execute(
                "INSERT INTO tides(id,fetched,data) VALUES(1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET fetched=excluded.fetched, data=excluded.data",
                (now, json.dumps(payload))
            )
        return {"cached": False, "fetched": now, **payload}
    except Exception as e:
        if row:
            return {"cached": True, "fetched": row["fetched"], "stale": True, **json.loads(row["data"])}
        return {"error": str(e)}

@app.get("/api/tides")
async def api_tides(refresh: bool = Query(default=False)):
    if refresh:
        # force stale by clearing cache then re-fetch
        with _db() as c:
            c.execute("DELETE FROM tides")
    import asyncio
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch_tides_sync)
    return data

@app.get("/api/ais/track/{mmsi}")
async def api_ais_track(mmsi: int, days: int = Query(default=14, le=14)):
    cutoff = int(time.time()) - days * 86400
    with _db() as c:
        rows = c.execute(
            "SELECT ts, lat, lon, speed, heading FROM ais_track "
            "WHERE mmsi=? AND ts>=? ORDER BY ts ASC",
            (mmsi, cutoff)
        ).fetchall()
    return [{"ts": r["ts"], "lat": r["lat"], "lon": r["lon"],
             "speed": r["speed"], "heading": r["heading"]} for r in rows]

@app.get("/api/adsb/aircraft")
async def api_adsb_aircraft():
    with _adsb_lock:
        out = list(_adsb_aircraft.values())
        local = set(_adsb_aircraft.keys())
    out += [a for h, a in _community_aircraft.items() if h not in local]
    return out

@app.get("/api/adsb/aircraft-info/{hex_code}")
async def api_adsb_aircraft_info(hex_code: str):
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, _adsb_fetch_info, hex_code)
    db_info = _adsb_lookup_db(hex_code)
    return {**db_info, **info}

@app.get("/api/adsb/track/{hex_code}")
async def api_adsb_track(hex_code: str, days: int = Query(default=7, le=7)):
    cutoff = int(time.time()) - days * 86400
    with _db() as c:
        rows = c.execute(
            "SELECT ts,lat,lon,alt,speed,track FROM adsb_track "
            "WHERE icao_hex=? AND ts>=? ORDER BY ts ASC",
            (hex_code.upper(), cutoff)
        ).fetchall()
    return [{"ts": r["ts"], "lat": r["lat"], "lon": r["lon"],
             "alt": r["alt"], "speed": r["speed"], "track": r["track"]} for r in rows]

@app.get("/api/adsb/intel")
async def api_adsb_intel():
    """Return known SAR/military aircraft history from intelligence DB."""
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM aircraft_intel ORDER BY last_seen DESC LIMIT 200"
        ).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/ais/intel")
async def api_ais_intel():
    """Return alerted-vessel history from intelligence DB."""
    with _db() as c:
        rows = c.execute(
            "SELECT * FROM vessel_intel ORDER BY last_seen DESC LIMIT 200"
        ).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/aprs/stations")
async def api_aprs_stations():
    """Live APRS-IS snapshot (all stations heard over Scotland)."""
    with _aprs_is_lock:
        return {"status": _aprs_is_status, "stations": list(_aprs_is_stations.values())}

@app.get("/api/aprs/lookup")
async def api_aprs_lookup(calls: str = Query(...)):
    """Proxy to aprs.fi API — ?calls=MM7BVP,GM4JJJ (comma-separated callsigns)."""
    try:
        url = (
            f"https://api.aprs.fi/api/get"
            f"?name={urllib.parse.quote(calls)}&what=loc"
            f"&apikey={APRS_FI_KEY}&format=json"
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": "AisAdsbDashboard/1.0 (+http://localhost:8083)"
        })
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"result": "fail", "found": 0, "entries": [], "error": str(e)})

@app.get("/api/check/gotify")
async def api_check_gotify():
    try:
        r = urllib.request.urlopen(
            urllib.request.Request(f"{GOTIFY_URL.rstrip('/')}/health"), timeout=5)
        return {"ok":True,"status":r.status}
    except Exception as e:
        return {"ok":False,"error":str(e)}

# ── HTML / JS / CSS ────────────────────────────────────────────────────────────
_CFG_JS = json.dumps({
    "version":           _VERSION,
    "aprsCall":          _APRS_CALL,
    "mapLat":            55.93,
    "mapLon":            -4.72,
    "mapZoom":           11,
    "aisLabelColors":    AIS_LABEL_COLORS,
})

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIS-ADSB DASHBOARD</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><text y='28' font-size='28'>⚓</text></svg>">
<link rel="stylesheet" href="/leaflet.css"/>
<script src="/leaflet.js"></script>
<script>
  /* stubs required by tar1090 markers.js */
  var halloween=false, atcStyle=false, squareMania=false, outlineWidth=1.7;
  var usp={has:function(){return false;},getInt:function(){return 16;}};
</script>
<script src="http://192.168.0.45/tar1090/markers_c269fed24b04a113d1e23fbf5121e9c5.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;
  --orange:#f97316;--green:#3fb950;--red:#f85149;--yellow:#d29922;
  --status:44px;
}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
     height:100vh;height:100dvh;display:flex;flex-direction:column;overflow:hidden}
/* header */
header{background:var(--card);border-bottom:1px solid var(--border);
       display:flex;flex-direction:column;padding:.3rem 1rem 0;flex-shrink:0}
.hdr-top{display:flex;align-items:center;gap:.65rem;padding-bottom:.25rem}
header h1{font-size:1.05rem;font-weight:700;color:var(--orange);letter-spacing:.02em}
header h1 span{font-size:.72rem;color:var(--muted);margin-left:.4rem;font-weight:400}
nav{display:flex;gap:.2rem;flex-wrap:wrap;border-top:1px solid var(--border);padding:.25rem 0 .3rem}
.tab-btn{background:transparent;border:1px solid transparent;color:var(--muted);
         padding:.35rem .9rem;border-radius:6px;cursor:pointer;font-size:.85rem;transition:.15s;
         min-height:44px;display:inline-flex;align-items:center;
         touch-action:manipulation;-webkit-tap-highlight-color:rgba(249,115,22,.2);
         user-select:none;-webkit-user-select:none}
.tab-btn:hover{color:var(--text);border-color:var(--border)}
.tab-btn.active{color:var(--orange);border-color:var(--orange);background:#f9731615}
/* status bar */
#statusbar{height:var(--status);background:var(--card);border-bottom:1px solid var(--border);
           display:flex;align-items:center;padding:0 1rem;gap:.75rem;flex-shrink:0;font-size:.82rem}
.dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.dot.running{background:var(--green);box-shadow:0 0 6px var(--green)}
.dot.stopped{background:var(--muted)}
.dot.error{background:var(--red);box-shadow:0 0 6px var(--red)}
.dot.starting{background:var(--yellow);animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#sdr-error{color:var(--red);font-size:.78rem;max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.status-sep{color:var(--border)}
.spacer{flex:1}
/* buttons */
.btn{padding:.3rem .85rem;border-radius:6px;border:none;cursor:pointer;font-size:.82rem;
     font-weight:600;transition:.15s}
.btn-start{background:var(--green);color:#000}.btn-start:hover{opacity:.85}
.btn-start:disabled{background:#2a4a2a;color:var(--muted);cursor:not-allowed}
.btn-stop{background:var(--red);color:#fff}.btn-stop:hover{opacity:.85}
.btn-stop:disabled{background:#3a1a1a;color:var(--muted);cursor:not-allowed}
.btn-primary{background:var(--orange);color:#000}.btn-primary:hover{opacity:.85}
.btn-sm{padding:.2rem .6rem;font-size:.75rem}
/* tab content */
main{flex:1;min-height:0;overflow:hidden;display:flex;flex-direction:column}
.tab{display:none;flex:1;overflow:hidden;flex-direction:column}
.tab.active{display:flex}
.table-wrap{flex:1;overflow:auto;background:var(--card);border:1px solid var(--border);
            border-radius:8px;min-height:0}
table{width:100%;border-collapse:collapse;font-size:.8rem}
thead th{padding:.5rem .75rem;text-align:left;color:var(--muted);font-weight:500;
         border-bottom:1px solid var(--border);position:sticky;top:0;
         background:var(--card);white-space:nowrap}
tbody tr{border-bottom:1px solid #21262d;cursor:pointer;transition:.1s}
tbody tr:hover{background:#1f2937}
tbody td{padding:.45rem .75rem;vertical-align:middle}
.badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.72rem;font-weight:600}
/* ── SETTINGS TAB ── */
#tab-settings{padding:.75rem;overflow-y:auto}
.settings-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
               gap:.75rem;margin-bottom:.75rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:.75rem}
.card h3{font-size:.85rem;font-weight:600;color:var(--orange);margin-bottom:.65rem;
         padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.field{display:flex;flex-direction:column;gap:.25rem;margin-bottom:.55rem}
.field label{font-size:.75rem;color:var(--muted)}
.field input,.field select{background:#0d1117;border:1px solid var(--border);color:var(--text);
  padding:.35rem .6rem;border-radius:6px;font-size:.82rem;width:100%}
.field input:focus,.field select:focus{outline:none;border-color:var(--orange)}
.checkbox-row{display:flex;align-items:center;gap:.5rem;margin-bottom:.35rem;font-size:.82rem;cursor:pointer}
.checkbox-row input[type=checkbox]{accent-color:var(--orange);width:14px;height:14px}
.status-chip{display:inline-flex;align-items:center;gap:.35rem;padding:.25rem .6rem;
             border-radius:20px;font-size:.75rem;font-weight:500}
.chip-ok{background:#1a3a1a;color:var(--green)}
.chip-err{background:#3a1a1a;color:var(--red)}
.chip-unk{background:#2a2a1a;color:var(--yellow)}
/* ── TOAST ── */
#toast{position:fixed;bottom:1.5rem;right:1.5rem;background:var(--card);
       border:1px solid var(--orange);border-left:4px solid var(--orange);
       border-radius:8px;padding:.75rem 1rem;max-width:340px;z-index:20000;
       box-shadow:0 4px 20px #0008;transition:.3s;transform:translateY(0);opacity:1}
#toast.hide{transform:translateY(20px);opacity:0;pointer-events:none}
#toast .toast-title{font-size:.82rem;font-weight:700;color:var(--orange);margin-bottom:.25rem}
#toast .toast-body{font-size:.75rem;color:var(--muted)}
/* scrollbar */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
/* ── AIS TAB ── */
#tab-ais{padding:.75rem;gap:.75rem}
#tab-weather,#tab-adsb{padding:0;overflow:hidden}
#tab-weather iframe,#tab-adsb iframe{width:100%;flex:1;border:none;display:block;min-height:0}
#tab-tides{padding:.75rem;overflow-y:auto}
#tab-aprs{padding:.75rem;gap:.75rem}
.aprs-bar{display:flex;align-items:center;gap:.65rem;flex-shrink:0;font-size:.82rem;
          background:var(--card);border:1px solid var(--border);border-radius:6px;padding:.4rem .75rem;flex-wrap:wrap}
.aprs-bar .status-sep{color:var(--border)}
#aprs-search{background:#0d1117;border:1px solid var(--border);color:var(--text);
             padding:.25rem .55rem;border-radius:5px;font-size:.78rem;width:180px}
#aprs-search:focus{outline:none;border-color:#7c3aed}
.aprs-chip{display:inline-flex;align-items:center;gap:.3rem;background:#7c3aed18;
           border:1px solid #7c3aed55;border-radius:4px;padding:.1rem .45rem;
           font-size:.72rem;color:#a78bfa;cursor:pointer;user-select:none}
.aprs-chip:hover{background:#7c3aed30}
.aprs-chip .chip-x{color:#7c3aed;font-weight:700;margin-left:.2rem}
.aprs-split{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:.75rem;overflow:hidden;min-height:0}
.aprs-table-wrap{background:var(--card);border:1px solid var(--border);border-radius:8px;
                 display:flex;flex-direction:column;overflow:hidden}
.aprs-table-hdr{padding:.45rem .75rem;border-bottom:1px solid var(--border);font-size:.75rem;
                color:var(--muted);display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.aprs-table{width:100%;border-collapse:collapse;font-size:.78rem}
.aprs-table th{padding:.38rem .6rem;text-align:left;color:var(--muted);font-weight:500;
               border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)}
.aprs-table td{padding:.35rem .6rem;border-bottom:1px solid #161b22;vertical-align:middle}
#aprs-tbody tr{cursor:pointer;transition:.1s}
#aprs-tbody tr:hover{background:#1f2937}
.aprs-call{font-weight:600;color:#a78bfa}
#aprs-map{border-radius:8px;border:1px solid var(--border);overflow:hidden}
.tide-table{width:100%;border-collapse:collapse;font-size:.82rem}
.tide-table th{padding:.4rem .75rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)}
.tide-table td{padding:.38rem .75rem;border-bottom:1px solid #161b22}
.tide-high{color:#3b82f6;font-weight:600}
.tide-low{color:var(--muted)}
.tide-bar-wrap{height:6px;background:#161b22;border-radius:3px;margin-top:3px}
.tide-bar{height:6px;border-radius:3px;background:#3b82f6}
.vp-watch-btn{width:100%;margin-top:.75rem;padding:.42rem .75rem;font-size:.8rem;border-radius:5px;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text);transition:background .15s}
.vp-watch-btn.watching{border-color:#3b82f6;color:#3b82f6;background:#3b82f615}
.vp-watch-btn:hover{background:#30363d}
#hdr-clock{font-size:.82rem;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;margin-left:.5rem}
.ais-bar{display:flex;align-items:center;gap:.65rem;flex-shrink:0;font-size:.82rem;
         background:var(--card);border:1px solid var(--border);border-radius:6px;
         padding:.4rem .75rem}
.ais-bar .status-sep{color:var(--border)}
#ais-search{background:#0d1117;border:1px solid var(--border);color:var(--text);
            padding:.25rem .55rem;border-radius:5px;font-size:.78rem;width:180px}
#ais-search:focus{outline:none;border-color:var(--orange)}
.ais-split{flex:1;display:grid;grid-template-columns:1fr 1fr;grid-template-rows:1fr;gap:.75rem;overflow:hidden;min-height:0}
.ais-vessel-panel{background:var(--card);border:1px solid var(--border);border-radius:8px;
                  display:flex;flex-direction:column;overflow:hidden}
.ais-vessel-header{padding:.45rem .75rem;border-bottom:1px solid var(--border);
                   font-size:.75rem;color:var(--muted);display:flex;
                   justify-content:space-between;align-items:center;flex-shrink:0;gap:.5rem}
#ais-tbody tr{border-bottom:1px solid #21262d;cursor:pointer;transition:.1s}
#ais-tbody tr:hover{background:#1f2937}
#ais-tbody td{padding:.35rem .6rem;vertical-align:middle}
#ais-map{border-radius:8px;border:1px solid var(--border);overflow:hidden}
.vessel-name-cell{line-height:1.2}
.vessel-alert-badge{display:inline-block;font-size:.68rem;font-weight:600;
                    padding:.1rem .35rem;border-radius:3px;margin-top:.1rem}
/* ── VESSEL INFO PANEL ── */
#vessel-panel{
  position:fixed;right:0;top:0;bottom:0;width:320px;
  background:var(--card);border-left:1px solid var(--border);
  z-index:15000;display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform .25s ease;
  box-shadow:-4px 0 24px #0008;
}
#vessel-panel.open{transform:translateX(0)}
#vessel-panel-photo{width:100%;height:180px;object-fit:cover;object-position:center;
                    display:none;background:#0d1117;flex-shrink:0}
#vessel-panel-photo.loaded{display:block}
.vp-placeholder{width:100%;height:120px;display:flex;align-items:center;justify-content:center;
                color:var(--muted);font-size:.78rem;background:#0d1117;flex-shrink:0}
#vessel-panel-body{flex:1;overflow-y:auto;padding:.85rem}
.vp-header{display:flex;justify-content:space-between;align-items:flex-start;gap:.5rem;margin-bottom:.75rem}
.vp-name{font-size:1.05rem;font-weight:700;color:var(--text);line-height:1.2}
.vp-close{background:none;border:none;color:var(--muted);font-size:1.3rem;cursor:pointer;
          line-height:1;padding:.1rem .4rem;border-radius:4px;flex-shrink:0}
.vp-close:hover{background:var(--border);color:var(--text)}
.vp-badge{display:inline-block;font-size:.72rem;font-weight:600;padding:.2rem .55rem;
          border-radius:4px;margin-bottom:.6rem}
.vp-row{display:flex;gap:.5rem;margin-bottom:.35rem;font-size:.8rem}
.vp-lbl{color:var(--muted);width:90px;flex-shrink:0}
.vp-val{color:var(--text);word-break:break-word}
.vp-link{display:block;margin-top:.85rem;text-align:center;padding:.45rem;
         border:1px solid var(--border);border-radius:6px;font-size:.8rem;
         color:var(--muted);text-decoration:none;transition:.15s}
.vp-link:hover{border-color:var(--orange);color:var(--orange)}
/* live AIS toggle button */
.ais-live-btn{padding:4px 8px;font-size:12px;cursor:pointer;
              background:#161b22;color:#8b949e;
              border:1px solid #30363d;border-radius:4px;transition:.2s}
.ais-live-btn.active{color:#3b82f6;border-color:#3b82f6}
/* ship name labels (Leaflet tooltips) */
.ship-lbl{background:rgba(13,17,23,0.88)!important;border:1px solid #445!important;
          color:#e6edf3!important;font-size:10px!important;font-weight:600;
          padding:2px 5px!important;border-radius:3px!important;
          white-space:nowrap;pointer-events:none;box-shadow:0 1px 4px #0006}
.ship-lbl::before{display:none!important}
/* ── ADSB TAB ── */
#tab-adsb{padding:.75rem;gap:.75rem}
.adsb-bar{display:flex;align-items:center;gap:.65rem;flex-shrink:0;font-size:.82rem;
          background:var(--card);border:1px solid var(--border);border-radius:6px;
          padding:.4rem .75rem;flex-wrap:wrap}
.adsb-bar .status-sep{color:var(--border)}
#adsb-search{background:#0d1117;border:1px solid var(--border);color:var(--text);
             padding:.25rem .55rem;border-radius:5px;font-size:.78rem;width:180px}
#adsb-search:focus{outline:none;border-color:var(--orange)}
.adsb-split{flex:1;display:grid;grid-template-columns:1fr 1fr;gap:.75rem;overflow:hidden;min-height:0}
.adsb-vessel-panel{background:var(--card);border:1px solid var(--border);border-radius:8px;
                   display:flex;flex-direction:column;overflow:hidden}
.adsb-vessel-header{padding:.45rem .75rem;border-bottom:1px solid var(--border);
                    font-size:.75rem;color:var(--muted);display:flex;
                    justify-content:space-between;align-items:center;flex-shrink:0}
#adsb-tbody tr{border-bottom:1px solid #21262d;cursor:pointer;transition:.1s}
#adsb-tbody tr:hover{background:#1f2937}
#adsb-tbody td{padding:.35rem .6rem;vertical-align:middle}
#adsb-map{border-radius:8px;border:1px solid var(--border);overflow:hidden}
/* ── AIRCRAFT PANEL ── */
#aircraft-panel{
  position:fixed;right:0;top:0;bottom:0;width:320px;
  background:var(--card);border-left:1px solid var(--border);
  z-index:15001;display:flex;flex-direction:column;
  transform:translateX(100%);transition:transform .25s ease;
  box-shadow:-4px 0 24px #0008;
}
#aircraft-panel.open{transform:translateX(0)}
#ac-panel-photo{width:100%;height:180px;object-fit:cover;object-position:center;
                display:none;background:#0d1117;flex-shrink:0}
#ac-panel-photo.loaded{display:block}
#ac-panel-body{flex:1;overflow-y:auto;padding:.85rem}
/* ── MOBILE (≤640px) ──────────────────────────────────────── */
@media(max-width:640px){
  /* compact header */
  header{padding:.2rem .6rem 0}
  .hdr-top{gap:.4rem;padding-bottom:.15rem}
  /* hide decorative SVG ships — they take up space for no info */
  .hdr-top svg{display:none}
  header h1{font-size:.9rem}
  #hdr-clock{font-size:.72rem;margin-left:auto}
  /* status bar: collapse to a single thin strip showing just the dot + status */
  #statusbar{height:auto;padding:.2rem .6rem;gap:.4rem;font-size:.75rem;flex-wrap:wrap}
  #freq-label,#today-count,#total-count,.status-sep{display:none}
  #sdr-error{max-width:none;white-space:normal;font-size:.72rem}
  .btn.btn-start,.btn.btn-stop{padding:.2rem .55rem;font-size:.72rem}
  /* nav: wraps to 2 rows, no scroll container (scroll conflicts with tap on Android) */
  nav{flex-wrap:wrap;gap:.15rem;padding:.2rem 0 .25rem}
  .tab-btn{font-size:.75rem;padding:.2rem .55rem;min-height:36px}
  /* tables */
  .table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{font-size:.72rem}
  thead th,tbody td{padding:.35rem .5rem}
  /* AIS tab */
  #tab-ais{padding:.4rem;gap:.4rem}
  .ais-split{grid-template-columns:1fr !important}
  #ais-map{min-height:45vh}
  /* ADS-B tab */
  .adsb-split{grid-template-columns:1fr !important}
  #adsb-map{min-height:45vh}
  /* panels: full-width bottom sheets */
  #vessel-panel,#aircraft-panel{
    right:0;left:0;top:auto;width:100%;height:75vh;
    border-left:none;border-top:1px solid var(--border);
    transform:translateY(100%)}
  #vessel-panel.open,#aircraft-panel.open{transform:translateY(0)}
  #vessel-panel-photo{height:120px}
  #ac-panel-photo{height:120px}
  /* settings */
  #tab-settings{padding:.4rem}
  .settings-grid{grid-template-columns:1fr}
  /* tides */
  #tab-tides{padding:.4rem;gap:.4rem}
  /* aprs */
  #tab-aprs{padding:.4rem;gap:.4rem}
}
</style>
</head>
<body>

<header>
  <div class="hdr-top">
    <svg width="88" height="36" viewBox="0 0 360 148" xmlns="http://www.w3.org/2000/svg">
      <!-- shadow --><ellipse cx="180" cy="144" rx="148" ry="4" fill="#000" opacity=".35"/>
      <!-- anti-fouling hull --><path d="M22,112 L338,112 Q350,110 354,97 L344,82 L18,82 L10,97 Q12,112 22,112Z" fill="#7a1818"/>
      <!-- main hull sides --><rect x="18" y="46" width="326" height="38" fill="#1a2744"/>
      <!-- waterline stripe --><rect x="18" y="80" width="326" height="4" fill="#e8e8e8" opacity=".55"/>
      <!-- bow --><path d="M344,46 L344,112 Q358,102 360,79 Q358,58 344,46Z" fill="#122038"/>
      <!-- stern --><path d="M18,46 L18,112 Q4,100 3,79 Q5,60 18,46Z" fill="#122038"/>
      <!-- deck --><rect x="18" y="38" width="326" height="10" rx="2" fill="#243860"/>
      <!-- bridge block --><rect x="30" y="10" width="132" height="30" rx="3" fill="#1e3565"/>
      <!-- bridge wings -->
      <rect x="12" y="18" width="22" height="8" rx="1" fill="#1a2f5a"/>
      <rect x="162" y="18" width="20" height="8" rx="1" fill="#1a2f5a"/>
      <!-- bridge windows -->
      <rect x="38"  y="15" width="14" height="9" rx="1" fill="#A8D8F0" opacity=".85"/>
      <rect x="57"  y="15" width="14" height="9" rx="1" fill="#A8D8F0" opacity=".85"/>
      <rect x="76"  y="15" width="14" height="9" rx="1" fill="#A8D8F0" opacity=".85"/>
      <rect x="95"  y="15" width="14" height="9" rx="1" fill="#A8D8F0" opacity=".85"/>
      <rect x="114" y="15" width="14" height="9" rx="1" fill="#A8D8F0" opacity=".85"/>
      <rect x="133" y="15" width="14" height="9" rx="1" fill="#A8D8F0" opacity=".85"/>
      <!-- bridge roof --><rect x="24" y="6" width="144" height="6" rx="2" fill="#162950"/>
      <!-- radar mast --><line x1="96" y1="6" x2="96" y2="2" stroke="#aaa" stroke-width="2"/>
      <circle cx="96" cy="2" r="3" fill="#f97316" opacity=".75"/>
      <!-- funnel --><rect x="178" y="6" width="28" height="34" rx="3" fill="#1a3a6c"/>
      <rect x="173" y="2" width="38" height="6" rx="2" fill="#0d1933"/>
      <rect x="178" y="24" width="28" height="5" fill="#f97316" opacity=".5"/>
      <!-- forward mast --><line x1="292" y1="20" x2="292" y2="40" stroke="#888" stroke-width="3"/>
      <line x1="275" y1="26" x2="309" y2="26" stroke="#888" stroke-width="2"/>
      <!-- aft mast --><line x1="242" y1="24" x2="242" y2="40" stroke="#888" stroke-width="2.5"/>
      <!-- cargo hatches -->
      <rect x="216" y="40" width="48" height="6" rx="1" fill="#1d3060"/>
      <rect x="270" y="40" width="48" height="6" rx="1" fill="#1d3060"/>
      <!-- portholes -->
      <circle cx="50"  cy="64" r="5" fill="#0d1933"/><circle cx="50"  cy="64" r="3" fill="#A8D8F0" opacity=".4"/>
      <circle cx="78"  cy="64" r="5" fill="#0d1933"/><circle cx="78"  cy="64" r="3" fill="#A8D8F0" opacity=".4"/>
      <circle cx="106" cy="64" r="5" fill="#0d1933"/><circle cx="106" cy="64" r="3" fill="#A8D8F0" opacity=".4"/>
      <circle cx="134" cy="64" r="5" fill="#0d1933"/><circle cx="134" cy="64" r="3" fill="#A8D8F0" opacity=".4"/>
    </svg>
    <h1 style="font-size:1.05rem;font-weight:700;color:var(--orange);letter-spacing:.02em">
      AIS-ADSB DASHBOARD <span id="ver" style="font-size:.72rem;color:var(--muted);font-weight:400"></span>
    </h1>
    <svg width="88" height="36" viewBox="0 0 360 148" xmlns="http://www.w3.org/2000/svg">
      <!-- shadow --><ellipse cx="180" cy="142" rx="140" ry="4" fill="#000" opacity=".35"/>
      <!-- tail fin --><path d="M52,66 L26,14 Q23,7 33,7 L62,7 Q73,9 77,22 L92,66 Z" fill="#1e3565"/>
      <!-- tail flash --><path d="M40,42 L30,20 Q28,14 36,14 L52,14 Z" fill="#f97316" opacity=".8"/>
      <!-- tailplane --><path d="M60,70 L14,54 L52,66 Z" fill="#162950"/>
      <!-- fuselage --><path d="M46,62 L292,62 Q334,66 350,79 Q334,92 292,96 L58,96 Q34,92 30,79 Q34,66 46,62 Z" fill="#1a2744"/>
      <!-- belly --><path d="M40,86 L310,86 Q330,84 342,80 Q334,92 292,96 L58,96 Q42,92 40,86 Z" fill="#122038"/>
      <!-- cheatline --><rect x="40" y="80" width="296" height="4" fill="#e8e8e8" opacity=".55"/>
      <!-- cockpit --><path d="M322,68 Q338,71 344,78 L320,78 Q314,74 316,68 Z" fill="#A8D8F0" opacity=".85"/>
      <!-- windows -->
      <rect x="94"  y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="112" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="130" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="148" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="166" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="184" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="202" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="220" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="238" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="256" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <rect x="274" y="70" width="9" height="8" rx="2" fill="#A8D8F0" opacity=".7"/>
      <!-- wing (swept, foreground) --><path d="M206,84 L130,128 L162,132 L242,88 Z" fill="#1e3565"/>
      <!-- engine pod --><rect x="196" y="98" width="52" height="22" rx="10" fill="#0d1933"/>
      <rect x="242" y="100" width="8" height="18" rx="3" fill="#f97316" opacity=".7"/>
      <circle cx="204" cy="109" r="8" fill="#162950"/>
      <!-- beacon --><circle cx="62" cy="5" r="3" fill="#FF3333" opacity=".95"/>
    </svg>
    <span id="hdr-clock"></span>
  </div>
  <nav>
    <button class="tab-btn active" data-tab="ais">AIS</button>
    <button class="tab-btn" data-tab="weather">Weather</button>
    <button class="tab-btn" data-tab="adsb">ADSB</button>
    <button class="tab-btn" data-tab="tides">Tides</button>
    <button class="tab-btn" data-tab="aprs">APRS</button>
    <button class="tab-btn" data-tab="intel">Intel</button>
    <button class="tab-btn" data-tab="settings">Settings</button>
  </nav>
</header>
<div id="dbg" style="background:#1a3a1a;color:#7ee787;font-size:.7rem;padding:.2rem .6rem;text-align:center;display:none">JS loading…</div>

<!-- receiver control bar: AIS-catcher (Airspy) + readsb (RTL-SDR) -->
<div id="statusbar">
  <div class="dot stopped" id="rx-ais-dot"></div>
  <span><b>AIS</b> <span style="color:var(--muted)">Airspy</span></span>
  <span id="rx-ais-state" style="color:var(--muted)">checking…</span>
  <button class="btn btn-sm btn-start" id="rx-ais-start"   onclick="rxCtl('ais','start')" disabled>▶</button>
  <button class="btn btn-sm btn-stop"  id="rx-ais-stop"    onclick="rxCtl('ais','stop')" disabled>■</button>
  <button class="btn btn-sm"           id="rx-ais-restart" onclick="rxCtl('ais','restart')" disabled>↻</button>
  <span class="status-sep">|</span>
  <div class="dot stopped" id="rx-adsb-dot"></div>
  <span><b>ADS-B</b> <span style="color:var(--muted)">RTL-SDR</span></span>
  <span id="rx-adsb-state" style="color:var(--muted)">checking…</span>
  <button class="btn btn-sm btn-start" id="rx-adsb-start"   onclick="rxCtl('adsb','start')" disabled>▶</button>
  <button class="btn btn-sm btn-stop"  id="rx-adsb-stop"    onclick="rxCtl('adsb','stop')" disabled>■</button>
  <button class="btn btn-sm"           id="rx-adsb-restart" onclick="rxCtl('adsb','restart')" disabled>↻</button>
  <div class="spacer"></div>
  <span id="rx-counts" style="color:var(--muted)"></span>
</div>


<main>
  <!-- ── SETTINGS ── -->
  <div class="tab" id="tab-settings">
    <div class="settings-grid">
      <div class="card">
        <h3>Notifications</h3>
        <label class="checkbox-row"><input type="checkbox" id="s-telegram"> Telegram</label>
        <label class="checkbox-row"><input type="checkbox" id="s-gotify"> Gotify</label>
        <div style="margin-top:.65rem">
          <div style="font-size:.75rem;color:var(--muted);margin-bottom:.35rem">Gotify status:</div>
          <div id="gotify-status" class="status-chip chip-unk">Checking…</div>
          <button class="btn btn-sm" style="margin-top:.5rem" onclick="checkGotify()">Re-check</button>
        </div>
        <div style="margin-top:.75rem;font-size:.72rem;color:var(--muted)">
          Credentials are stored in secrets.json — edit the file and restart to change them.
        </div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap;margin-top:.1rem">
      <button class="btn btn-primary" onclick="saveSettings()">Save settings</button>
      <span id="settings-saved" style="font-size:.8rem;color:var(--green);display:none">Saved ✓</span>
    </div>
  </div>

  <!-- ── AIS ── -->
  <div class="tab active" id="tab-ais">
    <div class="ais-bar">
      <div class="dot stopped" id="ais-dot"></div>
      <span id="ais-status-text">Connecting…</span>
      <span class="status-sep">|</span>
      <span id="ais-count">0 vessels</span>
      <span class="status-sep">|</span>
      <input type="text" id="ais-search" placeholder="Name / MMSI…" oninput="renderAisTable()">
      <select id="ais-type-filter" onchange="renderAisTable()"
        style="background:#0d1117;border:1px solid var(--border);color:var(--text);padding:.25rem .45rem;border-radius:5px;font-size:.78rem">
        <option value="">All types</option>
        <option value="cargo">Cargo</option>
        <option value="tanker">Tanker</option>
        <option value="passenger">Passenger</option>
        <option value="fishing">Fishing</option>
        <option value="pleasure">Pleasure</option>
        <option value="tug">Tug/Salvage</option>
        <option value="sar">SAR/Military</option>
        <option value="other">Other</option>
      </select>
      <label class="checkbox-row" style="margin:0;font-size:.8rem">
        <input type="checkbox" id="ais-watched-only" onchange="renderAisTable()"> Watched only
      </label>
    </div>
    <div class="ais-split">
      <div class="ais-vessel-panel">
        <div class="ais-vessel-header">
          <span>Vessels</span>
          <span id="ais-table-count" style="color:var(--muted)"></span>
        </div>
        <div style="overflow-y:auto;flex:1">
          <table style="width:100%;border-collapse:collapse;font-size:.8rem">
            <thead>
              <tr style="background:var(--card)">
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card);cursor:pointer" onclick="aisSort('name')">Name</th>
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)">MMSI</th>
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)">Ch</th>
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card);cursor:pointer" onclick="aisSort('dist')">Dist ▾</th>
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card);cursor:pointer" onclick="aisSort('time')">Last seen</th>
              </tr>
            </thead>
            <tbody id="ais-tbody"></tbody>
          </table>
        </div>
      </div>
      <div id="ais-map"></div>
    </div>
  </div>

  <!-- ── Weather ── -->
  <div class="tab" id="tab-weather">
    <div style="display:flex;gap:.45rem;padding:.45rem .6rem;flex-shrink:0;flex-wrap:wrap;background:var(--card);border-bottom:1px solid var(--border)">
      <button class="btn btn-sm wx-btn" data-wx="radar"     onclick="setWeatherView('radar',this)">🌧 Radar</button>
      <button class="btn btn-sm wx-btn" data-wx="wind"      onclick="setWeatherView('wind',this)">💨 Wind</button>
      <button class="btn btn-sm wx-btn" data-wx="waves"     onclick="setWeatherView('waves',this)">🌊 Waves</button>
      <button class="btn btn-sm wx-btn" data-wx="temp"      onclick="setWeatherView('temp',this)">🌡 Temp</button>
      <button class="btn btn-sm wx-btn" data-wx="clouds"    onclick="setWeatherView('clouds',this)">☁ Clouds</button>
      <button class="btn btn-sm wx-btn" data-wx="lightning" onclick="setWeatherView('lightning',this)">⚡ Lightning</button>
    </div>
    <iframe id="weather-frame" src="" allowfullscreen></iframe>
  </div>

  <!-- ── ADSB ── -->
  <div class="tab" id="tab-adsb">
    <div class="adsb-bar">
      <div class="dot stopped" id="adsb-dot"></div>
      <span id="adsb-status-text">Starting…</span>
      <span class="status-sep">|</span>
      <span id="adsb-count">0 aircraft</span>
      <span class="status-sep">|</span>
      <button class="btn btn-sm" id="adsb-net-btn" onclick="toggleNetAircraft(this)"
        title="Show aircraft beyond local receiver range via adsb.fi / adsb.lol / airplanes.live"
        style="color:#60a5fa">🌐 Network: ON</button>
      <span class="status-sep">|</span>
      <input type="text" id="adsb-search" placeholder="Reg / callsign / ICAO…" oninput="renderAdsbTable()">
      <select id="adsb-type-filter" onchange="renderAdsbTable()"
        style="background:#0d1117;border:1px solid var(--border);color:var(--text);padding:.25rem .45rem;border-radius:5px;font-size:.78rem">
        <option value="">All aircraft</option>
        <option value="heli">Helicopters</option>
        <option value="military">Military</option>
        <option value="alert">Alerts only</option>
      </select>
      <label class="checkbox-row" style="margin:0;font-size:.8rem">
        <input type="checkbox" id="adsb-alert-only" onchange="renderAdsbTable()"> Alerts only
      </label>
      <span class="status-sep">|</span>
      <a href="http://192.168.0.45/tar1090/" target="_blank" style="font-size:.75rem;color:var(--muted);text-decoration:none">tar1090 ↗</a>
    </div>
    <div class="adsb-split">
      <div class="adsb-vessel-panel">
        <div class="adsb-vessel-header">
          <span>Aircraft</span>
          <span id="adsb-table-count" style="color:var(--muted)"></span>
        </div>
        <div style="overflow-y:auto;flex:1">
          <table style="width:100%;border-collapse:collapse;font-size:.8rem">
            <thead>
              <tr style="background:var(--card)">
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)">Reg / Hex</th>
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)">Callsign</th>
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)">Type</th>
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card);cursor:pointer" onclick="adsbSort('dist')">Dist ▾</th>
                <th style="padding:.45rem .6rem;text-align:left;color:var(--muted);font-weight:500;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card)">Alt</th>
              </tr>
            </thead>
            <tbody id="adsb-tbody"></tbody>
          </table>
        </div>
      </div>
      <div id="adsb-map"></div>
    </div>
  </div>

  <!-- ── Intel ── -->
  <div class="tab" id="tab-intel" style="padding:.75rem;gap:.75rem;overflow-y:auto">
    <div style="display:flex;gap:.5rem;align-items:center;flex-shrink:0;flex-wrap:wrap;margin-bottom:.25rem">
      <span style="font-size:.82rem;font-weight:600;color:var(--text)">Aircraft Intelligence</span>
      <span id="intel-count" style="font-size:.75rem;color:var(--muted)"></span>
      <div style="flex:1"></div>
      <input type="text" id="intel-search" placeholder="Reg / ICAO / type…"
        oninput="renderIntelTable()"
        style="background:var(--card);border:1px solid var(--border);color:var(--text);
               padding:.3rem .6rem;border-radius:6px;font-size:.8rem;width:190px">
      <select id="intel-label-filter" onchange="renderIntelTable()"
        style="background:var(--card);border:1px solid var(--border);color:var(--text);
               padding:.3rem .55rem;border-radius:6px;font-size:.8rem">
        <option value="">All labels</option>
        <option value="Military">Military</option>
        <option value="HM Coastguard SAR">HM Coastguard SAR</option>
        <option value="SAR">SAR</option>
        <option value="Police">Police</option>
      </select>
      <button class="btn btn-sm" onclick="loadIntel()">↺ Refresh</button>
    </div>
    <div class="table-wrap" style="flex-shrink:0;max-height:45%">
      <table>
        <thead><tr>
          <th>ICAO</th><th>Reg</th><th>Type</th><th>Description</th>
          <th>Owner / Operator</th><th>Label</th><th>Alerts</th>
          <th>First seen</th><th>Last seen</th>
        </tr></thead>
        <tbody id="intel-body"></tbody>
      </table>
    </div>
    <div style="display:flex;gap:.5rem;align-items:center;flex-shrink:0;flex-wrap:wrap;margin:.5rem 0 .25rem">
      <span style="font-size:.82rem;font-weight:600;color:var(--text)">Vessel Intelligence</span>
      <span id="intel-ves-count" style="font-size:.75rem;color:var(--muted)"></span>
      <div style="flex:1"></div>
      <input type="text" id="intel-ves-search" placeholder="Name / MMSI / label…"
        oninput="renderVesIntelTable()"
        style="background:var(--card);border:1px solid var(--border);color:var(--text);
               padding:.3rem .6rem;border-radius:6px;font-size:.8rem;width:190px">
      <button class="btn btn-sm" onclick="loadVesselIntel()">↺ Refresh</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>MMSI</th><th>Name</th><th>Type</th><th>Label</th>
          <th>Alerts</th><th>First seen</th><th>Last seen</th>
        </tr></thead>
        <tbody id="intel-ves-body"></tbody>
      </table>
    </div>
    <div style="font-size:.7rem;color:var(--muted);flex-shrink:0">Click any row for details &amp; photo. Vessel history builds from AIS alerts (SAR, law enforcement, military, watched vessels).</div>
  </div>

  <!-- ── Tides ── -->
  <div class="tab" id="tab-tides">
    <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:.75rem">
      <div>
        <div style="font-size:.95rem;font-weight:700;color:var(--orange)">Tide Times — Greenock, River Clyde</div>
        <div id="tide-meta" style="font-size:.72rem;color:var(--muted);margin-top:.15rem"></div>
      </div>
      <button class="btn btn-sm" style="margin-left:auto" onclick="loadTides(true)">↺ Refresh</button>
    </div>
    <div id="tide-error" style="display:none;color:var(--red);font-size:.82rem;margin-bottom:.6rem"></div>
    <div id="tide-content"></div>
  </div>

  <!-- ── APRS ── -->
  <div class="tab" id="tab-aprs">
    <div class="aprs-bar">
      <div class="dot stopped" id="aprs-live-dot"></div>
      <span style="font-weight:600;color:#a78bfa">APRS-IS Live</span>
      <span id="aprs-live-count" style="color:var(--muted)">—</span>
      <span class="status-sep">|</span>
      <input type="text" id="aprs-search" placeholder="Callsign(s) e.g. MM7BVP,GM4JJJ" onkeydown="if(event.key==='Enter')searchAprs()">
      <button class="btn btn-sm" onclick="searchAprs()">Look up</button>
      <span class="status-sep">|</span>
      <span id="aprs-count" style="color:var(--muted)">—</span>
      <span class="status-sep">|</span>
      <span id="aprs-chips" style="display:flex;gap:.35rem;flex-wrap:wrap"></span>
    </div>
    <div class="aprs-split">
      <div class="aprs-table-wrap">
        <div class="aprs-table-hdr">
          <span>Stations</span>
          <span id="aprs-table-count" style="color:var(--muted)"></span>
        </div>
        <div style="overflow-y:auto;flex:1">
          <table class="aprs-table">
            <thead><tr>
              <th>Callsign</th><th>Last heard</th><th>Lat</th><th>Lon</th>
              <th>Spd</th><th>Crs</th><th>Comment</th>
            </tr></thead>
            <tbody id="aprs-tbody"></tbody>
          </table>
        </div>
      </div>
      <div id="aprs-map"></div>
    </div>
  </div>
</main>

<!-- vessel info panel (shared by AIS tab + live map overlay) -->
<div id="vessel-panel">
  <img id="vessel-panel-photo" src="" alt="Vessel photo">
  <div id="vp-placeholder" class="vp-placeholder">No photo available</div>
  <div id="vessel-panel-body">
    <div class="vp-header">
      <div>
        <div class="vp-name" id="vp-name">—</div>
        <span class="vp-badge" id="vp-badge" style="display:none"></span>
      </div>
      <button class="vp-close" onclick="closeVesselPanel()">×</button>
    </div>
    <div id="vp-rows"></div>
    <button class="vp-watch-btn" id="vp-watch-btn" onclick="toggleWatch()"></button>
    <a class="vp-link" id="vp-link" href="#" target="_blank" rel="noopener">View on VesselFinder ↗</a>
  </div>
</div>

<!-- aircraft info panel -->
<div id="aircraft-panel">
  <img id="ac-panel-photo" src="" alt="Aircraft photo">
  <div id="ac-placeholder" class="vp-placeholder">No photo available</div>
  <div id="ac-panel-body">
    <div class="vp-header">
      <div>
        <div class="vp-name" id="ac-reg">—</div>
        <span class="vp-badge" id="ac-badge" style="display:none"></span>
      </div>
      <button class="vp-close" onclick="closeAircraftPanel()">×</button>
    </div>
    <div id="ac-rows"></div>
    <a class="vp-link" id="ac-link" href="#" target="_blank" rel="noopener">View on ADSBExchange ↗</a>
  </div>
</div>

<!-- toast -->
<div id="toast" class="hide">
  <div class="toast-title" id="toast-title"></div>
  <div class="toast-body"  id="toast-body"></div>
</div>

<script>
""" + f"const CFG = {_CFG_JS};" + f"""
const ADSB_LABEL_COLORS = {json.dumps(ADSB_LABEL_COLORS)};
""" + """

// ── init ──────────────────────────────────────────────────────────────────────
(function(){const d=document.getElementById('dbg');d.style.display='block';d.textContent='✓ JS running — tap a tab';})();
document.getElementById('ver').textContent = 'v' + CFG.version;

// live clock
(function _clock() {
  const el = document.getElementById('hdr-clock');
  function tick() {
    const now = new Date();
    const d = now.toLocaleDateString('en-GB',{weekday:'short',day:'2-digit',month:'short'});
    const t = now.toLocaleTimeString('en-GB');
    el.textContent = d + '  ' + t;
  }
  tick();
  setInterval(tick, 1000);
})();

// watched vessels (DB-backed)
let watchedMmsis = new Set();
let _vpCurrentMmsi = null;

function _loadWatched() {
  fetch('/api/ais/watched').then(r=>r.json()).then(rows=>{
    watchedMmsis = new Set(rows.map(r=>r.mmsi));
  }).catch(()=>{});
}
_loadWatched();

function toggleWatch() {
  if (_vpCurrentMmsi == null) return;
  const mmsi = _vpCurrentMmsi;
  const name = document.getElementById('vp-name').textContent || '';
  if (watchedMmsis.has(mmsi)) {
    fetch(`/api/ais/watch/${mmsi}`, {method:'DELETE'}).then(()=>{
      watchedMmsis.delete(mmsi);
      _renderWatchBtn(mmsi);
      renderAisTable();
    });
  } else {
    fetch(`/api/ais/watch/${mmsi}`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name})
    }).then(()=>{
      watchedMmsis.add(mmsi);
      _renderWatchBtn(mmsi);
      renderAisTable();
    });
  }
}

function _renderWatchBtn(mmsi) {
  const btn = document.getElementById('vp-watch-btn');
  if (!btn) return;
  const w = watchedMmsis.has(mmsi);
  btn.textContent = w ? '👁 Watching — click to unwatch' : '+ Watch this vessel';
  btn.classList.toggle('watching', w);
}

let _toastTimer = null;

// ── debounced table renders ────────────────────────────────────────────────────
// SSE bursts (e.g. ~300 network aircraft every 30s) must NOT each rebuild the
// whole table — that blocks the main thread for seconds and swallows clicks.
let _aisTableTimer = null, _adsbTableTimer = null;
function _schedAisTable() {
  if (_aisTableTimer) return;
  _aisTableTimer = setTimeout(() => { _aisTableTimer = null; renderAisTable(); }, 900);
}
function _schedAdsbTable() {
  if (_adsbTableTimer) return;
  _adsbTableTimer = setTimeout(() => { _adsbTableTimer = null; renderAdsbTable(); }, 900);
}

// ── SSE ───────────────────────────────────────────────────────────────────────
const es = new EventSource('/api/events');
es.onmessage = function(e) {
  const ev = JSON.parse(e.data);
  if (ev.type === 'init') {
    // AIS init
    updateAisStatus(ev.ais_connected, null);
    if (ev.ais_vessels && ev.ais_vessels.length) {
      ev.ais_vessels.forEach(v => { aisVessels[v.mmsi] = v; });
      updateAisCount();
      if (document.getElementById('tab-ais').classList.contains('active')) {
        ev.ais_vessels.forEach(updateAisMarker);
        renderAisTable();
      }
    }
  } else if (ev.type === 'ais_status') {
    updateAisStatus(ev.connected, ev.count);
  } else if (ev.type === 'ais_vessel') {
    aisVessels[ev.mmsi] = ev;
    if (document.getElementById('tab-ais').classList.contains('active')) {
      updateAisMarker(ev);
      _schedAisTable();
    }
    updateAisCount();
    _putAisOnAdsb(ev);
  } else if (ev.type === 'ais_alert') {
    aisVessels[ev.mmsi] = ev;
    if (document.getElementById('tab-ais').classList.contains('active')) {
      updateAisMarker(ev);
      _schedAisTable();
    }
    showToastAis(ev);
    _putAisOnAdsb(ev);
  } else if (ev.type === 'ais_vessel_remove') {
    delete aisVessels[ev.mmsi];
    if (aisMarkers[ev.mmsi]) { aisMap && aisMap.removeLayer && aisMarkers[ev.mmsi] && aisMap.removeLayer(aisMarkers[ev.mmsi]); delete aisMarkers[ev.mmsi]; }
    if (liveAisMarkers[ev.mmsi]) { liveAisLayer && liveAisLayer.removeLayer(liveAisMarkers[ev.mmsi]); delete liveAisMarkers[ev.mmsi]; }
    updateAisCount();
    if (document.getElementById('tab-ais').classList.contains('active')) _schedAisTable();
    _removeAisFromAdsb(ev.mmsi);
  } else if (ev.type === 'adsb_aircraft') {
    adsbAircraft[ev.hex] = ev;
    if (document.getElementById('tab-adsb').classList.contains('active')) {
      updateAdsbMarker(ev); _schedAdsbTable();
    }
    _putAdsbOnAis(ev);
  } else if (ev.type === 'adsb_alert') {
    adsbAircraft[ev.hex] = ev;
    if (document.getElementById('tab-adsb').classList.contains('active')) {
      updateAdsbMarker(ev); _schedAdsbTable();
    }
    showToastAdsb(ev);
    _putAdsbOnAis(ev);
  } else if (ev.type === 'adsb_remove') {
    delete adsbAircraft[ev.hex];
    if (adsbMarkers[ev.hex]) { adsbMap && adsbMap.removeLayer(adsbMarkers[ev.hex]); delete adsbMarkers[ev.hex]; }
    if (document.getElementById('tab-adsb').classList.contains('active')) _schedAdsbTable();
    _removeAdsbFromAis(ev.hex);
  } else if (ev.type === 'aprs_station') {
    aprsLiveStations[ev.call] = ev;
    if (aprsMap) updateAprsLiveMarker(ev);
    updateAprsLiveCount();
  } else if (ev.type === 'aprs_remove') {
    removeAprsLiveMarker(ev.call);
  } else if (ev.type === 'correlation_alert') {
    showToastSimple(
      '⚠ SAR Co-location',
      `${ev.vessel_name} [${ev.vessel_label}] + ${ev.aircraft_reg} [${ev.aircraft_label}] — ${ev.distance_nm}nm`
    );
  }
};
es.onerror = function() {
  // EventSource auto-reconnects; receiver bar / watchdog cover visible status
};

// ── receiver controls (AIS-catcher / readsb) ──────────────────────────────────
const RX_NAMES = {ais: 'AIS receiver (Airspy)', adsb: 'ADS-B receiver (RTL-SDR)'};
function renderRx(name, d) {
  const dot = document.getElementById('rx-'+name+'-dot');
  const st  = document.getElementById('rx-'+name+'-state');
  if (!dot || !st || !d) return;
  const cls = d.active==='active'     ? 'running'
            : d.active==='failed'     ? 'error'
            : d.active==='activating' ? 'starting' : 'stopped';
  dot.className = 'dot '+cls;
  let txt = d.active;
  if (name==='ais'  && d.active==='active') txt += d.connected ? ' · feed OK' : ' · no feed';
  if (name==='adsb' && d.active==='active')
    txt += (d.data_age!=null && d.data_age < 30) ? ' · data OK' : ' · data stale';
  st.textContent = txt;
  st.style.color = d.active==='active' ? 'var(--green)'
                 : d.active==='failed' ? 'var(--red)' : 'var(--muted)';
  const busy = d.active==='activating' || d.active==='deactivating';
  document.getElementById('rx-'+name+'-start').disabled   = d.active==='active' || busy;
  document.getElementById('rx-'+name+'-stop').disabled    = d.active!=='active' || busy;
  document.getElementById('rx-'+name+'-restart').disabled = d.active!=='active' || busy;
}
function loadRx() {
  fetch('/api/receivers').then(r=>r.json()).then(d=>{
    renderRx('ais', d.ais); renderRx('adsb', d.adsb);
    const c = document.getElementById('rx-counts');
    if (c) c.textContent = (d.ais.count||0)+' local vessels · '+(d.adsb.count||0)+' aircraft';
  }).catch(()=>{});
}
function rxCtl(name, action) {
  if (action==='stop' && !confirm('Stop the '+RX_NAMES[name]+'?\\n\\nThis releases the USB dongle and halts the live feed'+(name==='adsb' ? ' (and everything else fed by readsb: tar1090, FlightAware, ADSBx…)' : ' (including the AIS-catcher web UI)')+'.')) return;
  ['start','stop','restart'].forEach(a => {
    const b = document.getElementById('rx-'+name+'-'+a); if (b) b.disabled = true;
  });
  fetch('/api/receivers/'+name+'/'+action, {method:'POST'}).then(r=>r.json()).then(d=>{
    if (!d.ok) showToastSimple('Receiver '+action+' failed', d.error||'');
    loadRx();
    setTimeout(loadRx, 3000);
  }).catch(()=>loadRx());
}
loadRx();
setInterval(loadRx, 10000);

// ── tabs ──────────────────────────────────────────────────────────────────────
function showTab(name, btn) {
  try{const d=document.getElementById('dbg');d.style.background='#1a1a3a';d.style.color='#79c0ff';d.textContent='showTab('+name+') called';}catch(e){}
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  btn.classList.add('active');
  try{document.getElementById('dbg').textContent='tab-'+name+' active: '+document.getElementById('tab-'+name).classList.contains('active');}catch(e){}
  closeVesselPanel();
  if (name==='settings') loadSettings();
  if (name==='ais') {
    initAisMap();
    loadAisVessels();
    setTimeout(()=>aisMap&&aisMap.invalidateSize(),80);
  }
  if (name==='weather') {
    const fr = document.getElementById('weather-frame');
    if (!fr.getAttribute('src'))
      setWeatherView('radar', document.querySelector('.wx-btn[data-wx="radar"]'));
  }
  if (name==='adsb') {
    initAdsbMap();
    loadAdsbAircraft();
    setTimeout(()=>adsbMap&&adsbMap.invalidateSize(),80);
  }
  if (name==='tides') loadTides(false);
  if (name==='aprs') { initAprsMap(); loadAprsWatched(); loadAprsLiveStations(); setTimeout(()=>aprsMap&&aprsMap.invalidateSize(),80); }
  if (name==='intel') { loadIntel(); loadVesselIntel(); }
}

// Escape closes both panels (see ADSB section below)

// ── weather views ──────────────────────────────────────────────────────────────
const _WINDY_BASE = 'https://embed.windy.com/embed2.html?lat=56.0&lon=-4.5'
  + '&detailLat=55.95&detailLon=-4.76&zoom=7&level=surface&menu=&message=&marker='
  + '&calendar=now&pressure=&type=map&location=coordinates&detail='
  + '&metricWind=kt&metricTemp=%C2%B0C&radarRange=-1';
const _WX_URLS = {
  radar:     _WINDY_BASE + '&overlay=radar',
  wind:      _WINDY_BASE + '&overlay=wind',
  waves:     _WINDY_BASE + '&overlay=waves',
  temp:      _WINDY_BASE + '&overlay=temp',
  clouds:    _WINDY_BASE + '&overlay=clouds',
  lightning: 'https://map.blitzortung.org/#7/56.0/-4.5',
};
function setWeatherView(name, btn) {
  document.querySelectorAll('.wx-btn').forEach(b => {
    b.style.color = ''; b.style.borderColor = '';
  });
  if (btn) { btn.style.color = '#f97316'; btn.style.borderColor = '#f97316'; }
  document.getElementById('weather-frame').src = _WX_URLS[name] || _WX_URLS.radar;
}

// ── tides ──────────────────────────────────────────────────────────────────────
function loadTides(force) {
  const content = document.getElementById('tide-content');
  const meta    = document.getElementById('tide-meta');
  const errEl   = document.getElementById('tide-error');
  content.innerHTML = '<div style="color:var(--muted);font-size:.82rem">Loading…</div>';
  errEl.style.display = 'none';
  fetch('/api/tides' + (force ? '?refresh=true' : ''))
    .then(r => r.json())
    .then(data => {
      if (data.error) {
        errEl.textContent = data.error;
        errEl.style.display = 'block';
        content.innerHTML = '';
        return;
      }
      const fetched = data.fetched ? new Date(data.fetched*1000).toLocaleString('en-GB') : '—';
      const staleNote = data.stale ? ' (stale — API unreachable)' : '';
      meta.textContent = 'Last updated: ' + fetched + staleNote + (data.cached ? ' · cached' : ' · fresh');

      const extremes = data.extremes || [];
      if (!extremes.length) {
        content.innerHTML = '<div style="color:var(--muted)">No tide data available.</div>';
        return;
      }

      // find max height for bar scaling
      const maxH = Math.max(...extremes.map(e => e.height));

      let lastDate = '';
      let html = '<table class="tide-table"><thead><tr>'
        + '<th>Date</th><th>Time</th><th>Type</th><th>Height (m)</th><th style="width:120px"></th>'
        + '</tr></thead><tbody>';

      extremes.forEach(ex => {
        const dt   = new Date(ex.dt * 1000);
        const date = dt.toLocaleDateString('en-GB', {weekday:'short', day:'2-digit', month:'short'});
        const time = dt.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
        const isHigh = ex.type === 'High';
        const cls  = isHigh ? 'tide-high' : 'tide-low';
        const barW = Math.round((ex.height / maxH) * 100);
        const barC = isHigh ? '#3b82f6' : '#555';
        const dateCell = date !== lastDate ? date : '';
        lastDate = date;
        html += `<tr>
          <td style="color:var(--muted);font-size:.78rem">${dateCell}</td>
          <td style="font-variant-numeric:tabular-nums">${time}</td>
          <td class="${cls}">${ex.type}</td>
          <td class="${cls}">${ex.height.toFixed(2)}</td>
          <td><div class="tide-bar-wrap"><div class="tide-bar" style="width:${barW}%;background:${barC}"></div></div></td>
        </tr>`;
      });
      html += '</tbody></table>';
      content.innerHTML = html;
    })
    .catch(e => {
      errEl.textContent = 'Failed to load tides: ' + e;
      errEl.style.display = 'block';
      content.innerHTML = '';
    });
}

// ── APRS ──────────────────────────────────────────────────────────────────────
let aprsMap = null, aprsMarkers = {}, aprsStations = {};
const APRS_DEFAULT_WATCHED = [CFG.aprsCall].filter(c => c && c !== 'N0CALL');

function aprsGetWatched() {
  try { return JSON.parse(localStorage.getItem('aprs_watched') || 'null') || APRS_DEFAULT_WATCHED; }
  catch(e) { return APRS_DEFAULT_WATCHED; }
}
function aprsSetWatched(list) {
  localStorage.setItem('aprs_watched', JSON.stringify(list));
}

function initAprsMap() {
  if (aprsMap) return;
  const dark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    {attribution:'&copy; OpenStreetMap &copy; CARTO',subdomains:'abcd',maxZoom:19});
  const street = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {attribution:'© OpenStreetMap contributors',maxZoom:19});
  const satellite = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {attribution:'© Esri, Maxar, GeoEye',maxZoom:19});
  const topo = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    {attribution:'© OpenTopoMap contributors',maxZoom:17});
  aprsMap = L.map('aprs-map', {layers:[dark], center:[56.5,-4.5], zoom:6});
  L.control.layers({'Dark':dark,'Street':street,'Satellite':satellite,'Topo':topo},{},{position:'topright'}).addTo(aprsMap);
  aprsLiveLayer = L.layerGroup().addTo(aprsMap);
  Object.values(aprsLiveStations).forEach(updateAprsLiveMarker);
}

// ── APRS-IS live stations ──────────────────────────────────────────────────────
let aprsLiveStations = {}, aprsLiveMarkers = {}, aprsLiveLayer = null;

function _aprsLiveColor(st) {
  if (st.lora) return '#22c55e';                       // LoRa APRS
  const sym = st.symbol || '';
  if (sym === '_') return '#3b82f6';                   // weather station
  if ('#&aI'.includes(sym)) return '#f97316';          // digi / iGate
  if (">jku[bs<URXY^O".includes(sym)) return '#eab308';   // mobiles/aircraft
  return '#a78bfa';
}

function updateAprsLiveMarker(st) {
  if (!aprsLiveLayer || st.lat == null) return;
  const c = _aprsLiveColor(st);
  const heard = st.last_ts ? new Date(st.last_ts*1000).toLocaleTimeString('en-GB') : '?';
  const bits = [`<b>${st.call}</b>${st.lora ? ' <span style="color:#22c55e">LoRa</span>' : ''}`];
  if (st.comment) bits.push(st.comment);
  if (st.speed != null && st.speed > 0) bits.push(`${st.speed.toFixed(0)} km/h${st.course != null ? ' · ' + st.course + '°' : ''}`);
  if (st.alt != null) bits.push(`${Math.round(st.alt)} m`);
  bits.push(`<span style="color:#8b949e">heard ${heard}</span>`);
  const popup = bits.join('<br>');
  if (aprsLiveMarkers[st.call]) {
    aprsLiveMarkers[st.call].setLatLng([st.lat, st.lon]).setStyle({color: c, fillColor: c});
    aprsLiveMarkers[st.call].setPopupContent(popup);
  } else {
    const mk = L.circleMarker([st.lat, st.lon],
      {radius: 5, color: c, weight: 1.5, fillColor: c, fillOpacity: .5}).addTo(aprsLiveLayer);
    mk.bindTooltip(st.call, {direction: 'right', offset: [7, 0], className: 'ship-lbl'});
    mk.bindPopup(popup);
    aprsLiveMarkers[st.call] = mk;
  }
}

function removeAprsLiveMarker(call) {
  delete aprsLiveStations[call];
  if (aprsLiveMarkers[call]) {
    aprsLiveLayer && aprsLiveLayer.removeLayer(aprsLiveMarkers[call]);
    delete aprsLiveMarkers[call];
  }
  updateAprsLiveCount();
}

function updateAprsLiveCount() {
  const n = Object.keys(aprsLiveStations).length;
  const lora = Object.values(aprsLiveStations).filter(s=>s.lora).length;
  const el = document.getElementById('aprs-live-count');
  if (el) el.textContent = n ? (n + ' stations' + (lora ? ' · ' + lora + ' LoRa' : '')) : '—';
}

function loadAprsLiveStations() {
  fetch('/api/aprs/stations').then(r=>r.json()).then(d => {
    document.getElementById('aprs-live-dot').className =
      'dot ' + (d.status === 'connected' ? 'running' : 'stopped');
    (d.stations||[]).forEach(st => { aprsLiveStations[st.call] = st; });
    Object.values(aprsLiveStations).forEach(updateAprsLiveMarker);
    updateAprsLiveCount();
  }).catch(()=>{});
}

function aprsIcon(call) {
  const c = '#a78bfa';
  return L.divIcon({
    className:'', iconSize:[22,22], iconAnchor:[11,11],
    html:`<svg width="22" height="22" viewBox="0 0 22 22" xmlns="http://www.w3.org/2000/svg">
      <circle cx="11" cy="11" r="8" fill="${c}" fill-opacity=".25" stroke="${c}" stroke-width="1.5"/>
      <circle cx="11" cy="11" r="3.5" fill="${c}"/>
    </svg>`
  });
}

function updateAprsMarker(s) {
  if (s.lat == null || s.lng == null) return;
  const lat = parseFloat(s.lat), lng = parseFloat(s.lng);
  if (isNaN(lat) || isNaN(lng)) return;
  const icon = aprsIcon(s.name);
  if (aprsMarkers[s.name]) {
    aprsMarkers[s.name].setLatLng([lat,lng]).setIcon(icon);
  } else {
    const mk = L.marker([lat,lng],{icon}).addTo(aprsMap);
    mk.bindPopup(`<b>${s.name}</b><br>${s.comment||''}`);
    mk.on('click', () => mk.openPopup());
    aprsMarkers[s.name] = mk;
  }
}

function renderAprsTable() {
  const tbody = document.getElementById('aprs-tbody');
  tbody.innerHTML = '';
  const rows = Object.values(aprsStations).sort((a,b) => (b.lasttime||0)-(a.lasttime||0));
  document.getElementById('aprs-table-count').textContent = rows.length + ' shown';
  rows.forEach(s => {
    const tr = document.createElement('tr');
    const ago = s.lasttime ? _aprsAgo(s.lasttime) : '—';
    const lat  = s.lat  ? parseFloat(s.lat).toFixed(4)  : '—';
    const lng  = s.lng  ? parseFloat(s.lng).toFixed(4)  : '—';
    const spd  = s.speed   != null ? s.speed + ' kn'   : '—';
    const crs  = s.course  != null ? s.course + '°'    : '—';
    tr.innerHTML = `<td class="aprs-call">${s.name}</td>
      <td style="color:var(--muted);font-size:.74rem">${ago}</td>
      <td style="font-size:.74rem">${lat}</td>
      <td style="font-size:.74rem">${lng}</td>
      <td style="font-size:.74rem">${spd}</td>
      <td style="font-size:.74rem">${crs}</td>
      <td style="font-size:.74rem;color:var(--muted);max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${s.comment||''}</td>`;
    tr.onclick = () => {
      if (s.lat && s.lng) aprsMap.setView([parseFloat(s.lat),parseFloat(s.lng)], 11);
      if (aprsMarkers[s.name]) aprsMarkers[s.name].openPopup();
    };
    tbody.appendChild(tr);
  });
}

function _aprsAgo(ts) {
  const diff = Math.floor(Date.now()/1000) - parseInt(ts);
  if (diff < 60)   return diff + 's ago';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}

function renderAprsChips() {
  const watched = aprsGetWatched();
  const el = document.getElementById('aprs-chips');
  el.innerHTML = '';
  watched.forEach(call => {
    const chip = document.createElement('span');
    chip.className = 'aprs-chip';
    chip.title = 'Click to zoom · × to remove';
    const label = document.createTextNode(call);
    const x = document.createElement('span');
    x.className = 'chip-x'; x.textContent = '×';
    x.onclick = ev => { ev.stopPropagation(); aprsRemoveWatched(call); };
    chip.appendChild(label); chip.appendChild(x);
    chip.onclick = e => { if (!e.target.classList.contains('chip-x')) searchAprsCall(call); };
    el.appendChild(chip);
  });
}

function aprsRemoveWatched(call) {
  const list = aprsGetWatched().filter(c => c !== call);
  aprsSetWatched(list);
  delete aprsStations[call];
  if (aprsMarkers[call]) { aprsMap.removeLayer(aprsMarkers[call]); delete aprsMarkers[call]; }
  renderAprsChips();
  renderAprsTable();
}

function aprsAddWatched(call) {
  const list = aprsGetWatched();
  if (!list.includes(call)) { list.push(call); aprsSetWatched(list); }
  renderAprsChips();
}

function _aprsFetch(calls, onDone) {
  fetch('/api/aprs/lookup?calls=' + encodeURIComponent(calls))
    .then(r=>r.json())
    .then(data => {
      (data.entries||[]).forEach(s => {
        aprsStations[s.name] = s;
        updateAprsMarker(s);
      });
      const count = Object.keys(aprsStations).length;
      document.getElementById('aprs-count').textContent = count + ' station' + (count!==1?'s':'');
      renderAprsTable();
      if (onDone) onDone(data);
    })
    .catch(() => {});
}

function loadAprsWatched() {
  const watched = aprsGetWatched();
  renderAprsChips();
  if (watched.length) _aprsFetch(watched.join(','));
  else { document.getElementById('aprs-count').textContent = '0 stations'; renderAprsTable(); }
}

function searchAprs() {
  const raw = (document.getElementById('aprs-search').value||'').trim().toUpperCase();
  if (!raw) return;
  _aprsFetch(raw, data => {
    if ((data.entries||[]).length) {
      raw.split(',').forEach(c => { const t = c.trim(); if(t) aprsAddWatched(t); });
      document.getElementById('aprs-search').value = '';
    } else {
      document.getElementById('aprs-count').textContent = 'Not found: ' + raw;
    }
  });
}

function searchAprsCall(call) {
  aprsMap.setView(aprsStations[call] ?
    [parseFloat(aprsStations[call].lat), parseFloat(aprsStations[call].lng)] : [56.5,-4.5], 11);
}

// ── intel ─────────────────────────────────────────────────────────────────────
let _intelRows = [];
const _LABEL_COLORS = {
  'Military':'#ef4444', 'HM Coastguard SAR':'#f97316',
  'SAR':'#f97316', 'Police':'#3b82f6',
};

function loadIntel() {
  fetch('/api/adsb/intel').then(r=>r.json()).then(rows => {
    _intelRows = rows;
    const el = document.getElementById('intel-count');
    if (el) el.textContent = `${rows.length} aircraft tracked`;
    renderIntelTable();
  });
}

function renderIntelTable() {
  const q   = (document.getElementById('intel-search')?.value||'').toLowerCase();
  const lbl = document.getElementById('intel-label-filter')?.value||'';
  const tbody = document.getElementById('intel-body');
  if (!tbody) return;
  const rows = _intelRows.filter(r => {
    if (lbl && r.label !== lbl) return false;
    if (q && !`${r.icao_hex} ${r.registration} ${r.icao_type} ${r.type_desc} ${r.owner} ${r.label}`.toLowerCase().includes(q)) return false;
    return true;
  });
  tbody.innerHTML = '';
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:1.5rem">No records match</td></tr>';
    return;
  }
  rows.forEach(r => {
    const lc   = _LABEL_COLORS[r.label] || '#8b949e';
    const fseen = r.first_seen ? new Date(r.first_seen*1000).toLocaleString('en-GB',{dateStyle:'short',timeStyle:'short'}) : '—';
    const lseen = r.last_seen  ? new Date(r.last_seen*1000).toLocaleString('en-GB',{dateStyle:'short',timeStyle:'short'}) : '—';
    const tr = document.createElement('tr');
    tr.style.cursor = 'pointer';
    tr.title = 'Click for details & photo';
    tr.onclick = () => {
      // live data if the aircraft is currently tracked, else synthesise from intel row
      const live = adsbAircraft[r.icao_hex];
      openAircraftPanel(live || {
        hex: r.icao_hex, registration: r.registration, icao_type: r.icao_type,
        label: r.label, last_ts: r.last_seen
      });
    };
    tr.innerHTML =
      `<td style="font-family:monospace;font-size:.78rem">${r.icao_hex||'—'}</td>`+
      `<td style="font-weight:600">${r.registration||'—'}</td>`+
      `<td style="font-family:monospace;font-size:.78rem;color:var(--muted)">${r.icao_type||'—'}</td>`+
      `<td style="font-size:.8rem">${r.type_desc||'—'}</td>`+
      `<td style="font-size:.78rem;color:var(--muted)">${r.owner||'—'}</td>`+
      `<td><span class="badge" style="background:${lc}22;color:${lc}">${r.label||'—'}</span></td>`+
      `<td style="text-align:center;font-weight:600;color:${r.alert_count>1?'#f97316':'var(--text)'}">${r.alert_count||0}</td>`+
      `<td style="font-size:.75rem;color:var(--muted);white-space:nowrap">${fseen}</td>`+
      `<td style="font-size:.75rem;color:var(--muted);white-space:nowrap">${lseen}</td>`;
    tbody.appendChild(tr);
  });
}

// ── vessel intelligence ────────────────────────────────────────────────────────
let _vesIntelRows = [];
function _shipTypeName(t) {
  if (t == null) return '—';
  if (t === 30) return 'Fishing';
  if (t === 31 || t === 32) return 'Towing';
  if (t === 35) return 'Military';
  if (t === 36) return 'Sailing';
  if (t === 37) return 'Pleasure craft';
  if (t >= 40 && t <= 49) return 'High-speed craft';
  if (t === 50) return 'Pilot';
  if (t === 51) return 'SAR';
  if (t === 52) return 'Tug';
  if (t === 53) return 'Port tender';
  if (t === 55) return 'Law enforcement';
  if (t === 58) return 'Medical';
  if (t >= 60 && t <= 69) return 'Passenger';
  if (t >= 70 && t <= 79) return 'Cargo';
  if (t >= 80 && t <= 89) return 'Tanker';
  return 'Type ' + t;
}
function loadVesselIntel() {
  fetch('/api/ais/intel').then(r=>r.json()).then(rows => {
    _vesIntelRows = rows;
    const el = document.getElementById('intel-ves-count');
    if (el) el.textContent = `${rows.length} vessels tracked`;
    renderVesIntelTable();
  }).catch(()=>{});
}
function renderVesIntelTable() {
  const q = (document.getElementById('intel-ves-search')?.value||'').toLowerCase();
  const tbody = document.getElementById('intel-ves-body');
  if (!tbody) return;
  const rows = _vesIntelRows.filter(r =>
    !q || `${r.mmsi} ${r.name} ${r.label} ${_shipTypeName(r.ship_type)}`.toLowerCase().includes(q));
  tbody.innerHTML = '';
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:1.5rem">No vessel intel yet — rows appear as AIS alerts fire</td></tr>';
    return;
  }
  rows.forEach(r => {
    const lc = (CFG.aisLabelColors && CFG.aisLabelColors[r.label]) || '#8b949e';
    const fseen = r.first_seen ? new Date(r.first_seen*1000).toLocaleString('en-GB',{dateStyle:'short',timeStyle:'short'}) : '—';
    const lseen = r.last_seen  ? new Date(r.last_seen*1000).toLocaleString('en-GB',{dateStyle:'short',timeStyle:'short'}) : '—';
    const tr = document.createElement('tr');
    tr.style.cursor = 'pointer';
    tr.title = 'Click for details & photo';
    tr.onclick = () => {
      const live = aisVessels[r.mmsi];
      openVesselPanel(live || {
        mmsi: r.mmsi, name: r.name, ship_type: r.ship_type,
        label: r.label, priority: r.priority, last_ts: r.last_seen
      });
    };
    tr.innerHTML =
      `<td style="font-family:monospace;font-size:.78rem">${r.mmsi}</td>`+
      `<td style="font-weight:600">${r.name||'—'}</td>`+
      `<td style="font-size:.8rem;color:var(--muted)">${_shipTypeName(r.ship_type)}</td>`+
      `<td><span class="badge" style="background:${lc}22;color:${lc}">${r.label||'—'}</span></td>`+
      `<td style="text-align:center;font-weight:600;color:${r.alert_count>1?'#f97316':'var(--text)'}">${r.alert_count||0}</td>`+
      `<td style="font-size:.75rem;color:var(--muted);white-space:nowrap">${fseen}</td>`+
      `<td style="font-size:.75rem;color:var(--muted);white-space:nowrap">${lseen}</td>`;
    tbody.appendChild(tr);
  });
}

// ── settings ──────────────────────────────────────────────────────────────────
function loadSettings() {
  fetch('/api/settings').then(r=>r.json()).then(s=>{
    document.getElementById('s-telegram').checked  = !!s.telegram_enabled;
    document.getElementById('s-gotify').checked    = !!s.gotify_enabled;
  });
  checkGotify();
}

function saveSettings() {
  const body = {
    telegram_enabled: document.getElementById('s-telegram').checked,
    gotify_enabled:   document.getElementById('s-gotify').checked,
  };
  fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body)})
    .then(()=>{
      const el=document.getElementById('settings-saved');
      el.style.display='inline';
      setTimeout(()=>el.style.display='none',3000);
    });
}

function checkGotify() {
  const el = document.getElementById('gotify-status');
  el.className='status-chip chip-unk'; el.textContent='Checking…';
  fetch('/api/check/gotify').then(r=>r.json()).then(d=>{
    if (d.ok) { el.className='status-chip chip-ok'; el.textContent='✓ Reachable'; }
    else {
      el.className='status-chip chip-err';
      el.textContent='✗ '+(d.error||'Unreachable');
    }
  }).catch(()=>{
    el.className='status-chip chip-err'; el.textContent='✗ Check failed';
  });
}

// ── toast ─────────────────────────────────────────────────────────────────────
function showToastSimple(title, body) {
  document.getElementById('toast-title').textContent = title;
  document.getElementById('toast-body').textContent  = body;
  const t = document.getElementById('toast');
  t.classList.remove('hide');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(()=>t.classList.add('hide'), 6000);
}
document.getElementById('toast').onclick = () =>
  document.getElementById('toast').classList.add('hide');

// ── AIS ───────────────────────────────────────────────────────────────────────
let aisMap = null;
let aisMarkers = {};   // mmsi → Leaflet marker
let aisVessels = {};   // mmsi → vessel data
let _aisRefresh = null;
let _activeTrackLine = null;
let _activeTrackMap  = null;
let _aisLabelMode  = 1;  // 0=off  1=auto(zoom≥11)  2=always
let _adsbLabelMode = 1;  // 0=off  1=auto(zoom≥9)   2=always
let _aisShowAdsb = false, _aisAdsbLayer = null, _aisAdsbMarkers = {};
let _adsbShowAis = false, _adsbAisLayer = null, _adsbAisMarkers = {};

// Ship type colors (AIS-catcher inspired palette)
const SHIP_COLORS = {
  cargo:     '#22c55e',
  tanker:    '#ef4444',
  passenger: '#3b82f6',
  fishing:   '#f59e0b',
  special:   '#f97316',  // SAR, tug, pilot, dredger
  highspeed: '#a855f7',
  classb:    '#06b6d4',  // sailing/small craft
  other:     '#94a3b8',
};

const SHIP_TYPE_LABELS = [
  ['cargo',    'Cargo',              '#22c55e'],
  ['tanker',   'Tanker',             '#ef4444'],
  ['passenger','Passenger',          '#3b82f6'],
  ['fishing',  'Fishing',            '#f59e0b'],
  ['special',  'SAR / Tug / Special','#f97316'],
  ['highspeed','High Speed',         '#a855f7'],
  ['classb',   'Class B / Sailing',  '#06b6d4'],
  ['other',    'Other / Unknown',    '#94a3b8'],
];

function shipTypeColor(v) {
  if (v.label && CFG.aisLabelColors && CFG.aisLabelColors[v.label])
    return CFG.aisLabelColors[v.label];
  const st = v.ship_type;
  if (st == null || st === 0)  return SHIP_COLORS.other;
  if (st >= 70 && st <= 79)    return SHIP_COLORS.cargo;
  if (st >= 60 && st <= 69)    return SHIP_COLORS.passenger;
  if (st >= 80 && st <= 89)    return SHIP_COLORS.tanker;
  if ((st >= 40 && st <= 49) || (st >= 20 && st <= 29)) return SHIP_COLORS.highspeed;
  if (st === 30)               return SHIP_COLORS.fishing;
  if (st === 36 || st === 37)  return SHIP_COLORS.classb;
  if (st >= 50 && st <= 59)    return SHIP_COLORS.special;
  return SHIP_COLORS.other;
}

// Legacy alias used by live-map vessel panel code
function aisVesselColor(v) { return shipTypeColor(v); }

function makeShipIcon(v, watched, community) {
  const color   = shipTypeColor(v);
  const moving  = v.speed != null && v.speed > 0.5;
  // Prefer true heading; fall back to COG; fall back to 0 (north-up)
  const hdg     = (v.heading != null && v.heading !== 511) ? v.heading : null;
  const rot     = moving ? (hdg ?? (v.cog ?? 0)) : (hdg ?? 0);
  const sz      = watched ? 26 : 18;
  const ageSecs = v.last_ts ? (Date.now()/1000 - v.last_ts) : 0;
  const ageOp   = ageSecs > 600 ? 0.4 : ageSecs > 300 ? 0.65 : 1.0;
  // Community vessels slightly more transparent unless no local data at all
  const baseOp  = community ? 0.8 : 1.0;
  const op      = (baseOp * ageOp).toFixed(2);
  const stroke  = watched ? 'white' : 'rgba(0,0,0,0.45)';
  const sw      = watched ? 2 : 1;
  // Ship polygon: bow at top (0,-10), stern at bottom, notched stern for realism
  // SVG-native rotate about the viewBox origin — CSS transform on an <svg>
  // root is unreliable across browsers (icons were stuck facing north)
  const svg =
    `<svg width="${sz}" height="${sz}" viewBox="-10 -13 20 26" xmlns="http://www.w3.org/2000/svg" ` +
    `style="opacity:${op};display:block;overflow:visible">` +
    `<g transform="rotate(${rot})">` +
    `<polygon points="0,-11 8,9 0,5 -8,9" fill="${color}" stroke="${stroke}" stroke-width="${sw}" stroke-linejoin="round"/>` +
    `</g>` +
    (watched ? `<circle cx="0" cy="0" r="12" fill="none" stroke="${color}" stroke-width="1.5" opacity="0.5"/>` : '') +
    `</svg>`;
  return L.divIcon({className:'', html:svg, iconSize:[sz,sz], iconAnchor:[sz/2,sz/2]});
}

// Icon signatures — regenerating a marker's SVG on every SSE update is the main
// cost with 700+ markers; only call setIcon() when the visual actually changes.
function _shipIconSig(v, watched, community) {
  const moving = v.speed != null && v.speed > 0.5;
  const hdg = (v.heading != null && v.heading !== 511) ? v.heading : null;
  const rot = moving ? (hdg ?? (v.cog ?? 0)) : (hdg ?? 0);
  const age = v.last_ts ? (Date.now()/1000 - v.last_ts) : 0;
  const ageB = age > 600 ? 2 : age > 300 ? 1 : 0;
  return shipTypeColor(v)+'|'+Math.round(rot/5)+'|'+(watched?1:0)+'|'+(community?1:0)+'|'+ageB;
}
function _acIconSig(a) {
  return (a.icao_type||'')+'|'+(a.category||'')+'|'+(a.label||'')+'|'
       + Math.round((a.track||0)/5)+'|'+(a.community?1:0);
}
function _setShipIcon(mk, v, watched, community) {
  const sig = _shipIconSig(v, watched, community);
  if (mk._iconSig !== sig) { mk._iconSig = sig; mk.setIcon(makeShipIcon(v, watched, community)); }
}
function _setAcIcon(mk, a) {
  const sig = _acIconSig(a);
  if (mk._iconSig !== sig) { mk._iconSig = sig; mk.setIcon(makeAircraftIcon(a)); }
}

function _nmFromGreenock(lat, lon) {
  const R = 3440.065;
  const lat1 = 55.9587 * Math.PI/180, lat2 = lat * Math.PI/180;
  const dLat = (lat - 55.9587) * Math.PI/180;
  const dLon = (lon - (-4.7656)) * Math.PI/180;
  const a = Math.sin(dLat/2)**2 + Math.cos(lat1)*Math.cos(lat2)*Math.sin(dLon/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

const _LABEL_MODES = [
  {c:'#6b7280', t:'Labels: off'},
  {c:'#f97316', t:'Labels: auto (zoom ≥ 11)'},
  {c:'#facc15', t:'Labels: always on'}
];
const _ADSB_LABEL_MODES = [
  {c:'#6b7280', t:'Labels: off'},
  {c:'#60a5fa', t:'Labels: auto (zoom ≥ 9)'},
  {c:'#facc15', t:'Labels: always on'}
];

function _labelShouldShow() {
  if (_aisLabelMode === 0) return false;
  if (_aisLabelMode === 2) return true;
  return aisMap && aisMap.getZoom() >= 11;
}
function _adsbLabelShouldShow() {
  if (_adsbLabelMode === 0) return false;
  if (_adsbLabelMode === 2) return true;
  return adsbMap && adsbMap.getZoom() >= 9;
}

function _applyLabelVisibility() {
  const show = _labelShouldShow();
  Object.values(aisMarkers).forEach(mk => {
    if (mk.getTooltip()) { show ? mk.openTooltip() : mk.closeTooltip(); }
  });
  Object.values(_aisAdsbMarkers).forEach(mk => {
    if (mk.getTooltip()) { show ? mk.openTooltip() : mk.closeTooltip(); }
  });
}
function _applyAdsbLabelVisibility() {
  const show = _adsbLabelShouldShow();
  Object.values(adsbMarkers).forEach(mk => {
    if (mk.getTooltip()) { show ? mk.openTooltip() : mk.closeTooltip(); }
  });
  Object.values(_adsbAisMarkers).forEach(mk => {
    if (mk.getTooltip()) { show ? mk.openTooltip() : mk.closeTooltip(); }
  });
}

function addShipLegend(targetMap) {
  const ctrl = L.control({position:'bottomleft'});
  ctrl.onAdd = function() {
    const wrap = L.DomUtil.create('div');
    const lid  = 'slb-' + Math.random().toString(36).slice(2,6);
    const rows = SHIP_TYPE_LABELS.map(([,label,color]) =>
      `<div style="display:flex;align-items:center;gap:7px;margin-bottom:3px">` +
      `<svg width="13" height="17" viewBox="-10 -13 20 26"><polygon points="0,-11 8,9 0,5 -8,9" fill="${color}" stroke="rgba(0,0,0,0.3)" stroke-width="1"/></svg>` +
      `<span>${label}</span></div>`
    ).join('');
    const commRow =
      `<div style="display:flex;align-items:center;gap:7px;margin-top:6px;padding-top:5px;border-top:1px solid #334">` +
      `<svg width="13" height="17" viewBox="-10 -13 20 26"><polygon points="0,-11 8,9 0,5 -8,9" fill="#94a3b8" stroke="rgba(0,0,0,0.3)" stroke-width="1" opacity="0.6"/></svg>` +
      `<span style="color:#8b949e">AISStream community</span></div>`;
    wrap.innerHTML =
      `<div id="${lid}" style="display:none;background:rgba(10,12,25,0.92);border:1px solid #334;border-radius:6px;padding:8px 10px;font-size:11px;color:#ccc;margin-bottom:4px;min-width:185px">` +
      `<div style="font-weight:700;color:#fff;margin-bottom:6px">Vessel Types</div>${rows}${commRow}</div>` +
      `<button onclick="var b=document.getElementById('${lid}');b.style.display=b.style.display==='none'?'block':'none'" ` +
      `style="background:rgba(10,12,25,0.85);border:1px solid #445;border-radius:4px;color:#bbb;font-size:11px;padding:3px 9px;cursor:pointer;display:block">⚑ Legend</button>`;
    L.DomEvent.disableClickPropagation(wrap);
    return wrap;
  };
  ctrl.addTo(targetMap);
}

function initAisMap() {
  if (aisMap) return;
  aisMap = L.map('ais-map', {zoomControl:true}).setView([CFG.mapLat, CFG.mapLon], CFG.mapZoom);
  const dark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
    attribution:'© OpenStreetMap © CartoDB', subdomains:'abcd', maxZoom:19
  });
  const street = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',{
    attribution:'© OpenStreetMap contributors', maxZoom:19
  });
  const satellite = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',{
    attribution:'© Esri, Maxar, GeoEye', maxZoom:19
  });
  const topo = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',{
    attribution:'© OpenTopoMap contributors', maxZoom:17
  });
  dark.addTo(aisMap);
  L.control.layers({'Dark':dark,'Street':street,'Satellite':satellite,'Topo':topo},{},{position:'topright'}).addTo(aisMap);
  addShipLegend(aisMap);

  // Labels toggle button (3-state: off → auto → always)
  const lblCtrl = L.control({position:'topright'});
  lblCtrl.onAdd = function() {
    const btn = L.DomUtil.create('button');
    btn.innerHTML = '🏷 Labels';
    btn.style.cssText = 'background:rgba(10,12,25,0.85);border:1px solid #445;border-radius:4px;' +
      'color:' + _LABEL_MODES[_aisLabelMode].c + ';font-size:11px;padding:4px 9px;cursor:pointer;display:block;margin-top:4px;white-space:nowrap';
    btn.title = _LABEL_MODES[_aisLabelMode].t;
    L.DomEvent.on(btn, 'click', () => {
      _aisLabelMode = (_aisLabelMode + 1) % 3;
      btn.style.color = _LABEL_MODES[_aisLabelMode].c;
      btn.title = _LABEL_MODES[_aisLabelMode].t;
      _applyLabelVisibility();
    });
    L.DomEvent.disableClickPropagation(btn);
    return btn;
  };
  lblCtrl.addTo(aisMap);
  aisMap.on('zoomend', _applyLabelVisibility);

  // Fullscreen button
  const aisFsCtrl = L.control({position:'topleft'});
  aisFsCtrl.onAdd = function() {
    const btn = L.DomUtil.create('button');
    btn.innerHTML = '⛶'; btn.title = 'Toggle fullscreen';
    btn.style.cssText = 'background:rgba(10,12,25,0.85);border:1px solid #445;border-radius:4px;color:#bbb;font-size:15px;padding:2px 8px;cursor:pointer;display:block';
    L.DomEvent.on(btn, 'click', () => {
      const el = document.getElementById('ais-map');
      if (!document.fullscreenElement) el.requestFullscreen().catch(()=>{});
      else document.exitFullscreen();
    });
    L.DomEvent.disableClickPropagation(btn);
    return btn;
  };
  aisFsCtrl.addTo(aisMap);
  document.getElementById('ais-map').addEventListener('fullscreenchange', () => { aisMap.invalidateSize(); _applyLabelVisibility(); });

  // ADS-B aircraft overlay toggle
  const adsbOvCtrl = L.control({position:'topleft'});
  adsbOvCtrl.onAdd = function() {
    const btn = L.DomUtil.create('button');
    btn.id = 'ais-adsb-overlay-btn'; btn.innerHTML = '✈ Aircraft'; btn.title = 'Overlay ADS-B aircraft';
    btn.style.cssText = 'background:rgba(10,12,25,0.85);border:1px solid #445;border-radius:4px;color:#6b7280;font-size:11px;padding:4px 9px;cursor:pointer;display:block;margin-top:4px;white-space:nowrap';
    L.DomEvent.on(btn, 'click', () => toggleAdsbOnAis(btn));
    L.DomEvent.disableClickPropagation(btn);
    return btn;
  };
  adsbOvCtrl.addTo(aisMap);
}

function updateAisMarker(v) {
  if (!aisMap || v.lat == null || v.lon == null) return;
  const watched = !!v.label || watchedMmsis.has(v.mmsi);
  const label   = v.name || '';

  if (aisMarkers[v.mmsi]) {
    const mk = aisMarkers[v.mmsi];
    mk.setLatLng([v.lat, v.lon]);
    _setShipIcon(mk, v, watched, !!v.community);
    mk._aisData = v;
    // Update or add tooltip if name is available
    if (label) {
      const tt = mk.getTooltip();
      if (!tt || tt.getContent() !== label) {
        mk.unbindTooltip();
        mk.bindTooltip(label, {permanent:true, direction:'right', offset:[10,0], className:'ship-lbl'});
        if (!_labelShouldShow()) mk.closeTooltip();
      }
    }
  } else {
    const mk = L.marker([v.lat, v.lon], {icon: makeShipIcon(v, watched, !!v.community)}).addTo(aisMap);
    mk._iconSig = _shipIconSig(v, watched, !!v.community);
    mk._aisData = v;
    mk.on('click', () => openVesselPanel(mk._aisData));
    if (label) {
      mk.bindTooltip(label, {permanent:true, direction:'right', offset:[10,0], className:'ship-lbl'});
      if (!_labelShouldShow()) mk.closeTooltip();
    }
    aisMarkers[v.mmsi] = mk;
  }
}

// ── Vessel info panel ─────────────────────────────────────────────────────────
function openVesselPanel(v) {
  _vpCurrentMmsi = v.mmsi;
  const color = (v.label && CFG.aisLabelColors[v.label]) || '#3b82f6';
  const ts = v.last_ts ? new Date(v.last_ts*1000).toLocaleString('en-GB') : '?';

  document.getElementById('vp-name').textContent = v.name || ('MMSI ' + v.mmsi);
  _renderWatchBtn(v.mmsi);
  const badge = document.getElementById('vp-badge');
  if (v.label) {
    badge.textContent = v.label;
    badge.style.cssText = `background:${color}22;color:${color};display:inline-block`;
  } else {
    badge.style.display = 'none';
  }

  const distNm = (v.lat != null && v.lon != null) ? _nmFromGreenock(v.lat, v.lon).toFixed(1) + ' nm' : '—';
  const rows = [
    ['MMSI',      v.mmsi],
    ['Channel',   v.channel || '?'],
    ['Position',  v.lat != null ? v.lat.toFixed(5) + ', ' + v.lon.toFixed(5) : '—'],
    ['Distance',  distNm],
    ['Last seen', ts],
  ];
  if (v.speed != null) rows.push(['Speed', v.speed.toFixed(1) + ' kn']);
  if (v.heading != null) rows.push(['Heading', v.heading + '°']);
  if (v.cog != null) rows.push(['Course', v.cog.toFixed(1) + '°']);
  if (v.callsign)    rows.push(['Callsign', v.callsign]);
  if (v.destination) rows.push(['Destination', v.destination]);
  if (v.eta)         rows.push(['ETA', v.eta]);
  if (v.draught)     rows.push(['Draught', v.draught.toFixed(1) + ' m']);
  if (v.imo)         rows.push(['IMO', v.imo]);
  document.getElementById('vp-rows').innerHTML = rows.map(([l,val]) =>
    `<div class="vp-row"><span class="vp-lbl">${l}</span><span class="vp-val">${val}</span></div>`
  ).join('');

  const vfUrl = `https://www.vesselfinder.com/?mmsi=${v.mmsi}`;
  document.getElementById('vp-link').href = vfUrl;

  // show panel immediately, then load photo
  const photo = document.getElementById('vessel-panel-photo');
  const placeholder = document.getElementById('vp-placeholder');
  photo.classList.remove('loaded');
  photo.src = '';
  placeholder.textContent = 'Loading photo…';
  placeholder.style.display = 'flex';

  document.getElementById('vessel-panel').classList.add('open');

  // fetch photo + local data
  fetch(`/api/ais/vessel-info/${v.mmsi}`)
    .then(r => r.json())
    .then(info => {
      if (info.photo_url) {
        photo.onload = () => {
          photo.classList.add('loaded');
          placeholder.style.display = 'none';
        };
        photo.onerror = () => {
          photo.classList.remove('loaded');
          placeholder.textContent = 'No photo available';
        };
        photo.src = info.photo_url;
      } else {
        placeholder.textContent = 'No photo available';
      }
      // enrich rows with local AIS-catcher data
      const loc = info.local || {};
      const extra = [];
      if (loc.speed != null && loc.speed > 0) extra.push(['Speed', loc.speed.toFixed(1) + ' kn']);
      if (loc.heading != null && loc.heading < 360) extra.push(['Heading', loc.heading + '°']);
      if (loc.cog != null && loc.cog < 360) extra.push(['Course', loc.cog.toFixed(1) + '°']);
      if (loc.shiptype) extra.push(['Ship type', loc.shiptype]);
      if (loc.callsign) extra.push(['Callsign', loc.callsign]);
      if (loc.destination && loc.destination.trim()) extra.push(['Destination', loc.destination]);
      if (loc.country) extra.push(['Flag', loc.country]);
      if (loc.imo) extra.push(['IMO', loc.imo]);
      if (loc.draught && loc.draught > 0) extra.push(['Draught', loc.draught.toFixed(1) + ' m']);
      if (extra.length) {
        document.getElementById('vp-rows').innerHTML += extra.map(([l,val]) =>
          `<div class="vp-row"><span class="vp-lbl">${l}</span><span class="vp-val">${val}</span></div>`
        ).join('');
      }
    })
    .catch(() => { placeholder.textContent = 'No photo available'; });

  // fetch and draw track
  _clearTrackLine();
  fetch(`/api/ais/track/${v.mmsi}`)
    .then(r => r.json())
    .then(pts => {
      if (pts.length < 2) return;
      const latlngs = pts.map(p => [p.lat, p.lon]);
      const targetMap = liveAisOn ? map : null;
      if (!targetMap) return;
      _activeTrackLine = L.polyline(latlngs, {
        color: color, weight: 2, opacity: 0.65,
        dashArray: '4 4'
      }).addTo(targetMap);
      _activeTrackMap = targetMap;
      // show track age in panel
      const oldest = new Date(pts[0].ts * 1000).toLocaleString('en-GB');
      document.getElementById('vp-rows').innerHTML +=
        `<div class="vp-row"><span class="vp-lbl">Track from</span><span class="vp-val" style="color:var(--muted);font-size:.75rem">${oldest} (${pts.length} pts)</span></div>`;
    })
    .catch(() => {});
}

function _clearTrackLine() {
  if (_activeTrackLine && _activeTrackMap) {
    _activeTrackMap.removeLayer(_activeTrackLine);
  }
  _activeTrackLine = null;
  _activeTrackMap  = null;
}

function closeVesselPanel() {
  document.getElementById('vessel-panel').classList.remove('open');
  _clearTrackLine();
  _vpCurrentMmsi = null;
}

let _aisSortCol = 'dist', _aisSortAsc = true;
function aisSort(col) {
  if (_aisSortCol === col) _aisSortAsc = !_aisSortAsc;
  else { _aisSortCol = col; _aisSortAsc = true; }
  renderAisTable();
}

const _AIS_TYPE_MAP = {
  cargo:     [70,71,72,73,74,75,76,77,78,79],
  tanker:    [80,81,82,83,84,85,86,87,88,89],
  passenger: [60,61,62,63,64,65,66,67,68,69],
  fishing:   [30],
  pleasure:  [36,37],
  tug:       [21,22,31,32,52],
  sar:       [35,51,55,57,58],
};

function _shipTypeCategory(stype) {
  if (stype == null) return 'other';
  for (const [cat, types] of Object.entries(_AIS_TYPE_MAP))
    if (types.includes(stype)) return cat;
  return 'other';
}

function renderAisTable() {
  const search      = (document.getElementById('ais-search').value||'').toLowerCase();
  const watchedOnly = document.getElementById('ais-watched-only').checked;
  const typeFilter  = document.getElementById('ais-type-filter').value;
  const tbody = document.getElementById('ais-tbody');
  tbody.innerHTML = '';

  const all = Object.values(aisVessels).filter(v => {
    if (watchedOnly && !v.label && !watchedMmsis.has(v.mmsi)) return false;
    if (search && !String(v.name||'').toLowerCase().includes(search) &&
        !String(v.mmsi).includes(search)) return false;
    if (typeFilter && _shipTypeCategory(v.ship_type) !== typeFilter) return false;
    return true;
  }).sort((a,b) => {
    const aw = a.label || watchedMmsis.has(a.mmsi);
    const bw = b.label || watchedMmsis.has(b.mmsi);
    if (aw && !bw) return -1;
    if (!aw && bw) return 1;
    const dir = _aisSortAsc ? 1 : -1;
    if (_aisSortCol === 'dist') {
      const da = (a.lat != null) ? _nmFromGreenock(a.lat, a.lon) : 9999;
      const db = (b.lat != null) ? _nmFromGreenock(b.lat, b.lon) : 9999;
      return (da - db) * dir;
    }
    if (_aisSortCol === 'name') return (a.name||'').localeCompare(b.name||'') * dir;
    return ((b.last_ts||0) - (a.last_ts||0)) * dir;
  });

  document.getElementById('ais-table-count').textContent = all.length + ' shown';
  updateAisCount();

  all.forEach(v => {
    const tr  = document.createElement('tr');
    const ts  = v.last_ts ? new Date(v.last_ts*1000).toLocaleTimeString('en-GB') : '?';
    const dist = (v.lat != null && v.lon != null) ? _nmFromGreenock(v.lat,v.lon).toFixed(1)+'nm' : '—';
    const color = aisVesselColor(v);
    const nameHtml = v.label
      ? `<div class="vessel-name-cell">
           <span style="color:${color};font-weight:600">${v.name||'—'}</span>
           <span class="vessel-alert-badge" style="background:${color}22;color:${color}">${v.label}</span>
         </div>`
      : `<span style="color:var(--text)">${v.name||'—'}</span>`;
    tr.innerHTML = `
      <td>${nameHtml}</td>
      <td style="color:var(--muted);font-size:.72rem;font-family:monospace">${v.mmsi}</td>
      <td style="color:var(--muted);font-size:.75rem">${v.channel||'?'}</td>
      <td style="color:var(--muted);font-size:.72rem">${dist}</td>
      <td style="color:var(--muted);font-size:.72rem">${ts}</td>`;
    tr.onclick = () => openVesselPanel(v);
    tbody.appendChild(tr);
  });
}

function updateAisCount() {
  const all  = Object.values(aisVessels);
  const comm = all.filter(v => v.community).length;
  let txt = all.length + ' vessels';
  if (comm) txt += ' (' + comm + ' community)';
  document.getElementById('ais-count').textContent = txt;
}

function updateAisStatus(connected, count) {
  const dot = document.getElementById('ais-dot');
  const txt = document.getElementById('ais-status-text');
  if (connected) {
    dot.className = 'dot running';
    txt.textContent = 'AIS-catcher connected';
  } else {
    dot.className = 'dot stopped';
    txt.textContent = 'AIS-catcher disconnected';
  }
  if (count != null)
    updateAisCount();
}

function loadAisVessels() {
  fetch('/api/ais/vessels').then(r=>r.json()).then(vessels => {
    vessels.forEach(v => { aisVessels[v.mmsi] = v; });
    Object.values(aisVessels).forEach(updateAisMarker);
    renderAisTable();
    updateAisCount();
  }).catch(()=>{});
  // auto-refresh every 20s while tab is active
  clearTimeout(_aisRefresh);
  _aisRefresh = setTimeout(() => {
    if (document.getElementById('tab-ais').classList.contains('active'))
      loadAisVessels();
  }, 20000);
}

function showToastAis(v) {
  const color = aisVesselColor(v);
  document.getElementById('toast-title').textContent = `🚢 ${v.name||v.mmsi} — ${v.label}`;
  document.getElementById('toast-body').textContent  =
    `MMSI: ${v.mmsi}` + (v.lat!=null ? `  ·  ${v.lat.toFixed(4)}, ${v.lon.toFixed(4)}` : '');
  document.getElementById('toast').style.borderLeftColor = color;
  document.getElementById('toast').style.borderColor = color;
  const t = document.getElementById('toast');
  t.classList.remove('hide');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(()=>{
    t.classList.add('hide');
    t.style.borderLeftColor='';
    t.style.borderColor='';
  }, 9000);
}

// ── ADS-B ─────────────────────────────────────────────────────────────────────
let adsbMap = null;
let adsbMarkers = {};
let adsbAircraft = {};
let _adsbRefresh = null;
let _adsbSortCol = 'dist', _adsbSortAsc = true;
let _acCurrentHex = null;
let _adsbTrackLine = null;
let _adsbTrackMap  = null;

function _nmFromGreenockAdv(lat, lon) {
  return _nmFromGreenock(lat, lon);
}

function adsbVesselColor(a) {
  if (a.label && ADSB_LABEL_COLORS[a.label]) return ADSB_LABEL_COLORS[a.label];
  if (a.is_military) return '#ef4444';
  if (a.is_helicopter) return '#f97316';
  return '#94a3b8';
}

function makeAircraftIcon(a) {
  const color    = adsbVesselColor(a);
  const hasAlert = !!a.label;
  const track    = a.track != null ? a.track : 0;
  const ageSecs  = a.last_ts ? (Date.now()/1000 - a.last_ts) : 0;
  const op       = ageSecs > 30 ? 0.55 : 1.0;
  const glow     = hasAlert ? `filter:drop-shadow(0 0 5px ${color});` : '';
  const pulse    = hasAlert ? 'animation:pulse 1.2s infinite;' : '';
  const sz       = hasAlert ? 34 : 26;  // px, square container

  if (typeof shapes !== 'undefined' && typeof getBaseMarker !== 'undefined') {
    try {
      const [shapeName] = getBaseMarker(a.category||'', a.icao_type||'', null, null, null, null, true);
      const shape = shapes[shapeName];
      if (shape && shape.path) {
        const vbParts = shape.viewBox.split(/[\s,]+/);
        const vx = parseFloat(vbParts[0]), vy = parseFloat(vbParts[1]);
        const vw = parseFloat(vbParts[2]), vh = parseFloat(vbParts[3]);
        const cx = (vx + vw/2).toFixed(2), cy = (vy + vh/2).toFixed(2);
        const maxVB = Math.max(vw, vh);
        // stroke-width in SVG units to produce ~1.5px visible outline at sz pixels
        const sw = (3 * maxVB / sz).toFixed(2);
        const rotAttr = shape.noRotate ? '' : ` transform="rotate(${track},${cx},${cy})"`;

        const paths = Array.isArray(shape.path) ? shape.path : [shape.path];
        let inner = paths.map(p =>
          `<path d="${p}" fill="${color}" stroke="#111827" stroke-width="${sw}" paint-order="stroke fill"/>`
        ).join('');
        if (shape.accent) {
          const accs = Array.isArray(shape.accent) ? shape.accent : [shape.accent];
          const aw = (parseFloat(sw) * (shape.accentMult||0.6)).toFixed(2);
          inner += accs.map(p => `<path d="${p}" fill="none" stroke="#111827" stroke-width="${aw}"/>`).join('');
        }

        const svgStr = `<svg width="${sz}" height="${sz}" viewBox="${shape.viewBox}" `
          + `preserveAspectRatio="xMidYMid meet" xmlns="http://www.w3.org/2000/svg">`
          + `<g${rotAttr}>${inner}</g></svg>`;
        const html = `<div style="width:${sz}px;height:${sz}px;opacity:${op};${glow}${pulse}">${svgStr}</div>`;
        return L.divIcon({className:'', html, iconSize:[sz,sz], iconAnchor:[sz/2,sz/2]});
      }
    } catch(e) { console.warn('aircraft icon:', e); }
  }

  // Fallback: simple arrow
  const fb = `<svg width="${sz}" height="${sz}" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg">
    <g transform="rotate(${track},10,10)">
      <polygon points="10,1 14,14 10,11 6,14" fill="${color}" stroke="#111" stroke-width=".5"/>
      <line x1="5" y1="9" x2="15" y2="9" stroke="${color}" stroke-width="1.2"/>
    </g></svg>`;
  return L.divIcon({className:'', html:`<div style="opacity:${op};${glow}${pulse}">${fb}</div>`,
    iconSize:[sz,sz], iconAnchor:[sz/2,sz/2]});
}

function initAdsbMap() {
  if (adsbMap) return;
  adsbMap = L.map('adsb-map',{zoomControl:true}).setView([CFG.mapLat,CFG.mapLon],CFG.mapZoom);
  const dark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
    {attribution:'© OpenStreetMap © CartoDB',subdomains:'abcd',maxZoom:19});
  const street = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    {attribution:'© OpenStreetMap contributors',maxZoom:19});
  const satellite = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {attribution:'© Esri, Maxar, GeoEye',maxZoom:19});
  const topo = L.tileLayer('https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    {attribution:'© OpenTopoMap contributors',maxZoom:17});
  dark.addTo(adsbMap);
  L.control.layers({'Dark':dark,'Street':street,'Satellite':satellite,'Topo':topo},{},{position:'topright'}).addTo(adsbMap);

  // Fullscreen button
  const adsbFsCtrl = L.control({position:'topleft'});
  adsbFsCtrl.onAdd = function() {
    const btn = L.DomUtil.create('button');
    btn.innerHTML = '⛶'; btn.title = 'Toggle fullscreen';
    btn.style.cssText = 'background:rgba(10,12,25,0.85);border:1px solid #445;border-radius:4px;color:#bbb;font-size:15px;padding:2px 8px;cursor:pointer;display:block';
    L.DomEvent.on(btn, 'click', () => {
      const el = document.getElementById('adsb-map');
      if (!document.fullscreenElement) el.requestFullscreen().catch(()=>{});
      else document.exitFullscreen();
    });
    L.DomEvent.disableClickPropagation(btn);
    return btn;
  };
  adsbFsCtrl.addTo(adsbMap);
  document.getElementById('adsb-map').addEventListener('fullscreenchange', () => { adsbMap.invalidateSize(); _applyAdsbLabelVisibility(); });

  // Aircraft labels toggle (3-state: off → auto → always)
  const adsbLblCtrl = L.control({position:'topright'});
  adsbLblCtrl.onAdd = function() {
    const btn = L.DomUtil.create('button');
    btn.innerHTML = '🏷 Labels';
    btn.style.cssText = 'background:rgba(10,12,25,0.85);border:1px solid #445;border-radius:4px;' +
      'color:' + _ADSB_LABEL_MODES[_adsbLabelMode].c + ';font-size:11px;padding:4px 9px;cursor:pointer;display:block;margin-top:4px;white-space:nowrap';
    btn.title = _ADSB_LABEL_MODES[_adsbLabelMode].t;
    L.DomEvent.on(btn, 'click', () => {
      _adsbLabelMode = (_adsbLabelMode + 1) % 3;
      btn.style.color = _ADSB_LABEL_MODES[_adsbLabelMode].c;
      btn.title = _ADSB_LABEL_MODES[_adsbLabelMode].t;
      _applyAdsbLabelVisibility();
    });
    L.DomEvent.disableClickPropagation(btn);
    return btn;
  };
  adsbLblCtrl.addTo(adsbMap);
  adsbMap.on('zoomend', _applyAdsbLabelVisibility);

  // AIS ships overlay toggle
  const aisOvCtrl = L.control({position:'topleft'});
  aisOvCtrl.onAdd = function() {
    const btn = L.DomUtil.create('button');
    btn.id = 'adsb-ais-overlay-btn'; btn.innerHTML = '⚓ Ships'; btn.title = 'Overlay AIS ships';
    btn.style.cssText = 'background:rgba(10,12,25,0.85);border:1px solid #445;border-radius:4px;color:#6b7280;font-size:11px;padding:4px 9px;cursor:pointer;display:block;margin-top:4px;white-space:nowrap';
    L.DomEvent.on(btn, 'click', () => toggleAisOnAdsb(btn));
    L.DomEvent.disableClickPropagation(btn);
    return btn;
  };
  aisOvCtrl.addTo(adsbMap);
}

function updateAdsbMarker(a) {
  if (!adsbMap || a.lat == null || a.lon == null) return;
  if (a.community && !_showNetAircraft) {
    if (adsbMarkers[a.hex]) { adsbMap.removeLayer(adsbMarkers[a.hex]); delete adsbMarkers[a.hex]; }
    return;
  }
  const lbl = (a.flight || a.registration || '').trim();
  if (adsbMarkers[a.hex]) {
    adsbMarkers[a.hex].setLatLng([a.lat,a.lon]);
    _setAcIcon(adsbMarkers[a.hex], a);
    adsbMarkers[a.hex]._acData = a;
    if (lbl) {
      const tt = adsbMarkers[a.hex].getTooltip();
      if (tt) tt.setContent(lbl);
      else {
        adsbMarkers[a.hex].bindTooltip(lbl, {permanent:true, direction:'right', offset:[13,0], className:'ship-lbl'});
        if (!_adsbLabelShouldShow()) adsbMarkers[a.hex].closeTooltip();
      }
    }
  } else {
    const mk = L.marker([a.lat,a.lon],{icon: makeAircraftIcon(a), opacity: a.community ? 0.55 : 1}).addTo(adsbMap);
    mk._iconSig = _acIconSig(a);
    mk._acData = a;
    mk.on('click', () => openAircraftPanel(mk._acData));
    if (lbl) {
      mk.bindTooltip(lbl, {permanent:true, direction:'right', offset:[13,0], className:'ship-lbl'});
      if (!_adsbLabelShouldShow()) mk.closeTooltip();
    }
    adsbMarkers[a.hex] = mk;
  }
}

// ── community/network aircraft toggle ─────────────────────────────────────────
let _showNetAircraft = true;
function toggleNetAircraft(btn) {
  _showNetAircraft = !_showNetAircraft;
  btn.textContent = _showNetAircraft ? '🌐 Network: ON' : '🌐 Network: OFF';
  btn.style.color = _showNetAircraft ? '#60a5fa' : '#6b7280';
  Object.values(adsbAircraft).forEach(updateAdsbMarker);
  renderAdsbTable();
}

// ── Cross-map overlays ────────────────────────────────────────────────────────
function toggleAdsbOnAis(btn) {
  _aisShowAdsb = !_aisShowAdsb;
  btn.style.color = _aisShowAdsb ? '#60a5fa' : '#6b7280';
  if (_aisShowAdsb) {
    if (!_aisAdsbLayer) _aisAdsbLayer = L.layerGroup().addTo(aisMap);
    Object.values(adsbAircraft).forEach(_putAdsbOnAis);
  } else {
    if (_aisAdsbLayer) _aisAdsbLayer.clearLayers();
    _aisAdsbMarkers = {};
  }
}
function _putAdsbOnAis(a) {
  if (!_aisShowAdsb || !_aisAdsbLayer || a.lat == null || a.lon == null) return;
  const lbl = (a.flight || a.registration || '').trim();
  if (_aisAdsbMarkers[a.hex]) {
    _aisAdsbMarkers[a.hex].setLatLng([a.lat, a.lon]);
    _setAcIcon(_aisAdsbMarkers[a.hex], a);
    _aisAdsbMarkers[a.hex]._acData = a;
    const tt = _aisAdsbMarkers[a.hex].getTooltip();
    if (lbl && tt && tt.getContent() !== lbl) tt.setContent(lbl);
  } else {
    const mk = L.marker([a.lat, a.lon], {icon: makeAircraftIcon(a)}).addTo(_aisAdsbLayer);
    mk._iconSig = _acIconSig(a);
    mk._acData = a;
    mk.on('click', () => openAircraftPanel(mk._acData));
    if (lbl) {
      mk.bindTooltip(lbl, {permanent:true, direction:'right', offset:[13,0], className:'ship-lbl'});
      if (!_labelShouldShow()) mk.closeTooltip();
    }
    _aisAdsbMarkers[a.hex] = mk;
  }
}
function _removeAdsbFromAis(hex) {
  if (_aisAdsbMarkers[hex]) {
    _aisAdsbLayer && _aisAdsbLayer.removeLayer(_aisAdsbMarkers[hex]);
    delete _aisAdsbMarkers[hex];
  }
}

function toggleAisOnAdsb(btn) {
  _adsbShowAis = !_adsbShowAis;
  btn.style.color = _adsbShowAis ? '#f97316' : '#6b7280';
  if (_adsbShowAis) {
    if (!_adsbAisLayer) _adsbAisLayer = L.layerGroup().addTo(adsbMap);
    Object.values(aisVessels).forEach(_putAisOnAdsb);
  } else {
    if (_adsbAisLayer) _adsbAisLayer.clearLayers();
    _adsbAisMarkers = {};
  }
}
function _putAisOnAdsb(v) {
  if (!_adsbShowAis || !_adsbAisLayer || v.lat == null || v.lon == null) return;
  const watched = !!v.label || watchedMmsis.has(v.mmsi);
  const lbl = v.name || '';
  if (_adsbAisMarkers[v.mmsi]) {
    _adsbAisMarkers[v.mmsi].setLatLng([v.lat, v.lon]);
    _setShipIcon(_adsbAisMarkers[v.mmsi], v, watched, !!v.community);
    _adsbAisMarkers[v.mmsi]._aisData = v;
    const tt = _adsbAisMarkers[v.mmsi].getTooltip();
    if (lbl && tt && tt.getContent() !== lbl) tt.setContent(lbl);
  } else {
    const mk = L.marker([v.lat, v.lon], {icon: makeShipIcon(v, watched, !!v.community)}).addTo(_adsbAisLayer);
    mk._iconSig = _shipIconSig(v, watched, !!v.community);
    mk._aisData = v;
    mk.on('click', () => openVesselPanel(mk._aisData));
    if (lbl) {
      mk.bindTooltip(lbl, {permanent:true, direction:'right', offset:[10,0], className:'ship-lbl'});
      if (!_adsbLabelShouldShow()) mk.closeTooltip();
    }
    _adsbAisMarkers[v.mmsi] = mk;
  }
}
function _removeAisFromAdsb(mmsi) {
  if (_adsbAisMarkers[mmsi]) {
    _adsbAisLayer && _adsbAisLayer.removeLayer(_adsbAisMarkers[mmsi]);
    delete _adsbAisMarkers[mmsi];
  }
}

function updateAdsbCount() {
  const all   = Object.values(adsbAircraft);
  const net   = all.filter(a=>a.community).length;
  const local = all.length - net;
  const alert = all.filter(a=>a.label).length;
  const heli  = all.filter(a=>a.is_helicopter).length;
  let txt = local + ' local';
  if (net)   txt += ' + ' + net + ' network';
  if (alert) txt += ' · ' + alert + ' alert';
  if (heli)  txt += ' · ' + heli + ' heli';
  document.getElementById('adsb-count').textContent = txt;
  document.getElementById('adsb-dot').className = 'dot ' + (local ? 'running' : 'stopped');
  document.getElementById('adsb-status-text').textContent = local ? 'Live' : 'No data';
}

function adsbSort(col) {
  if (_adsbSortCol === col) _adsbSortAsc = !_adsbSortAsc;
  else { _adsbSortCol = col; _adsbSortAsc = true; }
  renderAdsbTable();
}

function renderAdsbTable() {
  const search   = (document.getElementById('adsb-search').value||'').toLowerCase();
  const tfilt    = document.getElementById('adsb-type-filter').value;
  const alertOnly= document.getElementById('adsb-alert-only').checked;
  const tbody    = document.getElementById('adsb-tbody');
  tbody.innerHTML = '';

  const all = Object.values(adsbAircraft).filter(a => {
    if (a.community && !_showNetAircraft) return false;
    if (alertOnly && !a.label) return false;
    if (tfilt === 'heli'     && !a.is_helicopter) return false;
    if (tfilt === 'military' && !a.is_military)   return false;
    if (tfilt === 'alert'    && !a.label)          return false;
    if (search) {
      const hay = ((a.registration||'') + (a.flight||'') + (a.hex||'')).toLowerCase();
      if (!hay.includes(search)) return false;
    }
    return true;
  }).sort((a,b) => {
    const aw = !!a.label, bw = !!b.label;
    if (aw && !bw) return -1;
    if (!aw && bw) return 1;
    const dir = _adsbSortAsc ? 1 : -1;
    if (_adsbSortCol === 'dist') {
      const da = (a.lat!=null) ? _nmFromGreenock(a.lat,a.lon) : 9999;
      const db = (b.lat!=null) ? _nmFromGreenock(b.lat,b.lon) : 9999;
      return (da-db)*dir;
    }
    return ((b.last_ts||0)-(a.last_ts||0))*dir;
  });

  document.getElementById('adsb-table-count').textContent = all.length + ' shown';
  updateAdsbCount();

  all.forEach(a => {
    const tr    = document.createElement('tr');
    if (a.community) { tr.style.opacity = '0.6'; tr.title = 'via ' + (a.source||'network feed'); }
    const color = adsbVesselColor(a);
    const dist  = (a.lat!=null) ? _nmFromGreenock(a.lat,a.lon).toFixed(1)+'nm' : '—';
    const alt   = a.alt_baro != null ? Math.round(a.alt_baro/100)*100+'ft' : '—';
    const regHtml = a.label
      ? `<div><span style="color:${color};font-weight:600">${a.registration||a.hex}</span>
           <span class="vessel-alert-badge" style="background:${color}22;color:${color}">${a.label}</span></div>`
      : `<span style="color:var(--text)">${a.registration||a.hex}</span>`;
    tr.innerHTML = `
      <td>${regHtml}</td>
      <td style="color:var(--muted);font-size:.75rem">${a.flight||'—'}</td>
      <td style="color:var(--muted);font-size:.72rem">${a.icao_type||'—'}${a.is_helicopter?' 🚁':''}</td>
      <td style="color:var(--muted);font-size:.72rem">${dist}</td>
      <td style="color:var(--muted);font-size:.72rem">${alt}</td>`;
    tr.onclick = () => openAircraftPanel(a);
    tbody.appendChild(tr);
  });
}

function loadAdsbAircraft() {
  fetch('/api/adsb/aircraft').then(r=>r.json()).then(aircraft => {
    aircraft.forEach(a => { adsbAircraft[a.hex] = a; });
    Object.values(adsbAircraft).forEach(updateAdsbMarker);
    renderAdsbTable();
    updateAdsbCount();
  }).catch(()=>{});
  clearTimeout(_adsbRefresh);
  _adsbRefresh = setTimeout(() => {
    if (document.getElementById('tab-adsb').classList.contains('active'))
      loadAdsbAircraft();
  }, 15000);
}

function openAircraftPanel(a) {
  _acCurrentHex = a.hex;
  const color = adsbVesselColor(a);

  document.getElementById('ac-reg').textContent = a.registration || a.hex;
  const badge = document.getElementById('ac-badge');
  if (a.label) {
    badge.textContent = a.label;
    badge.style.cssText = `background:${color}22;color:${color};display:inline-block`;
  } else { badge.style.display='none'; }

  const dist = (a.lat!=null) ? _nmFromGreenock(a.lat,a.lon).toFixed(1)+' nm' : '—';
  const ts   = a.last_ts ? new Date(a.last_ts*1000).toLocaleString('en-GB') : '?';
  const rows = [
    ['ICAO Hex',  a.hex],
    ['Callsign',  a.flight  || '—'],
    ['Type',      a.icao_type || '—'],
    ['Category',  a.category || '—'],
    ['Squawk',    a.squawk  || '—'],
    ['Position',  a.lat!=null ? a.lat.toFixed(5)+', '+a.lon.toFixed(5) : '—'],
    ['Distance',  dist],
    ['Last seen', ts],
  ];
  if (a.alt_baro != null)  rows.push(['Altitude', a.alt_baro.toLocaleString() + ' ft']);
  if (a.gs != null)        rows.push(['Speed', a.gs.toFixed(0) + ' kn']);
  if (a.track != null)     rows.push(['Track', a.track.toFixed(0) + '°']);
  if (a.is_military)       rows.push(['Military', 'Yes']);
  if (a.is_helicopter)     rows.push(['Type', 'Helicopter']);

  document.getElementById('ac-rows').innerHTML = rows.map(([l,v])=>
    `<div class="vp-row"><span class="vp-lbl">${l}</span><span class="vp-val">${v}</span></div>`
  ).join('');

  document.getElementById('ac-link').href =
    `https://globe.adsbexchange.com/?icao=${a.hex.toLowerCase()}`;

  const photo = document.getElementById('ac-panel-photo');
  const placeholder = document.getElementById('ac-placeholder');
  photo.classList.remove('loaded'); photo.src='';
  placeholder.textContent='Loading…'; placeholder.style.display='flex';
  document.getElementById('aircraft-panel').classList.add('open');

  fetch(`/api/adsb/aircraft-info/${a.hex}`)
    .then(r=>r.json())
    .then(info => {
      const extra = [];
      if (info.registration && info.registration !== a.registration)
        extra.push(['Reg (confirmed)', info.registration]);
      if (info.type)         extra.push(['Full type', info.type]);
      if (info.manufacturer) extra.push(['Manufacturer', info.manufacturer]);
      if (info.owner)        extra.push(['Owner', info.owner]);
      if (info.country)      extra.push(['Country', info.country]);
      if (extra.length)
        document.getElementById('ac-rows').innerHTML +=
          extra.map(([l,v])=>`<div class="vp-row"><span class="vp-lbl">${l}</span><span class="vp-val">${v}</span></div>`).join('');
      if (info.photo_url) {
        photo.onload  = () => { photo.classList.add('loaded'); placeholder.style.display='none'; };
        photo.onerror = () => { photo.classList.remove('loaded'); placeholder.textContent='No photo'; };
        photo.src = info.photo_url;
      } else {
        placeholder.textContent = 'No photo available';
      }
    })
    .catch(() => { placeholder.textContent='No photo available'; });

  // Draw track if adsb map visible
  _clearAdsbTrack();
  fetch(`/api/adsb/track/${a.hex}`)
    .then(r=>r.json())
    .then(pts => {
      if (pts.length < 2 || !adsbMap) return;
      const lls = pts.map(p=>[p.lat,p.lon]);
      _adsbTrackLine = L.polyline(lls,{color,weight:2,opacity:0.65,dashArray:'4 4'}).addTo(adsbMap);
      _adsbTrackMap  = adsbMap;
      const oldest = new Date(pts[0].ts*1000).toLocaleString('en-GB');
      document.getElementById('ac-rows').innerHTML +=
        `<div class="vp-row"><span class="vp-lbl">Track from</span><span class="vp-val" style="color:var(--muted);font-size:.75rem">${oldest} (${pts.length} pts)</span></div>`;
    })
    .catch(()=>{});
}

function _clearAdsbTrack() {
  if (_adsbTrackLine && _adsbTrackMap) _adsbTrackMap.removeLayer(_adsbTrackLine);
  _adsbTrackLine = null; _adsbTrackMap = null;
}

function closeAircraftPanel() {
  document.getElementById('aircraft-panel').classList.remove('open');
  _clearAdsbTrack();
  _acCurrentHex = null;
}

function showToastAdsb(a) {
  const color = adsbVesselColor(a);
  const disp  = a.registration || a.flight || a.hex;
  document.getElementById('toast-title').textContent = `✈ ${disp} — ${a.label||'ADS-B'}`;
  document.getElementById('toast-body').textContent  =
    `${a.icao_type||'?'}  ·  ${a.alt_baro!=null?a.alt_baro+' ft':'?'}  ·  ` +
    (a.lat!=null ? `${a.lat.toFixed(3)}, ${a.lon.toFixed(3)}` : 'no pos');
  const t = document.getElementById('toast');
  t.style.borderLeftColor = color; t.style.borderColor = color;
  t.classList.remove('hide');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(()=>{
    t.classList.add('hide');
    t.style.borderLeftColor=''; t.style.borderColor='';
  }, 9000);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { closeAircraftPanel(); closeVesselPanel(); }
});

// ── tab buttons ───────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-btn[data-tab]').forEach(btn => {
  btn.addEventListener('click', function() {
    showTab(this.dataset.tab, this);
  });
});

// ── start ─────────────────────────────────────────────────────────────────────
showTab('ais', document.querySelector('.tab-btn[data-tab="ais"]'));
</script>
</body>
</html>"""

# ── main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _setup_logging()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]; s.close()
    except Exception:
        lan_ip = "localhost"
    log.info(f"AIS-ADSB DASHBOARD v{_VERSION} starting on http://{lan_ip}:{_PORT}")
    log.info(f"Log: {_LOG_FILE}")
    uvicorn.run(app, host="0.0.0.0", port=_PORT, log_level="warning")
