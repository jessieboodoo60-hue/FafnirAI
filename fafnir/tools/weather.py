"""Live weather from Open-Meteo (free, no API key)."""
from __future__ import annotations

import httpx

from .registry import Registry, Tool, ToolError

GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
WX_URL = "https://api.open-meteo.com/v1/forecast"

CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 80: "light showers", 81: "showers", 82: "violent showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


def register(reg: Registry, home: str) -> None:
    http = httpx.Client(timeout=15.0)

    def geocode(place: str) -> dict:
        # Try the full name, then progressively shorter (e.g. drop the district).
        parts = [p.strip() for p in place.split(",") if p.strip()]
        country = parts[-1].lower() if len(parts) > 1 else ""
        for i in range(len(parts)):
            name = parts[i]
            try:
                r = http.get(GEO_URL, params={"name": name, "count": 10, "language": "en"})
                r.raise_for_status()
            except httpx.HTTPError as e:
                raise ToolError(f"Couldn't reach the weather service: {e}")
            results = r.json().get("results") or []
            if country:
                in_country = [x for x in results if country in (x.get("country") or "").lower()]
                results = in_country or ([] if i < len(parts) - 1 else results)
            if results:
                return results[0]
        raise ToolError(f"Couldn't find a place called '{place}'.")

    def weather(place: str = "", days: int = 3) -> dict:
        place = place.strip() or home
        if not place:
            raise ToolError("No place given and no home location is set in config.toml.")
        loc = geocode(place)
        days = max(1, min(days or 3, 7))
        try:
            r = http.get(WX_URL, params={
                "latitude": loc["latitude"], "longitude": loc["longitude"], "timezone": "auto",
                "forecast_days": days,
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
            })
            r.raise_for_status()
            d = r.json()
        except httpx.HTTPError as e:
            raise ToolError(f"Couldn't reach the weather service: {e}")
        cur, day = d.get("current", {}), d.get("daily", {})
        return {
            "place": ", ".join(x for x in (loc.get("name"), loc.get("admin1"), loc.get("country")) if x),
            "local_time": cur.get("time"),
            "now": {
                "conditions": CODES.get(cur.get("weather_code"), "unknown"),
                "temp_c": cur.get("temperature_2m"), "feels_like_c": cur.get("apparent_temperature"),
                "humidity_pct": cur.get("relative_humidity_2m"), "rain_mm": cur.get("precipitation"),
                "wind_kmh": cur.get("wind_speed_10m"),
            },
            "forecast": [
                {"date": day["time"][i], "conditions": CODES.get(day["weather_code"][i], "unknown"),
                 "high_c": day["temperature_2m_max"][i], "low_c": day["temperature_2m_min"][i],
                 "rain_chance_pct": day["precipitation_probability_max"][i],
                 "rain_mm": day["precipitation_sum"][i]}
                for i in range(len(day.get("time", [])))
            ],
        }

    reg.register(Tool(
        name="weather",
        description=(
            "Get live current weather and a short daily forecast. Use this for any weather question "
            "instead of web search. Leave place empty for the owner's home location."
        ),
        properties={
            "place": {"type": "string", "description": "City or area, e.g. 'Port of Spain, Trinidad and Tobago'. Empty = home."},
            "days": {"type": "integer", "description": "Forecast days, 1-7. Default 3."},
        },
        handler=weather,
    ))
