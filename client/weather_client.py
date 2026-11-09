"""Sample weather client for the PatchPilot demo.

This client was written against API v1 (Open-Meteo `current_weather` format).
PatchPilot is expected to migrate it to the staged v2 format.

The server address comes from WEATHER_BASE_URL (the local PatchPilot proxy).
Key access uses a direct chain style on purpose, so usage detection can find it.
"""

import os

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:8787"
TIMEOUT_S = 10


def _base_url():
    return os.environ.get("WEATHER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def fetch_forecast(lat, lon):
    """Return the raw forecast JSON for one location."""
    params = {"latitude": lat, "longitude": lon, "current_weather": "true"}
    resp = requests.get(_base_url() + "/forecast", params=params, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


def get_current_temperature(lat, lon):
    """Current air temperature as a float."""
    data = fetch_forecast(lat, lon)
    temp = data["current"]["temperature_2m"]
    return float(temp)


def get_current_windspeed(lat, lon):
    """Current wind speed as a float."""
    data = fetch_forecast(lat, lon)
    wind = data["current"]["windspeed"]
    return float(wind)


def get_temperature_unit(lat, lon):
    """Unit string for the temperature value, for example a Celsius sign."""
    data = fetch_forecast(lat, lon)
    unit = data["current_units"]["temperature_2m"]
    return unit


def get_current_summary(lat, lon):
    """Small summary of current conditions."""
    data = fetch_forecast(lat, lon)
    return {
        "time": data["current"]["time"],
        "temperature": float(data["current"]["temperature_2m"]),
        "windspeed": float(data["current"]["windspeed"]),
        "unit": data["current_units"]["temperature_2m"],
    }


def get_location(lat, lon):
    """Location metadata that API v2 does not change."""
    data = fetch_forecast(lat, lon)
    return {
        "latitude": data["latitude"],
        "longitude": data["longitude"],
        "timezone": data["timezone"],
    }
