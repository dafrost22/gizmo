"""The demo MCP server ("explorer"): everyday tools (weather, world time, conversion, a calculator, an encyclopedia)
and live space data and flags, from free sources with no keys, and the Agent Skills that say how to use them,
served over MCP (SEP-2640, see skills_ext.py). What Gizmo has to try out of the box.

It runs inside the robot app at http://127.0.0.1:8772/mcp (the server the app calls "explorer"), so it works on
every backend wherever Gizmo has internet, and the AgentCore brain runs its own copy the same way. A host puts its
own name for the server in front of each tool (explorer_space_station), so the skills call the tools by their bare
names. On its own, to try it from any MCP client: python -m gizmo.demo_server [--port N] [--skills DIR].

Sources, all free with no account:
- Open-Meteo: the weather, and where a named place is and its time zone.
- Frankfurter: currency rates (the European Central Bank's daily reference rates).
- wheretheiss.at: where the International Space Station is; BigDataCloud: what's under it.
- The Space Devs' Launch Library 2: rocket launches and who's in space (15 calls an hour without a key,
  so cached for an hour or more). Open Notify's list of astronauts is years out of date: not used.
- NASA/JPL's close-approach API: asteroids passing Earth.
- Wikipedia: flags ("Flag of Japan"), and short Simple English summaries for anything else.
- astronomy-engine (computed here, no network): the sky tonight and the Moon.
Home is EXPLORER_LAT / EXPLORER_LON / EXPLORER_PLACE (the environment), else where our IP address says we
are. Times are said in home's time zone: EXPLORER_TZ, else this machine's, else (a robot's clock is often left
on UTC) the one at home's coordinates. Every answer is short plain text for a voice to read, numbers already
rounded.

The skills are the folders in skills/. Each shows a different thing a skill can be:
- universe, world-countries: facts a model gets wrong, plus which live tool to use.
- trip-briefing: a procedure that runs several tools and says how to give the answer.
- quiz-night: a small SKILL.md whose question rounds are separate files, read only when a round is chosen.
- about-this-robot: how the robot works, with the details in reference files.
They are read from disk on every request, so editing a file while the server runs is enough: the next load gets
the new text, with a new digest.
"""

from __future__ import annotations

import ast
import logging
import math
import operator
import os
import re
import socket
import threading
import time
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import astronomy as ae
import requests
from mcp.server.mcpserver import MCPServer

from .skills_ext import serve_skills, skill_uris

log = logging.getLogger("gizmo.explorer")
UA = {"User-Agent": "GizmoDemoServer/1.0 (the demo MCP server of an open-source talking robot)"}
METEO = "https://api.open-meteo.com/v1/forecast"
PLACES = "https://geocoding-api.open-meteo.com/v1/search"
RATES = "https://api.frankfurter.dev/v1/latest"
LL2 = "https://ll.thespacedevs.com/2.3.0"
MOON_KM = 384_400
AU_KM = 149_597_871
PLANETS = [ae.Body.Mercury, ae.Body.Venus, ae.Body.Mars, ae.Body.Jupiter, ae.Body.Saturn, ae.Body.Uranus,
           ae.Body.Neptune]
COMPASS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"]

http = requests.Session()
http.headers.update(UA)
_cache: dict[str, tuple[float, object]] = {}


def cached(key: str, ttl_s: float, fetch):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    value = fetch()
    _cache[key] = (time.monotonic(), value)
    return value


def get_json(url: str, **params):
    r = http.get(url, params=params or None, timeout=6)
    r.raise_for_status()
    return r.json()


def home() -> tuple[float, float, str]:
    """(latitude, longitude, place name) for the sky and the weather: the environment, else our IP address's
    location, named with its country ("Lisbon, Portugal") so a model can tell which units its listener uses."""
    if os.environ.get("EXPLORER_LAT") and os.environ.get("EXPLORER_LON"):
        return float(os.environ["EXPLORER_LAT"]), float(os.environ["EXPLORER_LON"]), os.environ.get("EXPLORER_PLACE", "home")

    def lookup():
        d = get_json("https://api.bigdatacloud.net/data/reverse-geocode-client", localityLanguage="en")
        town = d.get("city") or d.get("locality") or "home"
        return float(d["latitude"]), float(d["longitude"]), ", ".join(filter(None, [town, d.get("countryName")]))

    return cached("home", 24 * 3600, lookup)


def zone_at(lat: float, lon: float) -> tzinfo:
    """The time zone at a point on Earth. Without a time zone database (a bare container), its offset today."""
    d = get_json(METEO, latitude=lat, longitude=lon, current="temperature_2m", timezone="auto")
    try:
        return ZoneInfo(d["timezone"])
    except (KeyError, ValueError, OSError):  # ZoneInfoNotFoundError is a KeyError
        return timezone(timedelta(seconds=d["utc_offset_seconds"]), d.get("timezone_abbreviation") or None)


def machine_offset() -> timedelta:
    """How far this machine's clock is set from UTC."""
    return datetime.now().astimezone().utcoffset()


def home_zone() -> tzinfo | None:
    """Home's time zone: EXPLORER_TZ, else this machine's (None). A machine on UTC has usually never been told
    where it is (a robot, a container), so then it's the zone at home's coordinates."""
    if os.environ.get("EXPLORER_TZ"):
        return ZoneInfo(os.environ["EXPLORER_TZ"])
    if machine_offset():
        return None
    try:
        return cached("home_zone", 24 * 3600, lambda: zone_at(*home()[:2]))
    except (requests.RequestException, KeyError, ValueError):
        return None


def local(t: datetime) -> datetime:
    return t.astimezone(home_zone())


def when(t: datetime) -> str:
    """'tonight at 8:15 PM', 'tomorrow at 6:02 AM', 'Saturday, November 1 at 3:00 PM'."""
    t, now = local(t), local(datetime.now(timezone.utc))
    clock = t.strftime("%I:%M %p").lstrip("0")
    days = (t.date() - now.date()).days
    if days == 0:
        return ("tonight" if t.hour >= 18 else "today") + f" at {clock}"
    if days == 1:
        return f"tomorrow at {clock}"
    return t.strftime("%A, %B ") + str(t.day) + f" at {clock}"


def km_and_miles(km: float) -> str:
    return f"{round(km):,} kilometres ({round(km * 0.621371):,} miles)"


def compass(azimuth: float) -> str:
    return COMPASS[int((azimuth + 22.5) % 360 // 45)]


PORT = int(os.environ.get("EXPLORER_PORT", "8772"))
SKILLS = Path(__file__).parent / "skills"
INSTRUCTIONS = ("Gizmo's demo server: the weather, the time anywhere, unit and currency conversion, a calculator, an "
                "encyclopedia, flags and live space data. Answers are short plain text for a voice to read aloud.")

# --- everyday tools ---------------------------------------------------------------------------------------

SKY = {0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow",
       73: "snow", 75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers",
       85: "snow showers", 86: "heavy snow showers", 95: "a thunderstorm", 96: "a thunderstorm with hail",
       99: "a thunderstorm with hail"}  # the World Meteorological Organization's weather codes


def tidy(x: float, figures: int = 4) -> str:
    """A number for saying aloud: whole when it is, else about `figures` significant figures. 8.04672 -> 8.047."""
    if x == 0 or not math.isfinite(x):
        return "0" if x == 0 else str(x)
    if float(x).is_integer() and abs(x) < 1e15:
        return f"{int(x):,}"
    places = max(0, min(12, figures - 1 - math.floor(math.log10(abs(x)))))
    text = f"{x:,.{places}f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def degrees(c: float) -> str:
    return f"{round(c)} °C ({round(c * 9 / 5 + 32)} °F)"


def find_place(name: str) -> dict | None:
    """A named place: {name, country, latitude, longitude, timezone}. "Portland, Oregon" is the Portland whose
    region or country matches what follows the comma; otherwise the best-known place of that name."""
    first, _, rest = (part.strip() for part in name.strip().partition(","))
    if not first:
        return None

    def lookup():
        return get_json(PLACES, name=first, count=10, language="en").get("results") or []

    hits = cached(f"place:{first.lower()}", 24 * 3600, lookup)
    wanted = rest.lower()
    if wanted:
        hits = [h for h in hits if wanted in (str(h.get("admin1", "")).lower(), str(h.get("country", "")).lower(),
                                              str(h.get("country_code", "")).lower())] or hits
    return hits[0] if hits else None


def place_name(p: dict) -> str:
    """'Tokyo, Japan'; 'Portland, Oregon, United States'; 'Japan'."""
    parts = [p["name"]] + [p[k] for k in ("admin1", "country") if p.get(k)]
    return ", ".join(dict.fromkeys(parts))  # each name once, in order


def weather(place: str = "", days: int = 2) -> str:
    """The weather right now and the forecast for the days ahead, anywhere in the world.

    Args:
        place: a city or town, e.g. "Tokyo" or "Portland, Oregon". Leave it empty for the weather at home.
        days: how many days of forecast to give, counting today: 2 is today and tomorrow, 7 the week ahead, and
            14 reaches all of next week. 16 at most.
    """
    if place.strip():
        if (p := find_place(place)) is None:
            return f"I couldn't find a place called {place}."
        lat, lon, where = p["latitude"], p["longitude"], place_name(p)
    else:
        lat, lon, where = home()
    days = max(1, min(16, int(days)))
    d = cached(f"weather:{lat:.2f},{lon:.2f}:{days}", 5 * 60, lambda: get_json(  # asked again: no second request
        METEO, latitude=lat, longitude=lon, timezone="auto", forecast_days=days,
        current="temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        daily="weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"))
    now, daily = d["current"], d["daily"]
    wind = now["wind_speed_10m"]
    out = [f"{where}, right now: {SKY.get(now['weather_code'], 'mixed weather')}, {degrees(now['temperature_2m'])}, "
           f"feels like {degrees(now['apparent_temperature'])}, wind {round(wind)} km/h ({round(wind * 0.621371)} mph)."]
    dates = daily.get("time") or []
    for i in range(min(days, len(daily["weather_code"]))):
        try:  # each day with its date, so "next week" or "on Saturday" can be picked out
            on = date.fromisoformat(dates[i])
            named = on.strftime("%A, %B ") + str(on.day)
        except (IndexError, ValueError):
            named = ""
        day = ("Today", "Tomorrow")[i] + (f" ({named})" if named else "") if i < 2 else named or f"In {i} days"
        rain = daily["precipitation_probability_max"][i]
        chance = "" if rain is None else f", {round(rain)} percent chance of rain or snow"
        out.append(f"{day}: {SKY.get(daily['weather_code'][i], 'mixed weather')}, high "
                   f"{degrees(daily['temperature_2m_max'][i])}, low {degrees(daily['temperature_2m_min'][i])}{chance}.")
    far = " A forecast more than a week ahead is only a rough guide." if days > 7 else ""
    return " ".join(out) + far + " (Open-Meteo)"


def local_time(place: str) -> str:
    """The time and date right now in another city or country, and how many hours ahead of or behind home it is.

    Args:
        place: a city or country, e.g. "Sydney" or "India", or a time zone name such as "Asia/Tokyo".
    """
    name, zone = place.strip(), None
    if "/" in name:  # a time zone's own name needs no lookup
        try:
            zone, where = ZoneInfo(name), name.split("/")[-1].replace("_", " ")
        except (KeyError, ValueError, OSError):
            pass
    if zone is None:
        if (p := find_place(name)) is None:
            return f"I couldn't find a place called {place}."
        where = place_name(p)
        try:
            zone = ZoneInfo(p["timezone"])
        except (KeyError, ValueError, OSError):
            zone = zone_at(p["latitude"], p["longitude"])
    utc = datetime.now(timezone.utc)
    there, here = utc.astimezone(zone), local(utc)
    hours = (there.utcoffset() - here.utcoffset()).total_seconds() / 3600
    gap = "the same time as at home" if hours == 0 else \
        f"{tidy(abs(hours))} hour{'' if abs(hours) == 1 else 's'} {'ahead of' if hours > 0 else 'behind'} home"
    day = {1: " (already tomorrow there)", -1: " (still yesterday there)"}.get((there.date() - here.date()).days, "")
    try:
        at_home = f" ({home()[2]})"
    except (requests.RequestException, KeyError, ValueError):
        at_home = ""
    clock = "%I:%M %p"
    return (f"In {where} it is {there.strftime(clock).lstrip('0')} on {there.strftime('%A, %B ')}{there.day}{day}. "
            f"That is {gap}{at_home}, where it is {here.strftime(clock).lstrip('0')}.")


# Each unit's size in its family's base unit (metres, kilograms, litres, metres a second, seconds). Cups, pints,
# gallons and tons are the US ones ("t" is the metric tonne).
US_MEASURES = {"tsp", "tbsp", "fl oz", "cup", "pt", "qt", "gal", "ton"}
UNITS = {
    "length": {"mm": 0.001, "cm": 0.01, "m": 1, "km": 1000, "in": 0.0254, "ft": 0.3048, "yd": 0.9144,
               "mi": 1609.344, "nmi": 1852},
    "weight": {"mg": 1e-6, "g": 0.001, "kg": 1, "t": 1000, "oz": 0.028349523125, "lb": 0.45359237, "st": 6.35029318,
               "ton": 907.18474},
    "volume": {"ml": 0.001, "l": 1, "tsp": 0.00492892159375, "tbsp": 0.01478676478125, "fl oz": 0.0295735295625,
               "cup": 0.2365882365, "pt": 0.473176473, "qt": 0.946352946, "gal": 3.785411784},
    "speed": {"m/s": 1, "km/h": 1 / 3.6, "mph": 0.44704, "kn": 1852 / 3600},
    "time": {"s": 1, "min": 60, "h": 3600, "day": 86400, "week": 604800},
}
TEMPERATURES = {"c": "°C", "f": "°F", "k": "K"}
SPELLED = {
    "millimeter": "mm", "centimeter": "cm", "meter": "m", "kilometer": "km", "inch": "in", "inches": "in",
    "foot": "ft", "feet": "ft", "yard": "yd", "mile": "mi", "nautical mile": "nmi",
    "milligram": "mg", "gram": "g", "kilogram": "kg", "kilo": "kg", "tonne": "t", "metric ton": "t", "ounce": "oz",
    "pound": "lb", "lbs": "lb", "stone": "st",
    "milliliter": "ml", "liter": "l", "teaspoon": "tsp", "tablespoon": "tbsp", "fluid ounce": "fl oz", "floz": "fl oz",
    "pint": "pt", "quart": "qt", "gallon": "gal",
    "meter per second": "m/s", "meters per second": "m/s", "kilometer per hour": "km/h", "kilometers per hour": "km/h",
    "kph": "km/h", "kmh": "km/h", "mile per hour": "mph", "miles per hour": "mph", "knot": "kn", "kt": "kn",
    "second": "s", "sec": "s", "minute": "min", "hour": "h", "hr": "h", "d": "day", "wk": "week",
    "celsius": "c", "centigrade": "c", "°c": "c", "fahrenheit": "f", "°f": "f", "kelvin": "k",
}
MONEY = {"dollar": "USD", "us dollar": "USD", "euro": "EUR", "yen": "JPY", "pound sterling": "GBP",
         "british pound": "GBP", "swiss franc": "CHF", "canadian dollar": "CAD", "australian dollar": "AUD",
         "rupee": "INR", "yuan": "CNY", "peso": "MXN", "won": "KRW", "real": "BRL"}


def unit(text: str) -> tuple[str, str] | None:
    """('length', 'km') for "Kilometres"; ('temperature', 'c'); ('money', 'USD'); None for nothing we know."""
    raw = " ".join(text.strip().split())
    word = raw.lower().rstrip(".").replace("metre", "meter").replace("litre", "liter")
    for name in (word, word.removesuffix("s"), word.removesuffix("es")):
        name = SPELLED.get(name, name)
        if name in TEMPERATURES:
            return "temperature", name
        for family, units in UNITS.items():
            if name in units:
                return family, name
        if name in MONEY:
            return "money", MONEY[name]
    return ("money", raw.upper()) if re.fullmatch(r"[A-Za-z]{3}", raw) else None


def convert(amount: float, from_unit: str, to_unit: str) -> str:
    """Convert an amount between units of length, weight, volume, speed, time or temperature, or between
    currencies at today's exchange rate.

    Args:
        amount: how much, e.g. 5.
        from_unit: the unit it is in, e.g. "miles", "kg", "fahrenheit", or a currency code like "USD".
        to_unit: the unit you want, e.g. "km", "pounds", "celsius", or a currency code like "JPY".
    """
    a, b = unit(from_unit), unit(to_unit)
    if a is None or b is None:
        return f"I don't know the unit {from_unit if a is None else to_unit}."
    if "money" in (a[0], b[0]):  # "pounds" beside a currency is sterling, not weight
        a, b = (("money", "GBP") if x == ("weight", "lb") else x for x in (a, b))
    if a[0] != b[0]:
        return f"I can't convert {a[0]} to {b[0]}: {from_unit} and {to_unit} measure different things."
    family, source, target = a[0], a[1], b[1]
    if family == "money":
        if source == target:
            return f"{tidy(amount)} {source} is the same currency."
        try:
            d = cached(f"rate:{source}:{target}", 3 * 3600, lambda: get_json(RATES, base=source, symbols=target))
            rate = d["rates"][target]
        except (requests.RequestException, KeyError, ValueError):
            return f"I couldn't get an exchange rate from {source} to {target}. I know the main world currencies, by code."
        return (f"{tidy(amount)} {source} = {tidy(amount * rate)} {target}, at 1 {source} = {tidy(rate)} {target} "
                f"(the European Central Bank's reference rate for {d['date']}; a bank or card will give a little less).")
    if family == "temperature":
        kelvin = {"c": amount + 273.15, "f": (amount - 32) * 5 / 9 + 273.15, "k": amount}[source]
        if kelvin < 0:
            return "That is colder than absolute zero, which nothing can be."
        result = {"c": kelvin - 273.15, "f": (kelvin - 273.15) * 9 / 5 + 32, "k": kelvin}[target]
        return f"{tidy(amount)} {TEMPERATURES[source]} = {tidy(round(result, 1))} {TEMPERATURES[target]}"
    us = " (US measures)" if {source, target} & US_MEASURES else ""
    return f"{tidy(amount)} {source} = {tidy(amount * UNITS[family][source] / UNITS[family][target])} {target}{us}"


OPERATORS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
             ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
             ast.USub: operator.neg, ast.UAdd: operator.pos}
FUNCTIONS = {"sqrt": math.sqrt, "round": round, "abs": abs, "min": min, "max": max, "log": math.log,
             "log10": math.log10, "log2": math.log2, "exp": math.exp, "sin": math.sin, "cos": math.cos,
             "tan": math.tan, "floor": math.floor, "ceil": math.ceil}
CONSTANTS = {"pi": math.pi, "e": math.e}


def _evaluate(node: ast.AST) -> float:
    """The value of an arithmetic expression's syntax tree: numbers, the operators and functions above, nothing
    else (no names, attributes or calls that could reach the rest of Python)."""
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if isinstance(node, ast.Name) and node.id in CONSTANTS:
        return CONSTANTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in OPERATORS:
        return OPERATORS[type(node.op)](_evaluate(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) * math.log10(max(abs(left), 10)) > 300:
            raise OverflowError  # before Python spends a minute on 9 ** 9 ** 9
        return OPERATORS[type(node.op)](left, right)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
            and not node.keywords):
        return FUNCTIONS[node.func.id](*(_evaluate(arg) for arg in node.args))
    raise ValueError("only numbers, + - * / // % **, brackets, and " + ", ".join(FUNCTIONS) + ", pi, e")


def calculate(expression: str) -> str:
    """Work out arithmetic exactly. Use it for any sum, rather than working it out yourself.

    Args:
        expression: numbers with + - * / ** and brackets, and sqrt, round, abs, min, max, log, sin, cos, pi,
            e.g. "17 / 100 * 2340" or "sqrt(2) * (3 + 4) ** 2". Write a percentage as a division by 100
            (% is the remainder), and big numbers without commas.
    """
    text = expression.strip().replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-")
    try:
        if len(text) > 200:
            raise ValueError("that is too long")
        value = _evaluate(ast.parse(text, mode="eval"))
        if isinstance(value, complex):
            raise ValueError("the answer isn't a real number")
        if not math.isfinite(value):
            raise OverflowError
    except ZeroDivisionError:
        return "I couldn't work that out: it divides by zero."
    except OverflowError:
        return "I couldn't work that out: the answer is too big."
    except (ValueError, TypeError, SyntaxError, RecursionError) as e:
        reason = ("it isn't arithmetic I can read" if isinstance(e, (SyntaxError, RecursionError)) else
                  "the answer isn't a real number" if "domain" in str(e) else str(e))
        return f"I couldn't work that out: {reason}."
    return f"{text} = {tidy(value, 10)}"


# --- space, flags and the encyclopedia ----------------------------------------------------------------------


def space_station() -> str:
    """Where the International Space Station is right now: over which country or ocean, how high and how fast."""
    iss = get_json("https://api.wheretheiss.at/v1/satellites/25544")
    lat, lon = iss["latitude"], iss["longitude"]
    place = None
    try:
        g = get_json("https://api.bigdatacloud.net/data/reverse-geocode-client", latitude=lat, longitude=lon,
                     localityLanguage="en")
        if g.get("countryName"):
            region = g.get("principalSubdivision")
            place = f"{region}, in {g['countryName']}" if region else g["countryName"]
        else:
            water = [i["name"] for i in (g.get("localityInfo") or {}).get("informative", [])
                     if any(w in i.get("name", "") for w in ("Ocean", "Sea", "Gulf", "Bay"))]
            place = f"the {water[0]}" if water else "the ocean"
    except requests.RequestException:
        pass
    where = f"over {place}" if place else f"at latitude {lat:.0f}, longitude {lon:.0f}"
    side = "on the night side of Earth" if iss.get("visibility") == "eclipsed" else "in sunlight"
    return (f"The International Space Station is {where} right now. It is {round(iss['altitude'])} kilometres "
            f"({round(iss['altitude'] * 0.621371)} miles) up, moving at {km_and_miles(iss['velocity'])} an hour, "
            f"so it goes all the way around Earth about every 90 minutes. It is {side}.")


def people_in_space() -> str:
    """How many people are in space right now, their names and space agencies."""
    def fetch():
        return get_json(f"{LL2}/astronauts/", in_space="true", limit=40)["results"]

    # The list counts SpaceX's Starman (the mannequin in the Tesla, launched 2018) as "Non-Human": not a person.
    people = [p for p in cached("in_space", 6 * 3600, fetch) if (p.get("type") or {}).get("name") != "Non-Human"]
    names = [f"{p['name']} ({(p.get('agency') or {}).get('abbrev') or (p.get('agency') or {}).get('name', '')})"
             for p in people]
    return f"{len(people)} people are in space right now: " + "; ".join(names) + "." if people else \
        "I couldn't find anyone listed in space right now."


def rocket_launches(count: int = 3) -> str:
    """The next rocket launches anywhere in the world: rocket, who launches it, from where, when and what for.

    Args:
        count: how many launches, 1 to 5.
    """
    def fetch():
        return get_json(f"{LL2}/launches/upcoming/", limit=8, hide_recent_previous="true")["results"]

    launches = cached("launches", 3600, fetch)[: max(1, min(5, count))]
    lines = []
    for l in launches:
        rocket = ((l.get("rocket") or {}).get("configuration") or {}).get("name") or l["name"].split("|")[0].strip()
        mission = (l.get("mission") or {})
        provider = (l.get("launch_service_provider") or {}).get("name", "")
        pad = (l.get("pad") or {}).get("location", {}).get("name", "")
        t = datetime.fromisoformat(l["net"].replace("Z", "+00:00"))
        status = (l.get("status") or {}).get("abbrev", "")
        sure = "" if status == "Go" else " (the time may change)"
        what = mission.get("name") or l["name"].split("|")[-1].strip()
        desc = (mission.get("description") or "").split(". ")[0].rstrip(". ")
        lines.append(f"{rocket} by {provider}, {when(t)}{sure}, from {pad}: {what}." + (f" {desc}." if desc else ""))
    return " ".join(lines) if lines else "I couldn't find any upcoming launches."


def close_asteroids(days: int = 7) -> str:
    """Asteroids flying past Earth in the next few days (all of them miss), with how close and how big.

    Args:
        days: how many days ahead, 1 to 30.
    """
    days = max(1, min(30, days))
    d = get_json("https://ssd-api.jpl.nasa.gov/cad.api", **{"dist-max": "0.05", "date-min": "now",
                                                              "date-max": f"+{days}", "sort": "dist"})
    now = datetime.now(timezone.utc)

    def at(r: dict) -> datetime:
        return datetime.strptime(r["cd"], "%Y-%b-%d %H:%M").replace(tzinfo=timezone.utc)

    rows = [r for row in d.get("data", []) if at(r := dict(zip(d["fields"], row))) > now]  # "now" means today
    if not rows:
        return f"No asteroids come closer than 20 times the Moon's distance in the next {days} days."
    out = []
    for r in rows[:3]:
        km = float(r["dist"]) * AU_KM
        h = float(r["h"]) if r.get("h") else None
        size = ""
        if h is not None:  # diameter from brightness, for a typical reflectivity (it could be half or twice this)
            metres = 1329 / math.sqrt(0.14) * 10 ** (-h / 5) * 1000
            metres = round(metres) if metres < 20 else round(metres, -1 if metres < 1000 else -2)
            size = f", about {metres:,.0f} metres across"
        out.append(f"asteroid {r['des']}{size}, {when(at(r))}, {km_and_miles(km)} away, "
                   f"which is {km / MOON_KM:.1f} times as far as the Moon")
    return (f"{len(rows)} asteroids pass Earth in the next {days} days. The closest: " + "; ".join(out) +
            ". They all fly past and miss Earth; scientists track them to be sure.")


def _moon_phase_name(angle: float) -> str:
    names = [(22.5, "new moon"), (67.5, "waxing crescent"), (112.5, "first quarter"), (157.5, "waxing gibbous"),
             (202.5, "full moon"), (247.5, "waning gibbous"), (292.5, "last quarter"), (337.5, "waning crescent")]
    return next((n for limit, n in names if angle < limit), "new moon")


def moon() -> str:
    """The Moon tonight: its phase, how much of it is lit, and when the next full moon and new moon are."""
    now = ae.Time.Now()
    angle = ae.MoonPhase(now)
    lit = ae.Illumination(ae.Body.Moon, now).phase_fraction
    full = ae.SearchMoonPhase(180, now, 40)
    new = ae.SearchMoonPhase(0, now, 40)
    return (f"The Moon is a {_moon_phase_name(angle)}, {round(lit * 100)} percent lit. "
            f"The next full moon is {when(full.Utc().replace(tzinfo=timezone.utc))} and the next new moon "
            f"{when(new.Utc().replace(tzinfo=timezone.utc))}.")


def sky_tonight() -> str:
    """Which planets can be seen from home tonight (or now, if it's already dark): where to look and how bright,
    plus sunset and the Moon."""
    lat, lon, place = home()
    observer = ae.Observer(lat, lon, 0)
    now = ae.Time.Now()
    sunset = ae.SearchRiseSet(ae.Body.Sun, observer, ae.Direction.Set, now.AddDays(-0.5), 1)
    dark = sunset.AddDays(1 / 24) if sunset is not None else now  # an hour after sunset
    if dark.ut < now.ut:
        sunrise = ae.SearchRiseSet(ae.Body.Sun, observer, ae.Direction.Rise, dark, 1)
        if sunrise is None or now.ut < sunrise.ut:
            dark = now  # it's already dark
        else:
            dark = ae.SearchRiseSet(ae.Body.Sun, observer, ae.Direction.Set, now, 1).AddDays(1 / 24)
    seen = []
    for body in PLANETS:
        eq = ae.Equator(body, dark, observer, True, True)
        hor = ae.Horizon(dark, observer, eq.ra, eq.dec, ae.Refraction.Normal)
        mag = ae.Illumination(body, dark).mag
        if hor.altitude < 8 or mag > 6:
            continue
        height = "low in the sky" if hor.altitude < 25 else "high up" if hor.altitude > 55 else "halfway up the sky"
        bright = "very bright" if mag < -1.5 else "bright" if mag < 1 else "easy to see" if mag < 3 else "faint (binoculars help)"
        seen.append(f"{body.name}, {bright}, {height} in the {compass(hor.azimuth)}")
    moon_eq = ae.Equator(ae.Body.Moon, dark, observer, True, True)
    moon_up = ae.Horizon(dark, observer, moon_eq.ra, moon_eq.dec, ae.Refraction.Normal).altitude > 0
    t = when(dark.Utc().replace(tzinfo=timezone.utc))
    planets = ("You can see " + "; ".join(seen) + ".") if seen else "No planets are easy to see then."
    moon_line = f" The Moon is up, a {_moon_phase_name(ae.MoonPhase(dark))}." if moon_up else " The Moon isn't up then."
    return f"Looking from {place}, {t}: {planets}{moon_line} Planets shine steadily; stars twinkle."


def _summary(site: str, title: str) -> dict | None:
    try:
        r = http.get(f"https://{site}/api/rest_v1/page/summary/{requests.utils.quote(title.replace(' ', '_'))}",
                     timeout=6)
        return r.json() if r.ok and r.json().get("type") == "standard" else None
    except (requests.RequestException, ValueError):
        return None


def _search(site: str, query: str) -> str | None:
    d = get_json(f"https://{site}/w/api.php", action="query", list="search", srsearch=query, srlimit=1,
                 format="json")
    hits = d.get("query", {}).get("search", [])
    return hits[0]["title"] if hits else None


def _short(text: str, limit: int = 700) -> str:
    return text if len(text) <= limit else text[: text.rfind(". ", 0, limit) + 1 or limit]


def flag(country: str) -> str:
    """What a country's flag looks like (colours, shapes, what they mean), from Wikipedia.

    Args:
        country: the country, e.g. "Japan" or "the United Kingdom".
    """
    name = country.strip().removeprefix("the ").removeprefix("The ")
    page = _summary("en.wikipedia.org", f"Flag of {name}") or _summary("en.wikipedia.org", f"Flag of the {name}")
    if page is None:  # Wikipedia follows redirects ("Flag of the United Kingdom" is "Union Jack"), else search
        title = _search("en.wikipedia.org", f"flag of {name}")
        page = _summary("en.wikipedia.org", title) if title else None
        if page and "flag" not in page.get("extract", "").lower():
            page = None
    if page is None:
        return f"I couldn't find the flag of {country}."
    return f"{page['title']}: {_short(page['extract'])} (Wikipedia)"


def encyclopedia(topic: str) -> str:
    """A short, simple encyclopedia summary of almost anything (a person, place, animal, invention, planet, star,
    mission, country, mountain, river), from Simple English Wikipedia, else Wikipedia.

    Args:
        topic: what to look up, e.g. "Betelgeuse", "Mount Everest" or "Ada Lovelace".
    """
    for site in ("simple.wikipedia.org", "en.wikipedia.org"):
        page = _summary(site, topic)
        if page is None:
            title = _search(site, topic)
            page = _summary(site, title) if title else None
        if page:
            return f"{page['title']}: {_short(page['extract'])} (Wikipedia)"
    return f"I couldn't find anything about {topic}."


TOOLS = (weather, local_time, convert, calculate, encyclopedia, flag,
         space_station, people_in_space, rocket_launches, close_asteroids, moon, sky_tonight)


def build(skills: Path = SKILLS) -> MCPServer:
    """The server: the tools above, and the skills in the folder `skills` served over MCP."""
    # A model that never sees a skills listing can still find them: the instructions name each SKILL.md.
    uris = skill_uris(skills)
    pointer = f" It also serves Agent Skills that say how to use these tools well: {', '.join(uris)}." if uris else ""
    # WARNING keeps uvicorn's access log out of the app's log.
    server = MCPServer("explorer", instructions=INSTRUCTIONS + pointer, log_level="WARNING")
    for fn in TOOLS:
        server.add_tool(fn)
    serve_skills(server, skills)
    return server


# Built at import, which also calls logging.basicConfig: the app imports this module after its own logging setup.
mcp = build()


def serve(server: MCPServer = mcp, port: int = PORT) -> None:
    """Serve on 127.0.0.1 until stopped. Stateless: a client's MCP session survives a hiccup here."""
    server.run("streamable-http", host="127.0.0.1", port=port, stateless_http=True)


def warm_up() -> threading.Thread:
    """Look up where home is and its time zone now, in the background, so the first question doesn't wait for
    them: on a Reachy Mini they added about a second to the first local_time. Both are then kept for a day."""
    def run() -> None:
        try:
            home()
            home_zone()
        except (requests.RequestException, KeyError, ValueError) as e:  # offline: the tools look again when asked
            log.info(f"couldn't look up home ahead of time: {e!r}")

    thread = threading.Thread(target=run, name="explorer-warm-up", daemon=True)
    thread.start()
    return thread


def start(port: int = PORT, wait_s: float = 10.0) -> bool:
    """Serve on 127.0.0.1 in a background thread; True once it answers (the brain connects right after)."""
    def run() -> None:
        try:
            serve(mcp, port)
        except Exception as e:  # e.g. the port is taken: the brain just lacks these tools
            log.warning(f"explorer MCP server stopped: {e!r}")

    threading.Thread(target=run, name="explorer-mcp", daemon=True).start()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            log.info(f"explorer MCP server on 127.0.0.1:{port}")
            return True
        except OSError:
            time.sleep(0.2)
    log.warning(f"explorer MCP server didn't start on port {port}")
    return False
