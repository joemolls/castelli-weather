from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from locations import LOCATIONS
from weather_client import fetch_weather
from datetime import datetime

app = FastAPI(title="Castelli Weather API")
templates = Jinja2Templates(directory="templates")

def calculate_trail_conditions(hourly_data):
    """Calcola le condizioni dei sentieri per MTB"""
    current_hour = 0
    
    # Pioggia ultime 24 ore
    rain_24h = sum(hourly_data['precipitation'][:24]) if len(hourly_data['precipitation']) >= 24 else 0
    
    # Condizioni attuali
    current_temp = hourly_data['temperature_2m'][current_hour]
    current_wind = hourly_data['windspeed_10m'][current_hour]
    current_gust = hourly_data['windgusts_10m'][current_hour]
    
    # Calcola score (0-100)
    score = 100
    reasons = []
    
    # Pioggia recente
    if rain_24h > 15:
        score -= 40
        reasons.append(f"❌ Pioggia abbondante ultime 24h ({rain_24h:.1f}mm)")
    elif rain_24h > 5:
        score -= 20
        reasons.append(f"⚠️ Pioggia moderata ultime 24h ({rain_24h:.1f}mm)")
    elif rain_24h < 1:
        reasons.append("✅ Sentieri asciutti")
    
    # Vento
    if current_wind > 30:
        score -= 30
        reasons.append(f"❌ Vento forte ({current_wind:.0f} km/h)")
    elif current_wind > 20:
        score -= 15
        reasons.append(f"⚠️ Vento moderato ({current_wind:.0f} km/h)")
    else:
        reasons.append(f"✅ Vento calmo ({current_wind:.0f} km/h)")
    
    # Raffiche
    if current_gust > 40:
        score -= 20
        reasons.append(f"❌ Raffiche pericolose ({current_gust:.0f} km/h)")
    elif current_gust > 30:
        score -= 10
        reasons.append(f"⚠️ Raffiche moderate ({current_gust:.0f} km/h)")
    
    # Temperatura (ghiaccio/neve)
    if current_temp < 0:
        score -= 30
        reasons.append(f"❌ Rischio ghiaccio ({current_temp:.0f}°C)")
    elif current_temp < 3:
        score -= 15
        reasons.append(f"⚠️ Temperature basse ({current_temp:.0f}°C)")
    else:
        reasons.append(f"✅ Temperatura OK ({current_temp:.0f}°C)")
    
    # Determina rating
    if score >= 80:
        rating = "excellent"
        rating_text = "🟢 OTTIME"
        rating_emoji = "🚴‍♂️"
    elif score >= 60:
        rating = "good"
        rating_text = "🟡 DISCRETE"
        rating_emoji = "⚠️"
    else:
        rating = "poor"
        rating_text = "🔴 DIFFICILI"
        rating_emoji = "❌"
    
    return {
        "score": score,
        "rating": rating,
        "rating_text": rating_text,
        "rating_emoji": rating_emoji,
        "reasons": reasons,
        "rain_24h": rain_24h,
        "current_wind": current_wind,
        "current_gust": current_gust
    }

def find_best_riding_windows(hourly_data):
    """Trova le migliori finestre orarie per uscire oggi"""
    windows = []
    
    # Analizza le prossime 12 ore (oggi)
    for i in range(min(12, len(hourly_data['time']))):
        hour_temp = hourly_data['temperature_2m'][i]
        hour_precip = hourly_data['precipitation'][i]
        hour_wind = hourly_data['windspeed_10m'][i]
        hour_code = hourly_data['weather_code'][i]
        
        # Score per quest'ora
        hour_score = 100
        if hour_precip > 0.5:
            hour_score -= 50  # Pioggia
        if hour_wind > 25:
            hour_score -= 30  # Vento forte
        if hour_temp < 3:
            hour_score -= 20  # Freddo
        if hour_code in [95, 96, 99]:  # Temporale
            hour_score -= 60
        
        # Orario
        time_obj = datetime.fromisoformat(hourly_data['time'][i])
        hour_str = time_obj.strftime("%H:%M")
        
        windows.append({
            "time": hour_str,
            "score": hour_score,
            "temp": hour_temp,
            "wind": hour_wind,
            "precip": hour_precip
        })
    
    # Trova la finestra migliore
    best_windows = sorted(windows, key=lambda x: x['score'], reverse=True)[:3]
    
    if best_windows[0]['score'] >= 70:
        recommendation = f"✅ Ottimo tra le {best_windows[0]['time']} e le {best_windows[2]['time']}"
    elif best_windows[0]['score'] >= 50:
        recommendation = f"⚠️ Accettabile tra le {best_windows[0]['time']} e le {best_windows[1]['time']}"
    else:
        recommendation = "❌ Oggi sconsigliato, attendi condizioni migliori"
    
    return {
        "windows": best_windows,
        "recommendation": recommendation
    }

@app.get("/")
def root():
    """Redirect alla dashboard completa"""
    return RedirectResponse(url="/dashboard-completa")

@app.get("/locations")
def get_locations():
    return LOCATIONS

@app.get("/weather/{location}")
async def get_weather(location: str):
    if location not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")
    loc = LOCATIONS[location]
    data = await fetch_weather(loc["lat"], loc["lon"])
    return {
        "location": loc["name"],
        "elevation": loc["elevation"],
        "hourly": data["hourly"]
    }

@app.get("/dashboard/{location}", response_class=HTMLResponse)
async def dashboard(request: Request, location: str):
    if location not in LOCATIONS:
        raise HTTPException(status_code=404, detail="Location not found")
    loc = LOCATIONS[location]
    data = await fetch_weather(loc["lat"], loc["lon"])
    
    hourly = data["hourly"]
    hourly_safe = {
        "time": hourly.get("time", []),
        "temperature_2m": hourly.get("temperature_2m", []),
        "precipitation": hourly.get("precipitation", []),
        "weather_code": hourly.get("weather_code", []),
        "windspeed_10m": hourly.get("windspeed_10m", []),
        "windgusts_10m": hourly.get("windgusts_10m", [])
    }
    
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "location_name": loc["name"],
            "hourly": hourly_safe
        }
    )

@app.get("/dashboard-completa", response_class=HTMLResponse)
async def dashboard_completa(request: Request):
    """Dashboard con tutti i grafici delle località dei Castelli Romani"""
    
    all_data = []
    overall_trail_conditions = None
    overall_riding_windows = None
    
    for loc_key, loc_info in LOCATIONS.items():
        data = await fetch_weather(loc_info["lat"], loc_info["lon"])
        hourly = data["hourly"]
        
        # Calcola condizioni sentieri (solo per la prima località, rappresentativa)
        if overall_trail_conditions is None:
            overall_trail_conditions = calculate_trail_conditions(hourly)
            overall_riding_windows = find_best_riding_windows(hourly)
        
        all_data.append({
            "name": loc_info["name"],
            "elevation": loc_info["elevation"],
            "hourly": {
                "time": hourly.get("time", []),
                "temperature_2m": hourly.get("temperature_2m", []),
                "precipitation": hourly.get("precipitation", []),
                "weather_code": hourly.get("weather_code", []),
                "windspeed_10m": hourly.get("windspeed_10m", []),
                "windgusts_10m": hourly.get("windgusts_10m", [])
            }
        })
    
    return templates.TemplateResponse(
        "dashboard_completa.html",
        {
            "request": request,
            "locations_data": all_data,
            "trail_conditions": overall_trail_conditions,
            "riding_windows": overall_riding_windows
        }
    )
