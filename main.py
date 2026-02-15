from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from locations import LOCATIONS
from weather_client import fetch_weather

app = FastAPI(title="Castelli Weather API")
templates = Jinja2Templates(directory="templates")

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
        "weather_code": hourly.get("weather_code", [])  # Aggiungiamo weather_code
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
    for loc_key, loc_info in LOCATIONS.items():
        data = await fetch_weather(loc_info["lat"], loc_info["lon"])
        hourly = data["hourly"]
        all_data.append({
            "name": loc_info["name"],
            "elevation": loc_info["elevation"],
            "hourly": {
                "time": hourly.get("time", []),
                "temperature_2m": hourly.get("temperature_2m", []),
                "precipitation": hourly.get("precipitation", []),
                "weather_code": hourly.get("weather_code", [])  # Aggiungiamo weather_code
            }
        })
    
    return templates.TemplateResponse(
        "dashboard_completa.html",
        {
            "request": request,
            "locations_data": all_data
        }
    )
