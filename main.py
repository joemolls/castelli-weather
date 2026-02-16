from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from locations import LOCATIONS
from weather_client import fetch_weather
from datetime import datetime, timedelta

app = FastAPI(title="Castelli Weather API")
templates = Jinja2Templates(directory="templates")

# Health check endpoints per UptimeRobot
@app.head("/")
async def head_root():
    """Health check endpoint per UptimeRobot"""
    return {}

@app.head("/dashboard-completa")
async def head_dashboard():
    """Health check endpoint per UptimeRobot"""
    return {}

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
    """Trova le migliori finestre di 4-6h per uscite XCM nei prossimi 3 giorni"""
    now = datetime.now()
    daily_windows = []
    
    # Raggruppa ore per giorno
    hours_by_day = {}
    for i, time_str in enumerate(hourly_data['time']):
        time_obj = datetime.fromisoformat(time_str)
        
        # Skip ore già passate
        if time_obj <= now:
            continue
            
        day_key = time_obj.date()
        if day_key not in hours_by_day:
            hours_by_day[day_key] = []
        
        # Calcola score per quest'ora
        hour_temp = hourly_data['temperature_2m'][i]
        hour_precip = hourly_data['precipitation'][i]
        hour_wind = hourly_data['windspeed_10m'][i]
        hour_code = hourly_data['weather_code'][i]
        
        hour_score = 100
        if hour_precip > 0.5:
            hour_score -= 50
        if hour_wind > 25:
            hour_score -= 30
        if hour_temp < 3:
            hour_score -= 20
        if hour_code in [95, 96, 99]:
            hour_score -= 60
        
        hours_by_day[day_key].append({
            "time": time_obj,
            "hour": time_obj.hour,
            "score": hour_score,
            "temp": hour_temp,
            "wind": hour_wind,
            "precip": hour_precip
        })
    
    # Per ogni giorno, trova le migliori finestre di 4-6h
    for day, hours in sorted(hours_by_day.items())[:3]:  # Max 3 giorni
        if len(hours) < 4:
            continue
        
        # Cerca finestre consecutive di 4-6h con score medio alto
        best_window = None
        best_avg_score = 0
        
        for window_size in [6, 5, 4]:  # Prova prima 6h, poi 5h, poi 4h
            for start_idx in range(len(hours) - window_size + 1):
                window = hours[start_idx:start_idx + window_size]
                avg_score = sum(h['score'] for h in window) / len(window)
                avg_temp = sum(h['temp'] for h in window) / len(window)
                avg_wind = sum(h['wind'] for h in window) / len(window)
                max_precip = max(h['precip'] for h in window)
                
                if avg_score > best_avg_score:
                    best_avg_score = avg_score
                    best_window = {
                        "start": window[0]['time'],
                        "end": window[-1]['time'],
                        "duration": window_size,
                        "score": avg_score,
                        "temp": avg_temp,
                        "wind": avg_wind,
                        "precip": max_precip
                    }
        
        if best_window:
            # Determina rating
            if best_window['score'] >= 80:
                rating = "excellent"
                rating_icon = "🟢"
            elif best_window['score'] >= 60:
                rating = "good"
                rating_icon = "🟡"
            else:
                rating = "poor"
                rating_icon = "🔴"
            
            # Nome del giorno
            if day == now.date():
                day_name = "Oggi"
            elif day == (now + timedelta(days=1)).date():
                day_name = "Domani"
            else:
                day_name = day.strftime("%a %d %b")
            
            daily_windows.append({
                "day": day_name,
                "date": day.strftime("%d %b"),
                "start_time": best_window['start'].strftime("%H:%M"),
                "end_time": best_window['end'].strftime("%H:%M"),
                "duration": best_window['duration'],
                "rating": rating,
                "rating_icon": rating_icon,
                "temp": best_window['temp'],
                "wind": best_window['wind'],
                "precip": best_window['precip'],
                "score": best_window['score']
            })
    
    return daily_windows

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
