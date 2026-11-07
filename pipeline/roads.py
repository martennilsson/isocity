"""Drivable road network for the game layer (cars), from OpenStreetMap, on the map's ground.

Three steps; heights come from the renderer, so roads sit on the same meshes the map was drawn from:

  # 1. OSM extract -> road graph in ENU metres + the points the renderer must probe for height
  uv run python -m pipeline.roads points --pbf data/osm/Stockholm.osm.pbf \\
      --plan data/plan/sodermalm.json --map dist/sodermalm --out data/roads/sodermalm
  # 2. ground height under every point (Google 3D tiles at render LOD; closed mode only)
  cd render && npm run render -- probe --plan ../data/plan/sodermalm.json \\
      --points ../data/roads/sodermalm/probe.json --out ../data/roads/sodermalm/heights.json
  # 3. heights -> dist/<map>/roads.json for the viewer (--flat: skip step 2, all roads at h = 0)
  uv run python -m pipeline.roads build --plan data/plan/sodermalm.json --map dist/sodermalm \\
      --work data/roads/sodermalm [--flat] [--preview data/roads/sodermalm/preview.png]

Step 2 needs the Google 3D tiles

The OSM extract is a file (e.g. BBBike's Stockholm.osm.pbf), not Overpass: Overpass was
overloaded for hours (502/504 on every mirror, 2026-11-06).

Kept: drivable highways (motorway .. residential, living_street, service except parking aisles
and driveways), not access=no/private. Skipped for now: tunnels (cars would be drawn on top of
the hill above them) and bridges (OSM has no deck height and the probe would hit the water).

roads.json (ENU metres in the plan's frame; the viewer projects with manifest.json's camera):
  points: flat [e, n, h, ...]
  edges:  [{"p": [point index, ...], "oneway": 0|1, "lanes": n, "kmh": speed, "hw": class}]
  Edges meet at shared point indices (junctions); an edge's first/last point is a junction or a
  dead end (map edge, skipped bridge or tunnel).
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

import numpy as np

from pipeline.plan_tiles import Projection

DRIVABLE = {
    "motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential",
    "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link",
    "living_street", "service",
}
SKIP_SERVICE = {"parking_aisle", "driveway", "drive-through", "emergency_access"}
DEFAULT_KMH = {"motorway": 70, "trunk": 60, "primary": 50, "secondary": 40, "tertiary": 40,
               "unclassified": 30, "residential": 30, "living_street": 7, "service": 15}
DENSIFY_M = 8.0  # max point spacing along a road, so hills between OSM nodes get probed
MARGIN_M = 150.0  # extract this far beyond the map's ground footprint (heights shift it)
H_RANGE = (-10.0, 80.0)  # plausible ground heights (m above the plan's reference plane)
ATTRIBUTION = "© OpenStreetMap contributors"


def load_map(map_dir: Path) -> dict:
    return json.loads((map_dir / "manifest.json").read_text())


def image_rect_screen(manifest: dict) -> tuple[float, float, float, float]:
    ox, oy = manifest["origin_screen_px"]
    return ox, oy, ox + manifest["width"], oy + manifest["height"]


# --- 1. extract --------------------------------------------------------------------------------


def keep_way(tags: dict) -> bool:
    hw = tags.get("highway")
    if hw not in DRIVABLE:
        return False
    if hw == "service" and tags.get("service") in SKIP_SERVICE:
        return False
    if tags.get("access") in ("no", "private") or tags.get("motor_vehicle") in ("no", "private"):
        return False
    if tags.get("tunnel", "no") != "no" or tags.get("covered") == "yes":
        return False
    if tags.get("bridge", "no") != "no":
        return False
    if tags.get("area") == "yes":
        return False
    return True


def parse_oneway(tags: dict) -> int:
    """1 = drive along the way's node order, -1 = against it, 0 = both ways."""
    v = tags.get("oneway")
    if v in ("yes", "true", "1"):
        return 1
    if v == "-1":
        return -1
    if v == "no":
        return 0
    if tags.get("junction") in ("roundabout", "circular") or tags.get("highway") in ("motorway", "motorway_link"):
        return 1
    return 0


def parse_kmh(tags: dict) -> float:
    v = tags.get("maxspeed", "")
    try:
        return float(v.split()[0])
    except (ValueError, IndexError):
        return DEFAULT_KMH.get(tags["highway"].removesuffix("_link"), 30)


def parse_lanes(tags: dict, oneway: int) -> int:
    try:
        n = int(float(tags.get("lanes", "").split(";")[0]))
    except ValueError:
        n = 1 if oneway else 2
    return max(1, min(n, 6))


def read_ways(pbf: Path, bbox: tuple[float, float, float, float]):
    """Drivable ways with any node inside bbox (lon0, lat0, lon1, lat1) -> (ways, node lon/lat)."""
    import osmium

    lon0, lat0, lon1, lat1 = bbox
    ways = []
    fp = osmium.FileProcessor(str(pbf), osmium.osm.NODE | osmium.osm.WAY).with_locations()
    for w in fp:
        if not w.is_way():
            continue
        tags = dict(w.tags)
        if not keep_way(tags):
            continue
        nodes = [(n.ref, n.lon, n.lat) for n in w.nodes if n.location.valid()]
        if len(nodes) < 2 or not any(lon0 <= lon <= lon1 and lat0 <= lat <= lat1 for _, lon, lat in nodes):
            continue
        ways.append({"id": w.id, "tags": tags, "nodes": nodes})
    return ways


def split_at_junctions(ways: list[dict]) -> list[dict]:
    """Cut ways at every node shared with another way (or used twice), so edges meet at ends."""
    use = Counter()
    for w in ways:
        for ref, *_ in w["nodes"]:
            use[ref] += 1
        use[w["nodes"][0][0]] += 1  # way ends are always edge ends
        use[w["nodes"][-1][0]] += 1
    edges = []
    for w in ways:
        oneway = parse_oneway(w["tags"])
        nodes = w["nodes"][::-1] if oneway == -1 else w["nodes"]
        attrs = {
            "oneway": 1 if oneway else 0,
            "lanes": parse_lanes(w["tags"], oneway),
            "kmh": parse_kmh(w["tags"]),
            "hw": w["tags"]["highway"],
            "name": w["tags"].get("name", ""),
            "osm": w["id"],
        }
        cur = [nodes[0]]
        for nd in nodes[1:]:
            cur.append(nd)
            if use[nd[0]] > 1:
                edges.append({**attrs, "nodes": cur})
                cur = [nd]
        if len(cur) > 1:
            edges.append({**attrs, "nodes": cur})
    return edges


def cmd_points(args) -> None:
    plan = json.loads(args.plan.read_text())
    manifest = load_map(args.map)
    proj = Projection.from_config(plan["config"])

    # ground footprint of the image (at the reference plane), grown by a margin
    x0, y0, x1, y1 = image_rect_screen(manifest)
    corners = [proj.screen_to_ground(x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    es, ns = [c[0] for c in corners], [c[1] for c in corners]
    lo = proj.enu_to_lonlat((min(es) - MARGIN_M, min(ns) - MARGIN_M, 0.0))
    hi = proj.enu_to_lonlat((max(es) + MARGIN_M, max(ns) + MARGIN_M, 0.0))
    bbox = (lo[0], lo[1], hi[0], hi[1])

    ways = read_ways(args.pbf, bbox)
    edges = split_at_junctions(ways)

    # points: one per OSM node (shared between edges = junction), plus densified in-between points
    index: dict[int, int] = {}
    pts: list[tuple[float, float]] = []

    def node_point(ref, lon, lat) -> int:
        if ref not in index:
            e, n, _ = proj.lonlat_to_enu(lon, lat)
            index[ref] = len(pts)
            pts.append((e, n))
        return index[ref]

    out_edges = []
    for ed in edges:
        ids = [node_point(*nd) for nd in ed["nodes"]]
        dense = [ids[0]]
        for a, b in zip(ids, ids[1:]):
            (ea, na), (eb, nb) = pts[a], pts[b]
            k = math.ceil(math.hypot(eb - ea, nb - na) / DENSIFY_M)
            for i in range(1, k):
                t = i / k
                pts.append((ea + (eb - ea) * t, na + (nb - na) * t))
                dense.append(len(pts) - 1)
            dense.append(b)
        if len(set(dense)) < 2:
            continue
        out_edges.append({k: v for k, v in ed.items() if k != "nodes"} | {"p": dense})

    # which plan tiles might show each point (its screen y depends on the unknown height)
    tiles: dict[str, list[int]] = {t["id"]: [] for t in plan["tiles"]}
    pad = 32
    for i, (e, n) in enumerate(pts):
        sx, sy_lo = proj.enu_to_screen((e, n, H_RANGE[0]))
        _, sy_hi = proj.enu_to_screen((e, n, H_RANGE[1]))
        if not (x0 - pad <= sx <= x1 + pad and min(sy_lo, sy_hi) <= y1 + pad and max(sy_lo, sy_hi) >= y0 - pad):
            continue
        for t in plan["tiles"]:
            rx0, ry0, rx1, ry1 = t["screen_rect_px"]
            if rx0 - pad <= sx <= rx1 + pad and min(sy_lo, sy_hi) <= ry1 + pad and max(sy_lo, sy_hi) >= ry0 - pad:
                tiles[t["id"]].append(i)
    tiles = {k: v for k, v in tiles.items() if v}

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "graph.json").write_text(json.dumps({
        "source": str(args.pbf), "bbox_lonlat": bbox,
        "points": [[round(e, 3), round(n, 3)] for e, n in pts], "edges": out_edges,
    }))
    (args.out / "probe.json").write_text(json.dumps({
        "points": [[round(e, 3), round(n, 3)] for e, n in pts], "tiles": tiles,
    }))
    n_probe = sum(len(v) for v in tiles.values())
    print(f"{len(ways)} ways -> {len(out_edges)} edges, {len(pts)} points; "
          f"{n_probe} probes in {len(tiles)} tiles -> {args.out}")


# --- 3. build ----------------------------------------------------------------------------------


def pick_heights(plan: dict, proj: Projection, pts: np.ndarray, heights: dict) -> np.ndarray:
    """Per point, the height measured in the tile whose screen rect contains it (nearest the centre)."""
    by_id = {t["id"]: t for t in plan["tiles"]}
    probe = json.loads(Path(heights["_probe"]).read_text())["tiles"]
    best = np.full(len(pts), np.nan)
    best_d = np.full(len(pts), np.inf)
    for tid, idx in probe.items():
        hs = heights.get(tid)
        if hs is None:
            continue
        x0, y0, x1, y1 = by_id[tid]["screen_rect_px"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for i, h in zip(idx, hs):
            if h is None or not (H_RANGE[0] <= h <= H_RANGE[1]):
                continue
            sx, sy = proj.enu_to_screen((pts[i, 0], pts[i, 1], h))
            if not (x0 <= sx < x1 and y0 <= sy < y1):
                continue
            d = max(abs(sx - cx), abs(sy - cy))
            if d < best_d[i]:
                best[i], best_d[i] = h, d
    return best


def smooth_along(edges: list[dict], h: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Fill missing heights along each edge (linear in distance) and remove single-point spikes
    (a ray through a hole in the mesh, or a hit on a parked car) with a 5-point median."""
    out = h.copy()
    for ed in edges:
        p = np.array(ed["p"])
        v = h[p]
        ok = ~np.isnan(v)
        if not ok.any():
            continue
        d = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(pts[p], axis=0).T))])
        v = np.interp(d, d[ok], v[ok])
        if len(v) >= 5:
            pad = np.pad(v, 2, mode="edge")
            med = np.median(np.lib.stride_tricks.sliding_window_view(pad, 5), axis=1)
            spike = np.abs(v - med) > 1.5
            v[spike] = med[spike]
        # junction points keep their own measurement when they have one (shared with other edges)
        inner = slice(1, -1)
        out[p[inner]] = v[inner]
        for j in (0, -1):
            if np.isnan(out[p[j]]):
                out[p[j]] = v[j]
    return out


def cmd_build(args) -> None:
    plan = json.loads(args.plan.read_text())
    manifest = load_map(args.map)
    proj = Projection.from_config(plan["config"])
    graph = json.loads((args.work / "graph.json").read_text())
    pts = np.array(graph["points"], dtype=float)

    if args.flat:
        # placeholder until a ground model is available: every road on the reference plane
        h = np.zeros(len(pts))
        measured = 0
    else:
        heights = json.loads((args.work / "heights.json").read_text())
        heights["_probe"] = str(args.work / "probe.json")
        h = pick_heights(plan, proj, pts, heights)
        measured = int((~np.isnan(h)).sum())
        h = smooth_along(graph["edges"], h, pts)

    # keep only points on the image; split edges where they leave it
    x0, y0, x1, y1 = image_rect_screen(manifest)
    on_map = np.zeros(len(pts), bool)
    for i in range(len(pts)):
        if np.isnan(h[i]):
            continue
        sx, sy = proj.enu_to_screen((pts[i, 0], pts[i, 1], h[i]))
        on_map[i] = x0 <= sx < x1 and y0 <= sy < y1
    edges = []
    for ed in graph["edges"]:
        run: list[int] = []
        for i in ed["p"] + [-1]:
            if i >= 0 and on_map[i]:
                run.append(i)
                continue
            if len(run) >= 2:
                edges.append({"p": run, "oneway": ed["oneway"], "lanes": ed["lanes"], "kmh": ed["kmh"], "hw": ed["hw"]})
            run = []

    # renumber the points that are still used
    used = sorted({i for e in edges for i in e["p"]})
    remap = {old: new for new, old in enumerate(used)}
    for e in edges:
        e["p"] = [remap[i] for i in e["p"]]
    flat = []
    for i in used:
        flat += [round(float(pts[i, 0]), 2), round(float(pts[i, 1]), 2), round(float(h[i]), 2)]

    length_km = sum(np.hypot(*np.diff(pts[[used[j] for j in e["p"]]], axis=0).T).sum() for e in edges) / 1000
    out = {"attribution": ATTRIBUTION, "frame": "plan ENU metres (manifest.json camera)",
           "heights": "flat" if args.flat else "probed",
           "source": graph["source"], "points": flat, "edges": edges}
    (args.map / "roads.json").write_text(json.dumps(out, separators=(",", ":")))
    hs = h[used]
    print(f"{len(edges)} edges, {len(used)} points, {length_km:.1f} km of road; "
          f"{measured}/{len(pts)} points measured; heights {hs.min():.1f}..{hs.max():.1f} m "
          f"-> {args.map / 'roads.json'}")
    if args.preview:
        write_preview(args, plan, proj, manifest, edges, np.array(flat).reshape(-1, 3))


def write_preview(args, plan, proj, manifest, edges, P) -> None:
    """Roads drawn over the map at 1/4 scale, plus a 1:1 detail crop, to check they sit on streets."""
    from PIL import Image, ImageDraw

    x0, y0, *_ = image_rect_screen(manifest)
    # reassemble the pyramid level at 1/4 of full size (cheaper than restitching the mosaic)
    levels = sorted((int(p.name) for p in (args.map / "map_files").iterdir() if p.name.isdigit()), reverse=True)
    k = 4
    lvl = levels[2]
    ts = 512
    w, hgt = math.ceil(manifest["width"] / k), math.ceil(manifest["height"] / k)
    img = Image.new("RGB", (w, hgt))
    for f in (args.map / "map_files" / str(lvl)).glob("*.webp"):
        tx, ty = map(int, f.stem.split("_"))
        img.paste(Image.open(f).convert("RGB"), (tx * ts, ty * ts))
    d = ImageDraw.Draw(img)
    colours = {1: (255, 40, 200), 0: (255, 230, 0)}
    for e in edges:
        xy = [((proj.enu_to_screen(tuple(P[i]))[0] - x0) / k, (proj.enu_to_screen(tuple(P[i]))[1] - y0) / k) for i in e["p"]]
        d.line(xy, fill=colours[e["oneway"]], width=2)
    img.save(args.preview)
    print(f"preview -> {args.preview}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("points", help="OSM extract -> road graph + probe points")
    p.add_argument("--pbf", type=Path, required=True)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--map", type=Path, required=True, help="pyramid folder with manifest.json")
    p.add_argument("--out", type=Path, required=True, help="work folder")
    p.set_defaults(fn=cmd_points)
    b = sub.add_parser("build", help="graph + probed heights -> <map>/roads.json")
    b.add_argument("--plan", type=Path, required=True)
    b.add_argument("--map", type=Path, required=True)
    b.add_argument("--work", type=Path, required=True)
    b.add_argument("--preview", type=Path)
    b.add_argument("--flat", action="store_true", help="no heights yet: all roads at h = 0 (placeholder)")
    b.set_defaults(fn=cmd_build)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
