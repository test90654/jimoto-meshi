"""検索 API サーバー。

起動:
    uvicorn jimoto.server:app --reload

環境変数:
    JIMOTO_DB            build で作った SQLite（既定 data/jimoto.db）
    JIMOTO_PROVIDER      osm（既定）または google
    GOOGLE_MAPS_API_KEY  google を使う場合の API キー
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from .build import haversine_km
from .classify import BrandIndex, Rules, classify, is_excluded
from .normalize import squash
from .providers import GooglePlacesProvider, OSMLocalProvider

WEB = Path(__file__).resolve().parent.parent / "web"


def open_db(path: str) -> sqlite3.Connection | None:
    if not Path(path).exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    return conn


def create_app(db_path: str | None = None, provider: str | None = None,
               google_client=None) -> FastAPI:
    db_path = db_path or os.environ.get("JIMOTO_DB", "data/jimoto.db")
    provider = provider or os.environ.get("JIMOTO_PROVIDER", "osm")
    conn = open_db(db_path)
    index = BrandIndex.from_db(conn) if conn else BrandIndex([])

    if provider == "google":
        key = os.environ.get("GOOGLE_MAPS_API_KEY", "")
        if not key and google_client is None:
            raise RuntimeError("JIMOTO_PROVIDER=google には GOOGLE_MAPS_API_KEY が必要です")
        source = GooglePlacesProvider(key, client=google_client)
    else:
        if conn is None:
            raise RuntimeError(f"{db_path} がありません。先に python -m jimoto.build を実行してください")
        source = OSMLocalProvider(conn)

    app = FastAPI(title="ジモト飯 API")

    def find_stations(q: str, limit: int = 10) -> list[dict]:
        if conn is None:
            return []
        norm = squash(q).removesuffix("駅")
        if not norm:
            return []
        rows = conn.execute(
            "SELECT id, name, lat, lng, operators, pref, (norm = ?) AS exact FROM stations "
            "WHERE norm = ? OR norm LIKE ? ORDER BY exact DESC, length(norm) LIMIT ?",
            (norm, norm, norm + "%", limit),
        ).fetchall()
        return [{"id": r[0], "name": r[1], "lat": r[2], "lng": r[3], "operators": r[4],
                 "pref": r[5], "exact": bool(r[6])} for r in rows]

    @app.get("/api/health")
    def health():
        meta = dict(conn.execute("SELECT k, v FROM meta").fetchall()) if conn else {}
        return {"ok": True, "provider": source.name, "brands": len(index), "data": meta}

    @app.get("/api/stations")
    def stations(q: str = Query(..., min_length=1)):
        return {"stations": find_stations(q)}

    @app.get("/api/search")
    async def search(
        q: str | None = Query(None, description="キーワード（料理名・店名）"),
        lat: float | None = None, lng: float | None = None,
        station: str | None = Query(None, description="駅名"),
        station_id: int | None = None,
        radius: int = Query(1000, ge=100, le=20000, description="検索半径（m）"),
        min_stores: int = 20, min_prefs: int = 4, min_spread_km: float = 150,
        exclude_local: bool = False,
        hide_excluded: bool = Query(False, description="除外した店を結果から取り除く"),
    ):
        rules = Rules(min_stores=min_stores, min_prefs=min_prefs,
                      min_spread_km=min_spread_km, exclude_local=exclude_local)
        center = None
        if lat is not None and lng is not None:
            center = {"lat": lat, "lng": lng, "label": "現在地"}
        elif station_id is not None and conn is not None:
            r = conn.execute("SELECT name, lat, lng FROM stations WHERE id = ?", (station_id,)).fetchone()
            if not r:
                raise HTTPException(404, "駅が見つかりません")
            center = {"lat": r[1], "lng": r[2], "label": r[0] + "駅"}
        elif station:
            found = find_stations(station, limit=5)
            exact = [s for s in found if s["exact"]]
            if len(exact) > 1:
                # 同名の駅が複数ある：どれか選んでもらう
                return {"needs_choice": True, "stations": exact}
            if found:
                s = (exact or found)[0]
                center = {"lat": s["lat"], "lng": s["lng"], "label": s["name"] + "駅"}
            else:
                g = await source.geocode(station if station.endswith("駅") else station + "駅")
                if not g:
                    raise HTTPException(404, f"「{station}」という駅が見つかりません")
                center = {"lat": g[0], "lng": g[1], "label": g[2]}
        if center is None and not q:
            raise HTTPException(400, "現在地・駅名・キーワードのどれかを指定してください")

        places = await source.search(q, center and center["lat"], center and center["lng"], radius)
        results, counts = [], {"indie": 0, "local": 0, "chain": 0}
        for p in places:
            brand = index.get(p.brand_key) if p.brand_key else index.match(p.name)
            cat = classify(brand, rules)
            excluded = is_excluded(cat, rules)
            counts[cat] += 1
            if excluded and hide_excluded:
                continue
            item = asdict(p)
            item.pop("brand_key")
            item.update({
                "brand": brand.public() if brand else None, "category": cat, "excluded": excluded,
                "distance_m": round(haversine_km(center["lat"], center["lng"], p.lat, p.lng) * 1000)
                if center else None,
            })
            results.append(item)
        return {"center": center, "provider": source.name, "attribution": source.attribution,
                "rules": asdict(rules), "counts": counts, "results": results}

    @app.get("/")
    def index_page():
        return FileResponse(WEB / "index.html")

    return app


try:
    app = create_app()
except RuntimeError as e:  # データがないときも import は通す（テストや build のため）
    _err = str(e)
    app = FastAPI()

    @app.get("/{path:path}")
    def not_ready(path: str):
        raise HTTPException(503, _err)
