"""OSM データからブランド辞書・飲食店・駅のデータベースを作る。

使い方:
    python -m jimoto.build data/japan-latest.osm.pbf \
        --prefectures data/japan.geojson --out data/jimoto.db

入力の .osm.pbf は Geofabrik などから取得する（README 参照）。
都道府県の境界（GeoJSON）を渡すと、ブランドごとの展開都道府県数を数える。
渡さない場合は、店舗の分布範囲（km）で広がりを判定する。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import osmium

from .genres import FOOD_AMENITIES, genre_label
from .normalize import drop_branch, nfkc, squash, strip_branch

SCHEMA = """
CREATE TABLE pois (
  id INTEGER PRIMARY KEY, osm_id TEXT, name TEXT, lat REAL, lng REAL,
  genre TEXT, cuisine TEXT, amenity TEXT, brand_key TEXT, pref TEXT
);
CREATE INDEX pois_lat ON pois(lat);
CREATE INDEX pois_lng ON pois(lng);
CREATE TABLE brands (
  key TEXT PRIMARY KEY, name TEXT, stores INTEGER, prefs INTEGER, spread_km REAL,
  source TEXT, wikidata TEXT, aliases TEXT
);
CREATE TABLE stations (
  id INTEGER PRIMARY KEY, name TEXT, norm TEXT, lat REAL, lng REAL, operators TEXT, pref TEXT
);
CREATE INDEX stations_norm ON stations(norm);
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
"""


# ---------------------------------------------------------------- 読み込み

def _is_station(tags: dict) -> bool:
    if tags.get("railway") == "station":
        return True
    return tags.get("public_transport") == "station" and tags.get("train") == "yes"


def read_osm(path: str) -> tuple[list[dict], list[dict]]:
    """飲食店と駅を取り出す。way は構成ノードの平均を位置とする。

    全国版（数億ノード）でもメモリに収まるよう、全ノードの位置表は作らずに2回読む。
    1回目：タグ付きのノードと way を読み、必要な way の構成ノード ID を集める
    2回目：その ID のノードだけ位置を読む
    """
    found: list[tuple[str, dict, object]] = []  # (oid, tags, 位置 or 構成ノードID列)
    need: set[int] = set()
    fp = osmium.FileProcessor(path, osmium.osm.NODE | osmium.osm.WAY).with_filter(
        osmium.filter.KeyFilter("amenity", "railway", "public_transport"))
    for obj in fp:
        tags = {t.k: t.v for t in obj.tags}
        if not tags.get("name"):
            continue
        if not (tags.get("amenity") in FOOD_AMENITIES or _is_station(tags)):
            continue
        if obj.is_node():
            if obj.location.valid():
                found.append((f"n{obj.id}", tags, (obj.location.lat, obj.location.lon)))
        else:
            refs = [n.ref for n in obj.nodes]
            need.update(refs)
            found.append((f"w{obj.id}", tags, refs))

    locs: dict[int, tuple[float, float]] = {}
    if need:
        fp = osmium.FileProcessor(path, osmium.osm.NODE).with_filter(osmium.filter.IdFilter(need))
        for n in fp:
            if n.location.valid():
                locs[n.id] = (n.location.lat, n.location.lon)

    pois: list[dict] = []
    stations: list[dict] = []
    for oid, tags, where in found:
        if oid[0] == "n":
            lat, lng = where
        else:
            pts = [locs[r] for r in dict.fromkeys(where) if r in locs]  # 閉じた way の重複点を除く
            if not pts:
                continue
            lat = sum(p[0] for p in pts) / len(pts)
            lng = sum(p[1] for p in pts) / len(pts)
        name = tags["name"]
        food = tags.get("amenity") in FOOD_AMENITIES
        if food:
            pois.append({
                "osm_id": oid, "name": nfkc(name), "lat": lat, "lng": lng,
                "amenity": tags.get("amenity"), "cuisine": tags.get("cuisine") or "",
                "brand": tags.get("brand:ja") or tags.get("brand"),
                "brand_en": tags.get("brand"), "wikidata": tags.get("brand:wikidata"),
            })
        else:
            stations.append({"name": nfkc(name), "lat": lat, "lng": lng, "operator": tags.get("operator") or ""})
    return pois, stations


# ---------------------------------------------------------------- 都道府県

class PrefectureLocator:
    """GeoJSON の都道府県ポリゴンで、座標がどの都道府県か調べる。"""

    NAME_KEYS = ("nam_ja", "N03_001", "name_ja", "name", "nam")

    def __init__(self, geojson_path: str):
        from shapely import STRtree
        from shapely.geometry import shape

        data = json.loads(Path(geojson_path).read_text(encoding="utf-8"))
        self.names: list[str] = []
        geoms = []
        for f in data["features"]:
            props = f.get("properties") or {}
            name = next((props[k] for k in self.NAME_KEYS if props.get(k)), None)
            if name and f.get("geometry"):
                self.names.append(name)
                geoms.append(shape(f["geometry"]))
        self.tree = STRtree(geoms)

    def locate_many(self, points: list[tuple[float, float]]) -> list[str | None]:
        import shapely

        if not points:
            return []
        pts = shapely.points([(lng, lat) for lat, lng in points])
        out: list[str | None] = [None] * len(points)
        pi, gi = self.tree.query(pts, predicate="intersects")
        for p, g in zip(pi, gi):
            if out[p] is None:
                out[p] = self.names[g]
        return out


# ---------------------------------------------------------------- 集計

def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def group_key(p: dict, wikidata_by_brand: dict[str, str]) -> tuple[str, str]:
    """店をブランドに束ねるキーと、その根拠を返す。"""
    if p["wikidata"]:
        return "wd:" + p["wikidata"], "wikidata"
    if p["brand"]:
        b = squash(p["brand"])
        if b in wikidata_by_brand:
            return "wd:" + wikidata_by_brand[b], "wikidata"
        return "b:" + b, "brand"
    return "n:" + strip_branch(p["name"]), "name"


def build_brands(pois: list[dict], has_prefs: bool, min_stores: int = 2, min_name_group: int = 3) -> list[dict]:
    """ブランドごとに店舗数・都道府県数・分布範囲をまとめる。各 poi に brand_key を書き込む。"""
    wikidata_by_brand: dict[str, str] = {}
    for p in pois:
        if p["wikidata"] and p["brand"]:
            wikidata_by_brand.setdefault(squash(p["brand"]), p["wikidata"])

    groups: dict[str, list[dict]] = defaultdict(list)
    sources: dict[str, Counter] = defaultdict(Counter)
    for p in pois:
        key, src = group_key(p, wikidata_by_brand)
        groups[key].append(p)
        sources[key][src] += 1

    brands: list[dict] = []
    for key, members in groups.items():
        source = sources[key].most_common(1)[0][0]
        need = min_name_group if source == "name" else min_stores
        if len(members) < need or key in ("n:", "b:"):
            for p in members:
                p["brand_key"] = None
            continue
        for p in members:
            p["brand_key"] = key
        lats = [p["lat"] for p in members]
        lngs = [p["lng"] for p in members]
        spread = haversine_km(min(lats), min(lngs), max(lats), max(lngs))
        prefs = len({p["pref"] for p in members if p.get("pref")}) if has_prefs else None
        brand_names = Counter(p["brand"] for p in members if p["brand"])
        stripped = Counter(strip_branch(p["name"]) for p in members)
        if brand_names:
            name = brand_names.most_common(1)[0][0]
        else:
            name = Counter(drop_branch(p["name"]) for p in members).most_common(1)[0][0]
        aliases = {squash(n) for n in brand_names}
        aliases |= {squash(p["brand_en"]) for p in members if p.get("brand_en")}
        # 2店舗以上で使われている「支店名を除いた店名」も別名にする
        aliases |= {k for k, c in stripped.items() if c >= 2 and len(k) >= 2}
        brands.append({
            "key": key, "name": name, "stores": len(members), "prefs": prefs,
            "spread_km": round(spread, 1), "source": source,
            "wikidata": key[3:] if key.startswith("wd:") else None,
            "aliases": sorted(a for a in aliases if a),
        })
    brands.sort(key=lambda b: -b["stores"])
    return brands


def dedupe_stations(stations: list[dict], within_km: float = 0.6) -> list[dict]:
    """同じ駅名で近くにあるもの（事業者ごとのノードなど）をひとつにまとめる。"""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for s in stations:
        norm = squash(s["name"]).removesuffix("駅")
        for kept in by_name[norm]:
            if haversine_km(kept["lat"], kept["lng"], s["lat"], s["lng"]) <= within_km:
                if s["operator"] and s["operator"] not in kept["operators"]:
                    kept["operators"].append(s["operator"])
                break
        else:
            by_name[norm].append({**s, "norm": norm, "operators": [s["operator"]] if s["operator"] else []})
    return [s for group in by_name.values() for s in group]


# ---------------------------------------------------------------- 書き出し

def write_db(out: str, pois: list[dict], brands: list[dict], stations: list[dict], meta: dict) -> None:
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO pois (osm_id,name,lat,lng,genre,cuisine,amenity,brand_key,pref) VALUES (?,?,?,?,?,?,?,?,?)",
        [(p["osm_id"], p["name"], p["lat"], p["lng"], genre_label(p["amenity"], p["cuisine"]),
          p["cuisine"], p["amenity"], p.get("brand_key"), p.get("pref")) for p in pois],
    )
    conn.executemany(
        "INSERT INTO brands VALUES (?,?,?,?,?,?,?,?)",
        [(b["key"], b["name"], b["stores"], b["prefs"], b["spread_km"], b["source"], b["wikidata"],
          json.dumps(b["aliases"], ensure_ascii=False)) for b in brands],
    )
    conn.executemany(
        "INSERT INTO stations (name,norm,lat,lng,operators,pref) VALUES (?,?,?,?,?,?)",
        [(s["name"], s["norm"], s["lat"], s["lng"], "・".join(s["operators"]), s.get("pref")) for s in stations],
    )
    conn.executemany("INSERT INTO meta VALUES (?,?)", [(k, str(v)) for k, v in meta.items()])
    conn.commit()
    conn.close()
    tmp.replace(path)


def build(osm_path: str, out: str, prefectures: str | None = None) -> dict:
    t0 = time.time()
    pois, stations = read_osm(osm_path)
    if prefectures:
        loc = PrefectureLocator(prefectures)
        for p, pref in zip(pois, loc.locate_many([(p["lat"], p["lng"]) for p in pois])):
            p["pref"] = pref
        for s, pref in zip(stations, loc.locate_many([(s["lat"], s["lng"]) for s in stations])):
            s["pref"] = pref
    brands = build_brands(pois, has_prefs=bool(prefectures))
    stations = dedupe_stations(stations)
    meta = {"source": Path(osm_path).name, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "prefectures": bool(prefectures), "pois": len(pois), "brands": len(brands), "stations": len(stations)}
    write_db(out, pois, brands, stations, meta)
    Path(out).with_name("brands.json").write_text(
        json.dumps(brands, ensure_ascii=False, indent=1), encoding="utf-8")
    meta["seconds"] = round(time.time() - t0, 1)
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OSM データからブランド辞書と検索用データベースを作る")
    ap.add_argument("osm", help=".osm.pbf または .osm ファイル")
    ap.add_argument("--out", default="data/jimoto.db", help="出力する SQLite ファイル")
    ap.add_argument("--prefectures", help="都道府県境界の GeoJSON（展開都道府県数を数える）")
    args = ap.parse_args(argv)
    meta = build(args.osm, args.out, args.prefectures)
    print(f"飲食店 {meta['pois']:,} 件 / ブランド {meta['brands']:,} 件 / 駅 {meta['stations']:,} 件"
          f"（{meta['seconds']} 秒）→ {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
