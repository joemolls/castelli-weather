"""
Strava Integration - Castelli Romani MTB
Recupera attività del club e statistiche dei segmenti
"""

import httpx
import os
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from dotenv import load_dotenv

load_dotenv()

# Bounding Box Castelli Romani
CASTELLI_BBOX = {
    "lat_nord": 41.7974,
    "lat_sud": 41.6906,
    "lon_ovest": 12.7041,
    "lon_est": 12.7195
}

print(f"📦 Bounding Box Castelli Romani:")
print(f"   Latitudine: {CASTELLI_BBOX['lat_sud']} → {CASTELLI_BBOX['lat_nord']}")
print(f"   Longitudine: {CASTELLI_BBOX['lon_ovest']} → {CASTELLI_BBOX['lon_est']}")

# Club ID
STRAVA_CLUB_ID = 1433598

# File locale per salvare i token aggiornati
TOKEN_FILE = "strava_tokens.json"

# ─────────────────────────────────────────
# CACHE IN MEMORIA - evita 429 rate limit
# ─────────────────────────────────────────
_cache = {}
CACHE_TTL = 900  # 15 minuti in secondi

def get_cache(key: str):
    """Restituisce il valore dalla cache se non scaduto"""
    if key in _cache:
        data, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            remaining = int(CACHE_TTL - (time.time() - ts))
            print(f"📦 Cache HIT: {key} (scade tra {remaining}s)")
            return data
    return None

def set_cache(key: str, data):
    """Salva il valore nella cache con timestamp"""
    _cache[key] = (data, time.time())
    print(f"💾 Cache SET: {key}")




# ─────────────────────────────────────────
# TOKEN MANAGEMENT - Refresh automatico
# ─────────────────────────────────────────

def load_tokens():
    """Carica i token dal file locale (più recenti) o dalle env vars"""
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                tokens = json.load(f)
                if tokens.get("access_token"):
                    return tokens
        except Exception:
            pass

    # Fallback alle variabili d'ambiente
    return {
        "access_token": os.getenv("STRAVA_ACCESS_TOKEN"),
        "refresh_token": os.getenv("STRAVA_REFRESH_TOKEN"),
        "expires_at": int(os.getenv("STRAVA_EXPIRES_AT", "0"))
    }

def save_tokens(tokens):
    """Salva i token aggiornati nel file locale"""
    try:
        with open(TOKEN_FILE, "w") as f:
            json.dump(tokens, f)
        print(f"✅ Token Strava salvati, scadono: {datetime.fromtimestamp(tokens['expires_at'])}")
    except Exception as e:
        print(f"❌ Errore salvataggio token: {e}")

async def refresh_access_token():
    """Rinnova automaticamente l'access token usando il refresh token"""
    tokens = load_tokens()
    refresh_token = tokens.get("refresh_token")
    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")

    if not all([refresh_token, client_id, client_secret]):
        print("❌ Credenziali Strava mancanti per il refresh")
        return None

    print("🔄 Rinnovo token Strava in corso...")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://www.strava.com/oauth/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token"
                },
                timeout=10.0
            )

            if response.status_code != 200:
                print(f"❌ Errore refresh: {response.status_code} - {response.text}")
                return None

            new_tokens = response.json()
            updated = {
                "access_token": new_tokens["access_token"],
                "refresh_token": new_tokens["refresh_token"],
                "expires_at": new_tokens["expires_at"]
            }
            save_tokens(updated)
            print(f"✅ Token rinnovato con successo!")
            return new_tokens["access_token"]

    except Exception as e:
        print(f"❌ Errore durante refresh: {e}")
        return None

async def get_valid_token():
    """Restituisce un token valido, rinnovandolo automaticamente se necessario"""
    tokens = load_tokens()
    expires_at = tokens.get("expires_at", 0)

    # Rinnova se scade entro 10 minuti
    if time.time() > (expires_at - 600):
        print("⏰ Token in scadenza, rinnovo automatico...")
        return await refresh_access_token()

    return tokens.get("access_token")


# ─────────────────────────────────────────
# CLUB INFO
# ─────────────────────────────────────────

async def fetch_club_info() -> Optional[Dict]:
    """Recupera informazioni generali del club Strava"""
    cached = get_cache("club_info")
    if cached is not None:
        return cached

    token = await get_valid_token()
    if not token:
        print("❌ STRAVA_ACCESS_TOKEN non configurato in .env")
        return None

    print(f"🔍 Recupero info club {STRAVA_CLUB_ID}...")

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            headers = {"Authorization": f"Bearer {token}"}
            url = f"https://www.strava.com/api/v3/clubs/{STRAVA_CLUB_ID}"

            response = await client.get(url, headers=headers, timeout=10.0)
            response.raise_for_status()

            club = response.json()
            print(f"✅ Club trovato: {club.get('name')}")

            result = {
                "name": club.get("name", "Club MTB"),
                "member_count": club.get("member_count", 0),
                "sport_type": club.get("sport_type", "cycling"),
                "city": club.get("city", ""),
                "state": club.get("state", ""),
                "country": club.get("country", ""),
            }
            set_cache("club_info", result)
            return result

    except Exception as e:
        print(f"❌ Errore info club: {e}")
        return None


# ─────────────────────────────────────────
# CLUB ACTIVITIES
# ─────────────────────────────────────────

async def fetch_all_club_activities() -> List[Dict]:
    """
    Recupera le ultime attività del club
    NOTA: L'API /clubs/{id}/activities NON fornisce date né GPS, solo dati base
    """
    cached = get_cache("club_activities")
    if cached is not None:
        return cached

    token = await get_valid_token()
    if not token:
        return []

    print(f"🔍 Recupero attività del club...")

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            headers = {"Authorization": f"Bearer {token}"}
            url = f"https://www.strava.com/api/v3/clubs/{STRAVA_CLUB_ID}/activities"
            params = {"per_page": 10}

            response = await client.get(url, headers=headers, params=params, timeout=10.0)
            response.raise_for_status()

            activities = response.json()
            print(f"✅ Recuperate {len(activities)} attività dal club")

            result = []
            for idx, activity in enumerate(activities[:5], 1):
                activity_name = activity.get("name", "Senza titolo")
                athlete_name = "Unknown"

                if isinstance(activity.get("athlete"), dict):
                    firstname = activity["athlete"].get("firstname", "")
                    lastname_initial = activity["athlete"].get("lastname", "")
                    if lastname_initial:
                        lastname_initial = lastname_initial[0] + "."
                    athlete_name = f"{firstname} {lastname_initial}".strip()

                distance_km = round(activity.get("distance", 0) / 1000, 1)
                elevation = int(activity.get("total_elevation_gain", 0))
                moving_time = format_duration(activity.get("moving_time", 0))

                print(f"  {idx}. {athlete_name}: {activity_name[:40]} - {distance_km}km, {elevation}m D+")

                result.append({
                    "athlete_name": athlete_name,
                    "name": activity_name,
                    "distance_km": distance_km,
                    "elevation_gain": elevation,
                    "moving_time": moving_time,
                })

            print(f"✅ Mostro {len(result)} attività")
            set_cache("club_activities", result)
            return result

    except Exception as e:
        print(f"❌ Errore recupero attività club: {e}")
        return []


async def fetch_club_activities() -> List[Dict]:
    """Recupera le ultime attività del club nei Castelli Romani"""
    token = await get_valid_token()
    if not token:
        print("❌ STRAVA_ACCESS_TOKEN non configurato in .env")
        return []

    print(f"🔍 Recupero attività club {STRAVA_CLUB_ID}...")

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            headers = {"Authorization": f"Bearer {token}"}
            url = f"https://www.strava.com/api/v3/clubs/{STRAVA_CLUB_ID}/activities"
            params = {"per_page": 30}

            response = await client.get(url, headers=headers, params=params, timeout=10.0)
            response.raise_for_status()

            activities = response.json()
            print(f"✅ Recuperate {len(activities)} attività totali dal club")

            castelli_activities = []
            for activity in activities:
                if activity.get("start_latlng") and len(activity["start_latlng"]) == 2:
                    lat, lon = activity["start_latlng"]

                    if is_in_castelli_romani(lat, lon):
                        activity_time = datetime.fromisoformat(activity["start_date"].replace("Z", "+00:00"))
                        time_ago = get_time_ago(activity_time)

                        castelli_activities.append({
                            "athlete_name": activity.get("athlete", {}).get("firstname", "Unknown"),
                            "name": activity.get("name", "Senza titolo"),
                            "type": activity.get("type", "Ride"),
                            "distance_km": round(activity.get("distance", 0) / 1000, 1),
                            "elevation_gain": int(activity.get("total_elevation_gain", 0)),
                            "time_ago": time_ago,
                            "moving_time": format_duration(activity.get("moving_time", 0)),
                            "activity_id": activity.get("id"),
                        })

            print(f"✅ Filtrate {len(castelli_activities)} attività nei Castelli Romani")
            return castelli_activities[:10]

    except Exception as e:
        print(f"❌ Errore recupero attività Strava: {e}")
        return []


async def fetch_club_stats() -> Dict:
    """Calcola statistiche settimanali del club nei Castelli Romani"""
    activities = await fetch_club_activities()

    if not activities:
        return {
            "total_activities": 0,
            "total_km": 0,
            "total_elevation": 0,
            "top_riders": []
        }

    total_km = 0
    total_elevation = 0
    rider_stats = {}

    for activity in activities:
        total_km += activity["distance_km"]
        total_elevation += activity["elevation_gain"]

        rider = activity["athlete_name"]
        if rider not in rider_stats:
            rider_stats[rider] = {"km": 0, "elevation": 0, "rides": 0}

        rider_stats[rider]["km"] += activity["distance_km"]
        rider_stats[rider]["elevation"] += activity["elevation_gain"]
        rider_stats[rider]["rides"] += 1

    top_riders = sorted(
        [{"name": k, **v} for k, v in rider_stats.items()],
        key=lambda x: x["km"],
        reverse=True
    )[:3]

    return {
        "total_activities": len(activities),
        "total_km": round(total_km, 1),
        "total_elevation": int(total_elevation),
        "top_riders": top_riders
    }


async def fetch_segment_details(segment_id: int) -> Optional[Dict]:
    """Recupera dettagli aggiuntivi di un segmento Strava"""
    token = await get_valid_token()
    if not token:
        return None

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            headers = {"Authorization": f"Bearer {token}"}

            seg_url = f"https://www.strava.com/api/v3/segments/{segment_id}"
            seg_response = await client.get(seg_url, headers=headers, timeout=10.0)
            seg_response.raise_for_status()
            segment = seg_response.json()

            lb_url = f"https://www.strava.com/api/v3/segments/{segment_id}/leaderboard"
            lb_response = await client.get(lb_url, headers=headers, params={"per_page": 1}, timeout=10.0)
            lb_response.raise_for_status()
            leaderboard = lb_response.json()

            last_activity_time = None
            if leaderboard.get("entries") and len(leaderboard["entries"]) > 0:
                last_entry = leaderboard["entries"][0]
                if last_entry.get("start_date"):
                    last_activity_time = datetime.fromisoformat(
                        last_entry["start_date"].replace("Z", "+00:00")
                    )

            return {
                "athlete_count": segment.get("athlete_count", 0),
                "effort_count": segment.get("effort_count", 0),
                "last_activity": get_time_ago(last_activity_time) if last_activity_time else "N/A",
                "kom_time": format_duration(segment.get("xoms", {}).get("kom", 0)) if segment.get("xoms") else "N/A",
            }

    except Exception as e:
        print(f"❌ Errore dettagli segmento {segment_id}: {e}")
        return None


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def is_in_castelli_romani(start_lat: float, start_lon: float) -> bool:
    """Verifica se le coordinate sono nei Castelli Romani"""
    return (
        CASTELLI_BBOX["lat_sud"] <= start_lat <= CASTELLI_BBOX["lat_nord"] and
        CASTELLI_BBOX["lon_ovest"] <= start_lon <= CASTELLI_BBOX["lon_est"]
    )

def get_time_ago(dt: datetime) -> str:
    """Converte datetime in formato 'X ore fa' / 'X giorni fa'"""
    now = datetime.now(dt.tzinfo)
    delta = now - dt

    if delta.days > 0:
        return f"{delta.days} giorni fa"

    hours = delta.seconds // 3600
    if hours > 0:
        return f"{hours}h fa"

    minutes = delta.seconds // 60
    return f"{minutes}min fa"

def format_duration(seconds: int) -> str:
    """Converte secondi in formato MM:SS o HH:MM:SS"""
    if seconds == 0:
        return "N/A"

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


# ─────────────────────────────────────────
# STARRED SEGMENTS
# ─────────────────────────────────────────

async def fetch_starred_segments() -> List[Dict]:
    """
    Recupera i segmenti starred dell'atleta con statistiche complete.
    Due step: 1) lista starred  2) dettaglio per ogni segmento
    """
    cached = get_cache("starred_segments")
    if cached is not None:
        return cached

    token = await get_valid_token()
    if not token:
        print("❌ Nessun token valido per starred segments")
        return []

    print("🔍 Recupero segmenti starred...")

    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            headers = {"Authorization": f"Bearer {token}"}

            # Step 1: Lista ID dei segmenti starred
            resp = await client.get(
                "https://www.strava.com/api/v3/segments/starred",
                headers=headers,
                params={"per_page": 50},
                timeout=10.0
            )
            resp.raise_for_status()
            starred = resp.json()
            print(f"⭐ Trovati {len(starred)} segmenti starred")

            # Step 2: Dettaglio completo per ogni segmento
            segments = []
            for s in starred:
                seg_id = s["id"]
                try:
                    detail_resp = await client.get(
                        f"https://www.strava.com/api/v3/segments/{seg_id}",
                        headers=headers,
                        timeout=10.0
                    )
                    detail_resp.raise_for_status()
                    d = detail_resp.json()

                    # PR personale
                    pr_stats = d.get("athlete_segment_stats", {})
                    pr_time = pr_stats.get("pr_elapsed_time", 0)
                    pr_date = pr_stats.get("pr_date", "")
                    pr_efforts = pr_stats.get("effort_count", 0)

                    # Local legend (chi ha percorso di più negli ultimi 90gg)
                    legend = d.get("local_legend", {})
                    legend_name = legend.get("title", "")
                    legend_efforts = legend.get("effort_count", "")

                    # KOM
                    xoms = d.get("xoms", {})

                    # Coordinate e polyline per visualizzazione su mappa
                    start_ll = d.get("start_latlng", [])
                    end_ll   = d.get("end_latlng", [])
                    polyline = d.get("map", {}).get("polyline", "")

                    segments.append({
                        "id": seg_id,
                        "name": d.get("name", ""),
                        "distance_km": round(d.get("distance", 0) / 1000, 2),
                        "avg_grade": d.get("average_grade", 0),
                        "max_grade": d.get("maximum_grade", 0),
                        "elevation_gain": round(d.get("total_elevation_gain", 0)),
                        "effort_count": d.get("effort_count", 0),
                        "athlete_count": d.get("athlete_count", 0),
                        "kom": xoms.get("kom", "N/A"),
                        "elevation_profile": d.get("elevation_profiles", {}).get("light_url", ""),
                        "link": f"https://www.strava.com/segments/{seg_id}",
                        # PR personale
                        "pr_time": format_duration(pr_time) if pr_time else "N/A",
                        "pr_date": pr_date,
                        "pr_efforts": pr_efforts,
                        # Local legend
                        "legend_name": legend_name,
                        "legend_efforts": legend_efforts,
                        # Mappa
                        "start_latlng": start_ll,
                        "end_latlng":   end_ll,
                        "polyline":     polyline,
                    })

                    print(f"  ✅ {d.get('name')} - {d.get('effort_count', 0):,} tentativi")

                except Exception as e:
                    print(f"  ❌ Errore segmento {seg_id}: {e}")
                    continue

            print(f"✅ Recuperati {len(segments)} segmenti con dettagli")
            set_cache("starred_segments", segments)
            return segments

    except Exception as e:
        print(f"❌ Errore fetch_starred_segments: {e}")
        return []
