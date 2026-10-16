#!/usr/bin/env python3
"""
Pre-aggregate the scraped CSVs into heatmap grid cells for the dashboard.

Why: since the collector went to 5-minute snapshots around the clock (12 Oct
2026) a day is ~130k rows (~13 MB). Parsing a week of that (~900k rows) on
the dashboard's Render Free instance (a fraction of one CPU) took longer than
its 20 s deadline, so the 7-day map came back partial and the rebuild kept
the instance busy enough to fail health checks. GitHub's runners do the same
aggregation in seconds, so this script does it here, after every data push,
and the dashboard just downloads the result.

Output (written to --out, published by .github/scripts/publish-cells.sh to
the orphan `cells` branch, which is force-pushed so it never grows history):

    windows/1.bin.gz, windows/24.bin.gz, windows/168.bin.gz
        Cells for the last 1 / 24 / 168 hours of collected data, ending at the
        newest snapshot (data_end in the header).
    days/YYYY-MM-DD.bin.gz
        Cells for one Sydney calendar day, for the dashboard's custom date
        ranges. Rebuilt only when that day's CSVs change (manifest.json keeps
        a hash of the sources), and written byte-for-byte deterministically so
        an unchanged day isn't re-uploaded.
    manifest.json
        Build time, newest snapshot, and the source hash of every day file.

File format (all little-endian), gzip-compressed:
    b"CELLS1\\n"
    uint32 header length, then a UTF-8 JSON header:
        periods       ["all", "am_peak", "midday", "pm_peak", "evening"]
        counts        {period: number of cells}
        columns       the 8 column names below, in order
        rows, points, files, start, end, agency_stats {agency_id: [n_with_delay,
        n_early, n_on_time, n_late, late_sum_sec]}, and for windows data_end
        and built_at
    then for each period in header order:
        int64[count]   cell key = round(lat * 1e4) * 10_000_000 + round(lon * 1e4)
        float64[count] x 8 columns: lat_sum, lon_sum, n_total, late_sum_sec,
                       n_delay, speed_sum_kmh, n_speed, n_off_time

The cell maths (10 m grid, lateness floored at 0, TfNSW on-time window of
59 s early to 5:59 late, time-of-day buckets) must match CellGrid in the
dashboard's app.py, which reads these files.
"""

import argparse
import csv
import gzip
import hashlib
import json
import struct
import sys
from array import array
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
GRID_SCALE = 10 ** 4                      # app.py GRID_DECIMALS = 4 (~10 m)
ON_TIME_EARLY_SEC, ON_TIME_LATE_SEC = -59, 359
TIME_PERIODS = {
    "am_peak": (6, 10),
    "midday": (10, 15),
    "pm_peak": (15, 19),
    "evening": (19, 6),                   # wraps past midnight
}
PERIODS = ("all",) + tuple(TIME_PERIODS)
COLUMNS = ("lat_sum", "lon_sum", "n_total", "late_sum_sec",
           "n_delay", "speed_sum_kmh", "n_speed", "n_off_time")
WINDOW_HOURS = (1, 24, 168)
SOURCE_DIRS = ("data", "data-local")
KEEP_DAYS = 62                            # matches the dashboard's custom-range lookback
MAGIC = b"CELLS1\n"

if sys.byteorder != "little":
    sys.exit("build_cells.py writes native arrays and assumes a little-endian machine")


def _hour_bucket(hour):
    for name, (start, end) in TIME_PERIODS.items():
        if (start <= hour < end) if start < end else (hour >= start or hour < end):
            return name
    return "evening"


HOUR_BUCKET = [_hour_bucket(h) for h in range(24)]


class Grid:
    """Cell accumulators for one period: int64 keys plus 8 float64 columns."""

    def __init__(self):
        self.index = {}
        self.keys = array("q")
        self.cols = [array("d") for _ in COLUMNS]

    def add(self, key, lat, lon, delay, speed, off):
        i = self.index.get(key)
        cols = self.cols
        if i is None:
            i = len(self.keys)
            self.index[key] = i
            self.keys.append(key)
            for col in cols:
                col.append(0.0)
        cols[0][i] += lat
        cols[1][i] += lon
        cols[2][i] += 1
        if delay is not None:
            cols[3][i] += delay if delay > 0 else 0.0
            cols[4][i] += 1
            if off:
                cols[7][i] += 1
        if speed is not None and speed >= 0:
            cols[5][i] += speed
            cols[6][i] += 1


class Agg:
    """Cells for every period plus per-operator on-time tallies."""

    def __init__(self):
        self.grids = {p: Grid() for p in PERIODS}
        self.agency = defaultdict(lambda: [0, 0, 0, 0, 0.0])
        self.rows = 0
        self.files = set()

    def add(self, period, key, lat, lon, delay, speed, off, agency_id):
        self.rows += 1
        self.grids["all"].add(key, lat, lon, delay, speed, off)
        self.grids[period].add(key, lat, lon, delay, speed, off)
        if delay is not None and agency_id is not None:
            st = self.agency[agency_id]
            st[0] += 1
            if delay < ON_TIME_EARLY_SEC:
                st[1] += 1
            elif delay > ON_TIME_LATE_SEC:
                st[3] += 1
            else:
                st[2] += 1
            st[4] += max(delay, 0.0)

    def write(self, path, **header_extra):
        header = {
            "version": 1,
            "periods": list(PERIODS),
            "counts": {p: len(g.keys) for p, g in self.grids.items()},
            "columns": list(COLUMNS),
            "rows": self.rows,
            "files": len(self.files),
            "agency_stats": {k: [v[0], v[1], v[2], v[3], round(v[4], 1)]
                             for k, v in sorted(self.agency.items())},
            **header_extra,
        }
        hdr = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as raw:
            # mtime=0 and no filename: identical input gives identical bytes.
            with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6, mtime=0) as f:
                f.write(MAGIC)
                f.write(struct.pack("<I", len(hdr)))
                f.write(hdr)
                for p in PERIODS:
                    g = self.grids[p]
                    f.write(g.keys.tobytes())
                    for col in g.cols:
                        f.write(col.tobytes())
        tmp.replace(path)
        return path.stat().st_size


_ts_cache = {}


def parse_ts(raw):
    ts = _ts_cache.get(raw)
    if ts is None:
        try:
            ts = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(SYDNEY_TZ)
        except ValueError:
            return None
        if len(_ts_cache) > 50_000:
            _ts_cache.clear()
        _ts_cache[raw] = ts
    return ts


def iter_rows(path):
    """(ts, period, key, lat, lon, delay, speed, off, agency_id) per valid row."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            return
        try:
            ts_i, lat_i, lon_i = header.index("timestamp"), header.index("lat"), header.index("lon")
        except ValueError:
            print(f"  {path}: missing required columns, skipped", flush=True)
            return
        delay_i = header.index("delay_sec") if "delay_sec" in header else None
        speed_i = header.index("speed_kmh") if "speed_kmh" in header else None
        route_i = header.index("route_id") if "route_id" in header else None
        need = max(ts_i, lat_i, lon_i)
        for row in reader:
            if len(row) <= need:
                continue
            ts = parse_ts(row[ts_i])
            if ts is None:
                continue
            try:
                lat = float(row[lat_i])
                lon = float(row[lon_i])
            except ValueError:
                continue
            delay = speed = None
            if delay_i is not None and len(row) > delay_i and row[delay_i]:
                try:
                    delay = float(row[delay_i])
                except ValueError:
                    pass
            if speed_i is not None and len(row) > speed_i and row[speed_i]:
                try:
                    speed = float(row[speed_i])
                except ValueError:
                    pass
            off = delay is not None and not (ON_TIME_EARLY_SEC <= delay <= ON_TIME_LATE_SEC)
            agency = None
            if route_i is not None and len(row) > route_i and "_" in row[route_i]:
                agency = row[route_i].split("_", 1)[0]
            key = round(lat * GRID_SCALE) * 10_000_000 + round(lon * GRID_SCALE)
            yield ts, HOUR_BUCKET[ts.hour], key, lat, lon, delay, speed, off, agency


def last_timestamp(path):
    """Timestamp of the last row of a CSV, reading only its tail."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 4096))
        lines = f.read().decode("utf-8", "replace").strip().splitlines()
    for line in reversed(lines):
        ts = parse_ts(line.split(",", 1)[0])
        if ts is not None:
            return ts
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=".", help="gtfs-r-scrape checkout (holds data/ and data-local/)")
    ap.add_argument("--out", required=True, help="output folder (previous build's files are reused)")
    args = ap.parse_args()
    repo, out = Path(args.repo), Path(args.out)
    (out / "days").mkdir(parents=True, exist_ok=True)
    (out / "windows").mkdir(parents=True, exist_ok=True)

    manifest_path = out / "manifest.json"
    try:
        old_days = json.loads(manifest_path.read_text()).get("days", {})
    except (OSError, ValueError):
        old_days = {}

    today = datetime.now(tz=SYDNEY_TZ).date()
    oldest = (today - timedelta(days=KEEP_DAYS)).isoformat()
    sources = defaultdict(list)  # date -> [csv paths]
    for d in SOURCE_DIRS:
        for p in sorted((repo / d).glob("*.csv")):
            if p.stem >= oldest:
                sources[p.stem].append(p)
    if not sources:
        sys.exit("No CSVs found")

    newest_date = max(sources)
    data_end = max(filter(None, (last_timestamp(p) for p in sources[newest_date])), default=None)
    if data_end is None:
        sys.exit(f"Couldn't read the newest timestamp from {newest_date}")
    # Windows include the newest snapshot itself.
    cutoffs = {h: data_end - timedelta(hours=h) + timedelta(seconds=1) for h in WINDOW_HOURS}
    first_window_date = min(cutoffs.values()).date().isoformat()
    windows = {h: Agg() for h in WINDOW_HOURS}

    days = {}
    rebuilt = 0
    for date in sorted(sources):
        sha = hashlib.sha1()
        for p in sources[date]:
            sha.update(p.parent.name.encode() + b"\0" + p.read_bytes())
        digest = sha.hexdigest()
        day_file = out / "days" / f"{date}.bin.gz"
        need_day = old_days.get(date) != digest or not day_file.exists()
        need_window = date >= first_window_date
        days[date] = digest
        if not (need_day or need_window):
            continue
        day = Agg() if need_day else None
        for p in sources[date]:
            if day:
                day.files.add(str(p))
            for ts, period, key, lat, lon, delay, speed, off, agency in iter_rows(p):
                if day:
                    day.add(period, key, lat, lon, delay, speed, off, agency)
                if need_window:
                    for h, agg in windows.items():
                        if ts >= cutoffs[h]:
                            agg.add(period, key, lat, lon, delay, speed, off, agency)
                            agg.files.add(str(p))
        if day:
            size = day.write(day_file, date=date)
            rebuilt += 1
            print(f"  day {date}: {day.rows} rows, {len(day.grids['all'].keys)} cells, "
                  f"{size / 1e6:.1f} MB", flush=True)

    for f in (out / "days").glob("*.bin.gz"):
        if f.name[:10] not in days:
            f.unlink()

    built_at = datetime.now(tz=SYDNEY_TZ).isoformat(timespec="seconds")
    for h, agg in windows.items():
        size = agg.write(out / "windows" / f"{h}.bin.gz",
                         start=cutoffs[h].isoformat(timespec="seconds"),
                         end=data_end.isoformat(timespec="seconds"),
                         data_end=data_end.isoformat(timespec="seconds"),
                         built_at=built_at, window_hours=h)
        print(f"  window {h}h: {agg.rows} rows, {len(agg.grids['all'].keys)} cells, "
              f"{size / 1e6:.1f} MB", flush=True)

    manifest_path.write_text(json.dumps({
        "built_at": built_at,
        "data_end": data_end.isoformat(timespec="seconds"),
        "windows": list(WINDOW_HOURS),
        "days": days,
        "format": "see build_cells.py on main",
    }, indent=1, sort_keys=True) + "\n")
    print(f"Built cells up to {data_end:%Y-%m-%d %H:%M} ({rebuilt} day file(s) rebuilt)", flush=True)


if __name__ == "__main__":
    main()
