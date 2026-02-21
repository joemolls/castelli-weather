"""
cache.py — Cache Redis (Upstash) per dati meteo e Strava.

Strategia TTL:
  - forecast meteo : 60 min  (ICON aggiorna ogni 3h, 60min è ottimale)
  - storico meteo  : 2 ore   (Archive API ha lag ~2gg, inutile refreshare spesso)
  - segmenti Strava: 6 ore   (lista segmenti starred quasi statica)

Fix critico: _redis_set usa POST /pipeline con JSON nel body (non nell'URL).
Il vecchio approccio GET /set/key/value rompeva l'URL con dati JSON complessi.
"""

import os
import json
import httpx
from datetime import datetime

UPSTASH_URL   = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")

# TTL in secondi
TTL_FORECAST = 60 * 60        # 60 minuti (ICON aggiorna ogni 3h)
TTL_HISTORY  = 2  * 60 * 60   # 2 ore
TTL_STRAVA   = 6  * 60 * 60   # 6 ore


# ─── Upstash helpers ──────────────────────────────────────────────────────────

def _headers():
    return {
        "Authorization": f"Bearer {UPSTASH_TOKEN}",
        "Content-Type":  "application/json",
    }


def _pipeline(commands: list):
    """Esegue più comandi Redis in una sola richiesta POST (JSON nel body)."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        r = httpx.post(
            f"{UPSTASH_URL}/pipeline",
            headers=_headers(),
            content=json.dumps(commands),
            timeout=5.0,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"⚠️ Cache pipeline error: {e}")
        return None


def _redis_get(key: str):
    """Recupera un valore da Redis. Restituisce il valore deserializzato o None."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return None
    try:
        result = _pipeline([["GET", key]])
        if not result:
            return None
        raw = result[0].get("result")
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        print(f"⚠️ Cache GET error [{key}]: {e}")
        return None


def _redis_set(key: str, value, ttl: int):
    """
    Salva un valore in Redis con TTL (secondi).
    Usa pipeline POST — il JSON va nel body, non nell'URL.
    """
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return
    try:
        serialized = json.dumps(value, ensure_ascii=False)
        _pipeline([
            ["SET", key, serialized],
            ["EXPIRE", key, ttl],
        ])
    except Exception as e:
        print(f"⚠️ Cache SET error [{key}]: {e}")


def _coord_key(lat: float, lon: float) -> str:
    return f"{lat:.3f}_{lon:.3f}"


# ─── Cache wrapper: meteo forecast ───────────────────────────────────────────

async def cached_fetch_weather(lat: float, lon: float, fetch_fn):
    key = f"wx:forecast:{_coord_key(lat, lon)}"
    cached = _redis_get(key)
    if cached is not None:
        print(f"  📦 Cache HIT forecast {_coord_key(lat, lon)}")
        return cached

    print(f"  🌐 Cache MISS forecast {_coord_key(lat, lon)} — chiamo Open-Meteo")
    data = await fetch_fn(lat, lon)
    _redis_set(key, data, TTL_FORECAST)
    return data


# ─── Cache wrapper: meteo storico ────────────────────────────────────────────

async def cached_fetch_weather_history(lat: float, lon: float, days: int, fetch_fn):
    key = f"wx:history:{_coord_key(lat, lon)}:d{days}"
    cached = _redis_get(key)
    if cached is not None:
        print(f"  📦 Cache HIT history {_coord_key(lat, lon)} days={days}")
        return cached

    print(f"  🌐 Cache MISS history {_coord_key(lat, lon)} days={days} — chiamo Archive API")
    data = await fetch_fn(lat, lon, days)
    _redis_set(key, data, TTL_HISTORY)
    return data


# ─── Cache wrapper: Strava starred segments ───────────────────────────────────

async def cached_fetch_starred_segments(fetch_fn):
    key = "strava:starred_segments"
    cached = _redis_get(key)
    if cached is not None:
        print(f"  📦 Cache HIT Strava starred segments")
        return cached

    print(f"  🌐 Cache MISS Strava starred segments — chiamo API Strava")
    data = await fetch_fn()
    if data:
        _redis_set(key, data, TTL_STRAVA)
    return data


# ─── Utility: invalidazione manuale ──────────────────────────────────────────

def invalidate_weather_cache(lat: float, lon: float):
    coord = _coord_key(lat, lon)
    _pipeline([
        ["DEL", f"wx:forecast:{coord}"],
        ["DEL", f"wx:history:{coord}:d5"],
        ["DEL", f"wx:history:{coord}:d7"],
    ])


def invalidate_strava_cache():
    _pipeline([["DEL", "strava:starred_segments"]])
    print("🗑️ Cache Strava invalidata")


def get_cache_status() -> dict:
    """Stato attuale della cache — usato da /admin/cache."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return {"error": "Upstash non configurato"}

    status = {"timestamp": datetime.now().isoformat(), "keys": []}
    try:
        # Lista chiavi wx:* e strava:*
        results = _pipeline([
            ["KEYS", "wx:*"],
            ["KEYS", "strava:*"],
        ])
        if not results:
            return status

        wx_keys     = results[0].get("result", []) or []
        strava_keys = results[1].get("result", []) or []
        all_keys    = wx_keys + strava_keys

        if all_keys:
            ttl_cmds = [["TTL", k] for k in all_keys]
            ttl_results = _pipeline(ttl_cmds)
            for i, k in enumerate(all_keys):
                ttl = ttl_results[i].get("result", -1) if ttl_results else -1
                status["keys"].append({
                    "key":         k,
                    "ttl_seconds": ttl,
                    "expires_in":  f"{ttl // 60}min {ttl % 60}s" if ttl > 0 else "scaduta",
                })
    except Exception as e:
        status["error"] = str(e)

    return status
