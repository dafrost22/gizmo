"""The demo server's everyday tools, with the network stubbed out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import requests

from gizmo.demo_server import server

TOKYO = {"name": "Tokyo", "admin1": "Tokyo", "country": "Japan", "country_code": "JP", "latitude": 35.69,
         "longitude": 139.69, "timezone": "Asia/Tokyo"}
PORTLANDS = [
    {"name": "Portland", "admin1": "Oregon", "country": "United States", "country_code": "US", "latitude": 45.52,
     "longitude": -122.68, "timezone": "America/Los_Angeles"},
    {"name": "Portland", "admin1": "Maine", "country": "United States", "country_code": "US", "latitude": 43.66,
     "longitude": -70.26, "timezone": "America/New_York"},
]
FORECAST = {
    "timezone": "Asia/Tokyo", "utc_offset_seconds": 32400, "timezone_abbreviation": "GMT+9",
    "current": {"temperature_2m": 11.4, "apparent_temperature": 9.6, "weather_code": 61, "wind_speed_10m": 18.2},
    "daily": {"time": ["2026-11-13", "2026-11-14"], "weather_code": [63, 2], "temperature_2m_max": [16.2, 18.0],
              "temperature_2m_min": [9.1, 10.3], "precipitation_probability_max": [80, None]},
}


@pytest.fixture
def net(monkeypatch):
    """No network: places, forecasts and rates come from here, and every call is recorded."""
    calls = []

    def get_json(url, **params):
        calls.append((url, params))
        if url == server.PLACES:
            name = params["name"].lower()
            return {"results": [TOKYO] if name == "tokyo" else PORTLANDS if name == "portland" else []}
        if url == server.METEO:
            return FORECAST
        if url == server.RATES:
            if params["base"] == "ZZZ":
                raise requests.HTTPError("404")
            return {"base": params["base"], "date": "2026-11-13", "rates": {params["symbols"]: 160.0}}
        raise AssertionError(url)

    monkeypatch.setattr(server, "get_json", get_json)
    monkeypatch.setattr(server, "_cache", {})
    monkeypatch.setenv("EXPLORER_TZ", "America/New_York")
    monkeypatch.setenv("EXPLORER_LAT", "28.54")
    monkeypatch.setenv("EXPLORER_LON", "-81.38")
    monkeypatch.setenv("EXPLORER_PLACE", "Orlando")
    return calls


def test_calculate_works_sums_out_and_runs_nothing_else():
    assert server.calculate("17 / 100 * 2340") == "17 / 100 * 2340 = 397.8"
    assert server.calculate("0.1 + 0.2") == "0.1 + 0.2 = 0.3"  # not 0.30000000000000004
    assert server.calculate("2 ** 10") == "2 ** 10 = 1,024"
    assert server.calculate("5 × 4 ÷ 2") == "5 * 4 / 2 = 10"
    assert server.calculate("2^8") == "2**8 = 256"
    assert server.calculate("round(sqrt(2), 3)").endswith("= 1.414")
    assert server.calculate("max(3, 100) - min(3, 100)").endswith("= 97")
    assert "divides by zero" in server.calculate("1 / 0")
    assert "too big" in server.calculate("9 ** 9 ** 9") and "too big" in server.calculate("1e308 * 10")
    assert "isn't a real number" in server.calculate("(-8) ** 0.5")
    assert "isn't a real number" in server.calculate("sqrt(-1)")
    for sneaky in ("__import__('os').getcwd()", "open('/etc/passwd')", "(1).__class__", "x + 1", "'a' * 3",
                   "[1, 2]", "lambda: 1", "17% of 2340", "round(2.5, ndigits=0)", "1 if 2 else 3", "9" * 300):
        assert server.calculate(sneaky).startswith("I couldn't work that out"), sneaky


def test_convert_knows_units_however_they_are_said():
    assert server.convert(5, "miles", "km") == "5 mi = 8.047 km"
    assert server.convert(26.2, "mi", "Kilometres") == "26.2 mi = 42.16 km"
    assert server.convert(72, "kg", "pounds") == "72 kg = 158.7 lb"
    assert server.convert(100, "Fahrenheit", "celsius") == "100 °F = 37.8 °C"
    assert server.convert(0, "°C", "K") == "0 °C = 273.1 K" or server.convert(0, "°C", "K") == "0 °C = 273.2 K"
    assert server.convert(3, "hours", "minutes") == "3 h = 180 min"
    assert server.convert(60, "miles per hour", "kph") == "60 mph = 96.56 km/h"
    assert server.convert(2, "cups", "ml") == "2 cup = 473.2 ml (US measures)"
    assert server.convert(1, "stone", "lbs") == "1 st = 14 lb"
    assert "colder than absolute zero" in server.convert(-500, "c", "f")
    assert server.convert(5, "km", "kg") == "I can't convert length to weight: km and kg measure different things."
    assert server.convert(5, "parsecs", "km") == "I don't know the unit parsecs."


def test_convert_changes_money_at_the_days_rate(net):
    said = server.convert(100, "USD", "JPY")
    assert said.startswith("100 USD = 16,000 JPY, at 1 USD = 160 JPY") and "2026-11-13" in said
    assert server.convert(100, "dollars", "yen") == said and len(net) == 1  # by name too, and the rate is kept
    assert server.convert(50, "pounds", "euros").startswith("50 GBP = 8,000 EUR")  # sterling, beside a currency
    assert server.convert(50, "eur", "EUR") == "50 EUR is the same currency."
    assert server.convert(10, "ZZZ", "USD").startswith("I couldn't get an exchange rate from ZZZ to USD")
    assert "money" in server.convert(10, "USD", "km")


def test_weather_is_one_spoken_line_in_both_units(net):
    said = server.weather("Tokyo")
    assert said.startswith("Tokyo, Japan, right now: light rain, 11 °C (53 °F), feels like 10 °C (49 °F), "
                           "wind 18 km/h (11 mph).")
    assert "Today (Friday, November 13): rain, high 16 °C (61 °F), low 9 °C (48 °F), 80 percent chance of rain or snow." in said
    assert "Tomorrow (Saturday, November 14): partly cloudy, high 18 °C (64 °F), low 10 °C (51 °F)." in said  # no chance given: not said
    assert said.endswith("(Open-Meteo)") and "rough guide" not in said
    assert net[-1][1]["forecast_days"] == 2  # today and tomorrow unless asked for more
    assert server.weather("").startswith("Orlando, right now:")  # home
    assert net[-1][1]["latitude"] == 28.54
    assert server.weather("Atlantis") == "I couldn't find a place called Atlantis."
    asked = len(net)
    assert server.weather("Tokyo") == said and len(net) == asked  # the same place again: the forecast it has


def test_weather_reaches_a_trip_days_away(net, monkeypatch):
    def forecast(url, **params):
        net.append((url, params))
        if url == server.PLACES:
            return {"results": [TOKYO]}
        n = params["forecast_days"]
        return {"current": FORECAST["current"],
                "daily": {"time": [f"2026-11-{19 + i}" for i in range(n)], "weather_code": [0] * n,
                          "temperature_2m_max": [20.0 + i for i in range(n)], "temperature_2m_min": [10.0] * n,
                          "precipitation_probability_max": [10] * 3 + [None] * (n - 3)}}

    monkeypatch.setattr(server, "get_json", forecast)
    said = server.weather("Tokyo", days=9)
    assert net[-1][1]["forecast_days"] == 9
    assert "Today (Thursday, November 19): clear sky, high 20 °C" in said and "Tomorrow (Friday, November 20):" in said
    assert "Saturday, November 21: clear sky, high 22 °C (72 °F), low 10 °C (50 °F), 10 percent" in said
    assert "Friday, November 27: clear sky, high 28 °C (82 °F), low 10 °C (50 °F)." in said  # the ninth day, the last
    assert "November 28" not in said and said.endswith("A forecast more than a week ahead is only a rough guide. (Open-Meteo)")
    server.weather("Tokyo", days=40)
    assert net[-1][1]["forecast_days"] == 16  # as far as the forecast goes
    server.weather("Tokyo", days=0)
    assert net[-1][1]["forecast_days"] == 1
    asked = len(net)
    assert server.weather("Tokyo", days=9) == said and len(net) == asked  # the same days again: the forecast it has


def test_a_place_after_a_comma_picks_between_namesakes(net):
    assert server.find_place("Portland")["admin1"] == "Oregon"  # the best known
    assert server.find_place("Portland, Maine")["admin1"] == "Maine"
    assert server.find_place("Portland, US")["admin1"] == "Oregon"
    assert server.find_place("Portland, Narnia")["admin1"] == "Oregon"  # nothing matches: still the best known
    assert server.place_name(server.find_place("Portland, Maine")) == "Portland, Maine, United States"
    assert server.place_name(TOKYO) == "Tokyo, Japan" and server.find_place(" , ") is None


def test_local_time_says_the_gap_from_home(net, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 13, 17, 15, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(server, "datetime", Clock)
    assert server.local_time("Tokyo") == ("In Tokyo, Japan it is 2:15 AM on Saturday, November 14 (already tomorrow "
                                          "there). That is 14 hours ahead of home (Orlando), where it is 12:15 PM.")
    assert server.local_time("Asia/Kolkata") == ("In Kolkata it is 10:45 PM on Friday, November 13. That is 10.5 "
                                                 "hours ahead of home (Orlando), where it is 12:15 PM.")
    assert net == [(server.PLACES, {"name": "Tokyo", "count": 10, "language": "en"})]  # a zone's name needs no lookup
    assert "3 hours behind home" in server.local_time("Portland, Oregon")
    assert "the same time as at home (Orlando)" in server.local_time("America/New_York")
    assert server.local_time("Atlantis") == "I couldn't find a place called Atlantis."


def test_home_zone_is_the_machines_unless_it_was_never_set(net, monkeypatch):
    assert str(server.home_zone()) == "America/New_York"  # EXPLORER_TZ
    monkeypatch.delenv("EXPLORER_TZ")
    monkeypatch.setattr(server, "machine_offset", lambda: timedelta(hours=-5))
    assert server.home_zone() is None and net == []  # a laptop that knows where it is: its own zone, no lookup
    monkeypatch.setattr(server, "machine_offset", lambda: timedelta(0))  # a robot left on UTC
    assert str(server.home_zone()) == "Asia/Tokyo" and net[-1][1]["latitude"] == 28.54  # the zone at home
    assert server.when(datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)).endswith("at 12:00 PM")  # 3:00 UTC, there


def test_warm_up_looks_home_up_before_the_first_question(net, monkeypatch):
    monkeypatch.delenv("EXPLORER_TZ")
    monkeypatch.setattr(server, "machine_offset", lambda: timedelta(0))  # a robot left on UTC
    server.warm_up().join(5)
    looked_up = len(net)
    assert looked_up == 1 and net[0][0] == server.METEO  # home's zone (its coordinates come from the environment)
    # The stub puts home in Tokyo's zone. The first question then has nothing to wait for.
    assert "the same time as at home" in server.local_time("Asia/Tokyo") and len(net) == looked_up
    monkeypatch.setattr(server, "get_json", lambda url, **params: (_ for _ in ()).throw(requests.ConnectionError()))
    monkeypatch.setattr(server, "_cache", {})
    server.warm_up().join(5)  # offline: it says so in the log and the server carries on
