"""
Build a compact, de-duplicated bus route network for the CIVL3704 heatmap's
route mask, from the TfNSW bus GTFS schedule bundle's shapes.txt.

Output: shapes/route_shapes.json — consumed by the civl3704 dashboard
(/api/route_shapes), which clips the heatmap to within N metres of these
lines so it reads as corridors rather than round blobs.

Pipeline:
  1. Download the bus schedule bundle (same endpoint the dashboard uses;
     handles both a flat bundle and the per-agency nested-zip layout).
  2. Read every shape, keep only segments with an end inside RADIUS_KM of
     the CBD (the collector only records buses within 10 km).
  3. Densify each segment and snap it onto a CELL_M grid. Dozens of routes
     sharing one road produce the exact same chain of cells, so collapsing
     to a set of cell-to-cell edges de-duplicates them for free — the file
     ends up describing the road NETWORK once, not every route separately.
  4. Walk the edge graph back into polylines (breaking at junctions),
     Douglas-Peucker simplify them to SIMPLIFY_M to remove the grid
     staircase, and write them delta-encoded in 1e-5 degree integers.

Snapping moves a line by at most ~CELL_M/sqrt(2) (~7 m) — well inside the
mask widths the dashboard offers (±15 m and up) and inside typical bus GPS
error anyway.

Usage:
    TFNSW_API_KEY=... python build_route_shapes.py
    python build_route_shapes.py --zip path/to/bundle.zip   # skip download
"""

import argparse
import codecs
import csv
import json
import os
import sys
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime
from math import ceil, cos, hypot, radians
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEDULE_URL = os.getenv("TFNSW_GTFS_SCHEDULE_URL", "https://api.transport.nsw.gov.au/v1/gtfs/schedule/buses")
SYDNEY_TZ = ZoneInfo("Australia/Sydney")
SYDNEY_CBD = (-33.8688, 151.2093)
RADIUS_KM = float(os.getenv("SHAPES_RADIUS_KM", "12"))
CELL_M = 10.0
SIMPLIFY_M = 6.0
OUT_PATH = Path("shapes/route_shapes.json")

LAT0, LON0 = SYDNEY_CBD
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * cos(radians(LAT0))


def to_xy(lat, lon):
    return (lon - LON0) * M_PER_DEG_LON, (lat - LAT0) * M_PER_DEG_LAT


def to_latlon(x, y):
    return LAT0 + y / M_PER_DEG_LAT, LON0 + x / M_PER_DEG_LON


def download(dest):
    import requests
    key = os.environ["TFNSW_API_KEY"]
    with requests.get(SCHEDULE_URL, headers={"Authorization": f"apikey {key}"}, timeout=180, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                f.write(chunk)
    print(f"downloaded {os.path.getsize(dest) / 1e6:.1f} MB", flush=True)


def iter_shapes_members(zip_path, tmpdir):
    """Yield open binary file objects for every shapes.txt in the bundle,
    whether it's flat or one inner zip per agency."""
    with zipfile.ZipFile(zip_path) as outer:
        names = outer.namelist()
        if "shapes.txt" in names:
            with outer.open("shapes.txt") as f:
                yield f
            return
        for i, name in enumerate(names):
            if not name.endswith(".zip"):
                continue
            inner_path = os.path.join(tmpdir, f"inner_{i}.zip")
            with outer.open(name) as src, open(inner_path, "wb") as dst:
                while chunk := src.read(1 << 20):
                    dst.write(chunk)
            try:
                with zipfile.ZipFile(inner_path) as inner:
                    if "shapes.txt" in inner.namelist():
                        with inner.open("shapes.txt") as f:
                            yield f
            finally:
                os.unlink(inner_path)


def read_shapes(zip_path):
    """{shape_id: [(seq, x, y), ...]} for every shape point (unclipped —
    clipping happens per segment so a shape crossing the boundary keeps
    its inside part intact)."""
    shapes = defaultdict(list)
    n = 0
    with tempfile.TemporaryDirectory() as tmpdir:
        for member in iter_shapes_members(zip_path, tmpdir):
            reader = csv.reader(codecs.iterdecode(member, "utf-8-sig"))
            header = [h.strip() for h in next(reader)]
            si, la, lo, sq = (header.index(c) for c in
                              ("shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"))
            for row in reader:
                try:
                    x, y = to_xy(float(row[la]), float(row[lo]))
                    shapes[row[si]].append((int(float(row[sq])), x, y))
                except (ValueError, IndexError):
                    continue
                n += 1
    print(f"read {n} shape points across {len(shapes)} shapes", flush=True)
    return shapes


def build_edges(shapes):
    r2 = (RADIUS_KM * 1000) ** 2
    edges = set()
    step = CELL_M / 2
    for pts in shapes.values():
        pts.sort()
        for (_, x1, y1), (_, x2, y2) in zip(pts, pts[1:]):
            if x1 * x1 + y1 * y1 > r2 and x2 * x2 + y2 * y2 > r2:
                continue
            seg = hypot(x2 - x1, y2 - y1)
            if seg > 2000:  # a jump this long is a data gap, not a road
                continue
            k = max(1, ceil(seg / step))
            prev = (round(x1 / CELL_M), round(y1 / CELL_M))
            for j in range(1, k + 1):
                t = j / k
                cell = (round((x1 + (x2 - x1) * t) / CELL_M), round((y1 + (y2 - y1) * t) / CELL_M))
                if cell != prev:
                    edges.add((prev, cell) if prev < cell else (cell, prev))
                    prev = cell
    print(f"{len(edges)} unique {CELL_M:.0f} m grid edges inside {RADIUS_KM:g} km", flush=True)
    return edges


def chain(edges):
    """Turn an undirected edge set into polylines, breaking at any node
    that isn't a simple pass-through (degree != 2)."""
    adj = defaultdict(set)
    for a, b in edges:
        adj[a].add(b)
        adj[b].add(a)
    used = set()

    def walk(start, nxt):
        line = [start]
        prev, cur = start, nxt
        while True:
            used.add((prev, cur) if prev < cur else (cur, prev))
            line.append(cur)
            if len(adj[cur]) != 2:
                return line
            (a, b) = adj[cur]
            n2 = b if a == prev else a
            e = (cur, n2) if cur < n2 else (n2, cur)
            if e in used:
                return line
            prev, cur = cur, n2

    lines = []
    for node in [n for n in adj if len(adj[n]) != 2]:
        for nb in adj[node]:
            if ((node, nb) if node < nb else (nb, node)) not in used:
                lines.append(walk(node, nb))
    for a, b in edges:  # leftover pure cycles
        if (a, b) not in used:
            lines.append(walk(a, b))
    return lines


def smooth(pts, half=2):
    """Moving-average the grid-snapped points (window 2*half+1, endpoints
    fixed so junctions still meet). Removes the snapping staircase on
    diagonal roads, which Douglas-Peucker alone would otherwise keep as
    dozens of tiny zigzag vertices."""
    if len(pts) <= 2 * half + 1:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        lo, hi = max(0, i - half), min(len(pts), i + half + 1)
        w = pts[lo:hi]
        out.append((sum(p[0] for p in w) / len(w), sum(p[1] for p in w) / len(w)))
    out.append(pts[-1])
    return out


def simplify(pts, tol):
    """Iterative Douglas-Peucker on [(x, y), ...]."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i, j = stack.pop()
        (x1, y1), (x2, y2) = pts[i], pts[j]
        dx, dy = x2 - x1, y2 - y1
        L = hypot(dx, dy)
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            d = abs(dy * (px - x1) - dx * (py - y1)) / L if L else hypot(px - x1, py - y1)
            if d > best:
                best, bi = d, k
        if best > tol:
            keep[bi] = True
            stack += [(i, bi), (bi, j)]
    return [p for p, k in zip(pts, keep) if k]


def encode(lines):
    out = []
    n_pts = 0
    for line in lines:
        xy = simplify(smooth([(cx * CELL_M, cy * CELL_M) for cx, cy in line]), SIMPLIFY_M)
        enc, plat, plon = [], 0, 0
        for x, y in xy:
            lat, lon = to_latlon(x, y)
            ilat, ilon = round(lat * 1e5), round(lon * 1e5)
            enc += [ilat - plat, ilon - plon]
            plat, plon = ilat, ilon
        if len(enc) >= 4:
            out.append(enc)
            n_pts += len(enc) // 2
    return out, n_pts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", help="use a local GTFS bundle instead of downloading")
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    if args.zip:
        shapes = read_shapes(args.zip)
    else:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "bundle.zip")
            download(path)
            shapes = read_shapes(path)
    if not shapes:
        sys.exit("No shapes.txt found in the bundle")

    lines, n_pts = encode(chain(build_edges(shapes)))
    doc = {
        "generated": datetime.now(tz=SYDNEY_TZ).isoformat(timespec="seconds"),
        "source": "TfNSW bus GTFS schedule shapes.txt",
        "centre": list(SYDNEY_CBD),
        "radius_km": RADIUS_KM,
        "cell_m": CELL_M,
        "simplify_m": SIMPLIFY_M,
        "encoding": "each line = flat [dlat, dlon, ...] in 1e-5 deg, delta from previous point (first from 0,0)",
        "n_lines": len(lines),
        "n_points": n_pts,
        "lines": lines,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"wrote {out}: {len(lines)} lines, {n_pts} points, {out.stat().st_size / 1e3:.0f} kB", flush=True)


if __name__ == "__main__":
    main()
