#!/usr/bin/env python3
"""
CPUE environmental data pipeline
=================================
Pulls wind/wave forecast (NOAA National Weather Service api.weather.gov)
and ocean current forecast (PacIOOS ROMS Main Hawaiian Islands model,
hosted on a public ERDDAP server at the University of Hawaii) for the
O'ahu / Moloka'i / Lana'i fishing grounds, plus a computed moon phase
table, and writes three CSV files ready to feed a CPUE (catch-per-unit-
effort) model.

Data sources (all free, public, no API key required):
  - Wind, gusts, waves, swell: api.weather.gov (NOAA/NWS gridpoint forecast,
    Honolulu forecast office "HFO")
  - Ocean current (u/v -> speed/direction): PacIOOS ROMS Main Hawaiian
    Islands "roms_hiig" forecast, https://pae-paha.pacioos.hawaii.edu/erddap
  - Moon phase: computed locally (no external source needed)

Output (written to ./output/):
  - wind_wave_forecast.csv
  - ocean_current_forecast.csv
  - moon_phase.csv
  - README.md (column definitions, sources, run metadata)

Designed to be run on a recurring schedule; each run overwrites the
previous output with the freshest forecast.
"""

import csv
import json
import math
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Bounding box covering O'ahu, Moloka'i, Lana'i waters (matches the area
# used for the project's bathymetry work and the Windy.com view it was
# defined from).
LAT_MIN, LAT_MAX = 20.85, 22.05
LON_MIN, LON_MAX = -158.45, -156.35

# Spacing (degrees) for the wind/wave sample point grid. NOAA's NWS API is
# queried once per unique forecast-grid cell, so a coarser spacing keeps the
# number of requests reasonable without losing meaningful resolution over
# open ocean.
WIND_GRID_STEP_DEG = 0.5

# Index stride for the PacIOOS current grid (native resolution ~4km/0.037deg
# per index). Chosen, together with CURRENT_TIME_STRIDE, to keep the output
# file a manageable size for repeated automated uploads (this pipeline is
# re-run and re-uploaded on a schedule) while still covering the whole
# bounding box at a resolution comparable to the wind/wave sample grid.
CURRENT_GRID_STRIDE = 14
CURRENT_TIME_STRIDE = 2  # 2 = every other 3-hourly step (6-hourly)

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
NWS_USER_AGENT = "cpue-fishing-model (Matyi, personal fishing operations research)"

KMH_TO_KT = 0.539957
MS_TO_KT = 1.94384


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Wind / wave / swell — NOAA api.weather.gov
# ---------------------------------------------------------------------------

def http_get_json(url, headers=None, retries=3, backoff=2.0):
    headers = headers or {}
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            last_err = e
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"Failed GET {url}: {last_err}")


def build_wind_grid_points():
    points = []
    lat = LAT_MIN
    while lat <= LAT_MAX + 1e-9:
        lon = LON_MIN
        while lon <= LON_MAX + 1e-9:
            points.append((round(lat, 4), round(lon, 4)))
            lon += WIND_GRID_STEP_DEG
        lat += WIND_GRID_STEP_DEG
    return points


def resolve_nws_grid_cells(points):
    """Map each (lat, lon) sample point to its NWS forecast office/grid cell,
    de-duplicating points that fall in the same cell."""
    seen = {}
    cells = []
    for lat, lon in points:
        try:
            data = http_get_json(
                f"https://api.weather.gov/points/{lat},{lon}",
                headers={"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"},
            )
        except Exception as e:
            log(f"  WARN: could not resolve grid cell for {lat},{lon}: {e}")
            continue
        props = data["properties"]
        key = (props["gridId"], props["gridX"], props["gridY"])
        if key not in seen:
            seen[key] = {
                "gridId": props["gridId"],
                "gridX": props["gridX"],
                "gridY": props["gridY"],
                "lat": lat,
                "lon": lon,
            }
            cells.append(seen[key])
    return cells


def series_to_dict(series):
    """Convert a NWS gridpoint 'values' time series (with ISO8601 interval
    validTimes) into a list of (start_iso, hours, value) tuples."""
    out = []
    if not series:
        return out
    for v in series.get("values", []):
        vt = v["validTime"]
        start, dur = vt.split("/")
        # duration like PT3H, PT6H, P1D
        hours = 3
        if dur.startswith("PT") and dur.endswith("H"):
            hours = int(dur[2:-1])
        elif dur.startswith("P") and dur.endswith("D"):
            hours = int(dur[1:-1]) * 24
        out.append((start, hours, v["value"]))
    return out


CANONICAL_STEP_HOURS = 3


def canonical_time_grid(start_bound, end_bound):
    """A fixed set of timestamps, aligned to 3-hour UTC boundaries, shared
    by every field and every sample point so rows line up cleanly."""
    floored = start_bound.replace(minute=0, second=0, microsecond=0)
    floored -= timedelta(hours=floored.hour % CANONICAL_STEP_HOURS)
    grid = []
    t = floored
    while t <= end_bound:
        grid.append(t)
        t += timedelta(hours=CANONICAL_STEP_HOURS)
    return grid


def sample_step_series(series_dict, grid_times):
    """Given a step-function series [(start_iso, duration_hours, value), ...]
    (segments are contiguous and non-overlapping, as NWS returns them),
    resolve the value in effect at each timestamp in grid_times."""
    segments = []
    for start_iso, hours, value in series_dict:
        seg_start = datetime.fromisoformat(start_iso)
        seg_end = seg_start + timedelta(hours=hours)
        segments.append((seg_start, seg_end, value))
    segments.sort(key=lambda s: s[0])

    out = {}
    idx = 0
    n = len(segments)
    for t in grid_times:
        while idx < n and segments[idx][1] <= t:
            idx += 1
        if idx < n and segments[idx][0] <= t < segments[idx][1]:
            out[t] = segments[idx][2]
        else:
            out[t] = None
    return out


def fetch_wind_wave_for_cell(cell):
    url = f"https://api.weather.gov/gridpoints/{cell['gridId']}/{cell['gridX']},{cell['gridY']}"
    data = http_get_json(url, headers={"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"})
    props = data["properties"]

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=7)
    grid_times = canonical_time_grid(now, horizon)

    fields = {
        "wind_speed_kt": ("windSpeed", KMH_TO_KT),
        "wind_dir_deg": ("windDirection", 1.0),
        "wind_gust_kt": ("windGust", KMH_TO_KT),
        "wave_height_m": ("waveHeight", 1.0),
        "wave_period_s": ("wavePeriod", 1.0),
        "swell_height_m": ("primarySwellHeight", 1.0),
        "swell_period_s": ("wavePeriod2", 1.0),
        "swell_dir_deg": ("primarySwellDirection", 1.0),
    }

    resolved = {}
    for out_name, (src_name, factor) in fields.items():
        series = series_to_dict(props.get(src_name))
        per_t = sample_step_series(series, grid_times)
        resolved[out_name] = {t: (v * factor if v is not None else None) for t, v in per_t.items()}

    rows = []
    for t in grid_times:
        row = {
            "lat": cell["lat"],
            "lon": cell["lon"],
            "valid_time_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        has_any = False
        for out_name in fields:
            v = resolved[out_name].get(t)
            row[out_name] = round(v, 2) if isinstance(v, (int, float)) else ""
            if v is not None:
                has_any = True
        if has_any:
            rows.append(row)
    return rows


def fetch_all_wind_wave():
    log("Building wind/wave sample grid...")
    points = build_wind_grid_points()
    log(f"  {len(points)} candidate points at {WIND_GRID_STEP_DEG} deg spacing")
    cells = resolve_nws_grid_cells(points)
    log(f"  resolved to {len(cells)} unique NWS forecast grid cells")

    all_rows = []
    for i, cell in enumerate(cells, 1):
        try:
            rows = fetch_wind_wave_for_cell(cell)
            all_rows.extend(rows)
            log(f"  [{i}/{len(cells)}] {cell['gridId']} {cell['gridX']},{cell['gridY']} "
                f"({cell['lat']},{cell['lon']}) -> {len(rows)} timesteps")
        except Exception as e:
            log(f"  WARN: failed to fetch cell {cell}: {e}")
        time.sleep(0.3)  # be polite to the free public API
    return all_rows


# ---------------------------------------------------------------------------
# Ocean current — PacIOOS ROMS Main Hawaiian Islands forecast (ERDDAP)
# ---------------------------------------------------------------------------

PACIOOS_BASE = "https://pae-paha.pacioos.hawaii.edu/erddap/griddap/roms_hiig"


def fetch_current_grid():
    log("Fetching ocean current forecast grid from PacIOOS (roms_hiig)...")
    # Discover the latest available time step so we always start "now".
    latest = http_get_json_csv_first_value(f"{PACIOOS_BASE}.csv?time%5B(last)%5D")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")

    lat_expr = f"({LAT_MIN}):{CURRENT_GRID_STRIDE}:({LAT_MAX})"
    lon_expr = f"({LON_MIN}):{CURRENT_GRID_STRIDE}:({LON_MAX})"
    time_expr = f"({now_iso}):{CURRENT_TIME_STRIDE}:({latest})"
    depth_expr = "(0.25)"  # shallowest model layer = surface current

    url = (
        f"{PACIOOS_BASE}.csv?"
        f"u%5B{time_expr}%5D%5B{depth_expr}%5D%5B{lat_expr}%5D%5B{lon_expr}%5D,"
        f"v%5B{time_expr}%5D%5B{depth_expr}%5D%5B{lat_expr}%5D%5B{lon_expr}%5D"
    )
    log(f"  querying u/v surface currents for {LAT_MIN}-{LAT_MAX}N, {LON_MIN}-{LON_MAX}E, "
        f"{now_iso} to {latest}")
    csv_text = http_get_text(url)
    return parse_pacioos_uv_csv(csv_text)


def http_get_text(url, retries=3, backoff=2.0):
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": NWS_USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read().decode()
        except Exception as e:
            last_err = e
            time.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"Failed GET {url}: {last_err}")


def http_get_json_csv_first_value(url):
    text = http_get_text(url)
    lines = [l for l in text.strip().split("\n") if l.strip()]
    # lines[0] = "time", lines[1] = "UTC", lines[2] = value
    return lines[2].strip()


def parse_pacioos_uv_csv(csv_text):
    lines = csv_text.strip().split("\n")
    # Row 0: header names (time,depth,latitude,longitude,u,v)
    # Row 1: units
    reader = csv.reader(lines[2:])
    rows = []
    for r in reader:
        if len(r) < 6:
            continue
        t, depth, lat, lon, u, v = r[:6]
        try:
            u = float(u)
            v = float(v)
        except ValueError:
            continue  # NaN / land mask
        speed_ms = math.sqrt(u * u + v * v)
        # oceanographic "flowing towards" direction, 0=N, 90=E
        direction_deg = (math.degrees(math.atan2(u, v)) + 360) % 360
        rows.append({
            "valid_time_utc": t,
            "lat": round(float(lat), 4),
            "lon": round(float(lon), 4),
            "current_speed_kt": round(speed_ms * MS_TO_KT, 3),
            "current_dir_deg": round(direction_deg, 1),
        })
    return rows


# ---------------------------------------------------------------------------
# Moon phase (computed, no external source)
# ---------------------------------------------------------------------------

def moon_phase_for_date(d):
    """Simple, dependency-free synodic-month approximation. Returns
    (phase_name, illumination_fraction 0-1, age_days)."""
    known_new_moon = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    synodic_month = 29.530588853
    days_since = (datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - known_new_moon).total_seconds() / 86400.0
    age = days_since % synodic_month
    illumination = (1 - math.cos(2 * math.pi * age / synodic_month)) / 2

    if age < 1.84566:
        name = "New Moon"
    elif age < 5.53699:
        name = "Waxing Crescent"
    elif age < 9.22831:
        name = "First Quarter"
    elif age < 12.91963:
        name = "Waxing Gibbous"
    elif age < 16.61096:
        name = "Full Moon"
    elif age < 20.30228:
        name = "Waning Gibbous"
    elif age < 23.99361:
        name = "Last Quarter"
    elif age < 27.68493:
        name = "Waning Crescent"
    else:
        name = "New Moon"

    return name, round(illumination * 100, 1), round(age, 2)


def build_moon_phase_table(days_ahead=14):
    today = datetime.now(timezone.utc).date()
    rows = []
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        name, illum_pct, age = moon_phase_for_date(datetime(d.year, d.month, d.day))
        rows.append({
            "date": d.isoformat(),
            "moon_phase": name,
            "illumination_pct": illum_pct,
            "moon_age_days": age,
        })
    return rows


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_csv(path, rows, fieldnames):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    log(f"  wrote {len(rows)} rows -> {path}")


def write_readme(path, run_time, wind_rows, current_rows, moon_rows):
    content = f"""# CPUE environmental data — wind, current, moon phase

Auto-generated on {run_time.isoformat()}. Covers O'ahu / Moloka'i / Lana'i
waters: latitude {LAT_MIN} to {LAT_MAX}, longitude {LON_MIN} to {LON_MAX}.

Regenerated on a recurring schedule — this file and the three CSVs beside
it always reflect the latest forecast run. Older rows are replaced, not
appended.

## Files

### wind_wave_forecast.csv ({len(wind_rows)} rows)
Source: NOAA National Weather Service api.weather.gov, Honolulu forecast
office (HFO) gridpoint forecast. ~7 day horizon, 3-hourly.

| column | meaning |
|---|---|
| lat, lon | sample point location |
| valid_time_utc | forecast valid time (UTC) |
| wind_speed_kt | sustained wind speed, knots |
| wind_dir_deg | wind direction, degrees (meteorological, direction wind is FROM) |
| wind_gust_kt | wind gust speed, knots |
| wave_height_m | significant wave height, meters |
| wave_period_s | wave period, seconds |
| swell_height_m | primary swell height, meters |
| swell_period_s | swell period, seconds |
| swell_dir_deg | primary swell direction, degrees |

### ocean_current_forecast.csv ({len(current_rows)} rows)
Source: PacIOOS ROMS Main Hawaiian Islands regional ocean forecast model
(University of Hawaii / NOAA IOOS), surface layer (~0.25m depth),
~4km native grid, ~6 day horizon, 3-hourly.
https://pae-paha.pacioos.hawaii.edu/erddap/griddap/roms_hiig.html

| column | meaning |
|---|---|
| valid_time_utc | forecast valid time (UTC) |
| lat, lon | grid cell location |
| current_speed_kt | surface current speed, knots |
| current_dir_deg | current direction, degrees (oceanographic, direction current is FLOWING TOWARD) |

### moon_phase.csv ({len(moon_rows)} rows)
Computed locally (synodic month approximation, no external source).

| column | meaning |
|---|---|
| date | date (local calendar date, UTC-anchored) |
| moon_phase | phase name |
| illumination_pct | fraction of moon illuminated, percent |
| moon_age_days | days since preceding new moon |

## Notes on direction conventions
Wind and swell direction follow the meteorological convention (the
direction the wind/swell is coming FROM). Current direction follows the
oceanographic convention (the direction the water is flowing TOWARD) —
this is the standard for each field and matches how Windy.com displays
them, but it means a due-east wind and a due-east current point opposite
ways on a compass. Keep that in mind when combining columns in the model.
"""
    with open(path, "w") as f:
        f.write(content)
    log(f"  wrote README -> {path}")


def main():
    run_time = datetime.now(timezone.utc)
    log(f"=== CPUE environmental data pipeline run started ===")

    wind_rows = fetch_all_wind_wave()
    wind_fields = ["lat", "lon", "valid_time_utc", "wind_speed_kt", "wind_dir_deg",
                   "wind_gust_kt", "wave_height_m", "wave_period_s",
                   "swell_height_m", "swell_period_s", "swell_dir_deg"]
    write_csv(os.path.join(OUTPUT_DIR, "wind_wave_forecast.csv"), wind_rows, wind_fields)

    try:
        current_rows = fetch_current_grid()
    except Exception as e:
        log(f"  ERROR fetching current data: {e}")
        current_rows = []
    current_fields = ["valid_time_utc", "lat", "lon", "current_speed_kt", "current_dir_deg"]
    write_csv(os.path.join(OUTPUT_DIR, "ocean_current_forecast.csv"), current_rows, current_fields)

    moon_rows = build_moon_phase_table()
    moon_fields = ["date", "moon_phase", "illumination_pct", "moon_age_days"]
    write_csv(os.path.join(OUTPUT_DIR, "moon_phase.csv"), moon_rows, moon_fields)

    write_readme(os.path.join(OUTPUT_DIR, "README.md"), run_time, wind_rows, current_rows, moon_rows)

    log(f"=== Done. wind/wave rows={len(wind_rows)} current rows={len(current_rows)} moon rows={len(moon_rows)} ===")


if __name__ == "__main__":
    main()
