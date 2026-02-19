from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from locations import LOCATIONS
from weather_client import fetch_weather
from scraper import get_all_alerts
from strava_client import fetch_starred_segments
from counter import increment_visit
from datetime import datetime, timedelta
import csv
import httpx
import os
import xml.etree.ElementTree as ET
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Castelli Weather API")
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# ─── Health check ────────────────────────────────────────────────────────────
@app.head("/")
async def head_root():
    return {}

@app.head("/dashboard-completa")
async def head_dashboard():
    return {}

# ─── GPX locali — metadati e colori (devono corrispondere al JS in percorsi.html) ─
GPX_FILES = [
    {"key": "gpx-0", "file": "static/gpx/AeB_AmiciBici_C.R.1.gpx",  "name": "AeB Amici Bici C.R.",     "color": "#e74c3c"},
    {"key": "gpx-1", "file": "static/gpx/Like_Epic_100_C.R.6.gpx",  "name": "Like Epic 100 C.R.",       "color": "#e67e22"},
    {"key": "gpx-2", "file": "static/gpx/Like_Epic_50_C.R.4.gpx",   "name": "Like Epic 50 C.R.",        "color": "#2980b9"},
    {"key": "gpx-3", "file": "static/gpx/Monte_Cavo_Colle_Iano.gpx", "name": "Monte Cavo - Colle Jano", "color": "#27ae60"},
    {"key": "gpx-4", "file": "static/gpx/P2P_Castelli_Romani.gpx",  "name": "P2P Castelli Romani",      "color": "#8e44ad"},
]

def get_gpx_centroid(filepath: str):
    """
    Estrae latitudine e longitudine medie di un file GPX.
    Parsing XML senza dipendenze esterne. Campiona 1 punto ogni 50
    per non leggere milioni di coordinate su file grandi.
    """
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
        # Namespace GPX 1.1 (il più comune)
        ns = {"g": "http://www.topografix.com/GPX/1/1"}
        points = root.findall(".//g:trkpt", ns)
        if not points:
            # Prova senza namespace (GPX 1.0 o file non standard)
            points = root.findall(".//{http://www.topografix.com/GPX/1/0}trkpt")
        if not points:
            points = root.findall(".//trkpt")
        if not points:
            return None, None

        # Campiona per velocità su file grandi
        step = max(1, len(points) // 200)
        sample = points[::step]
        lats = [float(p.get("lat")) for p in sample if p.get("lat")]
        lons = [float(p.get("lon")) for p in sample if p.get("lon")]
        if not lats:
            return None, None
        return round(sum(lats) / len(lats), 5), round(sum(lons) / len(lons), 5)
    except Exception as e:
        print(f"Errore lettura GPX {filepath}: {e}")
        return None, None

# ─── Calcolo condizioni ───────────────────────────────────────────────────────
def calculate_trail_conditions(hourly_data):
    current_hour = 0
    rain_24h     = sum(hourly_data["precipitation"][:24]) if len(hourly_data["precipitation"]) >= 24 else 0
    current_temp = hourly_data["temperature_2m"][current_hour]
    current_wind = hourly_data["windspeed_10m"][current_hour]
    current_gust = hourly_data["windgusts_10m"][current_hour]

    score   = 100
    reasons = []

    if rain_24h > 15:
        score -= 40; reasons.append(f"❌ Pioggia abbondante ultime 24h ({rain_24h:.1f}mm)")
    elif rain_24h > 5:
        score -= 20; reasons.append(f"⚠️ Pioggia moderata ultime 24h ({rain_24h:.1f}mm)")
    else:
        reasons.append("✅ Sentieri asciutti")

    if current_wind > 30:
        score -= 30; reasons.append(f"❌ Vento forte ({current_wind:.0f} km/h)")
    elif current_wind > 20:
        score -= 15; reasons.append(f"⚠️ Vento moderato ({current_wind:.0f} km/h)")
    else:
        reasons.append(f"✅ Vento calmo ({current_wind:.0f} km/h)")

    if current_gust > 40:
        score -= 20; reasons.append(f"❌ Raffiche pericolose ({current_gust:.0f} km/h)")
    elif current_gust > 30:
        score -= 10; reasons.append(f"⚠️ Raffiche moderate ({current_gust:.0f} km/h)")

    if current_temp < 0:
        score -= 30; reasons.append(f"❌ Rischio ghiaccio ({current_temp:.0f}°C)")
    elif current_temp < 3:
        score -= 15; reasons.append(f"⚠️ Temperature basse ({current_temp:.0f}°C)")
    else:
        reasons.append(f"✅ Temperatura OK ({current_temp:.0f}°C)")

    if score >= 80:
        rating = "excellent"; rating_text = "🟢 OTTIME";   rating_emoji = "🚴‍♂️"
    elif score >= 60:
        rating = "good";      rating_text = "🟡 DISCRETE"; rating_emoji = "⚠️"
    else:
        rating = "poor";      rating_text = "🔴 DIFFICILI"; rating_emoji = "❌"

    return {
        "score": score, "rating": rating, "rating_text": rating_text,
        "rating_emoji": rating_emoji, "reasons": reasons,
        "rain_24h": rain_24h, "current_wind": current_wind, "current_gust": current_gust,
    }

def find_best_riding_windows(hourly_data):
    now = datetime.now()
    hours_by_day = {}

    for i, time_str in enumerate(hourly_data["time"]):
        time_obj = datetime.fromisoformat(time_str)
        if time_obj <= now or time_obj.hour < 7 or time_obj.hour >= 20:
            continue
        day_key = time_obj.date()
        if day_key not in hours_by_day:
            hours_by_day[day_key] = []

        hour_score = 100
        if hourly_data["precipitation"][i] > 0.5: hour_score -= 50
        if hourly_data["windspeed_10m"][i] > 25:  hour_score -= 30
        if hourly_data["temperature_2m"][i] < 3:  hour_score -= 20
        if hourly_data["weather_code"][i] in [95, 96, 99]: hour_score -= 60

        hours_by_day[day_key].append({
            "time": time_obj, "hour": time_obj.hour, "score": hour_score,
            "temp": hourly_data["temperature_2m"][i],
            "wind": hourly_data["windspeed_10m"][i],
            "precip": hourly_data["precipitation"][i],
        })

    daily_windows = []
    for day, hours in sorted(hours_by_day.items())[:3]:
        if len(hours) < 4:
            continue
        best_window = None
        best_avg    = 0
        for ws in [6, 5, 4]:
            for start in range(len(hours) - ws + 1):
                window   = hours[start:start + ws]
                avg_score = sum(h["score"] for h in window) / ws
                if avg_score > best_avg:
                    best_avg = avg_score
                    best_window = {
                        "start": window[0]["time"], "end": window[-1]["time"],
                        "duration": ws, "score": avg_score,
                        "temp":   sum(h["temp"]   for h in window) / ws,
                        "wind":   sum(h["wind"]   for h in window) / ws,
                        "precip": max(h["precip"] for h in window),
                    }
        if not best_window or best_window["score"] < 50:
            continue

        rating      = "excellent" if best_window["score"] >= 80 else ("good" if best_window["score"] >= 60 else "poor")
        rating_icon = "🟢" if rating == "excellent" else ("🟡" if rating == "good" else "🔴")

        if day == now.date():
            day_name = "Oggi"
        elif day == (now + timedelta(days=1)).date():
            day_name = "Domani"
        else:
            giorni = {"Mon":"Lun","Tue":"Mar","Wed":"Mer","Thu":"Gio","Fri":"Ven","Sat":"Sab","Sun":"Dom"}
            day_name = day.strftime("%a %d %b")
            for en, it in giorni.items():
                day_name = day_name.replace(en, it)

        daily_windows.append({
            "day": day_name, "date": day.strftime("%d %b"),
            "start_time": best_window["start"].strftime("%H:%M"),
            "end_time":   best_window["end"].strftime("%H:%M"),
            "duration":   best_window["duration"],
            "rating": rating, "rating_icon": rating_icon,
            "temp":   best_window["temp"],
            "wind":   best_window["wind"],
            "precip": best_window["precip"],
            "score":  best_window["score"],
        })

    return daily_windows

async def fetch_form_feedbacks():
    csv_url = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRdLrCbwcB8E9zjahAbON9zAHQJKH6_PHONk40EGhhzrF23jX0NA8oLd3xIk-Hj98-ZLq2CnST_Fpzq/pub?gid=2136983056&single=true&output=csv"
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            response = await client.get(csv_url, timeout=10.0)
            response.raise_for_status()
            lines  = response.text.strip().split("\n")
            if len(lines) < 2:
                return []
            reader    = csv.DictReader(lines)
            feedbacks = []
            now       = datetime.now()
            for row in reader:
                cols          = list(row.keys())
                timestamp_col = next((c for c in cols if "Informazioni" in c or "cronolog" in c), cols[0] if cols else "")
                location_col  = next((c for c in cols if "Sentiero" in c or "Localit" in c), cols[1] if len(cols) > 1 else "")
                condition_col = next((c for c in cols if "Condizione" in c), cols[2] if len(cols) > 2 else "")
                details_col   = next((c for c in cols if "Dettagli" in c), cols[4] if len(cols) > 4 else "")
                timestamp = row.get(timestamp_col, "")
                location  = row.get(location_col, "")
                condition = row.get(condition_col, "")
                details   = row.get(details_col, "")
                if not location or not location.strip():
                    continue
                time_ago = "Ora sconosciuta"
                if timestamp:
                    try:
                        dt   = datetime.strptime(timestamp.replace(".", ":"), "%d/%m/%Y %H:%M:%S")
                        diff = now - dt
                        if diff.days == 0:
                            hours = diff.seconds // 3600
                            if hours == 0:
                                mins = diff.seconds // 60
                                time_ago = "Pochi minuti fa" if mins < 5 else f"{mins} min fa"
                            else:
                                time_ago = "1 ora fa" if hours == 1 else f"{hours} ore fa"
                        elif diff.days == 1:
                            time_ago = "Ieri"
                        elif diff.days < 7:
                            time_ago = f"{diff.days} giorni fa"
                        else:
                            time_ago = dt.strftime("%d/%m/%Y")
                    except Exception as e:
                        print(f"Errore parsing data '{timestamp}': {e}")
                        time_ago = timestamp
                full_description = condition
                if details and details.strip():
                    full_description += f" - {details}"
                feedbacks.append({"location": location, "description": full_description, "date": time_ago, "timestamp": timestamp})
            return feedbacks[::-1][:10] if feedbacks else []
    except Exception as e:
        print(f"Errore recupero feedbacks: {e}")
        return []

# ─── Routes ──────────────────────────────────────────────────────────────────
@app.get("/")
def root():
    return RedirectResponse(url="/dashboard-completa")

@app.get("/locations")
def get_locations():
    return LOCATIONS

@app.get("/weather/{location}")
async def get_weather(location: str):
    if location not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")
    loc  = LOCATIONS[location]
    data = await fetch_weather(loc["lat"], loc["lon"])
    return {"location": loc["name"], "elevation": loc["elevation"], "hourly": data["hourly"]}

@app.get("/dashboard/{location}", response_class=HTMLResponse)
async def dashboard(request: Request, location: str):
    if location not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")
    loc    = LOCATIONS[location]
    data   = await fetch_weather(loc["lat"], loc["lon"])
    hourly = data["hourly"]
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "location_name": loc["name"],
        "hourly": {k: hourly.get(k, []) for k in ["time","temperature_2m","precipitation","weather_code","windspeed_10m","windgusts_10m"]},
    })

@app.get("/dashboard-completa", response_class=HTMLResponse)
async def dashboard_completa(request: Request):
    visit_stats = increment_visit()
    all_data    = []
    overall_trail_conditions = None
    overall_riding_windows   = None

    for loc_key, loc_info in LOCATIONS.items():
        data   = await fetch_weather(loc_info["lat"], loc_info["lon"])
        hourly = data["hourly"]
        if overall_trail_conditions is None:
            overall_trail_conditions = calculate_trail_conditions(hourly)
            overall_riding_windows   = find_best_riding_windows(hourly)
        all_data.append({
            "name": loc_info["name"], "elevation": loc_info["elevation"],
            "hourly": {k: hourly.get(k, []) for k in ["time","temperature_2m","precipitation","weather_code","windspeed_10m","windgusts_10m"]},
        })

    return templates.TemplateResponse("dashboard_completa.html", {
        "request": request,
        "locations_data": all_data,
        "trail_conditions": overall_trail_conditions,
        "riding_windows":   overall_riding_windows,
        "visit_stats":      visit_stats,
    })

@app.get("/avvisi", response_class=HTMLResponse)
async def avvisi(request: Request):
    alerts    = await get_all_alerts()
    feedbacks = await fetch_form_feedbacks()
    return templates.TemplateResponse("avvisi.html", {
        "request": request,
        "alerts":    alerts,
        "feedbacks": feedbacks,
    })

@app.get("/percorsi", response_class=HTMLResponse)
async def percorsi(request: Request):
    """Mappa percorsi GPX con meteo calcolato dal centroide del tracciato + dati Strava"""

    gpx_forecasts = []
    for gpx in GPX_FILES:
        lat, lon = get_gpx_centroid(gpx["file"])
        if lat is None:
            # Fallback: centro area Castelli Romani
            lat, lon = 41.745, 12.720

        weather        = await fetch_weather(lat, lon)
        hourly         = weather["hourly"]
        conditions     = calculate_trail_conditions(hourly)
        riding_windows = find_best_riding_windows(hourly)

        gpx_forecasts.append({
            "key":            gpx["key"],
            "name":           gpx["name"],
            "color":          gpx["color"],
            "file":           gpx["file"],
            "lat":            lat,
            "lon":            lon,
            "conditions":     conditions,
            "riding_windows": riding_windows,
        })

    starred_segments = await fetch_starred_segments()

    return templates.TemplateResponse("percorsi.html", {
        "request":          request,
        "gpx_forecasts":    gpx_forecasts,
        "starred_segments": starred_segments,
    })
