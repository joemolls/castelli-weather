from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from locations import LOCATIONS
from weather_client import fetch_weather, fetch_weather_history
from scraper import get_all_alerts
from strava_client import fetch_starred_segments
from counter import increment_visit
from reports import save_report, get_active_reports, delete_report
from cache import cached_fetch_weather, cached_fetch_weather_history, cached_fetch_starred_segments, invalidate_strava_cache, get_cache_status
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

@app.on_event("startup")
async def startup_event():
    """Pre-carica i file GPX in memoria al boot — evita parsing XML ad ogni request."""
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, preload_gpx_cache)

# ─── Health check ────────────────────────────────────────────────────────────
@app.head("/")
async def head_root():
    return {}

@app.head("/dashboard-completa")
async def head_dashboard():
    return {}

# ─── GPX locali — caricati da gpx_config.json ────────────────────────────────
# Per aggiungere un nuovo percorso:
#   1. Copia il file .gpx in static/gpx/
#   2. Aggiungi una voce in gpx_config.json (non toccare main.py)
#   3. Riavvia il server
#
# Formato di ogni voce in gpx_config.json:
#   { "key": "gpx-N", "file": "static/gpx/nome.gpx", "name": "Nome visibile", "color": "#rrggbb" }
#   Il "key" deve essere univoco e progressivo (gpx-0, gpx-1, ...).
# ─────────────────────────────────────────────────────────────────────────────
import json as _json

_GPX_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "gpx_config.json")

def _load_gpx_config() -> list:
    try:
        with open(_GPX_CONFIG_PATH, "r", encoding="utf-8") as _f:
            _data = _json.load(_f)
        print(f"✅ gpx_config.json caricato: {len(_data)} percorsi")
        return _data
    except FileNotFoundError:
        print(f"⚠️ gpx_config.json non trovato in {_GPX_CONFIG_PATH} — uso lista vuota")
        return []
    except _json.JSONDecodeError as _e:
        print(f"❌ Errore parsing gpx_config.json: {_e} — uso lista vuota")
        return []

GPX_FILES = _load_gpx_config()

# ─── Cache in-memory GPX (popolata una sola volta al primo accesso) ────────────
# I file GPX non cambiano mai a runtime — non ha senso rileggerli ad ogni request.
# Struttura: { "gpx-0": {"centroid": (lat, lon), "coords": [[lat,lon], ...]}, ... }
_GPX_CACHE: dict = {}

def _parse_gpx_points(filepath: str):
    """Parsing XML interno — chiamato una sola volta per file."""
    tree = ET.parse(filepath)
    root = tree.getroot()
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    points = root.findall(".//g:trkpt", ns)
    if not points:
        points = root.findall(".//{http://www.topografix.com/GPX/1/0}trkpt")
    if not points:
        points = root.findall(".//trkpt")
    return points


def _ensure_gpx_cached(key: str, filepath: str, max_points: int = 300):
    """Carica e cachea un GPX in memoria se non già presente."""
    if key in _GPX_CACHE:
        return
    try:
        points = _parse_gpx_points(filepath)
        if not points:
            _GPX_CACHE[key] = {"centroid": (None, None), "coords": []}
            return

        # Coordinate complete campionate per Leaflet
        step   = max(1, len(points) // max_points)
        sample = points[::step]
        coords = [[round(float(p.get("lat")), 5), round(float(p.get("lon")), 5)]
                  for p in sample if p.get("lat") and p.get("lon")]

        # Centroide (media su tutti i punti, non solo il campione)
        step2  = max(1, len(points) // 200)
        sample2 = points[::step2]
        lats   = [float(p.get("lat")) for p in sample2 if p.get("lat")]
        lons   = [float(p.get("lon")) for p in sample2 if p.get("lon")]
        centroid = (round(sum(lats)/len(lats), 5), round(sum(lons)/len(lons), 5)) if lats else (None, None)

        _GPX_CACHE[key] = {"centroid": centroid, "coords": coords}
        print(f"  📍 GPX cachato in memoria: {filepath} ({len(coords)} punti)")
    except Exception as e:
        print(f"  ⚠️ Errore lettura GPX {filepath}: {e}")
        _GPX_CACHE[key] = {"centroid": (None, None), "coords": []}


def get_gpx_centroid(filepath: str):
    """Restituisce il centroide di un GPX (dalla cache in memoria)."""
    key = next((g["key"] for g in GPX_FILES if g["file"] == filepath), filepath)
    _ensure_gpx_cached(key, filepath)
    return _GPX_CACHE[key]["centroid"]


def get_gpx_coords(filepath: str, max_points: int = 300):
    """Restituisce le coordinate campionate di un GPX (dalla cache in memoria)."""
    key = next((g["key"] for g in GPX_FILES if g["file"] == filepath), filepath)
    _ensure_gpx_cached(key, filepath, max_points)
    return _GPX_CACHE[key]["coords"]


def preload_gpx_cache():
    """Chiamata al startup — carica tutti i GPX in memoria una sola volta."""
    print("🗺️ Pre-caricamento GPX in memoria...")
    for g in GPX_FILES:
        _ensure_gpx_cached(g["key"], g["file"])
    print(f"  ✅ {len(_GPX_CACHE)}/{len(GPX_FILES)} GPX cachati")



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


def calculate_current_conditions(soil_dryness):
    """
    Condizioni ATTUALI del terreno basate sullo storico reale (soil_dryness).
    Restituisce rating, testo e motivi per il box "Ora sul Terreno".
    """
    if not soil_dryness:
        return None

    rating_soil = soil_dryness.get("rating", "dry")
    dry_days    = soil_dryness.get("dry_days", 0)
    rain_7d     = soil_dryness.get("rain_7d", 0)

    reasons = []

    if rating_soil == "saturated":
        score = 20
        reasons.append(f"❌ Terreno saturo ({rain_7d:.0f}mm negli ultimi 5 giorni)")
        reasons.append(f"❌ Sentieri danneggiati — evita di uscire")
        if dry_days == 0:
            reasons.append("⚠️ Pioggia ancora recente")
        else:
            reasons.append(f"⏳ {dry_days} giorn{'o' if dry_days==1 else 'i'} senza pioggia — troppo poco")
    elif rating_soil == "wet":
        score = 45
        reasons.append(f"⚠️ Terreno bagnato ({rain_7d:.0f}mm negli ultimi 5 giorni)")
        reasons.append(f"⚠️ Sentieri scivolosi — massima attenzione")
        reasons.append(f"⏳ {dry_days} giorn{'o' if dry_days==1 else 'i'} senza pioggia")
    elif rating_soil == "damp":
        score = 70
        reasons.append(f"🟡 Terreno umido ({rain_7d:.0f}mm negli ultimi 5 giorni)")
        reasons.append(f"✅ Sentieri percorribili con attenzione")
        reasons.append(f"☀️ {dry_days} giorn{'o' if dry_days==1 else 'i'} senza pioggia")
    else:  # dry
        score = 95
        reasons.append(f"✅ Terreno asciutto ({rain_7d:.0f}mm negli ultimi 5 giorni)")
        reasons.append(f"✅ Sentieri in ottime condizioni")
        reasons.append(f"☀️ {dry_days} giorn{'o' if dry_days==1 else 'i'} senza pioggia")

    if score >= 80:
        rating = "excellent"; rating_text = "🟢 ASCIUTTI";  rating_emoji = "✅"
    elif score >= 55:
        rating = "good";      rating_text = "🟡 UMIDI";     rating_emoji = "⚠️"
    elif score >= 35:
        rating = "good";      rating_text = "🟠 BAGNATI";   rating_emoji = "⚠️"
    else:
        rating = "poor";      rating_text = "🔴 SATURI";    rating_emoji = "❌"

    return {
        "score": score, "rating": rating,
        "rating_text": rating_text, "rating_emoji": rating_emoji,
        "reasons": reasons,
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


def calculate_soil_dryness(history_daily):
    """
    Calcola l'indice di asciugatura del terreno dagli ultimi 14 giorni.

    Logica:
    - Conta i giorni consecutivi (partendo da ieri) con pioggia < 2mm (soglia "asciutto")
    - Calcola anche la pioggia totale degli ultimi 7 e 14 giorni
    - Restituisce un rating: dry / damp / wet / saturated

    La soglia 2mm è conservativa per terreno MTB: sotto quel valore
    la pioggia evapora in giornata senza saturare il suolo.
    """
    precip = history_daily.get("daily", {}).get("precipitation_sum", [])
    dates  = history_daily.get("daily", {}).get("time", [])

    if not precip:
        return None

    # Giorni secchi consecutivi (da ieri a ritroso)
    dry_days = 0
    for p in reversed(precip):
        if p is None or p < 2.0:
            dry_days += 1
        else:
            break

    rain_7d  = sum(p for p in precip[-7:]  if p is not None)

    # Rating terreno basato sulla pioggia degli ultimi 7 giorni
    if rain_7d < 5:
        rating = "dry";       label = "Asciutto";  color = "#27ae60"
    elif rain_7d < 15:
        rating = "damp";      label = "Umido";     color = "#f7b733"
    elif rain_7d < 35:
        rating = "wet";       label = "Bagnato";   color = "#e67e22"
    else:
        rating = "saturated"; label = "Saturo";    color = "#e74c3c"

    # Storico giornaliero formattato per il grafico frontend
    history_chart = []
    for i, (d, p) in enumerate(zip(dates, precip)):
        try:
            dt = datetime.fromisoformat(d)
            history_chart.append({
                "date":  dt.strftime("%d/%m"),
                "day":   dt.strftime("%a").replace("Mon","Lun").replace("Tue","Mar")
                             .replace("Wed","Mer").replace("Thu","Gio").replace("Fri","Ven")
                             .replace("Sat","Sab").replace("Sun","Dom"),
                "precip": round(p, 1) if p is not None else 0,
                "temp_max": round(history_daily["daily"].get("temperature_2m_max", [None]*len(dates))[i] or 0, 1),
                "temp_min": round(history_daily["daily"].get("temperature_2m_min", [None]*len(dates))[i] or 0, 1),
            })
        except Exception:
            continue

    return {
        "dry_days": dry_days,
        "rain_7d":  round(rain_7d, 1),
        "rating":   rating,
        "label":    label,
        "color":    color,
        "history":  history_chart,
    }


def calculate_soil_dryness_5d(history_daily):
    """
    Variante 5 giorni di calculate_soil_dryness.
    Usa precip[-5:] invece di precip[-7:] per il calcolo SMI e rating.
    """
    precip = history_daily.get("daily", {}).get("precipitation_sum", [])
    dates  = history_daily.get("daily", {}).get("time", [])

    if not precip:
        return None

    dry_days = 0
    for p in reversed(precip):
        if p is None or p < 2.0:
            dry_days += 1
        else:
            break

    rain_5d = sum(p for p in precip[-5:] if p is not None)

    if rain_5d < 4:
        rating = "dry";       label = "Asciutto";  color = "#27ae60"
    elif rain_5d < 12:
        rating = "damp";      label = "Umido";     color = "#f7b733"
    elif rain_5d < 28:
        rating = "wet";       label = "Bagnato";   color = "#e67e22"
    else:
        rating = "saturated"; label = "Saturo";    color = "#e74c3c"

    history_chart = []
    for i, (d, p) in enumerate(zip(dates, precip)):
        try:
            dt = datetime.fromisoformat(d)
            history_chart.append({
                "date":  dt.strftime("%d/%m"),
                "day":   dt.strftime("%a").replace("Mon","Lun").replace("Tue","Mar")
                             .replace("Wed","Mer").replace("Thu","Gio").replace("Fri","Ven")
                             .replace("Sat","Sab").replace("Sun","Dom"),
                "precip": round(p, 1) if p is not None else 0,
                "temp_max": round(history_daily["daily"].get("temperature_2m_max", [None]*len(dates))[i] or 0, 1),
                "temp_min": round(history_daily["daily"].get("temperature_2m_min", [None]*len(dates))[i] or 0, 1),
            })
        except Exception:
            continue

    return {
        "dry_days": dry_days,
        "rain_7d":  round(rain_5d, 1),  # chiave mantenuta per compatibilità template
        "rating":   rating,
        "label":    label,
        "color":    color,
        "history":  history_chart,
    }


async def calculate_zone_matrix_5d(hourly_forecast: dict) -> list:
    """
    Variante 5 giorni di calculate_zone_matrix.
    Usa storico 5gg e calculate_soil_dryness_5d per SMI e proiezioni.
    """
    from datetime import datetime, timedelta

    now = datetime.now()

    daily_forecast_precip = {}
    for i, t in enumerate(hourly_forecast.get("time", [])):
        try:
            dt  = datetime.fromisoformat(t)
            day = dt.date()
            p   = hourly_forecast["precipitation"][i] if i < len(hourly_forecast.get("precipitation", [])) else 0
            daily_forecast_precip[day] = daily_forecast_precip.get(day, 0) + (p or 0)
        except Exception:
            continue

    matrix = []
    for zone_key, geo in ZONE_GEOLOGY.items():
        try:
            history = await cached_fetch_weather_history(geo["lat"], geo["lon"], 5, fetch_weather_history)
            soil    = calculate_soil_dryness_5d(history)
        except Exception:
            soil = None

        rain_5d   = soil["rain_7d"] if soil else 0  # chiave mantenuta per compatibilità
        dry_days  = soil["dry_days"] if soil else 0
        smi_now   = calculate_smi(rain_5d, geo["field_capacity"])
        rec_days  = estimate_recovery_days(smi_now, geo["drainage_rate"])

        days_out = []
        for offset in range(3):
            day    = (now + timedelta(days=offset)).date()
            rain_f = round(daily_forecast_precip.get(day, 0), 1)

            smi_proj = smi_now
            for d in range(offset):
                prev_day  = (now + timedelta(days=d)).date()
                prev_rain = daily_forecast_precip.get(prev_day, 0)
                if prev_rain < 2:
                    smi_proj = max(0, smi_proj - geo["drainage_rate"] * 0.15)
                elif prev_rain > 10:
                    smi_proj = min(2.0, smi_proj + 0.2)

            gng = gonogo(smi_proj, rain_f, dry_days + offset)

            if offset == 0:   label = "Oggi"
            elif offset == 1: label = "Domani"
            else:             label = "Dopodomani"

            days_out.append({
                "label":  label,
                "date":   day.isoformat(),
                "smi":    round(smi_proj, 2),
                "rain_f": rain_f,
                **gng,
            })

        if smi_now > 1.2:    terrain_label, terrain_emoji = "Saturo",   "🔴"
        elif smi_now > 0.8:  terrain_label, terrain_emoji = "Bagnato",  "🟠"
        elif smi_now > 0.5:  terrain_label, terrain_emoji = "Umido",    "🟡"
        else:                terrain_label, terrain_emoji = "Asciutto", "🟢"

        matrix.append({
            "key":            zone_key,
            "name":           geo["name"],
            "elevation":      geo["elevation"],
            "geology":        geo["geology"],
            "geology_detail": geo["geology_detail"],
            "field_capacity": geo["field_capacity"],
            "drainage_rate":  geo["drainage_rate"],
            "rain_7d":        rain_5d,
            "dry_days":       dry_days,
            "smi":            smi_now,
            "rec_days":       rec_days,
            "terrain_label":  terrain_label,
            "terrain_emoji":  terrain_emoji,
            "days":           days_out,
        })

    return matrix


def adjust_windows_for_soil(windows, soil_dryness, hourly_data=None):
    """
    Abbassa il rating delle finestre in base allo stato del terreno,
    con recupero progressivo giorno per giorno se non piove.

    Logica:
      - Giorno 1: applica il rating del terreno attuale invariato
      - Giorno 2: se nelle 24h precedenti la previsione mostra <2mm totali
                  il terreno recupera un livello (saturo→bagnato, bagnato→umido)
      - Giorno 3: stesso meccanismo sulle 24h ulteriori

    Livelli terreno: saturated > wet > damp > dry
    Cap sul rating meteo:
      saturated → max poor  (🔴)
      wet       → max good  (🟡)
      damp      → invariato
    """
    if not soil_dryness or not windows:
        return windows

    base_rating = soil_dryness.get("rating")
    if base_rating not in ("saturated", "wet"):
        return windows

    # Costruisce un dizionario {date: precip_totale} dalle previsioni orarie
    daily_forecast_precip = {}
    if hourly_data:
        for i, time_str in enumerate(hourly_data.get("time", [])):
            try:
                dt = datetime.fromisoformat(time_str)
                day = dt.date()
                p   = hourly_data["precipitation"][i] if i < len(hourly_data.get("precipitation", [])) else 0
                daily_forecast_precip[day] = daily_forecast_precip.get(day, 0) + (p or 0)
            except Exception:
                continue

    # Progressione: per ogni giorno successivo al primo,
    # se la giornata precedente ha <2mm previsti → il terreno recupera un livello
    LEVELS = ["saturated", "wet", "damp", "dry"]

    adjusted = []
    for idx, w in enumerate(windows):
        w = dict(w)
        effective_rating = base_rating

        if idx > 0 and daily_forecast_precip:
            # Calcola la data del giorno corrente dalla finestra
            try:
                window_date = datetime.strptime(w["date"], "%d %b").replace(year=datetime.now().year).date()
            except Exception:
                window_date = None

            # Per ogni giorno passato tra oggi e questa finestra, controlla se è stato secco
            now_date = datetime.now().date()
            if window_date:
                level_idx = LEVELS.index(effective_rating)
                check_date = now_date + timedelta(days=1)  # parto da domani
                while check_date < window_date and level_idx < len(LEVELS) - 1:
                    precip_day = daily_forecast_precip.get(check_date, 0)
                    if precip_day < 2.0:
                        level_idx += 1  # recupera un livello
                    check_date += timedelta(days=1)
                effective_rating = LEVELS[level_idx]

        # Applica il cap in base al rating effettivo del terreno
        if effective_rating == "saturated":
            w["rating"]      = "poor"
            w["rating_icon"] = "🔴"
        elif effective_rating == "wet":
            # wet → sempre medium (arancione), indipendentemente dal meteo
            w["rating"]      = "medium"
            w["rating_icon"] = "🟠"
        elif effective_rating == "damp":
            if w["rating"] == "excellent":
                w["rating"]      = "good"
                w["rating_icon"] = "🟡"
        # dry → invariato

        adjusted.append(w)
    return adjusted


def project_soil_forecast(soil_dryness, hourly_data, riding_windows):
    """
    Proietta il rating del terreno per i prossimi 3 giorni,
    combinando lo stato attuale con le precipitazioni previste.

    Logica recupero:
      - Se il giorno prevede <2mm totali → terreno recupera un livello
      - Se ≥2mm → rimane al livello attuale (o peggiora se >10mm)

    Livelli: saturated > wet > damp > dry
    """
    if not soil_dryness or not hourly_data:
        return []

    LEVELS = ["saturated", "wet", "damp", "dry"]
    LABELS = {
        "saturated": ("poor",      "🔴", "Saturo"),
        "wet":       ("good",      "🟠", "Bagnato"),
        "damp":      ("good",      "🟡", "Umido"),
        "dry":       ("excellent", "🟢", "Asciutto"),
    }

    # Precipitazioni previste per giorno
    daily_precip = {}
    for i, time_str in enumerate(hourly_data.get("time", [])):
        try:
            dt  = datetime.fromisoformat(time_str)
            day = dt.date()
            p   = hourly_data["precipitation"][i] if i < len(hourly_data.get("precipitation", [])) else 0
            daily_precip[day] = daily_precip.get(day, 0) + (p or 0)
        except Exception:
            continue

    # Mappa finestre per data (per mostrare orario consigliato)
    windows_by_day = {}
    for w in (riding_windows or []):
        try:
            d = datetime.strptime(w["date"], "%d %b").replace(year=datetime.now().year).date()
            windows_by_day[d] = w
        except Exception:
            pass

    base_rating = soil_dryness.get("rating", "dry")
    level_idx   = LEVELS.index(base_rating)

    now      = datetime.now()
    forecast = []

    for offset in range(3):
        day = (now + timedelta(days=offset)).date()

        # Calcola il rating effettivo per questo giorno
        # (applica recupero progressivo dai giorni precedenti)
        effective_idx = level_idx
        for d_off in range(offset):
            prev_day   = (now + timedelta(days=d_off)).date()
            prev_precip = daily_precip.get(prev_day, 0)
            if prev_precip < 2.0 and effective_idx < len(LEVELS) - 1:
                effective_idx += 1
            elif prev_precip > 10.0 and effective_idx > 0:
                effective_idx -= 1

        effective_rating = LEVELS[effective_idx]
        css, icon, label = LABELS[effective_rating]

        # Nome giorno
        if offset == 0:
            day_name = "Oggi"
        elif offset == 1:
            day_name = "Domani"
        else:
            giorni = {"Mon":"Lun","Tue":"Mar","Wed":"Mer","Thu":"Gio","Fri":"Ven","Sat":"Sab","Sun":"Dom"}
            day_name = day.strftime("%a %d %b")
            for en, it in giorni.items():
                day_name = day_name.replace(en, it)

        precip_day = daily_precip.get(day, 0)
        window     = windows_by_day.get(day)

        forecast.append({
            "day":    day_name,
            "date":   day.strftime("%d %b"),
            "rating": css,
            "icon":   icon,
            "label":  label,
            "precip": round(precip_day, 1),
            "window": (f"{window['start_time']}-{window['end_time']} ({window['duration']}h)") if window else None,
            "temp":   round(window["temp"]) if window else None,
            "wind":   round(window["wind"]) if window else None,
        })

    return forecast


# ─── Parametri geologici per zona ─────────────────────────────────────────────
# field_capacity: mm pioggia che il terreno può contenere prima di saturarsi
# drainage_rate:  livelli di recupero per giorno senza pioggia (1.0 = standard)
# Dati derivati da letteratura scientifica su suoli vulcanici laziali
ZONE_GEOLOGY = {
    "monte_cavo": {
        "name":           "Monte Cavo",
        "elevation":      949,
        "lat":            41.7517, "lon": 12.7100,
        "geology":        "Tufo + pozzolana",
        "geology_detail": "Tufo litoide in cima, pozzolana grigia sui versanti. Drenaggio rapido in superficie ma lento in profondità.",
        "field_capacity": 45,   # mm
        "drainage_rate":  1.4,  # recupera 1.4x più veloce della media
    },
    "faete": {
        "name":           "Maschio delle Faete",
        "elevation":      956,
        "lat":            41.7569, "lon": 12.7442,
        "geology":        "Tufo compatto",
        "geology_detail": "Tufo stratificato compatto. Drenaggio mediocre, trattiene umidità a lungo.",
        "field_capacity": 40,
        "drainage_rate":  0.9,
    },
    "colle_jano": {
        "name":           "Colle Jano",
        "elevation":      938,
        "lat":            41.7570, "lon": 12.7260,
        "geology":        "Peperino + argilla",
        "geology_detail": "Peperino compatto con strati argillosi. Drenaggio molto lento, alta ritenzione idrica.",
        "field_capacity": 35,
        "drainage_rate":  0.6,
    },
    "ariano": {
        "name":           "Maschio d'Ariano",
        "elevation":      891,
        "lat":            41.7394, "lon": 12.7908,
        "geology":        "Pozzolana + lapilli",
        "geology_detail": "Pozzolana sciolta con lapilli vulcanici. Drenaggio eccellente, asciuga velocemente.",
        "field_capacity": 52,
        "drainage_rate":  1.6,
    },
    "artemisio": {
        "name":           "Maschio d'Artemisio",
        "elevation":      812,
        "lat":            41.7122, "lon": 12.7534,
        "geology":        "Tufo + pozzolana",
        "geology_detail": "Alternanza tufo/pozzolana. Comportamento intermedio, buon drenaggio sui crinali.",
        "field_capacity": 45,
        "drainage_rate":  1.3,
    },
    "fontana_tempesta": {
        "name":           "Fontana Tempesta",
        "elevation":      560,
        "lat":            41.7350, "lon": 12.7120,
        "geology":        "Misto vulcanico",
        "geology_detail": "Depositi misti: tufo, pozzolana e terre rosse. Drenaggio variabile per zona.",
        "field_capacity": 42,
        "drainage_rate":  1.0,
    },
}



def nearest_zone(lat: float, lon: float) -> dict:
    """
    Restituisce il dizionario ZONE_GEOLOGY più vicino alle coordinate date.
    Distanza euclidea semplice (sufficiente per zone ravvicinate come i Castelli).
    """
    best_key, best_dist = None, float("inf")
    for key, geo in ZONE_GEOLOGY.items():
        dist = (lat - geo["lat"]) ** 2 + (lon - geo["lon"]) ** 2
        if dist < best_dist:
            best_dist, best_key = dist, key
    return ZONE_GEOLOGY[best_key]


def project_soil_forecast_smi(rain_5d: float, zone: dict, hourly_data: dict, riding_windows: list) -> list:
    """
    Proiezione 3 giorni usando SMI + gonogo() con parametri geologici della zona.
    Allineato alla logica della matrice Go/NoGo — nessuna soglia flat.

    Per ogni giorno:
      - SMI proiettato: diminuisce di drainage_rate*0.15 per ogni giorno <2mm
      - gonogo() decide il label in base a SMI proiettato + pioggia prevista
    """
    from datetime import datetime, timedelta

    field_capacity = zone["field_capacity"]
    drainage_rate  = zone["drainage_rate"]

    # Precipitazioni previste per giorno
    daily_precip = {}
    for i, time_str in enumerate(hourly_data.get("time", [])):
        try:
            dt  = datetime.fromisoformat(time_str)
            day = dt.date()
            p   = hourly_data["precipitation"][i] if i < len(hourly_data.get("precipitation", [])) else 0
            daily_precip[day] = daily_precip.get(day, 0) + (p or 0)
        except Exception:
            continue

    # Mappa finestre per data
    windows_by_day = {}
    for w in (riding_windows or []):
        try:
            d = datetime.strptime(w["date"], "%d %b").replace(year=datetime.now().year).date()
            windows_by_day[d] = w
        except Exception:
            pass

    smi_now = calculate_smi(rain_5d, field_capacity)
    now     = datetime.now()
    forecast = []

    for offset in range(3):
        day = (now + timedelta(days=offset)).date()

        # Proietta SMI applicando recupero/peggioramento dei giorni precedenti
        smi_proj = smi_now
        for d in range(offset):
            prev_day  = (now + timedelta(days=d)).date()
            prev_rain = daily_precip.get(prev_day, 0)
            if prev_rain < 2:
                smi_proj = max(0, smi_proj - drainage_rate * 0.15)
            elif prev_rain > 10:
                smi_proj = min(2.0, smi_proj + 0.2)

        rain_f = round(daily_precip.get(day, 0), 1)
        gng    = gonogo(smi_proj, rain_f, 0)

        if offset == 0:   day_name = "Oggi"
        elif offset == 1: day_name = "Domani"
        else:
            giorni = {"Mon":"Lun","Tue":"Mar","Wed":"Mer","Thu":"Gio","Fri":"Ven","Sat":"Sab","Sun":"Dom"}
            day_name = day.strftime("%a %d %b")
            for en, it in giorni.items():
                day_name = day_name.replace(en, it)

        window = windows_by_day.get(day)
        forecast.append({
            "day":    day_name,
            "date":   day.strftime("%d %b"),
            "rating": "poor" if gng["status"] == "nogo" else ("good" if gng["status"] == "caution" else "excellent"),
            "icon":   gng["emoji"],
            "label":  gng["label"],
            "smi":    round(smi_proj, 2),
            "precip": rain_f,
            "window": (f"{window['start_time']}-{window['end_time']} ({window['duration']}h)") if window else None,
            "temp":   round(window["temp"]) if window and window.get("temp") is not None else None,
            "wind":   round(window["wind"]) if window and window.get("wind") is not None else None,
        })

    return forecast

def calculate_smi(rain_7d: float, field_capacity: float) -> float:
    """Soil Moisture Index: rapporto pioggia/capacità di campo. >1 = saturo."""
    if field_capacity <= 0:
        return 0.0
    return round(rain_7d / field_capacity, 2)


def estimate_recovery_days(smi: float, drainage_rate: float) -> int:
    """
    Stima giorni al recupero (SMI < 0.5 = Go sicuro).
    Con pioggia prevista = 0 e temperatura positiva.
    """
    if smi <= 0.5:
        return 0
    # Ogni giorno senza pioggia il terreno recupera ~drainage_rate * 0.15 di SMI
    daily_recovery = drainage_rate * 0.15
    days = 0
    current = smi
    while current > 0.5 and days < 30:
        current -= daily_recovery
        days += 1
    return days


def gonogo(smi: float, rain_forecast_mm: float, dry_days: int) -> dict:
    """
    Calcola Go/Caution/NoGo per una zona+giorno.
    
    Regole:
      NoGo    : SMI > 1.2  O  pioggia_prevista > 5mm
      Caution : SMI 0.8-1.2 O  pioggia_prevista 2-5mm
      Go      : SMI < 0.8  E  pioggia_prevista < 2mm
    """
    if smi > 1.2 or rain_forecast_mm > 5:
        return {"status": "nogo",    "label": "Saturo",     "emoji": "🔴", "color": "#e74c3c"}
    elif smi > 0.8 or rain_forecast_mm > 2:
        return {"status": "caution", "label": "Fangoso",    "emoji": "🟠", "color": "#e67e22"}
    elif smi > 0.5:
        return {"status": "caution", "label": "Umido",      "emoji": "🟡", "color": "#f7b733"}
    else:
        return {"status": "go",      "label": "Praticabile","emoji": "🟢", "color": "#27ae60"}


async def calculate_zone_matrix(hourly_forecast: dict) -> list:
    """
    Per ogni zona: recupera storico 7gg, calcola SMI, proietta Go/NoGo per 3 giorni.
    """
    from datetime import datetime, timedelta

    now = datetime.now()

    # Precipitazioni previste per i prossimi 3 giorni dall'hourly forecast
    daily_forecast_precip = {}
    for i, t in enumerate(hourly_forecast.get("time", [])):
        try:
            dt  = datetime.fromisoformat(t)
            day = dt.date()
            p   = hourly_forecast["precipitation"][i] if i < len(hourly_forecast.get("precipitation", [])) else 0
            daily_forecast_precip[day] = daily_forecast_precip.get(day, 0) + (p or 0)
        except Exception:
            continue

    LEVELS = ["saturated", "wet", "damp", "dry"]

    matrix = []
    for zone_key, geo in ZONE_GEOLOGY.items():
        try:
            history = await cached_fetch_weather_history(geo["lat"], geo["lon"], 7, fetch_weather_history)
            soil    = calculate_soil_dryness(history)
        except Exception:
            soil = None

        rain_7d   = soil["rain_7d"] if soil else 0
        dry_days  = soil["dry_days"] if soil else 0
        smi_now   = calculate_smi(rain_7d, geo["field_capacity"])
        rec_days  = estimate_recovery_days(smi_now, geo["drainage_rate"])

        # Go/NoGo per oggi, domani, +2gg
        days_out = []
        for offset in range(3):
            day      = (now + timedelta(days=offset)).date()
            rain_f   = round(daily_forecast_precip.get(day, 0), 1)

            # SMI proiettato: migliora di drainage_rate*0.15 per ogni giorno senza pioggia
            smi_proj = smi_now
            for d in range(offset):
                prev_day = (now + timedelta(days=d)).date()
                prev_rain = daily_forecast_precip.get(prev_day, 0)
                if prev_rain < 2:
                    smi_proj = max(0, smi_proj - geo["drainage_rate"] * 0.15)
                elif prev_rain > 10:
                    smi_proj = min(2.0, smi_proj + 0.2)

            gng = gonogo(smi_proj, rain_f, dry_days + offset)

            if offset == 0:   label = "Oggi"
            elif offset == 1: label = "Domani"
            else:             label = "Dopodomani" 

            days_out.append({
                "label":    label,
                "date":     day.isoformat(),
                "smi":      round(smi_proj, 2),
                "rain_f":   rain_f,
                **gng,
            })

        # Rating terreno attuale (testo)
        if smi_now > 1.2:    terrain_label, terrain_emoji = "Saturo",   "🔴"
        elif smi_now > 0.8:  terrain_label, terrain_emoji = "Bagnato",  "🟠"
        elif smi_now > 0.5:  terrain_label, terrain_emoji = "Umido",    "🟡"
        else:                terrain_label, terrain_emoji = "Asciutto", "🟢"

        matrix.append({
            "key":            zone_key,
            "name":           geo["name"],
            "elevation":      geo["elevation"],
            "geology":        geo["geology"],
            "geology_detail": geo["geology_detail"],
            "field_capacity": geo["field_capacity"],
            "drainage_rate":  geo["drainage_rate"],
            "rain_7d":        rain_7d,
            "dry_days":       dry_days,
            "smi":            smi_now,
            "rec_days":       rec_days,
            "terrain_label":  terrain_label,
            "terrain_emoji":  terrain_emoji,
            "days":           days_out,
        })

    return matrix

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
    data = await cached_fetch_weather(loc["lat"], loc["lon"], fetch_weather)
    return {"location": loc["name"], "elevation": loc["elevation"], "hourly": data["hourly"]}

@app.get("/dashboard/{location}", response_class=HTMLResponse)
async def dashboard(request: Request, location: str):
    if location not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")
    loc    = LOCATIONS[location]
    data   = await cached_fetch_weather(loc["lat"], loc["lon"], fetch_weather)
    hourly = data["hourly"]
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "location_name": loc["name"],
        "hourly": {k: hourly.get(k, []) for k in ["time","temperature_2m","precipitation","weather_code","windspeed_10m","windgusts_10m"]},
    })

async def _fetch_all_locations():
    """
    Fetch meteo forecast + storico per tutte le LOCATIONS in parallelo.
    Restituisce (all_data, soil_dryness_first).
    """
    import asyncio
    loc_items = list(LOCATIONS.items())

    # Fetch forecast e history in parallelo per tutte le zone
    forecast_tasks = [cached_fetch_weather(li["lat"], li["lon"], fetch_weather)      for _, li in loc_items]
    history_tasks  = [cached_fetch_weather_history(li["lat"], li["lon"], 5, fetch_weather_history) for _, li in loc_items]

    forecasts, histories = await asyncio.gather(
        asyncio.gather(*forecast_tasks,  return_exceptions=True),
        asyncio.gather(*history_tasks,   return_exceptions=True),
    )

    all_data     = []
    soil_dryness = None

    for (loc_key, loc_info), forecast, history in zip(loc_items, forecasts, histories):
        if isinstance(forecast, Exception):
            print(f"⚠️ Forecast non disponibile per {loc_info['name']}: {forecast}")
            continue
        hourly = forecast["hourly"]

        loc_soil = None
        if not isinstance(history, Exception):
            try:
                loc_soil = calculate_soil_dryness_5d(history)
                if soil_dryness is None:
                    soil_dryness = loc_soil
            except Exception as e:
                print(f"⚠️ Storico non calcolabile per {loc_info['name']}: {e}")
        else:
            print(f"⚠️ Storico non disponibile per {loc_info['name']}: {history}")

        all_data.append({
            "name":        loc_info["name"],
            "elevation":   loc_info["elevation"],
            "hourly":      {k: hourly.get(k, []) for k in ["time","temperature_2m","precipitation","weather_code","windspeed_10m","windgusts_10m"]},
            "soil_dryness": loc_soil,
        })

    return all_data, soil_dryness


@app.get("/dashboard-completa", response_class=HTMLResponse)
async def dashboard_completa(request: Request):
    visit_stats = increment_visit(page="dashboard")

    all_data, soil_dryness = await _fetch_all_locations()

    first_hourly             = all_data[0]["hourly"] if all_data else {}
    overall_trail_conditions = calculate_trail_conditions(first_hourly)
    overall_riding_windows   = find_best_riding_windows(first_hourly)
    overall_riding_windows   = adjust_windows_for_soil(overall_riding_windows, soil_dryness, first_hourly)

    current_conditions = calculate_current_conditions(soil_dryness)
    soil_forecast      = project_soil_forecast(soil_dryness, first_hourly, overall_riding_windows)

    try:
        matrix = await calculate_zone_matrix_5d(first_hourly)
    except Exception as e:
        print(f"⚠️ Matrice terreno non disponibile: {e}")
        matrix = []

    return templates.TemplateResponse("dashboard_completa.html", {
        "request": request,
        "locations_data":      all_data,
        "trail_conditions":    overall_trail_conditions,
        "current_conditions":  current_conditions,
        "soil_forecast":       soil_forecast,
        "riding_windows":      overall_riding_windows,
        "soil_dryness":        soil_dryness,
        "visit_stats":         visit_stats,
        "matrix":              matrix,
    })


@app.get("/admin/home-test", response_class=HTMLResponse)
async def home_test(request: Request):
    all_data    = []
    overall_trail_conditions = None
    overall_riding_windows   = None
    soil_dryness = None

    for loc_key, loc_info in LOCATIONS.items():
        data   = await cached_fetch_weather(loc_info["lat"], loc_info["lon"], fetch_weather)
        hourly = data["hourly"]
        if overall_trail_conditions is None:
            overall_trail_conditions = calculate_trail_conditions(hourly)
            overall_riding_windows   = find_best_riding_windows(hourly)

        loc_soil_dryness = None
        try:
            history = await cached_fetch_weather_history(loc_info["lat"], loc_info["lon"], 5, fetch_weather_history)
            loc_soil_dryness = calculate_soil_dryness_5d(history)
            if soil_dryness is None:
                soil_dryness = loc_soil_dryness
                overall_riding_windows = adjust_windows_for_soil(overall_riding_windows, soil_dryness, hourly)
        except Exception as e:
            print(f"⚠️ Storico meteo non disponibile per {loc_info['name']}: {e}")

        all_data.append({
            "name": loc_info["name"], "elevation": loc_info["elevation"],
            "hourly": {k: hourly.get(k, []) for k in ["time","temperature_2m","precipitation","weather_code","windspeed_10m","windgusts_10m"]},
            "soil_dryness": loc_soil_dryness,
        })

    current_conditions = calculate_current_conditions(soil_dryness)
    soil_forecast      = project_soil_forecast(soil_dryness,
                             all_data[0]["hourly"] if all_data else None,
                             overall_riding_windows)

    try:
        matrix = await calculate_zone_matrix_5d(all_data[0]["hourly"] if all_data else {})
    except Exception as e:
        print(f"⚠️ Matrice terreno non disponibile: {e}")
        matrix = []

    return templates.TemplateResponse("admin/home-test.html", {
        "request":          request,
        "locations_data":   all_data,
        "trail_conditions": overall_trail_conditions,
        "current_conditions": current_conditions,
        "soil_forecast":    soil_forecast,
        "riding_windows":   overall_riding_windows,
        "soil_dryness":     soil_dryness,
        "visit_stats":      {"today": 0, "total": 0},
        "matrix":           matrix,
    })





@app.get("/metodologia", response_class=HTMLResponse)
async def metodologia(request: Request):
    """Pagina di spiegazione della metodologia."""
    return templates.TemplateResponse("metodologia.html", {"request": request})

@app.get("/terreno", response_class=HTMLResponse)
async def terreno(request: Request):
    """Pagina principale: matrice Go/NoGo per zona."""
    # Prendi hourly forecast dalla prima location per le precipitazioni previste
    first_loc = list(LOCATIONS.values())[0]
    data    = await cached_fetch_weather(first_loc["lat"], first_loc["lon"], fetch_weather)
    hourly  = data["hourly"]
    matrix  = await calculate_zone_matrix(hourly)
    reports = get_active_reports()
    return templates.TemplateResponse("terreno.html", {
        "request": request,
        "matrix":  matrix,
        "reports": reports,
        "updated": datetime.now().strftime("%d/%m/%Y %H:%M"),
    })

@app.get("/sim-report", response_class=HTMLResponse)
async def sim_report():
    """Pagina di simulazione segnalazione GPS — solo per test."""
    with open("templates/admin/sim_report.html", "r") as f:
        return f.read()


ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "mtbadmin")

@app.get("/admin/segnalazioni", response_class=HTMLResponse)
async def admin_segnalazioni(request: Request, pwd: str = ""):
    """Pagina admin per gestire le segnalazioni."""
    if pwd != ADMIN_PASSWORD:
        return HTMLResponse("""
        <!DOCTYPE html><html><head><meta charset="UTF-8">
        <title>Admin — Login</title>
        <style>
          body{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;
               height:100vh;margin:0;background:#ecf0f1;}
          .box{background:white;padding:30px;border-radius:12px;box-shadow:0 4px 20px rgba(0,0,0,0.1);
               text-align:center;min-width:300px;}
          h2{color:#2c3e50;margin-bottom:20px;}
          input{width:100%;padding:10px;border-radius:8px;border:1px solid #ddd;
                font-size:14px;box-sizing:border-box;margin-bottom:12px;}
          button{width:100%;padding:10px;background:#2c3e50;color:white;border:none;
                 border-radius:8px;font-size:14px;font-weight:bold;cursor:pointer;}
          button:hover{background:#34495e;}
        </style></head><body>
        <div class="box">
          <h2>🔐 Admin Segnalazioni</h2>
          <form method="get">
            <input type="password" name="pwd" placeholder="Password admin" autofocus>
            <button type="submit">Accedi</button>
          </form>
        </div></body></html>
        """, status_code=401)

    reports = get_active_reports()
    return templates.TemplateResponse("admin/admin_segnalazioni.html", {
        "request": request,
        "reports": reports,
        "pwd":     pwd,
    })

@app.post("/admin/elimina/{report_id}")
async def admin_elimina(report_id: str, pwd: str = ""):
    """Elimina una segnalazione dal DB."""
    if pwd != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Non autorizzato")
    ok = delete_report(report_id)
    return {"ok": ok}


@app.get("/admin/cache", response_class=HTMLResponse)
async def admin_cache(request: Request, pwd: str = ""):
    """Pagina admin per monitorare e invalidare la cache Redis."""
    if pwd != ADMIN_PASSWORD:
        return HTMLResponse("<p>Non autorizzato</p>", status_code=401)
    status = get_cache_status()
    keys_html = ""
    for k in status.get("keys", []):
        ttl = k["ttl_seconds"]
        mins = ttl // 60 if ttl > 0 else 0
        color = "#27ae60" if ttl > 600 else ("#f7b733" if ttl > 0 else "#e74c3c")
        keys_html += f"""<tr>
          <td style="font-family:monospace;font-size:12px">{k['key']}</td>
          <td style="color:{color};font-weight:bold">{mins}m {ttl % 60}s</td>
        </tr>"""
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
    <title>Admin — Cache</title>
    <style>body{{font-family:Arial,sans-serif;padding:20px;background:#f0f2f5}}
    h2{{color:#2c3e50}} table{{background:white;border-radius:8px;padding:16px;
    box-shadow:0 2px 8px rgba(0,0,0,0.08);border-collapse:collapse;width:100%}}
    th{{background:#2c3e50;color:white;padding:8px 12px;text-align:left}}
    td{{padding:8px 12px;border-bottom:1px solid #ecf0f1}}
    .btn{{display:inline-block;margin:8px 4px;padding:8px 16px;border-radius:6px;
    background:#e74c3c;color:white;text-decoration:none;font-size:13px}}</style>
    </head><body>
    <h2>🗄️ Cache Redis — stato attuale</h2>
    <p style="color:#7f8c8d;font-size:13px">Aggiornato: {status['timestamp']}</p>
    <table><thead><tr><th>Chiave</th><th>TTL residuo</th></tr></thead>
    <tbody>{keys_html}</tbody></table>
    <br>
    <a class="btn" href="/admin/cache/invalidate?pwd={pwd}&target=weather">🌐 Invalida cache meteo</a>
    <a class="btn" href="/admin/cache/invalidate?pwd={pwd}&target=strava">⭐ Invalida cache Strava</a>
    <a class="btn" href="/admin/cache/invalidate?pwd={pwd}&target=all" style="background:#c0392b">🗑️ Invalida tutto</a>
    <br><br><a href="/admin/segnalazioni?pwd={pwd}" style="color:#3498db">← Torna alle segnalazioni</a>
    </body></html>""")


@app.get("/admin/cache/invalidate")
async def admin_cache_invalidate(pwd: str = "", target: str = "all"):
    """Invalida manualmente la cache Redis."""
    if pwd != ADMIN_PASSWORD:
        raise HTTPException(status_code=403, detail="Non autorizzato")
    import httpx as _httpx
    from cache import UPSTASH_URL, _headers
    deleted = []
    if target in ("weather", "all"):
        try:
            r = _httpx.get(f"{UPSTASH_URL}/keys/wx:*", headers=_headers(), timeout=5.0)
            keys = r.json().get("result", [])
            for k in keys:
                _httpx.get(f"{UPSTASH_URL}/del/{k}", headers=_headers(), timeout=3.0)
                deleted.append(k)
        except Exception as e:
            print(f"⚠️ Errore invalidazione cache meteo: {e}")
    if target in ("strava", "all"):
        from cache import invalidate_strava_cache
        invalidate_strava_cache()
        deleted.append("strava:starred_segments")
    return {"ok": True, "invalidated": deleted}

@app.post("/segnala")
async def segnala(request: Request):
    """Salva una segnalazione con posizione GPS su Upstash Redis."""
    try:
        body = await request.json()
        lat  = float(body.get("lat", 0))
        lon  = float(body.get("lon", 0))
        kind = body.get("kind", "")
        desc = body.get("description", "")

        if not lat or not lon or not kind:
            return {"ok": False, "error": "Dati mancanti"}

        report = save_report(lat, lon, kind, desc)
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/avvisi", response_class=HTMLResponse)
async def avvisi(request: Request):
    increment_visit(page="avvisi")
    alerts    = await get_all_alerts()
    feedbacks = await fetch_form_feedbacks()
    reports   = get_active_reports()
    return templates.TemplateResponse("avvisi.html", {
        "request":   request,
        "alerts":    alerts,
        "feedbacks": feedbacks,
        "reports":   reports,
    })

@app.get("/percorsi", response_class=HTMLResponse)
async def percorsi(request: Request):
    """Mappa percorsi GPX con meteo calcolato dal centroide del tracciato + dati Strava"""
    increment_visit(page="percorsi")

    # Calcola coordinate centroide per ogni GPX + zona geologica più vicina
    gpx_with_coords = []
    for gpx in GPX_FILES:
        lat, lon = get_gpx_centroid(gpx["file"])
        if lat is None:
            lat, lon = 41.745, 12.720
        zone = nearest_zone(lat, lon)
        coords = get_gpx_coords(gpx["file"])
        gpx_with_coords.append({**gpx, "lat": lat, "lon": lon, "zone": zone, "coords": coords})

    # Fetch meteo + storico per ogni GPX in PARALLELO
    import asyncio
    weather_results = await asyncio.gather(*[
        cached_fetch_weather(g["lat"], g["lon"], fetch_weather)
        for g in gpx_with_coords
    ], return_exceptions=True)

    history_results = await asyncio.gather(*[
        cached_fetch_weather_history(g["zone"]["lat"], g["zone"]["lon"], 5, fetch_weather_history)
        for g in gpx_with_coords
    ], return_exceptions=True)

    gpx_forecasts = []
    for gpx, weather, history in zip(gpx_with_coords, weather_results, history_results):
        if isinstance(weather, Exception):
            print(f"⚠️ Meteo non disponibile per {gpx['name']}: {weather}")
            continue

        zone = gpx["zone"]
        hourly = weather["hourly"]

        # Soil dryness dalla zona geologica più vicina (non più da Monte Cavo fisso)
        gpx_soil_dryness = None
        if not isinstance(history, Exception):
            try:
                gpx_soil_dryness = calculate_soil_dryness_5d(history)
            except Exception as e:
                print(f"⚠️ Storico non calcolabile per {gpx['name']}: {e}")

        rain_5d = gpx_soil_dryness["rain_7d"] if gpx_soil_dryness else 0

        conditions     = calculate_trail_conditions(hourly)
        riding_windows = find_best_riding_windows(hourly)
        riding_windows = adjust_windows_for_soil(riding_windows, gpx_soil_dryness, hourly)

        # Proiezione SMI con geologia della zona più vicina — allineato alla matrice
        soil_forecast = project_soil_forecast_smi(rain_5d, zone, hourly, riding_windows)

        # Badge terreno attuale basato su SMI (non più su soglie flat)
        smi_now = calculate_smi(rain_5d, zone["field_capacity"])
        if smi_now > 1.2:    terrain_label, terrain_emoji = "Saturo",      "🔴"
        elif smi_now > 0.8:  terrain_label, terrain_emoji = "Fangoso",     "🟠"
        elif smi_now > 0.5:  terrain_label, terrain_emoji = "Umido",       "🟡"
        else:                terrain_label, terrain_emoji = "Praticabile", "🟢"

        gpx_forecasts.append({
            "key":            gpx["key"],
            "name":           gpx["name"],
            "color":          gpx["color"],
            "file":           gpx["file"],
            "coords":         gpx.get("coords", []),
            "lat":            gpx["lat"],
            "lon":            gpx["lon"],
            "zone_name":      zone["name"],
            "smi":            round(smi_now, 2),
            "terrain_label":  terrain_label,
            "terrain_emoji":  terrain_emoji,
            "conditions":     conditions,
            "riding_windows": riding_windows,
            "soil_forecast":  soil_forecast,
        })

    # current_conditions generale (prima zona come riferimento — solo per compatibilità template)
    percorsi_current_conditions = None
    if gpx_forecasts:
        first = gpx_forecasts[0]
        percorsi_current_conditions = {
            "rating":       "poor" if first["smi"] > 1.2 else ("good" if first["smi"] > 0.5 else "excellent"),
            "rating_text":  first["terrain_label"],
            "rating_emoji": first["terrain_emoji"],
            "reasons":      [],
        }

    #strava_club_info      = await fetch_club_info()
    #strava_all_activities = await fetch_all_club_activities()
    starred_segments      = await cached_fetch_starred_segments(fetch_starred_segments)

    reports = get_active_reports()
    return templates.TemplateResponse("percorsi.html", {
        "request":                   request,
        "gpx_forecasts":             gpx_forecasts,
        "current_conditions":        percorsi_current_conditions,
        "soil_dryness":              gpx_forecasts[0].get("smi") if gpx_forecasts else None,
        #"strava_club_info":         strava_club_info,
        #"strava_all_activities":    strava_all_activities,
        "starred_segments":          starred_segments,
        "reports":                   reports,
    })
