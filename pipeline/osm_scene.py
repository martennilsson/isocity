"""OpenStreetMap -> simple 3D scene for the renderer (`--source osm`).

  fetch: download OSM data for the plan's area (+ margin) from Overpass, with retries across
         mirrors; cached as data/osm/<area>.overpass.json (re-used unless --refresh).
  build: turn it into data/osm/<area>.scene.json in the plan's ENU frame (metres, x east, y north):
         flat ground layers (water, greens, roads, rail, ...) and extruded buildings with a wall
         and a roof colour each. The renderer only draws what is in this file.

Colours are fixed per OSM object (seeded by its id), so every tile and every overlapping
generation window sees the same building the same colour.

Usage:
    uv run python -m pipeline.osm_scene fetch --plan data/plan/sodermalm.json --out-dir data/osm
    uv run python -m pipeline.osm_scene build --plan data/plan/sodermalm.json --out-dir data/osm
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
import urllib.parse
import urllib.request
from pathlib import Path

import shapely
from shapely.geometry import LineString, MultiPolygon, Polygon, box
from shapely.ops import linemerge, polygonize, unary_union

from pipeline.plan_tiles import Projection

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
USER_AGENT = "isocity/0.1 (isometric map prototype)"

# --- look (step-1 test; tune freely, but changing colours changes every render) ---------------

GROUND = "#cbc5b6"  # paved / unmapped land
LAYERS = [  # (kind, z offset m, colour) drawn bottom to top; z offsets avoid z-fighting
    ("water", 0.00, "#3f6d8e"),
    ("landuse", 0.02, None),  # per-feature colour below
    ("green", 0.04, None),
    ("pitch", 0.05, "#6a9f50"),
    ("rail", 0.06, "#8f877a"),
    ("road_minor", 0.07, "#b3ada2"),
    ("road", 0.08, "#9d9890"),
    ("pier", 0.6, "#8c7458"),
]
LANDUSE_COLOURS = {
    "construction": "#b9a588", "railway": "#bdb5a6", "cemetery": "#86a86a", "allotments": "#8fb06a",
    "residential": GROUND, "commercial": GROUND, "industrial": "#c2beb4",
}
GREEN_COLOURS = {
    "park": "#86b25c", "garden": "#86b25c", "grass": "#8fbb62", "meadow": "#9cbf68",
    "playground": "#d6c79c", "forest": "#4f7d3c", "wood": "#4f7d3c", "scrub": "#6f8f4a",
    "grassland": "#9cbf68", "bare_rock": "#9b988c", "track": "#b5654a",
}
ROAD_WIDTH_M = {
    "motorway": 16, "trunk": 14, "primary": 13, "secondary": 11, "tertiary": 9, "residential": 7,
    "unclassified": 7, "living_street": 6, "pedestrian": 6, "service": 4, "busway": 7,
    "footway": 2.5, "cycleway": 2.5, "path": 2, "steps": 2.5, "track": 3,
}
MINOR_ROADS = {"footway", "cycleway", "path", "steps", "track", "service", "pedestrian"}
WALLS = ["#d9a441", "#e3bf74", "#e8cf98", "#c8743f", "#b8573b", "#e9dcc0", "#d8a08a",
         "#c9c1ae", "#b3a58b", "#e6d4a8", "#9c4a35", "#b8c09a"]
WALLS_MODERN = ["#c9c6be", "#b6b2aa", "#dcd8cf", "#8f8e8a", "#a89e8e"]
ROOFS = [("#4b4d52", 45), ("#5c5e63", 15), ("#9a4a32", 20), ("#3c3a38", 10), ("#6c9a86", 10)]
MODERN_TYPES = {"office", "commercial", "industrial", "retail", "warehouse", "train_station",
                "transportation", "parking", "hospital", "university", "school", "public"}
LEVEL_M = 3.1


def _rng(osm_id: str, salt: str) -> float:
    return int(hashlib.sha1(f"{osm_id}:{salt}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF


def _pick_weighted(options: list[tuple[str, int]], u: float) -> str:
    total = sum(w for _, w in options)
    acc = 0.0
    for value, w in options:
        acc += w / total
        if u <= acc:
            return value
    return options[-1][0]


def _osm_colour(value: str | None) -> str | None:
    named = {"white": "#ece8de", "yellow": "#e3bf74", "red": "#a8432f", "brown": "#8a5a3c",
             "grey": "#a9a59c", "gray": "#a9a59c", "beige": "#e2d2ad", "orange": "#d08a4a",
             "black": "#3a3a3a", "green": "#6c9a86", "pink": "#d8a08a", "blue": "#6d8fb0"}
    if not value:
        return None
    v = value.strip().lower()
    if v.startswith("#") and len(v) in (4, 7):
        return v
    return named.get(v)


# --- fetch --------------------------------------------------------------------------------


def plan_bbox_lonlat(plan: dict, margin_m: float) -> tuple[float, float, float, float]:
    proj = Projection.from_config(plan["config"])
    xs, ys = [], []
    for t in plan["tiles"]:
        x0, y0, x1, y1 = t["screen_rect_px"]
        for sx, sy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            e, n, _ = proj.screen_to_ground(sx, sy)
            xs.append(e)
            ys.append(n)
    corners = [(min(xs) - margin_m, min(ys) - margin_m), (max(xs) + margin_m, max(ys) + margin_m)]
    (lon0, lat0, _), (lon1, lat1, _) = (proj.enu_to_lonlat((x, y, 0.0)) for x, y in corners)
    return lat0, lon0, lat1, lon1  # Overpass order: south, west, north, east


def overpass_query(bbox: tuple[float, float, float, float]) -> str:
    b = ",".join(f"{v:.5f}" for v in bbox)
    return f"""[out:json][timeout:180];(
way[building]({b});relation[building]({b});way["building:part"]({b});
nwr[natural~"^(water|coastline|wood|scrub|grassland|bare_rock)$"]({b});nwr[waterway=riverbank]({b});
nwr[landuse~"^(grass|forest|meadow|construction|railway|residential|commercial|industrial|cemetery|allotments)$"]({b});
nwr[leisure~"^(park|garden|pitch|playground|track)$"]({b});way[highway]({b});way[railway]({b});
nwr[man_made~"^(bridge|pier)$"]({b});nwr["area:highway"]({b}););out geom;"""


def fetch(query: str, attempts: int = 6) -> bytes:
    data = urllib.parse.urlencode({"data": query}).encode()
    wait = 15
    for attempt in range(attempts):
        for url in OVERPASS:
            try:
                req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=240) as r:
                    body = r.read()
                if body.lstrip().startswith(b"{"):
                    print(f"  fetched {len(body) / 2**20:.1f} MB from {url}")
                    return body
            except Exception as e:  # busy servers answer 429/502/504
                print(f"  {url}: {e}")
        print(f"  all servers busy, retrying in {wait} s ({attempt + 1}/{attempts})")
        time.sleep(wait)
        wait *= 2
    raise SystemExit("Overpass: all retries failed")


# --- build --------------------------------------------------------------------------------


class Builder:
    def __init__(self, plan: dict, osm: dict, margin_m: float):
        self.proj = Projection.from_config(plan["config"])
        s, w, n, e = plan_bbox_lonlat(plan, margin_m)
        x0, y0 = self.xy(w, s)
        x1, y1 = self.xy(e, n)
        self.clip = box(x0, y0, x1, y1)
        self.els = osm["elements"]

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        e, n, _ = self.proj.lonlat_to_enu(lon, lat)
        return e, n

    def line(self, geom: list[dict]) -> list[tuple[float, float]]:
        return [self.xy(p["lon"], p["lat"]) for p in geom]

    def area(self, el: dict):
        """Polygon / MultiPolygon for a closed way or a multipolygon relation, clipped."""
        if el["type"] == "way":
            pts = self.line(el.get("geometry", []))
            if len(pts) < 4 or pts[0] != pts[-1]:
                return None
            g = Polygon(pts)
        elif el["type"] == "relation":
            outer = [LineString(self.line(m["geometry"])) for m in el.get("members", [])
                     if m.get("role") in ("outer", "") and len(m.get("geometry", [])) >= 2]
            inner = [LineString(self.line(m["geometry"])) for m in el.get("members", [])
                     if m.get("role") == "inner" and len(m.get("geometry", [])) >= 2]
            if not outer:
                return None
            g = unary_union(list(polygonize(linemerge(outer))))
            if inner:
                g = g.difference(unary_union(list(polygonize(linemerge(inner)))))
        else:
            return None
        if not g.is_valid:
            g = g.buffer(0)
        g = g.intersection(self.clip)
        return None if g.is_empty else g

    def sea(self):
        """Faces of the clip box on the water side (right) of natural=coastline ways."""
        coast = [self.line(e["geometry"]) for e in self.els
                 if e["type"] == "way" and e.get("tags", {}).get("natural") == "coastline"]
        if not coast:
            return None
        lines = linemerge([LineString(c) for c in coast])
        faces = list(polygonize(unary_union([lines.intersection(self.clip), self.clip.exterior])))
        segs = []
        for ln in getattr(lines, "geoms", [lines]):
            cs = list(ln.coords)
            segs += list(zip(cs, cs[1:]))
        sea_faces = set()
        for (ax, ay), (bx, by) in segs:
            dx, dy = bx - ax, by - ay
            ln_ = math.hypot(dx, dy)
            if ln_ < 0.5:
                continue
            px, py = (ax + bx) / 2 + dy / ln_ * 0.3, (ay + by) / 2 - dx / ln_ * 0.3  # 0.3 m to the right
            pt = shapely.Point(px, py)
            for i, f in enumerate(faces):
                if i not in sea_faces and f.contains(pt):
                    sea_faces.add(i)
                    break
        return unary_union([faces[i] for i in sea_faces]) if sea_faces else None

    def build(self) -> dict:
        flat: dict[str, list] = {k: [] for k, _, _ in LAYERS}
        colours: dict[str, list] = {k: [] for k, _, _ in LAYERS}
        roads: dict[str, list] = {"road": [], "road_minor": []}
        rail = []
        buildings, parts = [], []
        bridges = []

        sea = self.sea()
        if sea is not None:
            flat["water"].append(sea)

        for el in self.els:
            t = el.get("tags", {})
            oid = f"{el['type'][0]}{el['id']}"
            if "building" in t or "building:part" in t:
                g = self.area(el)
                if g is not None:
                    (parts if "building:part" in t else buildings).append((oid, t, g))
            elif t.get("natural") == "water" or t.get("waterway") == "riverbank":
                g = self.area(el)
                if g is not None:
                    flat["water"].append(g)
            elif t.get("leisure") == "pitch":
                g = self.area(el)
                if g is not None:
                    flat["pitch"].append(g)
            elif (v := t.get("leisure") or t.get("natural") or t.get("landuse")) in GREEN_COLOURS:
                g = self.area(el)
                if g is not None:
                    flat["green"].append(g)
                    colours["green"].append(GREEN_COLOURS[v])
            elif t.get("landuse") in LANDUSE_COLOURS:
                g = self.area(el)
                if g is not None:
                    flat["landuse"].append(g)
                    colours["landuse"].append(LANDUSE_COLOURS[t["landuse"]])
            elif t.get("man_made") == "pier":
                g = self.area(el) if el["type"] == "relation" or (el.get("geometry") and
                    el["geometry"][0] == el["geometry"][-1]) else None
                if g is None and el["type"] == "way":
                    g = LineString(self.line(el["geometry"])).buffer(2, cap_style="flat")
                if g is not None:
                    flat["pier"].append(g)
            elif t.get("man_made") == "bridge":
                g = self.area(el)
                if g is not None:
                    bridges.append(g)
            elif el["type"] == "way" and "highway" in t and t.get("tunnel") not in ("yes", "building_passage"):
                if t.get("area") == "yes" or "area:highway" in t:
                    g = self.area(el)
                    if g is not None:
                        roads["road_minor"].append(g)
                    continue
                w = ROAD_WIDTH_M.get(t["highway"])
                if w and len(el.get("geometry", [])) >= 2:
                    g = LineString(self.line(el["geometry"])).buffer(w / 2, cap_style="flat")
                    roads["road_minor" if t["highway"] in MINOR_ROADS else "road"].append(g)
            elif el["type"] == "way" and t.get("railway") in ("rail", "subway", "light_rail", "tram") \
                    and t.get("tunnel") != "yes" and len(el.get("geometry", [])) >= 2:
                rail.append(LineString(self.line(el["geometry"])).buffer(1.8, cap_style="flat"))

        flat["road"] += roads["road"]
        flat["road_minor"] += roads["road_minor"]
        flat["rail"] += rail

        layers = []
        for kind, z, colour in LAYERS:
            geoms = flat[kind]
            if not geoms:
                continue
            if colours[kind]:  # per-feature colours: keep features separate
                for g, c in zip(geoms, colours[kind]):
                    layers.append({"kind": kind, "z": z, "color": c, "polygons": _polys(g, self.clip)})
            else:
                layers.append({"kind": kind, "z": z, "color": colour,
                               "polygons": _polys(unary_union(geoms), self.clip)})
        for g in bridges:  # thin deck above the water; roads on top are drawn flat at ground level
            layers.append({"kind": "bridge", "z": 5.0, "height": 1.0, "color": "#a19a8e",
                           "polygons": _polys(g, self.clip)})

        solids = []
        part_union = unary_union([g for _, _, g in parts]) if parts else None
        for oid, t, g in buildings:
            if t.get("building") in ("roof", "no") or t.get("location") == "underground":
                continue
            if part_union is not None and g.intersection(part_union).area > 0.5 * g.area:
                continue  # OSM 3D convention: parts replace the outline
            solids.append(_solid(oid, t, g, self.clip))
        for oid, t, g in parts:
            if t.get("building:part") in ("no",):
                continue
            solids.append(_solid(oid, t, g, self.clip))
        solids = [s for s in solids if s and s["polygons"] and s["height"] > s["min_height"]]
        return {"frame": "plan ENU metres (x east, y north)", "ground": GROUND,
                "layers": layers, "buildings": solids}


def _polys(g, clip) -> list:
    g = g.intersection(clip)
    out = []
    for p in getattr(g, "geoms", [g]):
        if isinstance(p, Polygon) and not p.is_empty and p.area > 0.5:
            p = shapely.set_precision(p, 0.01)
            if isinstance(p, Polygon) and not p.is_empty:
                out.append([list(map(list, p.exterior.coords))] + [list(map(list, r.coords)) for r in p.interiors])
        elif isinstance(p, MultiPolygon):
            out += _polys(p, clip)
    return out


def _height(t: dict, area_m2: float) -> tuple[float, float]:
    def num(v):
        try:
            return float(str(v).split()[0].replace(",", "."))
        except (ValueError, IndexError):
            return None

    h = num(t.get("height"))
    if h is None and (lv := num(t.get("building:levels"))) is not None:
        h = lv * LEVEL_M + (num(t.get("roof:levels")) or 0) * LEVEL_M * 0.6 + 1.0
    if h is None:
        kind = t.get("building", "yes")
        h = {"garage": 3, "garages": 3, "shed": 2.5, "hut": 2.5, "kiosk": 3, "church": 25}.get(
            kind, 9.0 if area_m2 < 150 else 16.0)
    mh = num(t.get("min_height"))
    if mh is None and (ml := num(t.get("building:min_level"))) is not None:
        mh = ml * LEVEL_M
    return h, mh or 0.0


def _solid(oid: str, t: dict, g, clip) -> dict | None:
    polys = _polys(g, clip)
    if not polys:
        return None
    h, mh = _height(t, g.area)
    kind = t.get("building", t.get("building:part", "yes"))
    wall = _osm_colour(t.get("building:colour"))
    if wall is None:
        palette = WALLS_MODERN if kind in MODERN_TYPES else WALLS
        wall = palette[int(_rng(oid, "wall") * len(palette)) % len(palette)]
    roof = _osm_colour(t.get("roof:colour")) or _pick_weighted(ROOFS, _rng(oid, "roof"))
    return {"id": oid, "height": round(h, 2), "min_height": round(mh, 2), "wall": wall, "roof": roof,
            "polygons": polys}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["fetch", "build"])
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--margin-m", type=float, default=200.0,
                    help="extra ground around the tiles (tall buildings south of a tile show in it)")
    ap.add_argument("--refresh", action="store_true", help="fetch even if a cached response exists")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    raw = args.out_dir / f"{plan['area']}.overpass.json"
    if args.cmd == "fetch":
        if raw.exists() and not args.refresh:
            print(f"{raw} exists (use --refresh to re-download)")
            return
        args.out_dir.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(fetch(overpass_query(plan_bbox_lonlat(plan, args.margin_m))))
        print(f"-> {raw}")
        return

    osm = json.loads(raw.read_text())
    scene = Builder(plan, osm, args.margin_m).build()
    scene["osm_timestamp"] = osm.get("osm3s", {}).get("timestamp_osm_base")
    scene["attribution"] = "© OpenStreetMap contributors"
    out = args.out_dir / f"{plan['area']}.scene.json"
    out.write_text(json.dumps(scene, separators=(",", ":")))
    n_poly = sum(len(l["polygons"]) for l in scene["layers"])
    print(f"{len(scene['buildings'])} buildings, {len(scene['layers'])} ground layers ({n_poly} polygons) "
          f"-> {out} ({out.stat().st_size / 2**20:.1f} MB)")


if __name__ == "__main__":
    main()
