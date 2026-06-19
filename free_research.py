"""Free research data sources - no API keys, no LLM costs.
Provides structured data the agent can use for probability estimation.
"""
import json
import logging
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# NWS API - free, no key, US weather forecasts
NWS_BASE = "https://api.weather.gov"

# Open-Meteo - free, no key, global weather
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"

# City coordinates for weather markets
CITY_COORDS = {
    "DC": (38.89, -77.03),
    "NYC": (40.71, -74.01),
    "CHI": (41.88, -87.63),
    "PHIL": (39.95, -75.17),
    "LAX": (34.05, -118.24),
    "SEA": (47.61, -122.33),
}

# NWS station IDs
NWS_STATIONS = {
    "DC": "KDCA",
    "NYC": "KJFK",
    "CHI": "KORD",
    "PHIL": "KPHL",
    "LAX": "KLAX",
    "SEA": "KSEA",
}


def get_weather_forecast(city: str) -> dict | None:
    """
    Get hourly temperature forecast for a city using Open-Meteo (free, no key).
    Also pulls recent actuals to calculate forecast bias.
    Returns dict with today's high, tomorrow's high, hourly temps, and bias.
    """
    coords = CITY_COORDS.get(city.upper())
    if not coords:
        return None

    try:
        # Get forecast
        resp = requests.get(OPEN_METEO_BASE, params={
            "latitude": coords[0],
            "longitude": coords[1],
            "hourly": "temperature_2m",
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit",
            "timezone": "America/New_York",
            "forecast_days": 3,
            "past_days": 3,  # Get last 3 days of actuals
        }, timeout=10)

        if resp.status_code != 200:
            return None

        data = resp.json()
        daily = data.get("daily", {})

        max_temps = daily.get("temperature_2m_max", [])
        dates = daily.get("time", [])

        result = {
            "city": city,
            "source": "Open-Meteo",
            "forecast_highs": {},
            "recent_actuals": {},
            "bias_adjustment": 0.0,
            "adjusted_forecast": {},
        }

        today_str = datetime.now().strftime("%Y-%m-%d")

        for i, date in enumerate(dates):
            if i < len(max_temps):
                if date < today_str:
                    result["recent_actuals"][date] = max_temps[i]
                else:
                    result["forecast_highs"][date] = max_temps[i]

        # Calculate bias: how much hotter/cooler have actuals been vs this model?
        # If we have past days, the "actuals" from Open-Meteo are observed temps
        # We compare them to what the forecast WOULD have said
        # Simple approach: look at if recent days trend hotter or cooler
        actuals = list(result["recent_actuals"].values())
        if len(actuals) >= 2:
            # Check trend: are temps rising or falling?
            trend = actuals[-1] - actuals[0]  # positive = warming trend
            # Typical forecast bias: models lag trends by ~1°F
            if abs(trend) > 2:
                result["bias_adjustment"] = trend * 0.3  # Apply 30% of trend as adjustment

        # Apply bias to forecasts
        for date, temp in result["forecast_highs"].items():
            result["adjusted_forecast"][date] = round(temp + result["bias_adjustment"], 1)

        # Get current hour temp
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        now_str = datetime.now().strftime("%Y-%m-%dT%H:00")
        for i, t in enumerate(times):
            if t == now_str and i < len(temps):
                result["current_hour_temp"] = temps[i]
                break

        logger.info(f"Weather for {city}: forecast={result['forecast_highs']}, bias={result['bias_adjustment']:+.1f}°F, adjusted={result['adjusted_forecast']}")
        return result

    except Exception as e:
        logger.warning(f"Weather fetch failed for {city}: {e}")
        return None


def get_nws_forecast(city: str) -> dict | None:
    """
    Get NWS forecast for a city (free, no key, authoritative US source).
    This is the actual data source Kalshi uses for settlement.
    """
    station = NWS_STATIONS.get(city.upper())
    if not station:
        return None

    try:
        # Get the forecast grid point
        coords = CITY_COORDS[city.upper()]
        resp = requests.get(
            f"{NWS_BASE}/points/{coords[0]},{coords[1]}",
            headers={"User-Agent": "TradingAgent/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        grid_data = resp.json()
        forecast_url = grid_data.get("properties", {}).get("forecastHourly")
        if not forecast_url:
            return None

        # Get hourly forecast
        resp = requests.get(
            forecast_url,
            headers={"User-Agent": "TradingAgent/1.0"},
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        periods = resp.json().get("properties", {}).get("periods", [])

        # Extract max temp for today and tomorrow
        today = datetime.now().strftime("%Y-%m-%d")
        tomorrow = (datetime.now().replace(hour=0) + __import__("datetime").timedelta(days=1)).strftime("%Y-%m-%d")

        today_temps = []
        tomorrow_temps = []
        for p in periods:
            start = p.get("startTime", "")[:10]
            temp = p.get("temperature")
            if temp is not None:
                if start == today:
                    today_temps.append(temp)
                elif start == tomorrow:
                    tomorrow_temps.append(temp)

        result = {
            "city": city,
            "source": "NWS (weather.gov)",
            "today_high": max(today_temps) if today_temps else None,
            "tomorrow_high": max(tomorrow_temps) if tomorrow_temps else None,
            "periods_count": len(periods),
        }
        logger.info(f"NWS forecast for {city}: today_high={result['today_high']}, tomorrow_high={result['tomorrow_high']}")
        return result

    except Exception as e:
        logger.warning(f"NWS fetch failed for {city}: {e}")
        return None


def research_market_free(market: dict) -> str | None:
    """
    Get free research data for a market based on its type.
    Returns a text summary the agent can use, or None if no free data available.
    """
    ticker = market.get("ticker", "")
    title = market.get("title", "")
    series = ticker.split("-")[0] if ticker else ""

    # Weather markets
    if series in ("KXHIGHTDC", "KXHIGHTNYC", "KXHIGHTCHI", "KXHIGHPHIL", "KXLOWTSEA", "KXTEMPNYCH", "KXHIGHCHI"):
        city_map = {
            "KXHIGHTDC": "DC", "KXHIGHTNYC": "NYC", "KXHIGHTCHI": "CHI",
            "KXHIGHPHIL": "PHIL", "KXLOWTSEA": "SEA", "KXTEMPNYCH": "NYC",
            "KXHIGHCHI": "CHI",
        }
        city = city_map.get(series)
        if city:
            # Get both sources for cross-reference
            om = get_weather_forecast(city)
            nws = get_nws_forecast(city)

            parts = []
            if om and om.get("forecast_highs"):
                parts.append(f"Open-Meteo forecast highs: {om['forecast_highs']}")
                if om.get("bias_adjustment") and om["bias_adjustment"] != 0:
                    parts.append(f"Trend bias: {om['bias_adjustment']:+.1f}°F (recent days ran {'hotter' if om['bias_adjustment'] > 0 else 'cooler'} than expected)")
                    parts.append(f"Adjusted forecast: {om['adjusted_forecast']}")
                if om.get("recent_actuals"):
                    parts.append(f"Last 3 days actuals: {om['recent_actuals']}")
            if nws:
                if nws.get("today_high"):
                    parts.append(f"NWS today's high: {nws['today_high']}°F")
                if nws.get("tomorrow_high"):
                    parts.append(f"NWS tomorrow's high: {nws['tomorrow_high']}°F")
            if om and om.get("current_hour_temp"):
                parts.append(f"Current temp: {om['current_hour_temp']}°F")

            if parts:
                return f"FREE WEATHER DATA ({city}):\n" + "\n".join(parts)

    return None


def gather_free_research(markets: list) -> dict:
    """
    Gather free research for all applicable markets.
    Returns dict mapping ticker -> research text.
    No API keys or LLM calls needed.
    """
    research = {}

    for market in markets:
        result = research_market_free(market)
        if result:
            research[market["ticker"]] = result

    if research:
        logger.info(f"Free research gathered for {len(research)} markets (no LLM cost)")

    return research
