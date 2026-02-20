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
        reasons.append(f"❌ Terreno saturo ({rain_7d:.0f}mm negli ultimi 7 giorni)")
        reasons.append(f"❌ Sentieri danneggiati — evita di uscire")
        if dry_days == 0:
            reasons.append("⚠️ Pioggia ancora recente")
        else:
            reasons.append(f"⏳ {dry_days} giorn{'o' if dry_days==1 else 'i'} senza pioggia — troppo poco")
    elif rating_soil == "wet":
        score = 45
        reasons.append(f"⚠️ Terreno bagnato ({rain_7d:.0f}mm negli ultimi 7 giorni)")
        reasons.append(f"⚠️ Sentieri scivolosi — massima attenzione")
        reasons.append(f"⏳ {dry_days} giorn{'o' if dry_days==1 else 'i'} senza pioggia")
    elif rating_soil == "damp":
        score = 70
        reasons.append(f"🟡 Terreno umido ({rain_7d:.0f}mm negli ultimi 7 giorni)")
        reasons.append(f"✅ Sentieri percorribili con attenzione")
        reasons.append(f"☀️ {dry_days} giorn{'o' if dry_days==1 else 'i'} senza pioggia")
    else:  # dry
        score = 95
        reasons.append(f"✅ Terreno asciutto ({rain_7d:.0f}mm negli ultimi 7 giorni)")
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
    visit_stats = increment_visit(page="dashboard")
    all_data    = []
    overall_trail_conditions = None
    overall_riding_windows   = None

    soil_dryness = None

    for loc_key, loc_info in LOCATIONS.items():
        data   = await fetch_weather(loc_info["lat"], loc_info["lon"])
        hourly = data["hourly"]
        if overall_trail_conditions is None:
            overall_trail_conditions = calculate_trail_conditions(hourly)
            overall_riding_windows   = find_best_riding_windows(hourly)

        # Storico precipitazioni per ogni località
        loc_soil_dryness = None
        try:
            history = await fetch_weather_history(loc_info["lat"], loc_info["lon"], days=7)
            loc_soil_dryness = calculate_soil_dryness(history)
            if soil_dryness is None:
                soil_dryness = loc_soil_dryness  # mantieni il primo per compatibilità
                # Aggiusta le finestre in base allo stato del terreno
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

    return templates.TemplateResponse("dashboard_completa.html", {
        "request": request,
        "locations_data":      all_data,
        "trail_conditions":    overall_trail_conditions,
        "current_conditions":  current_conditions,
        "soil_forecast":       soil_forecast,
        "riding_windows":      overall_riding_windows,
        "soil_dryness":        soil_dryness,
        "visit_stats":         visit_stats,
    })



@app.get("/sim-report", response_class=HTMLResponse)
async def sim_report():
    """Pagina di simulazione segnalazione GPS — solo per test."""
    with open("templates/sim_report.html", "r") as f:
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
    return templates.TemplateResponse("admin_segnalazioni.html", {
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

    # Fetch soil_dryness una volta sola (Monte Cavo come riferimento)
    ref_loc     = list(LOCATIONS.values())[0]
    percorsi_soil_dryness = None
    try:
        history = await fetch_weather_history(ref_loc["lat"], ref_loc["lon"], days=7)
        percorsi_soil_dryness = calculate_soil_dryness(history)
    except Exception as e:
        print(f"⚠️ Storico meteo non disponibile per percorsi: {e}")

    percorsi_current_conditions = calculate_current_conditions(percorsi_soil_dryness)

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

        # Aggiusta finestre in base allo stato del terreno
        riding_windows = adjust_windows_for_soil(riding_windows, percorsi_soil_dryness, hourly)
        soil_forecast  = project_soil_forecast(percorsi_soil_dryness, hourly, riding_windows)

        gpx_forecasts.append({
            "key":            gpx["key"],
            "name":           gpx["name"],
            "color":          gpx["color"],
            "file":           gpx["file"],
            "lat":            lat,
            "lon":            lon,
            "conditions":     conditions,
            "riding_windows": riding_windows,
            "soil_forecast":  soil_forecast,
        })

    #strava_club_info      = await fetch_club_info()
    #strava_all_activities = await fetch_all_club_activities()
    starred_segments      = await fetch_starred_segments()

    reports = get_active_reports()
    return templates.TemplateResponse("percorsi.html", {
        "request":                   request,
        "gpx_forecasts":             gpx_forecasts,
        "current_conditions":        percorsi_current_conditions,
        "soil_dryness":              percorsi_soil_dryness,
        #"strava_club_info":         strava_club_info,
        #"strava_all_activities":    strava_all_activities,
        "starred_segments":          starred_segments,
        "reports":                   reports,
    })
