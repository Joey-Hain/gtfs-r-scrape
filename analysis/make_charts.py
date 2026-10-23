#!/usr/bin/env python3
"""
Presentation charts and headline numbers for the CIVL3704 final presentation.

Reads the collected CSVs (data/ and data-local/), takes a window of days
(default: the 7 days ending at the newest snapshot) and writes:

    charts/*.png      slide-ready figures (maps of the bus network coloured by
                      the same metrics as the dashboard, plus summary charts)
    stats.json        every number quoted on the slides, machine-readable
    hotspots.csv      ranked hotspots behind the map labels
    README.md         a gallery of the above, with the numbers as text

Run locally:   python analysis/make_charts.py --out analysis-out
               python analysis/make_charts.py --start 2026-10-20 --end 2026-10-26
The GitHub Action (.github/workflows/analysis.yml) runs it daily and
force-pushes the result to the `analysis` branch.

Methodology matches the dashboard (civl3704/app.py) so the charts and the
projected heatmap agree:
  - on time = no more than 59 s early and no more than 5:59 late (TfNSW KPI)
  - lateness is floored at 0, so early running can't cancel late running
  - averaged maps are a normalised convolution (Gaussian, sigma = R/2, cut at
    R; R = 250 m for delay metrics, 160 m for speed) with every pixel shrunk
    toward the network value as if it held 3 extra readings (empirical Bayes)
  - delay burden (total bus-minutes late) is additive: busy roads rank high
    by design
Southwest Link (SW1-SW3, the T3 rail replacement terminating at Sydenham) is
excluded from the main analysis because buses laying over at Sydenham keep
reporting a delay; it gets its own chart from the latest window it ran in.
"""

import argparse
import json
import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.collections import LineCollection
from matplotlib.colors import LinearSegmentedColormap, Normalize, LogNorm
from scipy.ndimage import gaussian_filter
from scipy.spatial import cKDTree
from scipy.stats import spearmanr

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SYD = "Australia/Sydney"

# --- methodology constants (keep in step with app.py) -----------------------
EARLY_LIMIT = -59          # s; more than 59 s early is early
LATE_LIMIT = 359           # s; more than 5:59 late is late
SHRINK_N = 3               # SHRINK_PRIOR_N
DELAY_R_M, SPEED_R_M = 250, 160
FULL_SUPPORT = {"delay": 3.0, "speed": 0.4}
PIX_M = 20                 # raster resolution for the averaged field
TOWN_HALL = (-33.8731, 151.2068)
CBD_RADIUS_KM = 1.5
CENTRE = (-33.8688, 151.2093)
MODEL_SW, MODEL_NE = (-33.895, 151.145), (-33.835, 151.265)   # TransportLab model
SW_LINK_ROUTES = ("SW1", "SW2", "SW3")
SYDENHAM = (-33.9147, 151.1678)
SW_WINDOW = ("2026-10-11", "2026-10-16")   # where the Sydenham artefact was found

PERIODS = [("AM peak", 6, 10), ("Midday", 10, 15), ("PM peak", 15, 19), ("Evening", 19, 30)]

# --- look ------------------------------------------------------------------
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#8a8984"
GRID = "#e4e3df"
SURFACE = "#ffffff"
EARLY_C, ONTIME_C, LATE_C = "#2a78d6", "#c9c7c1", "#e34948"   # diverging: blue / neutral / red
NETWORK_C = "#dcdad5"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a"]
# Same ramps as the dashboard so the slides match what's projected
YLORRD = LinearSegmentedColormap.from_list("ylorrd", ["#ffeda0", "#fed976", "#feb24c", "#fd8d3c",
                                                     "#fc4e2a", "#e31a1c", "#bd0026", "#800026"])
SPEED = LinearSegmentedColormap.from_list("speed", ["#f6a623", "#e8577a", "#9b3fb5", "#3b1f7a"])
PURPLES = LinearSegmentedColormap.from_list("burden", ["#fde0c5", "#f59e72", "#e0533d", "#a3123a", "#4a0c3d"])


def setup_fonts():
    plt.rcParams["font.family"] = "DejaVu Sans"     # matplotlib's own default font
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "text.color": INK, "axes.labelcolor": INK2, "xtick.color": INK2, "ytick.color": INK2,
        "axes.edgecolor": GRID, "axes.spines.top": False, "axes.spines.right": False,
        "axes.titlesize": 15, "axes.titleweight": "bold", "axes.titlelocation": "left",
        "axes.labelsize": 11, "xtick.labelsize": 10, "ytick.labelsize": 10,
        "legend.frameon": False, "legend.fontsize": 10, "figure.dpi": 100,
    })


# --- data --------------------------------------------------------------------
def load(repo, start, end):
    """Rows from data/ and data-local/ for Sydney days start..end inclusive."""
    frames = []
    d = start
    while d <= end:
        for folder in ("data", "data-local"):
            p = repo / folder / f"{d:%Y-%m-%d}.csv"
            if p.exists():
                try:
                    frames.append(pd.read_csv(p, usecols=["timestamp", "trip_id", "route_id", "lat", "lon",
                                                          "speed_kmh", "delay_sec"],
                                              dtype={"trip_id": str, "route_id": str}))
                except Exception as e:  # a half-written file shouldn't stop the build
                    print(f"skip {p}: {e}", file=sys.stderr)
        d += timedelta(days=1)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df[df["route_id"].notna() & (df["route_id"] != "route_id")]
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce").dt.tz_convert(SYD)
    df = df.dropna(subset=["ts", "lat", "lon"]).drop_duplicates(["ts", "trip_id"])
    df["agency"] = df["route_id"].str.split("_").str[0]
    df["route"] = df["route_id"].str.split("_", n=1).str[1].fillna("")
    df["sw_link"] = df["route"].isin(SW_LINK_ROUTES)
    df["has_delay"] = df["delay_sec"].notna()
    df["early"] = df["delay_sec"] < EARLY_LIMIT
    df["late"] = df["delay_sec"] > LATE_LIMIT
    df["off"] = df["early"] | df["late"]
    df["late_min"] = df["delay_sec"].clip(lower=0) / 60
    df["hour"] = df["ts"].dt.hour
    return df


def snapshots_per_day(repo):
    out = {}
    for folder in ("data", "data-local"):
        for p in sorted((repo / folder).glob("*.csv")):
            try:
                ts = pd.read_csv(p, usecols=["timestamp"])["timestamp"]
            except Exception:
                continue
            out.setdefault(p.stem, set()).update(ts.unique())
    return pd.Series({k: len(v) for k, v in out.items()}).sort_index()


def haversine_km(lat1, lon1, lat2, lon2):
    p = np.pi / 180
    a = (np.sin((lat2 - lat1) * p / 2) ** 2
         + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * np.arcsin(np.sqrt(a))


def otr(sub):
    s = sub[sub["has_delay"]]
    n = len(s)
    if n == 0:
        return {"n": 0, "on_time": None, "early": None, "late": None, "mean_late_min": None}
    return {"n": int(n),
            "on_time": round(100 * (1 - s["off"].mean()), 1),
            "early": round(100 * s["early"].mean(), 1),
            "late": round(100 * s["late"].mean(), 1),
            "mean_late_min": round(float(s["late_min"].mean()), 2)}


# --- geometry ---------------------------------------------------------------
M_PER_DEG_LAT = 111_320.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(CENTRE[0]))


def to_xy(lat, lon):
    return (np.asarray(lon) - CENTRE[1]) * M_PER_DEG_LON, (np.asarray(lat) - CENTRE[0]) * M_PER_DEG_LAT


def load_network(repo):
    doc = json.loads((repo / "shapes" / "route_shapes.json").read_text())
    lines = []
    for flat in doc["lines"]:
        lat = lon = 0
        pts = []
        for i in range(0, len(flat), 2):
            lat += flat[i]; lon += flat[i + 1]
            pts.append((lat / 1e5, lon / 1e5))
        if len(pts) >= 2:
            lines.append(np.array(pts))
    # densify to <= 40 m pieces so colour can change along a road
    segs = []
    for pts in lines:
        x, y = to_xy(pts[:, 0], pts[:, 1])
        for i in range(len(x) - 1):
            L = math.hypot(x[i + 1] - x[i], y[i + 1] - y[i])
            k = max(1, int(L // 40) + 1)
            t = np.linspace(0, 1, k + 1)
            xs = x[i] + (x[i + 1] - x[i]) * t
            ys = y[i] + (y[i + 1] - y[i]) * t
            segs.extend(np.stack([np.c_[xs[:-1], ys[:-1]], np.c_[xs[1:], ys[1:]]], axis=1))
    return np.array(segs)


def load_stops(repo):
    doc = json.loads((repo / "shapes" / "stops.json").read_text())
    names, lats, lons = [], [], []
    for v in doc.values():
        lats.append(v[0] / 1e5); lons.append(v[1] / 1e5); names.append(v[2])
    x, y = to_xy(np.array(lats), np.array(lons))
    return cKDTree(np.c_[x, y]), names


# --- the averaged field (FieldLayer, offline) -------------------------------
class Raster:
    def __init__(self, half_m=10_500, pix=PIX_M):
        self.pix, self.half = pix, half_m
        self.n = int(2 * half_m / pix)

    def idx(self, x, y):
        ix = ((np.asarray(x) + self.half) / self.pix).astype(int)
        iy = ((np.asarray(y) + self.half) / self.pix).astype(int)
        ok = (ix >= 0) & (ix < self.n) & (iy >= 0) & (iy < self.n)
        return ix, iy, ok

    def accumulate(self, x, y, w):
        ix, iy, ok = self.idx(x, y)
        g = np.zeros((self.n, self.n))
        np.add.at(g, (iy[ok], ix[ok]), np.asarray(w, float)[ok])
        return g

    def smooth(self, g, radius_m):
        """Sum over readings of exp(-d^2 / 2 sigma^2), sigma = R/2, cut at R."""
        s = (radius_m / 2) / self.pix
        return gaussian_filter(g, s, mode="constant", truncate=2.0) * (2 * math.pi * s * s)

    def sample(self, g, x, y):
        ix, iy, ok = self.idx(x, y)
        out = np.full(len(ix), np.nan)
        out[ok] = g[iy[ok], ix[ok]]
        return out


def field(r, x, y, values, radius_m, prior):
    """Shrunk local mean and support (summed kernel weight) per pixel."""
    num = r.smooth(r.accumulate(x, y, values), radius_m)
    den = r.smooth(r.accumulate(x, y, np.ones(len(x))), radius_m)
    return (num + SHRINK_N * prior) / (den + SHRINK_N), den


# --- drawing helpers --------------------------------------------------------
SLIDE = (13.33, 7.5)      # full 16:9 slide


def save(fig, out, name):
    p = out / "charts" / f"{name}.png"
    fig.savefig(p, dpi=200, bbox_inches="tight", pad_inches=0.2)
    plt.close(fig)
    print("wrote", p)
    return p.name


def header(fig, title, sub=None, adjust=True):
    """Charts carry no title or subtitle (the slide supplies one); title and
    sub are kept as arguments only to document what each chart shows."""
    if adjust:
        fig.subplots_adjust(top=1 - 0.3 / fig.get_figheight())


def footer(fig, text):
    pass


def map_figure(extent):
    if extent == "area":
        fig = plt.figure(figsize=(8, 8))
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_xlim(-10_200, 10_200); ax.set_ylim(-10_200, 10_200)
        side = None
    else:
        fig = plt.figure(figsize=(SLIDE[0], SLIDE[0] / 2))
        ax = fig.add_axes([0, 0, 1, 1])
        x0, y0 = to_xy(MODEL_SW[0], MODEL_SW[1]); x1, y1 = to_xy(MODEL_NE[0], MODEL_NE[1])
        ax.set_xlim(x0, x1); ax.set_ylim(y0, y1)
        side = None
    ax.set_aspect("equal"); ax.axis("off")
    return fig, ax, side


def draw_network_map(ax, segs, vals, support, cmap, norm, full, lw=1.4):
    ax.add_collection(LineCollection(segs, colors=NETWORK_C, linewidths=0.6, zorder=1, capstyle="round"))
    keep = np.isfinite(vals) & (support > 0.05 * full)
    rgba = cmap(norm(vals[keep]))
    rgba[:, 3] = np.clip(support[keep] / full, 0.15, 1.0)
    order = np.argsort(vals[keep])           # worst drawn on top
    ax.add_collection(LineCollection(segs[keep][order], colors=rgba[order], linewidths=lw,
                                     zorder=2, capstyle="round"))


def colourbar(fig, rect, cmap, norm, label, ticks, labels):
    cax = fig.add_axes(rect)
    cb = fig.colorbar(plt.cm.ScalarMappable(cmap=cmap, norm=norm), cax=cax, orientation="horizontal")
    cb.outline.set_visible(False)
    cb.set_ticks(ticks); cb.set_ticklabels(labels)
    cb.ax.xaxis.set_minor_locator(matplotlib.ticker.NullLocator())
    cb.ax.tick_params(labelsize=9.5, length=0, colors=INK2)
    cax.set_title(label, fontsize=10, color=INK2, loc="left", pad=5)


def map_colourbar(fig, ax, side, cmap, norm, label, ticks, labels):
    if side is not None:
        b = side.get_position()
        rect = [b.x0 + 0.01, b.y0 + 0.03, 0.26, 0.022]
    else:
        b = ax.get_position()
        wide = fig.get_figwidth() > fig.get_figheight()
        rect = [b.x0 + 0.03, b.y0 + 0.05, 0.22 if wide else 0.34, 0.022 if wide else 0.016]
        bg = fig.add_axes([rect[0] - 0.012, rect[1] - 0.05, rect[2] + 0.034, 0.115])
        bg.set_xticks([]); bg.set_yticks([]); bg.patch.set_alpha(0.92)
        for sp in bg.spines.values():
            sp.set_visible(True); sp.set_color(GRID)
    colourbar(fig, rect, cmap, norm, label, ticks, labels)


def cbd_ring(ax):
    tx, ty = to_xy(*TOWN_HALL)
    ax.add_patch(plt.Circle((tx, ty), CBD_RADIUS_KM * 1000, fill=False, ls=(0, (4, 3)),
                            ec=INK, lw=1, zorder=4))
    ax.annotate("CBD", (tx + CBD_RADIUS_KM * 707, ty + CBD_RADIUS_KM * 707), xytext=(4, 4),
                textcoords="offset points", fontsize=9, color=INK, zorder=5)


def label_hotspots(ax, spots, colour=INK):
    return
    for i, s in enumerate(spots, 1):
        ax.plot(s["x"], s["y"], "o", ms=16, mfc="white", mec=colour, mew=1.6, zorder=6)
        ax.text(s["x"], s["y"], str(i), ha="center", va="center", fontsize=9,
                fontweight="bold", color=colour, zorder=7)


def hotspot_panel(side, spots, title, fmt, note=None, colour=INK):
    if side is None:
        return
    side.set_xlim(0, 1); side.set_ylim(0, 1)
    side.text(0, 0.97, title, fontsize=11.5, fontweight="bold", va="top")
    y = 0.88
    for i, s in enumerate(spots, 1):
        side.plot(0.025, y, "o", ms=16, mfc="white", mec=colour, mew=1.6, clip_on=False)
        side.text(0.025, y, str(i), ha="center", va="center", fontsize=9, fontweight="bold", color=colour)
        side.text(0.08, y + 0.012, s["name"], fontsize=10.5, va="center")
        side.text(0.08, y - 0.03, fmt(s), fontsize=9.5, color=INK2, va="center")
        y -= 0.095
    if note:
        side.text(0, y - 0.01, note, fontsize=10.5, color=INK2, va="top", wrap=True, linespacing=1.5)


# --- hotspot ranking (200 m cells) ------------------------------------------
def ranked_cells(d, value, min_n, prior, n=6, sep_km=1.2, stops=None, by="shrunk"):
    x, y = to_xy(d["lat"].values, d["lon"].values)
    g = pd.DataFrame({"cx": (x // 200).astype(int), "cy": (y // 200).astype(int),
                      "x": x, "y": y, "v": value.values, "stat": (d["speed_kmh"] < 1).values})
    agg = g.groupby(["cx", "cy"]).agg(x=("x", "mean"), y=("y", "mean"), n=("v", "size"),
                                      s=("v", "sum"), stationary=("stat", "mean"))
    agg = agg[agg["n"] >= min_n].copy()
    agg["mean"] = agg["s"] / agg["n"]
    agg["shrunk"] = (agg["s"] + SHRINK_N * prior) / (agg["n"] + SHRINK_N)
    agg = agg.sort_values(by, ascending=False)
    picked = []
    for _, r in agg.iterrows():
        if all(math.hypot(r.x - p["x"], r.y - p["y"]) > sep_km * 1000 for p in picked):
            name = ""
            if stops is not None:
                _, j = stops[0].query([r.x, r.y]); name = stops[1][j]
            picked.append({"x": r.x, "y": r.y, "n": int(r.n), "value": float(r[by]),
                           "mean": float(r["mean"]), "sum": float(r.s),
                           "stationary_pct": round(100 * float(r.stationary), 1), "name": name})
        if len(picked) == n:
            break
    return picked


def short_stop(name):
    """'Military Rd at Ben Boyd Rd, Neutral Bay' -> 'Military Rd at Ben Boyd Rd'"""
    name = name.split(",")[0]
    return name if len(name) <= 36 else name[:34] + "…"


def pct(v):
    return f"{v:.0f}%"


# --- charts -----------------------------------------------------------------
SOURCE = "Source: TfNSW GTFS-R trip updates + vehicle positions, collected every 5 min. Southwest Link excluded."


def chart_maps(d, out, segs, stops, stats, window_txt):
    files = []
    x, y = to_xy(d["lat"].values, d["lon"].values)
    r = Raster()
    mid = segs.mean(axis=1)
    mx, my = mid[:, 0], mid[:, 1]
    dd = d[d["has_delay"]]
    dx, dy = to_xy(dd["lat"].values, dd["lon"].values)
    cbd = stats["cbd_vs_elsewhere"]

    # 1. average delay. The scale is stretched (1-3.5 min) against the
    # dashboard's 0-5 so differences show on a slide; 90% of the network sits
    # between about 1 and 3.3 min.
    prior = float(dd["late_min"].mean())
    f, w = field(r, dx, dy, dd["late_min"].values, DELAY_R_M, prior)
    vals, sup = r.sample(f, mx, my), r.sample(w, mx, my)
    spots = ranked_cells(dd, dd["late_min"], 200, prior, stops=stops)
    for s in spots:
        s["name"] = short_stop(s["name"])
    stats["hotspots_average_delay"] = spots
    norm = Normalize(1, 3.5)
    ticks, labs = [1, 1.5, 2, 2.5, 3, 3.5], ["≤1", "1.5", "2", "2.5", "3", "3.5+"]
    worse_outside = cbd["Elsewhere"]["mean_late_min"] > cbd["CBD"]["mean_late_min"]
    title = ("Average delay is higher outside the CBD than in it" if worse_outside
             else "Average delay by location")
    sub = (f"Mean lateness of readings within 250 m (early counts as 0) · CBD {cbd['CBD']['mean_late_min']:.1f} min "
           f"vs elsewhere {cbd['Elsewhere']['mean_late_min']:.1f} min · {window_txt}")
    for extent in ("area", "model"):
        fig, ax, side = map_figure(extent)
        draw_network_map(ax, segs, vals, sup, YLORRD, norm, FULL_SUPPORT["delay"],
                         lw=1.4 if extent == "area" else 2.4)
        cbd_ring(ax)
        header(fig, title + ("" if extent == "area" else " (TransportLab model extent)"), sub, adjust=False)
        if side is not None:
            label_hotspots(ax, spots)
            hotspot_panel(side, spots, "Highest average delay (200 m areas)",
                          lambda s: f"{s['value']:.1f} min · {s['n']:,} readings · "
                                    f"{s['stationary_pct']:.0f}% stationary",
                          note="Thin data is shrunk toward the network mean "
                               f"({prior:.1f} min), so one late bus can't make a hotspot.")
        map_colourbar(fig, ax, side, YLORRD, norm, "Average delay (min)", ticks, labs)
        footer(fig, "Line opacity shows how much data backs each estimate. " + SOURCE)
        files.append(save(fig, out, f"map_average_delay_{extent}"))

    # 2. delay burden (additive)
    days = max(1, (d["ts"].max() - d["ts"].min()).total_seconds() / 86400)
    bur = r.smooth(r.accumulate(dx, dy, dd["late_min"].values), DELAY_R_M) / days
    bv = r.sample(bur, mx, my)
    spots_b = ranked_cells(dd, dd["late_min"], 1, 0, stops=stops, by="s")
    for s in spots_b:
        s["name"] = short_stop(s["name"])
        s["per_day_bus_min"] = round(s["sum"] / days, 0)
    stats["hotspots_delay_burden"] = spots_b
    pos = bv[np.isfinite(bv) & (bv > 0)]
    lo, hi = np.percentile(pos, 50), np.percentile(pos, 99.5)
    fig, ax, side = map_figure("area")
    norm = LogNorm(lo, hi, clip=True)
    show = np.isfinite(bv) & (bv > lo)
    draw_network_map(ax, segs, np.where(show, bv, np.nan), np.where(show, 1.0, 0), PURPLES, norm, 1.0, lw=1.4)
    cbd_ring(ax)
    header(fig, "Delay burden: total bus-minutes late piles up where the buses are",
           f"Sum of lateness within 250 m, log scale, busiest half of the network coloured · {window_txt}",
           adjust=False)
    label_hotspots(ax, spots_b, colour="#a3123a")
    hotspot_panel(side, spots_b, "Most bus-minutes late (200 m areas)",
                  lambda s: f"{s['per_day_bus_min']:,.0f} bus-min late per day · {s['n']:,} readings",
                  note="Additive by design: an interchange accumulates burden even when each bus is only "
                       "a little late. Average delay asks the other question.", colour="#a3123a")
    map_colourbar(fig, ax, side, PURPLES, norm, "Delay burden", [lo, hi], ["lower", "higher"])
    footer(fig, SOURCE)
    files.append(save(fig, out, "map_delay_burden"))

    # 3. not on time
    prior_o = float(dd["off"].mean())
    f, w = field(r, dx, dy, dd["off"].values.astype(float), DELAY_R_M, prior_o)
    vals, sup = r.sample(f, mx, my), r.sample(w, mx, my)
    fig, ax, side = map_figure("area")
    norm = Normalize(0.1, 0.5)
    draw_network_map(ax, segs, vals, sup, YLORRD, norm, FULL_SUPPORT["delay"], lw=1.4)
    cbd_ring(ax)
    header(fig, "Not on time: share of readings outside −0:59 / +5:59",
           f"Within 250 m; early and late both count · {window_txt}", adjust=False)
    n = stats["network"]
    hotspot_panel(side, [], "Network-wide", None,
                  note=f"{n['on_time']:.0f}% of readings on time\n{n['early']:.0f}% more than 0:59 early\n"
                       f"{n['late']:.0f}% more than 5:59 late")
    map_colourbar(fig, ax, side, YLORRD, norm, "Not on time", [.1, .2, .3, .4, .5],
                  ["≤10%", "20%", "30%", "40%", "50%+"])
    footer(fig, "Line opacity shows how much data backs each estimate. " + SOURCE)
    files.append(save(fig, out, "map_not_on_time"))

    # 4. speed
    prior_s = float(d["speed_kmh"].mean())
    f, w = field(r, x, y, d["speed_kmh"].fillna(prior_s).values, SPEED_R_M, prior_s)
    vals, sup = r.sample(f, mx, my), r.sample(w, mx, my)
    fig, ax, side = map_figure("area")
    norm = Normalize(0, 80)
    draw_network_map(ax, segs, vals, sup, SPEED, norm, FULL_SUPPORT["speed"], lw=1.4)
    cbd_ring(ax)
    header(fig, "Average bus speed, including time stopped",
           f"Within 160 m · CBD {cbd['CBD']['speed_kmh']:.0f} km/h vs elsewhere "
           f"{cbd['Elsewhere']['speed_kmh']:.0f} km/h · {window_txt}", adjust=False)
    hotspot_panel(side, [], "Reading the colours", None,
                  note="Amber: CBD streets\nPink: arterial roads\nPurple: motorways\n\n"
                       "Scale capped at 80 km/h, the motorway limit.")
    map_colourbar(fig, ax, side, SPEED, norm, "Speed (km/h)", [0, 20, 40, 60, 80], ["0", "20", "40", "60", "80"])
    footer(fig, SOURCE)
    files.append(save(fig, out, "map_speed"))
    return files


def cbd_stats(d, stats):
    dist = haversine_km(d["lat"].values, d["lon"].values, *TOWN_HALL)
    d = d.assign(cbd=dist <= CBD_RADIUS_KM)
    dl = d[d["has_delay"]]
    rows = {}
    for lab, sub in (("CBD", d[d["cbd"]]), ("Elsewhere", d[~d["cbd"]])):
        o = otr(sub)
        o["speed_kmh"] = round(float(sub["speed_kmh"].mean()), 1)
        o["share_readings"] = round(100 * len(sub) / len(d), 1)
        o["share_burden"] = round(100 * sub["late_min"].sum() / dl["late_min"].sum(), 1)
        rows[lab] = o
    stats["cbd_vs_elsewhere"] = rows


def chart_hourly(d, out, stats, window_txt):
    dd = d[d["has_delay"]]
    g = dd.groupby("hour").agg(n=("off", "size"), early=("early", "mean"), late=("late", "mean"))
    g["on"] = 1 - g["early"] - g["late"]
    stats["by_hour"] = {int(h): {"n": int(r.n), "early": round(100 * r.early, 1), "on_time": round(100 * r.on, 1),
                                 "late": round(100 * r.late, 1)} for h, r in g.iterrows()}
    per = {}
    for name, a, b in PERIODS:
        per[name] = otr(d[d["hour"].isin([h % 24 for h in range(a, b)])])
    stats["by_period"] = per
    n = stats["network"]

    fig, ax = plt.subplots(figsize=(SLIDE[0], 6.2))
    title = ("Buses that miss on-time are more often early than late" if n["early"] > n["late"]
             else "On-time running by hour of day")
    header(fig, title, f"Share of readings by hour · network {n['early']:.0f}% early vs {n['late']:.0f}% late · "
                       f"{window_txt}")
    fig.subplots_adjust(top=1 - 0.6 / 6.2, bottom=0.17)
    h = g.index.values
    ax.bar(h, 100 * g["early"], color=EARLY_C, width=0.8, label="Early (more than 0:59 early)")
    ax.bar(h, 100 * g["on"], bottom=100 * g["early"], color=ONTIME_C, width=0.8, label="On time")
    ax.bar(h, 100 * g["late"], bottom=100 * (g["early"] + g["on"]), color=LATE_C, width=0.8,
           label="Late (more than 5:59 late)")
    for name, a, b in PERIODS:
        p = per[name]
        xc = (a + min(b, 24) - 1) / 2
        if "peak" in name:
            ax.axvspan(a - 0.5, b - 0.5, color="#f4f3f0", zorder=0)
            ax.axvspan(a - 0.5, b - 0.5, ymin=1.0, ymax=1.08, color="#f4f3f0", clip_on=False, zorder=0)
        ax.text(xc if name != "Evening" else 21.5, 104, name, ha="center", va="center",
                fontsize=10, color=INK2)
    ax.set_xlim(-0.6, 23.6); ax.set_ylim(0, 100)
    ax.set_xticks(range(0, 24, 2)); ax.set_xticklabels([f"{i:02d}:00" for i in range(0, 24, 2)])
    ax.set_yticks([0, 25, 50, 75, 100]); ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.tick_params(length=0); ax.spines["left"].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.08), ncol=3)
    footer(fig, "Evening period runs 19:00–06:00. " + SOURCE)
    return [save(fig, out, "otr_by_hour")]


def chart_cbd(d, out, stats, window_txt):
    rows = stats["cbd_vs_elsewhere"]
    metrics = [("mean_late_min", "Average lateness", lambda v: f"{v:.1f} min"),
               ("late", "More than 5:59 late", lambda v: f"{v:.1f}%"),
               ("early", "More than 0:59 early", lambda v: f"{v:.1f}%"),
               ("speed_kmh", "Average speed", lambda v: f"{v:.0f} km/h")]
    fig, axes = plt.subplots(1, 4, figsize=(SLIDE[0], 5.2))
    slower_not_later = (rows["CBD"]["speed_kmh"] < rows["Elsewhere"]["speed_kmh"]
                        and rows["CBD"]["mean_late_min"] < rows["Elsewhere"]["mean_late_min"])
    header(fig, "CBD buses are slower but not later: the slowness is already in the timetable"
           if slower_not_later else "CBD against the rest of the network",
           f"CBD = within 1.5 km of Town Hall: {rows['CBD']['share_readings']:.0f}% of readings but "
           f"{rows['CBD']['share_burden']:.0f}% of bus-minutes late · {window_txt}")
    fig.subplots_adjust(top=1 - 1.5 / 5.2, bottom=0.12, wspace=0.3)
    for ax, (k, lab, fmt) in zip(axes, metrics):
        v = [rows["CBD"][k], rows["Elsewhere"][k]]
        bars = ax.bar([0, 1], v, color=[SERIES[1], SERIES[0]], width=0.62)
        for b, val in zip(bars, v):
            ax.text(b.get_x() + b.get_width() / 2, val, fmt(val), ha="center", va="bottom",
                    fontsize=12, fontweight="bold", color=INK)
        ax.set_xticks([0, 1]); ax.set_xticklabels(["CBD", "Elsewhere"], fontsize=11)
        ax.set_yticks([]); ax.spines["left"].set_visible(False)
        ax.set_ylim(0, max(v) * 1.2); ax.tick_params(length=0)
        ax.set_title(lab, fontsize=12, loc="center", pad=14)
    footer(fig, SOURCE)
    return [save(fig, out, "cbd_vs_elsewhere")]


def chart_concentration(d, out, stats, window_txt):
    dd = d[d["has_delay"]]
    x, y = to_xy(dd["lat"].values, dd["lon"].values)
    g = pd.DataFrame({"c": (x // 200).astype(int) * 100000 + (y // 200).astype(int),
                      "b": dd["late_min"].values}).groupby("c")["b"].agg(["sum", "size"])
    g = g.sort_values("sum", ascending=False)
    cx = np.arange(1, len(g) + 1) / len(g) * 100
    cb = g["sum"].cumsum().values / g["sum"].sum() * 100
    cn = g["size"].cumsum().values / g["size"].sum() * 100
    i5 = int(math.ceil(0.05 * len(g))) - 1
    stats["burden_concentration"] = {"top5pct_cells_burden": round(cb[i5], 1),
                                     "top5pct_cells_readings": round(cn[i5], 1), "cells_200m": len(g)}
    fig, ax = plt.subplots(figsize=(10, 6.2))
    header(fig, "Delay burden tracks bus volume",
           f"So a burden map mostly shows where buses are; average delay is the separate map · {window_txt}")
    fig.subplots_adjust(bottom=0.13, left=0.1)
    ax.plot(cx, cb, color=LATE_C, lw=2.2, label="Bus-minutes late (delay burden)")
    ax.plot(cx, cn, color=MUTED, lw=2.2, ls=(0, (4, 3)), label="Bus readings (volume)")
    ax.legend(loc="lower right", bbox_to_anchor=(1, 0.08))
    ax.plot([0, 100], [0, 100], color=GRID, lw=1)
    ax.set_xlim(0, 100); ax.set_ylim(0, 101)
    ax.set_xlabel("Share of 200 m areas, highest burden first"); ax.set_ylabel("Cumulative share")
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0f}%"); ax.yaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
    ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True); ax.tick_params(length=0)
    return [save(fig, out, "burden_concentration")]


def chart_operators(d, out, stats, published, window_txt):
    rows = []
    for name, info in published["operators"].items():
        o = otr(d[d["agency"].isin(info["agencies"])])
        if o["n"] < 1000:
            continue
        rows.append({"operator": name, **o, "published": info.get("published_on_time")})
    df = pd.DataFrame(rows).sort_values("on_time")
    stats["operators"] = df.to_dict("records")
    m = df.dropna(subset=["published"])
    files = []
    if len(m) >= 3:
        rho = spearmanr(m["on_time"], m["published"])[0]
        gap = m["published"] - m["on_time"]
        stats["operators_spearman_rho"] = round(float(rho), 2)
        stats["operators_gap_points"] = [round(float(gap.min()), 1), round(float(gap.max()), 1)]
        h = 0.62 * len(m) + 2.6
        fig, ax = plt.subplots(figsize=(SLIDE[0], h))
        header(fig, f"Measured mid-trip, operators run {gap.min():.0f}–{gap.max():.0f} points below published OTR",
               f"TfNSW measures at timing points, where an early bus can wait; we measure every bus every 5 minutes "
               f"· rank agreement ρ = {rho:.2f} (n = {len(m)}) · {window_txt}")
        fig.subplots_adjust(top=1 - 1.15 / h, bottom=0.9 / h, left=0.2, right=0.97)
        yy = np.arange(len(m))
        ax.hlines(yy, m["on_time"], m["published"], color=GRID, lw=3, zorder=1)
        ax.scatter(m["on_time"], yy, s=110, color=SERIES[1], zorder=3, edgecolor="white", linewidth=2,
                   label="Mid-trip, this project")
        ax.scatter(m["published"], yy, s=110, color=SERIES[0], zorder=3, edgecolor="white", linewidth=2,
                   label=f"TfNSW published, {published['published_period']}")
        for yi, (_, r) in zip(yy, m.iterrows()):
            ax.text(r.on_time - 0.9, yi, f"{r.on_time:.0f}%", ha="right", va="center", fontsize=10, color=INK2)
            ax.text(r.published + 0.9, yi, f"{r.published:.0f}%", ha="left", va="center", fontsize=10, color=INK2)
        ax.set_yticks(yy); ax.set_yticklabels(m["operator"], fontsize=10.5); ax.tick_params(length=0)
        ax.set_xlim(55, 101); ax.xaxis.set_major_formatter(lambda v, _: f"{v:.0f}%")
        ax.grid(axis="x", color=GRID, lw=0.8); ax.set_axisbelow(True); ax.spines["left"].set_visible(False)
        ax.set_ylim(-0.6, len(m) - 0.4)
        ax.legend(loc="lower center", bbox_to_anchor=(0.4, 1.0), ncol=2)
        files.append(save(fig, out, "otr_vs_published"))

    h = 0.55 * len(df) + 2.6
    fig, ax = plt.subplots(figsize=(SLIDE[0], h))
    all_early = bool((df["early"] > df["late"]).all())
    header(fig, "Every operator runs early more often than late" if all_early else "Early and late running by operator",
           f"Share of readings · {window_txt}")
    fig.subplots_adjust(top=1 - 1.3 / h, bottom=0.6 / h, left=0.2, right=0.97)
    yy = np.arange(len(df))
    ax.barh(yy, df["early"], color=EARLY_C, height=0.64, label="Early")
    ax.barh(yy, df["on_time"], left=df["early"], color=ONTIME_C, height=0.64, label="On time")
    ax.barh(yy, df["late"], left=df["early"] + df["on_time"], color=LATE_C, height=0.64, label="Late")
    for yi, (_, r) in zip(yy, df.iterrows()):
        ax.text(r.early / 2, yi, f"{r.early:.0f}%", ha="center", va="center", fontsize=9.5, color="white")
        ax.text(r.early + r.on_time / 2, yi, f"{r.on_time:.0f}% on time", ha="center", va="center",
                fontsize=9.5, color=INK)
        ax.text(100 - r.late / 2, yi, f"{r.late:.0f}%", ha="center", va="center", fontsize=9.5, color="white")
    ax.set_yticks(yy); ax.set_yticklabels(df["operator"], fontsize=10.5); ax.set_xlim(0, 100)
    ax.set_xticks([]); ax.tick_params(length=0)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_visible(False)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3)
    files.append(save(fig, out, "otr_by_operator"))
    return files


def chart_sw_link(repo, out, stats, start, end):
    """Southwest Link layover artefact, from a fixed window where it showed
    (11-16 Oct, the period analysed in the reports; SW Link ends 30 Oct)."""
    d = load(repo, start, end)
    if d.empty or not d["sw_link"].any():
        return []
    d = d[d["has_delay"]]
    sw = d[d["sw_link"]]
    syd = haversine_km(sw["lat"].values, sw["lon"].values, *SYDENHAM) <= 0.3
    at, away = sw[syd], sw[~syd]
    s = {"window": f"{start:%d/%m}–{end:%d/%m}",
         "share_readings": round(100 * len(sw) / len(d), 1),
         "share_lateness": round(100 * sw["late_min"].sum() / d["late_min"].sum(), 1),
         "share_at_sydenham": round(100 * len(at) / max(1, len(sw)), 1),
         "stationary_at_sydenham": round(100 * float((at["speed_kmh"] < 1).mean()), 1) if len(at) else None,
         "mean_late_at_sydenham": round(float(at["late_min"].mean()), 1) if len(at) else None,
         "on_time_away": otr(away)["on_time"], "mean_late_away": otr(away)["mean_late_min"],
         "mean_late_rest_of_network": otr(d[~d["sw_link"]])["mean_late_min"]}
    stats["southwest_link"] = s
    fig, axes = plt.subplots(1, 2, figsize=(SLIDE[0], 5.4), gridspec_kw={"width_ratios": [1, 1.4]})
    header(fig, "Our biggest early hotspot was an artefact: buses laying over at Sydenham",
           f"Southwest Link (SW1–SW3, T3 Bankstown rail replacement) · {s['window']} · "
           f"{s['stationary_at_sydenham']:.0f}% of its Sydenham readings were stationary buses still reporting a delay")
    fig.subplots_adjust(top=1 - 1.5 / 5.4, bottom=0.17, wspace=0.25)
    for ax, labs, v, cols, fmt, ttl in (
        (axes[0], ["Share of\nreadings", "Share of\nbus-minutes late"], [s["share_readings"], s["share_lateness"]],
         [MUTED, LATE_C], lambda x: f"{x:.1f}%", "SW1–SW3 in the whole network"),
        (axes[1], ["SW Link at\nSydenham", "SW Link\nelsewhere", "Rest of\nnetwork"],
         [s["mean_late_at_sydenham"], s["mean_late_away"], s["mean_late_rest_of_network"]],
         [LATE_C, MUTED, ONTIME_C], lambda x: f"{x:.1f} min", "Average lateness")):
        bars = ax.bar(range(len(v)), v, color=cols, width=0.6)
        for b, val in zip(bars, v):
            ax.text(b.get_x() + b.get_width() / 2, val, fmt(val), ha="center", va="bottom",
                    fontsize=12, fontweight="bold")
        ax.set_xticks(range(len(v))); ax.set_xticklabels(labs, fontsize=10.5)
        ax.set_yticks([]); ax.spines["left"].set_visible(False)
        ax.set_ylim(0, max(v) * 1.2); ax.tick_params(length=0)
        ax.set_title(ttl, fontsize=12, loc="center", pad=12)
    footer(fig, "Sydenham = within 300 m of the station. Excluded from every other chart.")
    return [save(fig, out, "southwest_link_artefact")]


def period_hours(a, b):
    return [h % 24 for h in range(a, b)]


def chart_periods(d, out, segs, stats, window_txt):
    """Weekday time-of-day comparison: average delay maps per period on one
    shared scale, and the early / on time / late split per period."""
    files = []
    wk = d[d["ts"].dt.dayofweek < 5]
    dd = wk[wk["has_delay"]]
    r = Raster()
    mid = segs.mean(axis=1)
    norm = Normalize(1, 3.5)
    prior = float(dd["late_min"].mean())
    fields = {}
    for name, a, b in PERIODS:
        sub = dd[dd["hour"].isin(period_hours(a, b))]
        x, y = to_xy(sub["lat"].values, sub["lon"].values)
        f, w = field(r, x, y, sub["late_min"].values, DELAY_R_M, prior)
        fields[name] = (r.sample(f, mid[:, 0], mid[:, 1]), r.sample(w, mid[:, 0], mid[:, 1]))
    stats["weekday_by_period"] = {name: otr(wk[wk["hour"].isin(period_hours(a, b))]) for name, a, b in PERIODS}

    def label(name, a, b):
        return f"{name}  {a:02d}:00–{b % 24:02d}:00"

    for fname, names, figsize, grid in (
            ("map_average_delay_peaks", ["AM peak", "PM peak"], (13.33, 6.9), (1, 2)),
            ("map_average_delay_periods", [p[0] for p in PERIODS], (11, 11.6), (2, 2))):
        fig, axes = plt.subplots(*grid, figsize=figsize)
        fig.subplots_adjust(left=0.005, right=0.995, top=0.95, bottom=0.07, wspace=0.02, hspace=0.08)
        for ax, name in zip(np.ravel(axes), names):
            a, b = next((p[1], p[2]) for p in PERIODS if p[0] == name)
            ax.set_aspect("equal"); ax.axis("off")
            ax.set_xlim(-10_200, 10_200); ax.set_ylim(-10_200, 10_200)
            v, w = fields[name]
            draw_network_map(ax, segs, v, w, YLORRD, norm, FULL_SUPPORT["delay"], lw=1.0)
            cbd_ring(ax)
            ax.set_title(label(name, a, b), fontsize=13, loc="left")
        colourbar(fig, [0.35, 0.03, 0.3, 0.014 if grid == (2, 2) else 0.02], YLORRD, norm,
                  "Average delay (min)", [1, 1.5, 2, 2.5, 3, 3.5], ["≤1", "1.5", "2", "2.5", "3", "3.5+"])
        files.append(save(fig, out, fname))

    # early / on time / late per period
    rows = stats["weekday_by_period"]
    names = [p[0] for p in PERIODS][::-1]
    fig, ax = plt.subplots(figsize=(SLIDE[0], 4.2))
    fig.subplots_adjust(left=0.12, right=0.98, top=0.86, bottom=0.08)
    yy = np.arange(len(names))
    e = np.array([rows[n]["early"] for n in names]); o = np.array([rows[n]["on_time"] for n in names])
    l = np.array([rows[n]["late"] for n in names])
    ax.barh(yy, e, color=EARLY_C, height=0.64, label="Early")
    ax.barh(yy, o, left=e, color=ONTIME_C, height=0.64, label="On time")
    ax.barh(yy, l, left=e + o, color=LATE_C, height=0.64, label="Late")
    for yi in yy:
        ax.text(e[yi] / 2, yi, f"{e[yi]:.0f}%", ha="center", va="center", fontsize=10.5, color="white")
        ax.text(e[yi] + o[yi] / 2, yi, f"{o[yi]:.0f}% on time", ha="center", va="center", fontsize=10.5, color=INK)
        ax.text(100 - l[yi] / 2, yi, f"{l[yi]:.0f}%", ha="center", va="center", fontsize=10.5, color="white")
    ax.set_yticks(yy); ax.set_yticklabels(names, fontsize=11.5); ax.set_xlim(0, 100)
    ax.set_xticks([]); ax.tick_params(length=0)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_visible(False)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3)
    files.append(save(fig, out, "otr_by_period"))
    return files


def chart_collection(repo, out, stats):
    s = snapshots_per_day(repo)
    s = s[(s.index >= "2026-09-04") & (s.index < s.index.max())]   # drop today's partial day
    stats["snapshots_per_day"] = {k: int(v) for k, v in s.items()}
    fig, ax = plt.subplots(figsize=(SLIDE[0], 5))
    header(fig, "Collector snapshots per day: from a few a day to continuous",
           "GitHub ran the 5-minute schedule only every few hours; self-chaining runs fixed it from 12 October")
    fig.subplots_adjust(bottom=0.1)
    x = pd.to_datetime(s.index)
    ax.bar(x, s.values, color=SERIES[0], width=0.8)
    ax.axhline(288, color=INK2, lw=1, ls=(0, (4, 3)))
    ax.set_ylim(0, 330); ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
    ax.tick_params(length=0); ax.spines["left"].set_visible(False)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%d/%m"))
    return [save(fig, out, "collection_per_day")]


# --- README gallery ---------------------------------------------------------
def write_readme(out, stats, files):
    s = stats
    c = s["cbd_vs_elsewhere"]
    n = s["network"]
    lines = [f"# CIVL3704 presentation charts", "",
             f"Built {s['built_at']} from {s['window']} ({n['n']:,} delay readings, "
             f"{s['snapshots']:,} snapshots). Rebuilt daily by `.github/workflows/analysis.yml` on main; "
             "download a PNG by opening it and choosing *Download raw file*.", "",
             "## Headline numbers", "",
             f"- Network: **{n['on_time']}% on time**, {n['early']}% early, {n['late']}% late; "
             f"mean lateness {n['mean_late_min']} min (Southwest Link excluded)",
             f"- CBD (≤1.5 km of Town Hall): {c['CBD']['mean_late_min']} min average lateness, "
             f"{c['CBD']['late']}% late, {c['CBD']['speed_kmh']} km/h — elsewhere "
             f"{c['Elsewhere']['mean_late_min']} min, {c['Elsewhere']['late']}% late, {c['Elsewhere']['speed_kmh']} km/h",
             f"- CBD holds {c['CBD']['share_readings']}% of readings and {c['CBD']['share_burden']}% of bus-minutes late",
             f"- Top 5% of 200 m areas hold {s['burden_concentration']['top5pct_cells_burden']}% of burden "
             f"and {s['burden_concentration']['top5pct_cells_readings']}% of readings"]
    for name, p in s["by_period"].items():
        lines.append(f"- {name}: {p['on_time']}% on time, {p['early']}% early, {p['late']}% late")
    for name, p in s["weekday_by_period"].items():
        lines.append(f"- {name}, weekdays only: {p['on_time']}% on time, {p['early']}% early, {p['late']}% late, "
                     f"mean lateness {p['mean_late_min']} min")
    if "operators_gap_points" in s:
        g = s["operators_gap_points"]
        lines.append(f"- Operators read {g[0]}–{g[1]} points below published OTR "
                     f"({s['published_period']}); Spearman ρ = {s['operators_spearman_rho']}")
    if "southwest_link" in s:
        w = s["southwest_link"]
        lines.append(f"- Southwest Link ({w['window']}): {w['share_readings']}% of readings, "
                     f"{w['share_lateness']}% of lateness; {w['mean_late_at_sydenham']} min late at Sydenham "
                     f"({w['stationary_at_sydenham']}% stationary) vs {w['mean_late_away']} min elsewhere")
    lines += ["", "### Highest average delay", ""]
    for i, h in enumerate(s["hotspots_average_delay"], 1):
        flag = " — layover-heavy, check" if h["stationary_pct"] > 60 else ""
        lines.append(f"{i}. {h['name']}: {h['value']:.1f} min ({h['n']:,} readings, "
                     f"{h['stationary_pct']}% stationary){flag}")
    lines += ["", "### Highest delay burden", ""]
    for i, h in enumerate(s["hotspots_delay_burden"], 1):
        lines.append(f"{i}. {h['name']}: {h['per_day_bus_min']:,.0f} bus-minutes late per day ({h['n']:,} readings)")
    lines += ["", "## Charts", ""]
    for f in files:
        lines += [f"### {f}", "", f"![{f}](charts/{f})", ""]
    (out / "README.md").write_text("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(REPO))
    ap.add_argument("--out", default="analysis-out")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--sw-start", default=SW_WINDOW[0]); ap.add_argument("--sw-end", default=SW_WINDOW[1])
    a = ap.parse_args()
    repo, out = Path(a.repo), Path(a.out)
    (out / "charts").mkdir(parents=True, exist_ok=True)
    setup_fonts()

    newest = max(datetime.strptime(p.stem, "%Y-%m-%d").date()
                 for p in (repo / "data").glob("*.csv"))
    end = datetime.strptime(a.end, "%Y-%m-%d").date() if a.end else newest
    start = datetime.strptime(a.start, "%Y-%m-%d").date() if a.start else end - timedelta(days=a.days - 1)
    allrows = load(repo, start, end)
    if allrows.empty:
        sys.exit("no data in window")
    d = allrows[~allrows["sw_link"]]
    t0, t1 = d["ts"].min(), d["ts"].max()
    window_txt = f"{t0:%d/%m} – {t1:%d/%m/%Y}"
    published = json.loads((HERE / "published_otr.json").read_text())

    stats = {"built_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
             "window": f"{t0:%Y-%m-%d %H:%M} to {t1:%Y-%m-%d %H:%M} (Sydney)",
             "snapshots": int(d["ts"].nunique()), "network": otr(d),
             "southwest_link_in_window": round(100 * allrows["sw_link"].mean(), 1),
             "published_period": published["published_period"]}
    segs = load_network(repo)
    stops = load_stops(repo)
    files = []
    cbd_stats(d, stats)
    files += chart_maps(d, out, segs, stops, stats, window_txt)
    files += chart_hourly(d, out, stats, window_txt)
    files += chart_cbd(d, out, stats, window_txt)
    files += chart_concentration(d, out, stats, window_txt)
    files += chart_operators(d, out, stats, published, window_txt)
    files += chart_sw_link(repo, out, stats, datetime.strptime(a.sw_start, "%Y-%m-%d").date(),
                           datetime.strptime(a.sw_end, "%Y-%m-%d").date())
    files += chart_periods(d, out, segs, stats, window_txt)
    files += chart_collection(repo, out, stats)

    for k in ("hotspots_average_delay", "hotspots_delay_burden"):
        for h in stats[k]:
            h["lat"] = round(CENTRE[0] + h["y"] / M_PER_DEG_LAT, 5)
            h["lon"] = round(CENTRE[1] + h["x"] / M_PER_DEG_LON, 5)
            h.pop("x"); h.pop("y")
    rows = [{"map": k.replace("hotspots_", ""), "rank": i, **h}
            for k in ("hotspots_average_delay", "hotspots_delay_burden") for i, h in enumerate(stats[k], 1)]
    pd.DataFrame(rows).to_csv(out / "hotspots.csv", index=False)
    (out / "stats.json").write_text(json.dumps(stats, indent=2, default=str))
    write_readme(out, stats, files)


if __name__ == "__main__":
    main()
