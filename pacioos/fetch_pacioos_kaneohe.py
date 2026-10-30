#!/usr/bin/env python3
"""
PacIOOS feed for the Kaneohe pelagic forecast (squares K3, L3, L4 off windward O'ahu).

Runs on GitHub Actions (PacIOOS ERDDAP is reachable from GitHub's runners but not from
Claude's cloud workspace). Pulls, for the box 21.20-21.80 N, 158.06-157.30 W (K3, L3, L4 plus T buoy):

  kaneohe_roms.json  PacIOOS ROMS Main Hawaiian Islands (roms_hiig), shallowest layer (0.25 m):
                     u, v (m/s, eastward / northward -- the direction the water is flowing TOWARD)
                     and temp (deg C). ~4 km grid, 3-hourly, includes TPXO tidal forcing.
  kaneohe_ww3.json   PacIOOS WaveWatch III Hawaii (ww3_hawaii_lon180): Thgt/Tper/Tdir (total sea),
                     shgt/sper/sdir (swell), whgt (wind wave). 0.1 deg (every 2nd 0.05 cell), 3-hourly.

Both cover from 3 hours ago to 60 hours ahead (or the end of the model run, if sooner).
Standard library only. Usage: python3 fetch_pacioos_kaneohe.py --out DIR
"""
import argparse, csv, io, json, math, os, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

ERDDAP = "https://pae-paha.pacioos.hawaii.edu/erddap/griddap"
LAT0, LAT1, LON0, LON1 = 21.20, 21.80, -158.06, -157.30  # east edge widened 2026-10-29 to cover T buoy (157-25.8W)
BACK_H, AHEAD_H = 3, 60
UA = "kaneohe-pelagic-forecast/1.0 (GitHub Actions; personal fishing research)"


def log(*a):
    print("[pacioos]", *a, flush=True)


def get(url, timeout=180, retries=4):
    err = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            err = e
            log(f"  retry {i + 1}/{retries} after error: {e}")
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"GET failed: {url[:160]} ({err})")


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def last_time(dataset):
    txt = get(f"{ERDDAP}/{dataset}.csv?time%5B(last)%5D")
    return datetime.strptime(txt.strip().splitlines()[2].strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def window(dataset):
    now = datetime.now(timezone.utc)
    t0 = (now - timedelta(hours=BACK_H)).replace(minute=0, second=0, microsecond=0)
    t0 -= timedelta(hours=t0.hour % 3)
    last = last_time(dataset)
    t1 = min((now + timedelta(hours=AHEAD_H)).replace(minute=0, second=0, microsecond=0), last)
    if t1 <= t0:
        raise RuntimeError(f"{dataset}: latest model time {iso(last)} is before the window start {iso(t0)}")
    return t0, t1, last


def grid_query(dataset, variables, t0, t1, depth, tstride=1, sstride=1):
    sel = (f"%5B({iso(t0)}):{tstride}:({iso(t1)})%5D%5B({depth})%5D"
           f"%5B({LAT0}):{sstride}:({LAT1})%5D%5B({LON0}):{sstride}:({LON1})%5D")
    return f"{ERDDAP}/{dataset}.csv?" + ",".join(v + sel for v in variables)


def parse_grid(txt, variables, ndigits):
    """ERDDAP griddap CSV (time, depth, latitude, longitude, var...) -> regular arrays [t][lat][lon]."""
    rows = list(csv.reader(io.StringIO(txt)))
    head = rows[0]
    idx = {name: head.index(name) for name in ["time", "latitude", "longitude"] + list(variables)}
    body = rows[2:]
    times = sorted({r[idx["time"]] for r in body})
    lats = sorted({float(r[idx["latitude"]]) for r in body})
    lons = sorted({float(r[idx["longitude"]]) for r in body})
    ti = {t: k for k, t in enumerate(times)}
    li = {v: k for k, v in enumerate(lats)}
    oi = {v: k for k, v in enumerate(lons)}
    out = {v: [[[None] * len(lons) for _ in lats] for _ in times] for v in variables}
    for r in body:
        a, b, c = ti[r[idx["time"]]], li[float(r[idx["latitude"]])], oi[float(r[idx["longitude"]])]
        for v in variables:
            s = r[idx[v]]
            if s in ("", "NaN", "nan"):
                continue
            x = float(s)
            if math.isfinite(x):
                out[v][a][b][c] = round(x, ndigits[v])
    return dict(times=times, lat=[round(x, 5) for x in lats], lon=[round(x, 5) for x in lons], **out)


def fetch_roms():
    ds = "roms_hiig"
    t0, t1, last = window(ds)
    vars_ = ("u", "v", "temp")
    log(f"ROMS {iso(t0)} -> {iso(t1)} (model run ends {iso(last)})")
    g = parse_grid(get(grid_query(ds, vars_, t0, t1, 0.25)), vars_, {"u": 3, "v": 3, "temp": 2})
    return dict(dataset=ds, title="PacIOOS ROMS Main Hawaiian Islands, surface layer (0.25 m)",
                source=f"{ERDDAP}/{ds}.html", model_last_time=iso(last), depth_m=0.25,
                units=dict(u="m/s eastward (toward)", v="m/s northward (toward)", temp="degC"), **g)


def fetch_ww3():
    ds = "ww3_hawaii_lon180"
    t0, t1, last = window(ds)
    vars_ = ("Thgt", "Tper", "Tdir", "shgt", "sper", "sdir", "whgt")
    log(f"WW3 {iso(t0)} -> {iso(t1)} (model run ends {iso(last)})")
    nd = {v: (2 if v.endswith("hgt") else 1) for v in vars_}
    g = parse_grid(get(grid_query(ds, vars_, t0, t1, 0.0, tstride=3, sstride=2)), vars_, nd)
    return dict(dataset=ds, title="PacIOOS WaveWatch III Hawaii regional wave model",
                source=f"{ERDDAP}/{ds}.html", model_last_time=iso(last),
                units=dict(Thgt="m significant wave height", Tper="s peak period", Tdir="deg coming FROM",
                           shgt="m swell height", sper="s swell period", sdir="deg swell coming FROM",
                           whgt="m wind-wave height"), **g)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="feed")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    gen = iso(datetime.now(timezone.utc))
    status, ok = [], 0
    for name, fn in (("kaneohe_roms.json", fetch_roms), ("kaneohe_ww3.json", fetch_ww3)):
        try:
            d = fn()
            d["generated"] = gen
            d["box"] = dict(lat=[LAT0, LAT1], lon=[LON0, LON1])
            with open(os.path.join(a.out, name), "w") as f:
                json.dump(d, f, separators=(",", ":"))
            n = len(d["times"])
            status.append(f"- {name}: {n} time steps, {d['times'][0]} to {d['times'][-1]}, "
                          f"{len(d['lat'])}x{len(d['lon'])} grid")
            ok += 1
        except Exception as e:  # noqa: BLE001
            status.append(f"- {name}: FAILED ({e})")
            log(f"{name} failed: {e}")
    with open(os.path.join(a.out, "README.md"), "w") as f:
        f.write("# PacIOOS feed: Kaneohe box (K3, L3, L4)\n\n"
                f"Generated {gen} by GitHub Actions (`pacioos/fetch_pacioos_kaneohe.py` on `main`).\n"
                "This branch is overwritten on every run; it keeps no history.\n\n" + "\n".join(status) + "\n\n"
                "Sources: PacIOOS ERDDAP `roms_hiig` and `ww3_hawaii_lon180` (free to use and redistribute; "
                "not for navigation).\n")
    log("\n".join(status))
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
