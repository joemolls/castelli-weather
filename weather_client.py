import httpx

BASE_URL = "https://api.open-meteo.com/v1/forecast"

async def fetch_weather(lat: float, lon: float):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,weather_code,windspeed_10m,windgusts_10m",
        "models": "icon_seamless",  # Modello DWD ICON - migliore per Europa/Italia
        "forecast_days": 3,
        "timezone": "Europe/Rome"
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(BASE_URL, params=params, timeout=10.0)
        response.raise_for_status()
        return response.json()
