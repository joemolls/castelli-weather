"""
Strava Integration - Castelli Romani MTB
Recupera attività del club e statistiche dei segmenti
"""

import httpx
import os
from datetime import datetime, timedelta
from typing import List, Dict, Optional

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


async def fetch_all_club_activities() -> List[Dict]:
    """
    Recupera le ultime attività del club
    NOTA: L'API /clubs/{id}/activities NON fornisce date né GPS, solo dati base
    """
    token = os.getenv("STRAVA_ACCESS_TOKEN")
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
            return result
            
    except Exception as e:
        print(f"❌ Errore recupero attività club: {e}")
        return []


def is_in_castelli_romani(start_lat: float, start_lon: float) -> bool:
    """Verifica se le coordinate sono nei Castelli Romani"""
    in_bbox = (
        CASTELLI_BBOX["lat_sud"] <= start_lat <= CASTELLI_BBOX["lat_nord"] and
        CASTELLI_BBOX["lon_ovest"] <= start_lon <= CASTELLI_BBOX["lon_est"]
    )
    return in_bbox


async def fetch_club_info() -> Optional[Dict]:
    """
    Recupera informazioni generali del club Strava
    """
    token = os.getenv("STRAVA_ACCESS_TOKEN")
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
            
            return {
                "name": club.get("name", "Club MTB"),
                "member_count": club.get("member_count", 0),
                "sport_type": club.get("sport_type", "cycling"),
                "city": club.get("city", ""),
                "state": club.get("state", ""),
                "country": club.get("country", ""),
            }
            
    except Exception as e:
        print(f"❌ Errore info club: {e}")
        return None


async def fetch_club_activities() -> List[Dict]:
    """
    Recupera le ultime attività del club nei Castelli Romani
    """
    token = os.getenv("STRAVA_ACCESS_TOKEN")
    if not token:
        print("❌ STRAVA_ACCESS_TOKEN non configurato in .env")
        return []
    
    print(f"🔍 Recupero attività club {STRAVA_CLUB_ID}...")
    
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # Recupera le ultime 30 attività del club
            headers = {"Authorization": f"Bearer {token}"}
            url = f"https://www.strava.com/api/v3/clubs/{STRAVA_CLUB_ID}/activities"
            params = {"per_page": 30}
            
            response = await client.get(url, headers=headers, params=params, timeout=10.0)
            response.raise_for_status()
            
            activities = response.json()
            print(f"✅ Recuperate {len(activities)} attività totali dal club")
            
            # Filtra solo quelle nei Castelli Romani
            castelli_activities = []
            for activity in activities:
                # Verifica se ha coordinate di partenza
                if activity.get("start_latlng") and len(activity["start_latlng"]) == 2:
                    lat, lon = activity["start_latlng"]
                    
                    if is_in_castelli_romani(lat, lon):
                        # Formatta i dati
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
            
            # Limita alle ultime 10
            return castelli_activities[:10]
            
    except Exception as e:
        print(f"❌ Errore recupero attività Strava: {e}")
        return []


async def fetch_club_stats() -> Dict:
    """
    Calcola statistiche settimanali del club nei Castelli Romani
    """
    activities = await fetch_club_activities()
    
    if not activities:
        return {
            "total_activities": 0,
            "total_km": 0,
            "total_elevation": 0,
            "top_riders": []
        }
    
    # Filtra attività dell'ultima settimana
    now = datetime.now()
    week_ago = now - timedelta(days=7)
    
    total_km = 0
    total_elevation = 0
    rider_stats = {}
    
    for activity in activities:
        total_km += activity["distance_km"]
        total_elevation += activity["elevation_gain"]
        
        # Accumula per rider
        rider = activity["athlete_name"]
        if rider not in rider_stats:
            rider_stats[rider] = {"km": 0, "elevation": 0, "rides": 0}
        
        rider_stats[rider]["km"] += activity["distance_km"]
        rider_stats[rider]["elevation"] += activity["elevation_gain"]
        rider_stats[rider]["rides"] += 1
    
    # Top 3 riders per km
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
    """
    Recupera dettagli aggiuntivi di un segmento Strava
    (popolarità, ultima attività)
    """
    token = os.getenv("STRAVA_ACCESS_TOKEN")
    if not token:
        return None
    
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            headers = {"Authorization": f"Bearer {token}"}
            
            # Dettagli del segmento
            seg_url = f"https://www.strava.com/api/v3/segments/{segment_id}"
            seg_response = await client.get(seg_url, headers=headers, timeout=10.0)
            seg_response.raise_for_status()
            segment = seg_response.json()
            
            # Leaderboard per ultima attività
            lb_url = f"https://www.strava.com/api/v3/segments/{segment_id}/leaderboard"
            lb_params = {"per_page": 1}  # Solo il più recente
            lb_response = await client.get(lb_url, headers=headers, params=lb_params, timeout=10.0)
            lb_response.raise_for_status()
            leaderboard = lb_response.json()
            
            # Calcola "ultima attività"
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
