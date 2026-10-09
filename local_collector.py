"""
Home-machine collector: runs collector.py on a fixed interval (default
every 15 minutes, on the quarter hour) and appends to data-local/YYYY-MM-DD.csv
— the same columns and format as the GitHub Actions scrape in data/.

Why a separate folder: the Action keeps writing data/ every few hours. If
both wrote the same daily file, every push would be a merge conflict. The
dashboard reads data/ AND data-local/ for each day, so pushed local files
simply add to the Action's data.

Usage (from this folder):
    1. pip install requests gtfs-realtime-bindings tzdata python-dotenv
       (tzdata is required on Windows for the Sydney time zone)
    2. Put your key in a .env file next to this script:
           TFNSW_API_KEY=your_key_here
       (or set the TFNSW_API_KEY environment variable)
    3. python local_collector.py              # every 15 min until Ctrl+C
       python local_collector.py --interval 5 # every 5 min
       python local_collector.py --once       # single snapshot, then exit
       python local_collector.py --no-ping    # don't keep the dashboard awake

While it runs it also pings the dashboard every 10 minutes (--ping-url),
because Render's free tier puts the site to sleep after 15 minutes without
a request and the first visit afterwards takes ~30-60 s to wake it.

Then push whenever you like:
    git add data-local && git commit -m "local data" && git pull --rebase && git push

Keep the machine awake (Windows: Settings > System > Power > Sleep = Never).
A missed snapshot (sleep, network drop, API hiccup) just leaves a gap; the
loop logs the error and tries again at the next slot.
"""

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.chdir(HERE)  # data-local/ is created next to this script, inside the repo

try:  # optional: read TFNSW_API_KEY from a .env file
    from dotenv import load_dotenv
    load_dotenv(HERE / ".env")
except ImportError:
    pass

if not os.getenv("TFNSW_API_KEY"):
    sys.exit("TFNSW_API_KEY not set. Add it to a .env file next to this script "
             "or set it as an environment variable.")

os.environ.setdefault("COLLECTOR_DATA_DIR", "data-local")
import collector  # noqa: E402  (must import after COLLECTOR_DATA_DIR is set)

LOG_PATH = HERE / "local_collector.log"  # outside data-local/ so it never gets pushed


def log(msg):
    line = f"[{datetime.now(tz=collector.SYDNEY_TZ):%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def next_slot(now, interval_min):
    """Next wall-clock multiple of interval_min (e.g. :00/:15/:30/:45), so
    snapshots line up across days and machines regardless of start time."""
    base = now.replace(second=0, microsecond=0)
    minutes_past = base.hour * 60 + base.minute
    wait = interval_min - (minutes_past % interval_min)
    return base + timedelta(minutes=wait)


def snapshot():
    try:
        collector.main()
        return True
    except BaseException as e:  # SystemExit included: one bad poll must not end the loop
        if isinstance(e, KeyboardInterrupt):
            raise
        log(f"Snapshot failed: {e!r}")
        traceback.print_exc()
        return False


PING_URL_DEFAULT = "https://civl3704.joeyhain.org/ping"
PING_EVERY_SEC = 600  # Render free tier sleeps after 15 min idle


def ping(url):
    """Keep-alive request to the dashboard. Failures are logged, never fatal."""
    try:
        import requests
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            log(f"Ping {url} -> HTTP {r.status_code}")
    except Exception as e:  # network blip, site restarting, etc.
        log(f"Ping {url} failed: {e!r}")


def run(interval_min, ping_url=None):
    log(f"Collecting every {interval_min} min into {collector.DATA_DIR.resolve()} (Ctrl+C to stop)")
    if ping_url:
        log(f"Keeping {ping_url} awake (ping every {PING_EVERY_SEC // 60} min)")
    run.last_ping = float("-inf")
    ok = fail = 0
    while True:
        if snapshot():
            ok += 1
        else:
            fail += 1
        nxt = next_slot(datetime.now(tz=collector.SYDNEY_TZ), interval_min)
        log(f"{ok} ok / {fail} failed so far; next snapshot at {nxt:%H:%M}")
        # Sleep in short steps so a machine waking from sleep catches up at
        # the next slot instead of oversleeping by the length of the nap.
        while datetime.now(tz=collector.SYDNEY_TZ) < nxt:
            if ping_url and time.monotonic() - run.last_ping >= PING_EVERY_SEC:
                ping(ping_url)
                run.last_ping = time.monotonic()
            time.sleep(min(30, max(1, (nxt - datetime.now(tz=collector.SYDNEY_TZ)).total_seconds())))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Collect TfNSW bus positions + delays to data-local/ on a timer.")
    ap.add_argument("--interval", type=int, default=15, help="minutes between snapshots (default 15)")
    ap.add_argument("--once", action="store_true", help="take one snapshot and exit")
    ap.add_argument("--ping-url", default=PING_URL_DEFAULT,
                    help=f"dashboard URL to ping every {PING_EVERY_SEC // 60} min so it never sleeps (default {PING_URL_DEFAULT})")
    ap.add_argument("--no-ping", action="store_true", help="don't ping the dashboard")
    args = ap.parse_args()
    collector.DATA_DIR.mkdir(exist_ok=True)
    try:
        if args.once:
            sys.exit(0 if snapshot() else 1)
        if not 1 <= args.interval <= 1440:
            sys.exit("--interval must be between 1 and 1440 minutes")
        run(args.interval, None if args.no_ping else args.ping_url)
    except KeyboardInterrupt:
        log("Stopped.")
