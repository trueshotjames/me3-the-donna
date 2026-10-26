"""Weather through Open-Meteo (free, no key): geocode the city, then current + 3-day forecast.

One warm httpx client for the life of the server; geocoding results are cached, so a repeat
question costs one round trip (about 120 ms warm from the Pi, 300 ms cold).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
TIMEOUT_S = 6.0

# WMO weather codes -> spoken description and an icon key the UI maps to a glyph
CODES: dict[int, tuple[str, str]] = {
    0: ("clear sky", "sun"), 1: ("mostly clear", "sun"), 2: ("partly cloudy", "cloud-sun"), 3: ("overcast", "cloud"),
    45: ("foggy", "fog"), 48: ("freezing fog", "fog"),
    51: ("light drizzle", "drizzle"), 53: ("drizzle", "drizzle"), 55: ("heavy drizzle", "drizzle"),
    56: ("freezing drizzle", "drizzle"), 57: ("freezing drizzle", "drizzle"),
    61: ("light rain", "rain"), 63: ("rain", "rain"), 65: ("heavy rain", "rain"),
    66: ("freezing rain", "rain"), 67: ("freezing rain", "rain"),
    71: ("light snow", "snow"), 73: ("snow", "snow"), 75: ("heavy snow", "snow"), 77: ("snow grains", "snow"),
    80: ("rain showers", "rain"), 81: ("rain showers", "rain"), 82: ("violent rain showers", "rain"),
    85: ("snow showers", "snow"), 86: ("heavy snow showers", "snow"),
    95: ("thunderstorm", "storm"), 96: ("thunderstorm with hail", "storm"), 99: ("thunderstorm with heavy hail", "storm"),
}


def describe(code: int | None) -> tuple[str, str]:
    return CODES.get(int(code) if code is not None else -1, ("unknown conditions", "cloud"))


class WeatherClient:
    def __init__(self, *, default_city: str = "", timeout_s: float = TIMEOUT_S):
        self.default_city = default_city
        self._client = httpx.AsyncClient(timeout=timeout_s, headers={"User-Agent": "me3-voice-assistant/1.0"})
        self._geo: dict[str, dict[str, Any]] = {}
        self.last: dict[str, Any] | None = None  # the last answer, for the UI widget on reconnect

    async def close(self) -> None:
        await self._client.aclose()

    async def geocode(self, city: str) -> dict[str, Any]:
        key = city.strip().lower()
        if key in self._geo:
            return self._geo[key]
        r = await self._client.get(GEOCODE_URL, params={"name": city, "count": 1, "language": "en", "format": "json"})
        r.raise_for_status()
        results = r.json().get("results") or []
        if not results:
            raise LookupError(f"no place called {city!r}")
        g = results[0]
        place = {
            "name": g.get("name"),
            "admin": g.get("admin1"),
            "country": g.get("country"),
            "latitude": g["latitude"],
            "longitude": g["longitude"],
            "timezone": g.get("timezone"),
        }
        self._geo[key] = place
        return place

    async def get(self, city: str = "", day: str = "today") -> dict[str, Any]:
        """Current conditions and a 3-day outlook for `city` (or the default city)."""
        city = (city or self.default_city or "").strip()
        if not city:
            return {"error": "which city? (set WEATHER_DEFAULT_CITY in .env for a default)"}
        t0 = time.perf_counter()
        try:
            place = await self.geocode(city)
            r = await self._client.get(
                FORECAST_URL,
                params={
                    "latitude": place["latitude"],
                    "longitude": place["longitude"],
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day",
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "forecast_days": 3,
                    "timezone": "auto",
                },
            )
            r.raise_for_status()
            data = r.json()
        except LookupError as e:
            return {"error": str(e)}
        except (httpx.HTTPError, ValueError, KeyError) as e:
            return {"error": f"I can't reach the weather service right now ({type(e).__name__})"}
        cur = data.get("current") or {}
        daily = data.get("daily") or {}
        desc, icon = describe(cur.get("weather_code"))
        days = []
        for i, date in enumerate(daily.get("time") or []):
            d_desc, d_icon = describe((daily.get("weather_code") or [None] * 3)[i])
            days.append(
                {
                    "date": date,
                    "label": "today" if i == 0 else "tomorrow" if i == 1 else "day after tomorrow",
                    "high": (daily.get("temperature_2m_max") or [None] * 3)[i],
                    "low": (daily.get("temperature_2m_min") or [None] * 3)[i],
                    "rain_chance": (daily.get("precipitation_probability_max") or [None] * 3)[i],
                    "conditions": d_desc,
                    "icon": d_icon,
                }
            )
        where = ", ".join(x for x in (place["name"], place.get("country")) if x)
        result: dict[str, Any] = {
            "city": where,
            "unit": "C",
            "now": {
                "temperature": cur.get("temperature_2m"),
                "feels_like": cur.get("apparent_temperature"),
                "humidity": cur.get("relative_humidity_2m"),
                "wind_kmh": cur.get("wind_speed_10m"),
                "conditions": desc,
                "icon": icon,
                "is_day": bool(cur.get("is_day", 1)),
            },
            "days": days,
            "fetched_ms": round((time.perf_counter() - t0) * 1000),
        }
        want = (day or "today").strip().lower()
        pick = next((d for d in days if d["label"] == want or want in d["label"]), None)
        if pick and want != "today":
            result["asked_for"] = pick
            result["spoken"] = f"{pick['label'].capitalize()} in {place['name']}: {pick['conditions']}, high {_deg(pick['high'])}, low {_deg(pick['low'])}" + (
                f", {pick['rain_chance']} percent chance of rain" if pick.get("rain_chance") is not None else ""
            ) + "."
        else:
            result["spoken"] = f"{place['name']} now: {desc}, {_deg(cur.get('temperature_2m'))}, feels like {_deg(cur.get('apparent_temperature'))}." + (
                f" High {_deg(days[0]['high'])}, low {_deg(days[0]['low'])}." if days else ""
            )
        self.last = result
        return result


def _deg(v: Any) -> str:
    return "unknown" if v is None else f"{round(float(v))} degrees"
