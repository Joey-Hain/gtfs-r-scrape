"""
TfNSW bus vehicle position + delay collector.

Fetches the GTFS-Realtime vehicle positions feed and the trip-update
(delay) feed, joins them on trip_id, and appends one row per vehicle
to a daily CSV file at data/YYYY-MM-DD.csv in the current repo.

Designed to be run via GitHub Actions on a schedule (every 5 minutes).
The Actions workflow handles git commit/push — this script only writes
the CSV row(s) and exits.

Required environment variables (set as GitHub Actions secrets):
    TFNSW_API_KEY            — TfNSW Open Data Hub API key
    TFNSW_GTFS_RT_URL        — trip-update feed URL
                               e.g. https://api.transport.nsw.gov.au/v1/gtfs/realtime/buses
    TFNSW_VEHICLE_POS_URL    — vehicle positions feed URL (optional,
                               defaults to the buses endpoint)

Bounding box: restricted to within 10km of Sydney CBD by default,
matching the dashboard. Set COLLECTOR_RADIUS_KM in the environment
to override (or 0 to disable).
"""

import csv
import os
import sys
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from google.transit import gtfs_realtime_pb2

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
SYDNEY_CBD = (-33.8688, 151.2093)
RADIUS_KM = float(os.getenv("COLLECTOR_RADIUS_KM", "10"))

API_KEY = os.environ["TFNSW_API_KEY"]
TRIP_UPDATE_URL = os.environ["TFNSW_GTFS_RT_URL"]
VEHICLE_POS_URL = os.getenv(
    "TFNSW_VEHICLE_POS_URL",
    "https://api.transport.nsw.gov.au/v1/gtfs/vehiclepos/buses"
)

ANOMALY_ABS_SEC = 3600
ON_TIME_EARLY_SEC = -60
ON_TIME_LATE_SEC = 300

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

FIELDNAMES = [
    "timestamp",
    "trip_id",
    "route_id",
    "lat",
    "lon",
    "bearing",
    "speed_kmh",
    "delay_sec",
    "delay_min",
    "on_time",
]


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def fetch_feed(url: str) -> gtfs_realtime_pb2.FeedMessage:
    resp = requests.get(url, headers={"Authorization": f"apikey {API_KEY}"}, timeout=20)
    resp.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)
    return feed


def extract_delays(feed: gtfs_realtime_pb2.FeedMessage) -> dict[str, int]:
    """Return {trip_id: delay_sec} using the freshest stop reading per trip
    (lowest remaining stop_sequence, same logic as the dashboard)."""
    best: dict[str, tuple[int, int]] = {}  # trip_id -> (stop_sequence, delay_sec)
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        tu = entity.trip_update
        trip_id = tu.trip.trip_id
        for stu in tu.stop_time_update:
            arr = stu.arrival.delay if stu.HasField("arrival") and stu.arrival.HasField("delay") else None
            dep = stu.departure.delay if stu.HasField("departure") and stu.departure.HasField("delay") else None
            delay = arr if arr is not None else dep
            if delay is None:
                continue
            seq = stu.stop_sequence
            existing = best.get(trip_id)
            if existing is None or seq < existing[0]:
                best[trip_id] = (seq, delay)
    return {tid: v[1] for tid, v in best.items()}


def extract_vehicles(feed: gtfs_realtime_pb2.FeedMessage) -> list[dict]:
    vehicles = []
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        v = entity.vehicle
        if not v.HasField("position"):
            continue
        lat = v.position.latitude
        lon = v.position.longitude
        if RADIUS_KM > 0 and haversine_km(*SYDNEY_CBD, lat, lon) > RADIUS_KM:
            continue
        vehicles.append({
            "trip_id": v.trip.trip_id if v.HasField("trip") else None,
            "route_id": v.trip.route_id if v.HasField("trip") else None,
            "lat": lat,
            "lon": lon,
            "bearing": v.position.bearing if v.HasField("position") else None,
            "speed_ms": v.position.speed if v.HasField("position") else None,
        })
    return vehicles


def main():
    now = datetime.now(tz=SYDNEY_TZ)
    timestamp = now.isoformat(timespec="seconds")
    date_str = now.strftime("%Y-%m-%d")
    out_path = DATA_DIR / f"{date_str}.csv"

    print(f"[{timestamp}] Fetching feeds...", flush=True)
    try:
        trip_feed = fetch_feed(TRIP_UPDATE_URL)
        vehicle_feed = fetch_feed(VEHICLE_POS_URL)
    except Exception as e:
        print(f"Feed fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    delays = extract_delays(trip_feed)
    vehicles = extract_vehicles(vehicle_feed)
    print(f"  {len(vehicles)} vehicles in bounds, {len(delays)} trips with delay data", flush=True)

    file_exists = out_path.exists()
    rows_written = 0
    with out_path.open("a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        for veh in vehicles:
            trip_id = veh["trip_id"]
            delay_sec = delays.get(trip_id)
            anomaly = delay_sec is not None and abs(delay_sec) > ANOMALY_ABS_SEC
            on_time = (
                None if delay_sec is None or anomaly
                else ON_TIME_EARLY_SEC <= delay_sec <= ON_TIME_LATE_SEC
            )
            writer.writerow({
                "timestamp": timestamp,
                "trip_id": trip_id,
                "route_id": veh["route_id"],
                "lat": veh["lat"],
                "lon": veh["lon"],
                "bearing": round(veh["bearing"], 1) if veh["bearing"] is not None else None,
                "speed_kmh": round(veh["speed_ms"] * 3.6, 1) if veh["speed_ms"] is not None else None,
                "delay_sec": delay_sec if not anomaly else None,
                "delay_min": round(delay_sec / 60, 2) if delay_sec is not None and not anomaly else None,
                "on_time": on_time,
            })
            rows_written += 1

    print(f"  Wrote {rows_written} rows to {out_path}", flush=True)

    # Purge daily files older than 7 days to keep repo size bounded
    cutoff = now.date() - timedelta(days=7)
    for old_file in DATA_DIR.glob("*.csv"):
        try:
            file_date = datetime.strptime(old_file.stem, "%Y-%m-%d").date()
            if file_date < cutoff:
                old_file.unlink()
                print(f"  Purged old file: {old_file.name}", flush=True)
        except ValueError:
            pass  # not a date-named file, leave it alone


if __name__ == "__main__":
    main()
