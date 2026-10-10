# gtfs-r-scrape

Collects TfNSW bus positions and delays into daily CSV files for the historical heatmaps on the [TfNSW Delay Board](https://github.com/Joey-Hain/civl3704) ([civl3704.joeyhain.org](https://civl3704.joeyhain.org)), and builds the bus route network the dashboard uses to clip its heatmaps.

TfNSW doesn't publish historical GTFS-Realtime data, and the dashboard's Render.com instance has no permanent storage, so this repository is the project's data store. Built for CIVL3704 Transport Informatics (University of Sydney) by Group 1.

## How it works

- **GitHub Action collector:** `.github/workflows/collect.yml` starts hourly and runs `collector.py --minutes 55`, which stays up and takes a snapshot at :05, :20, :35 and :50 past each hour, then commits the new rows to `data/` once. GitHub's short schedules only fired 3-6 times a day; a long-running job gets about 90 snapshots a day. The :05 offset interleaves with the home collector's quarter-hour snapshots instead of duplicating them.
- **Home machine collector:** `local_collector.py` runs the same collector every 15 minutes on any always-on computer and writes to `data-local/`. Push or upload those files whenever you like; the dashboard reads both folders.
- **Route shapes:** `.github/workflows/route-shapes.yml` runs `build_route_shapes.py` weekly (and whenever the script changes) to rebuild `shapes/route_shapes.json` and the per-trip lookup from the TfNSW bus timetable.

Each snapshot fetches the GTFS-Realtime trip update feed (delays) and vehicle position feed, joins them on trip ID and keeps every bus within 10 km of the Sydney CBD. Daily files older than 60 days are deleted (they remain in the git history).

## Data format

One file per day, named `YYYY-MM-DD.csv` (Sydney time), one row per bus per snapshot:

| Column | Description |
|---|---|
| `timestamp` | Snapshot time, ISO 8601 with Sydney offset |
| `trip_id` | GTFS trip ID |
| `route_id` | GTFS route ID; the part before `_` is the operator's agency ID |
| `lat`, `lon` | Vehicle position |
| `bearing` | Direction of travel in degrees |
| `speed_kmh` | Vehicle speed |
| `delay_sec`, `delay_min` | Delay at the bus's latest reported stop (negative = early); blank if no delay was reported or it was over an hour early or late |
| `on_time` | Whether the delay is within the TfNSW on-time running KPI: no more than 59 seconds early and no more than 5 minutes 59 seconds late |

Files collected before 9 October 2026 used a different `on_time` window (one minute early to five minutes late). The dashboard recalculates on time from `delay_sec`, so this only affects the `on_time` column itself.

## Running the home collector

Install Python 3.11 or later, then from this folder in Command Prompt (not the Python `>>>` prompt):

```bash
python -m pip install requests gtfs-realtime-bindings tzdata python-dotenv
```

On Windows, `tzdata` is required for the Sydney time zone. If `python` isn't recognised, install Python with `winget install Python.Python.3.12` (or from python.org, ticking "Add python.exe to PATH") and open a new Command Prompt.

Create a `.env` file in this folder:

```env
TFNSW_API_KEY=your_api_key_here
```

`.env` is in `.gitignore`, so the key can't be committed. Then run:

```bash
python local_collector.py
```

It takes a snapshot at :00, :15, :30 and :45 past each hour until you close the window (Ctrl+C). `--interval 5` changes the interval and `--once` takes a single snapshot. A failed snapshot (network drop, API hiccup) is logged to `local_collector.log` and retried at the next slot.

While it runs it also pings the dashboard every 10 minutes, because Render's free tier puts the site to sleep after 15 minutes without a request. Use `--no-ping` to turn this off.

Keep the computer awake (Windows: Settings > System > Power > Sleep = Never). If the folder is inside OneDrive, a sync can occasionally lock the file during a snapshot; that snapshot is skipped and the next one runs as normal.

To add the data to GitHub, either commit and push `data-local/`:

```bash
git add data-local && git commit -m "local data" && git pull --rebase && git push
```

or upload the CSV files through GitHub (Add file > Upload files, into the `data-local` folder).

## Building the route shapes

```bash
TFNSW_API_KEY=your_key python build_route_shapes.py
```

This downloads the TfNSW bus timetable, keeps route shapes within 12 km of the CBD, snaps them to a 10 m grid so routes sharing a road merge into one line, simplifies them and writes `shapes/route_shapes.json` (about 1 MB). The weekly Action does this automatically using the repository's `TFNSW_API_KEY` secret.

The same run writes the per-trip lookup used when a bus is clicked on the dashboard to show its route and upcoming stops:

| File | Contents |
|---|---|
| `shapes/trip_lookup.json` | Manifest: build time, shard count, totals |
| `shapes/trips/NNN.json` | `{trip_id: shape_id}` for trips whose route comes within 12 km of the CBD |
| `shapes/geom/NNN.json` | `{shape_id: line}` — each route's full shape, simplified to 5 m, encoded like `route_shapes.json` |
| `shapes/stops.json` | `{stop_id: [lat × 10⁵, lon × 10⁵, name]}` for stops within 25 km of the CBD |

Trips and shapes are split across 128 files by `zlib.crc32(id) % 128`, so the dashboard downloads one small file per click rather than the whole timetable. `--zip path/to/bundle.zip` uses a local timetable instead of downloading.

## Files

- `collector.py`: one snapshot of both feeds into the day's CSV (used by the Action and the home collector)
- `local_collector.py`: runs `collector.py` on a timer into `data-local/` and keeps the dashboard awake
- `build_route_shapes.py`: builds `shapes/route_shapes.json` and the per-trip lookup (`shapes/trips/`, `shapes/geom/`, `shapes/stops.json`)
- `.github/workflows/collect.yml`, `.github/workflows/route-shapes.yml`: the two scheduled Actions
- `data/`, `data-local/`: daily CSV files
- `shapes/route_shapes.json`: bus road network for route clipping
