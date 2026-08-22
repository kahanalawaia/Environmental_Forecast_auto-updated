# CPUE environmental data — wind, current, moon phase

Auto-generated on 2026-08-22T19:56:14.338542+00:00. Covers O'ahu / Moloka'i / Lana'i
waters: latitude 20.85 to 22.05, longitude -158.45 to -156.35.

Regenerated on a recurring schedule — this file and the three CSVs beside
it always reflect the latest forecast run. Older rows are replaced, not
appended.

## Files

### wind_wave_forecast.csv (798 rows)
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

### ocean_current_forecast.csv (300 rows)
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

### moon_phase.csv (14 rows)
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

## How this gets refreshed
A recurring scheduled task re-runs fetch_environmental_data.py (also in
this folder) against fresh NOAA and PacIOOS data and replaces these three
CSVs plus this README in place. Nothing here is hand-maintained.
