"""Locked tests for the PatchPilot demo.

Rules:
- These tests must never be edited after the first commit. PatchPilot hashes this folder.
- Expected values are NOT constants. Each test reads the raw proxy response itself and
  compares the client's result with the value in that response. A patch that hard-codes
  a number cannot pass when the source value changes.
- The proxy tells which stage it serves in the X-PatchPilot-Stage header (v1 or v2).
  The tests use that header only to know where the source value sits in the raw JSON.
"""

import os

import pytest
import requests

from client import weather_client

LAT = 37.57
LON = 126.98

# Where each value sits in the raw response, per proxy stage.
PATHS = {
    "v1": {"block": "current_weather", "temp": "temperature", "units": "current_weather_units"},
    "v2": {"block": "current", "temp": "temperature_2m", "units": "current_units"},
}


def _base_url():
    return os.environ.get("WEATHER_BASE_URL", "http://127.0.0.1:8787").rstrip("/")


@pytest.fixture(scope="module")
def source():
    """Raw proxy response and its stage, read directly by the test."""
    params = {"latitude": LAT, "longitude": LON, "current_weather": "true"}
    resp = requests.get(_base_url() + "/forecast", params=params, timeout=10)
    resp.raise_for_status()
    stage = resp.headers.get("X-PatchPilot-Stage")
    assert stage in PATHS, f"proxy did not send a known X-PatchPilot-Stage header: {stage!r}"
    return stage, resp.json()


def test_current_temperature_matches_source(source):
    """T1: current temperature equals the value in the raw response."""
    stage, raw = source
    p = PATHS[stage]
    expected = float(raw[p["block"]][p["temp"]])
    assert weather_client.get_current_temperature(LAT, LON) == expected


def test_current_summary_fields(source):
    """T2: summary fields equal the current block in the raw response."""
    stage, raw = source
    p = PATHS[stage]
    block = raw[p["block"]]
    summary = weather_client.get_current_summary(LAT, LON)
    assert summary["time"] == block["time"]
    assert summary["temperature"] == float(block[p["temp"]])
    assert summary["windspeed"] == float(block["windspeed"])


def test_temperature_unit_matches_source(source):
    """T3: temperature unit equals the units block in the raw response."""
    stage, raw = source
    p = PATHS[stage]
    expected = raw[p["units"]][p["temp"]]
    assert weather_client.get_temperature_unit(LAT, LON) == expected


def test_location_unchanged(source):
    """T4: top-level location fields that v2 does not change still read correctly."""
    _, raw = source
    loc = weather_client.get_location(LAT, LON)
    assert loc["latitude"] == raw["latitude"]
    assert loc["longitude"] == raw["longitude"]
    assert loc["timezone"] == raw["timezone"]
