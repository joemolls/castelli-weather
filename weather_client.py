import httpx
from datetime import datetime, timedelta

BASE_URL     = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"

async def fetch_weather(lat: float, lon: float):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,weather_code,windspeed_10m,windgusts_10m",
        "models": "icon_seamless",
        "forecast_days": 3,
        "timezone": "Europe/Rome"
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(BASE_URL, params=params, timeout=10.0)
        response.raise_for_status()
        return response.json()


async def fetch_weather_history(lat: float, lon: float, days: int = 14):
    """
    Recupera lo storico meteo degli ultimi N giorni (Open-Meteo Archive API).
    Dati giornalieri aggregati: precipitazione totale, temp max/min, vento max.
    NB: l'archive API ha un lag di ~2 giorni rispetto a oggi.
    """
    today      = datetime.now().date()
    end_date   = today - timedelta(days=1)      # ieri (massimo disponibile)
    start_date = end_date - timedelta(days=days - 1)

    params = {
        "latitude":   lat,
        "longitude":  lon,
        "start_date": start_date.isoformat(),
        "end_date":   end_date.isoformat(),
        "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min,windspeed_10m_max",
        "timezone": "Europe/Rome"
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(ARCHIVE_URL, params=params, timeout=15.0)
        response.raise_for_status()
        return response.json()
