"""飲食店のデータ源。

- OSMLocalProvider：build で作った SQLite を検索する。APIキー不要で、開発や試験に使える。
- GooglePlacesProvider：Google Places API (New) の Text Search を呼ぶ。情報の鮮度と網羅性が高い。

どちらも同じ Place の一覧を返し、チェーン判定はサーバー側でまとめて行う。
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

import httpx

from .build import haversine_km
from .normalize import nfkc


@dataclass
class Place:
    id: str
    name: str
    lat: float
    lng: float
    genre: str = ""
    address: str = ""
    rating: float | None = None
    url: str = ""
    brand_key: str | None = None  # データ源がブランドを知っている場合（OSM）


def osm_url(osm_id: str) -> str:
    kind = {"n": "node", "w": "way"}.get(osm_id[:1], "node")
    return f"https://www.openstreetmap.org/{kind}/{osm_id[1:]}"


class OSMLocalProvider:
    name = "osm"
    attribution = "© OpenStreetMap contributors"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    async def search(self, q: str | None, lat: float | None, lng: float | None,
                     radius_m: int = 1000, limit: int = 100) -> list[Place]:
        where, args = [], []
        if q:
            kw = f"%{nfkc(q)}%"
            where.append("(name LIKE ? OR genre LIKE ? OR cuisine LIKE ?)")
            args += [kw, kw, kw]
        if lat is not None and lng is not None:
            dlat = radius_m / 111_000
            dlng = radius_m / (111_000 * max(math.cos(math.radians(lat)), 0.01))
            where.append("lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?")
            args += [lat - dlat, lat + dlat, lng - dlng, lng + dlng]
        sql = "SELECT osm_id, name, lat, lng, genre, brand_key FROM pois"
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self.conn.execute(sql + " LIMIT 5000", args).fetchall()
        places = [
            Place(id=r[0], name=r[1], lat=r[2], lng=r[3], genre=r[4], brand_key=r[5],
                  url=osm_url(r[0]))
            for r in rows
        ]
        if lat is not None and lng is not None:
            places = [p for p in places if haversine_km(lat, lng, p.lat, p.lng) * 1000 <= radius_m]
            places.sort(key=lambda p: haversine_km(lat, lng, p.lat, p.lng))
        return places[:limit]

    async def geocode(self, text: str) -> tuple[float, float, str] | None:
        return None


class GooglePlacesProvider:
    """Google Places API (New) の Text Search。

    利用前に Google Maps Platform の利用規約（データの保存期間、表示方法、帰属表示）を確認すること。
    このクラスは結果を保存せず、受け取ったその場で判定して返す。
    """

    name = "google"
    attribution = "Google"
    URL = "https://places.googleapis.com/v1/places:searchText"
    FIELDS = ",".join([
        "places.id", "places.displayName", "places.location", "places.primaryTypeDisplayName",
        "places.shortFormattedAddress", "places.rating", "places.googleMapsUri", "nextPageToken",
    ])

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None, max_pages: int = 3):
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=10)
        self.max_pages = max_pages

    async def _query(self, body: dict) -> list[dict]:
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": self.FIELDS}
        out: list[dict] = []
        for _ in range(self.max_pages):
            r = await self.client.post(self.URL, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            out += data.get("places", [])
            token = data.get("nextPageToken")
            if not token:
                break
            body = {**body, "pageToken": token}
        return out

    async def search(self, q: str | None, lat: float | None, lng: float | None,
                     radius_m: int = 1000, limit: int = 60) -> list[Place]:
        body: dict = {"textQuery": nfkc(q) if q else "飲食店", "languageCode": "ja",
                      "regionCode": "JP", "pageSize": 20}
        if lat is not None and lng is not None:
            body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng},
                                               "radius": float(min(radius_m, 50_000))}}
        places = []
        for p in await self._query(body):
            loc = p.get("location") or {}
            places.append(Place(
                id=p.get("id", ""), name=(p.get("displayName") or {}).get("text", ""),
                lat=loc.get("latitude"), lng=loc.get("longitude"),
                genre=(p.get("primaryTypeDisplayName") or {}).get("text", ""),
                address=p.get("shortFormattedAddress", ""), rating=p.get("rating"),
                url=p.get("googleMapsUri", ""),
            ))
        places = [p for p in places if p.lat is not None]
        if lat is not None and lng is not None:
            # locationBias は範囲外も返すので、半径の1.5倍より遠いものは外す
            places = [p for p in places if haversine_km(lat, lng, p.lat, p.lng) * 1000 <= radius_m * 1.5]
            places.sort(key=lambda p: haversine_km(lat, lng, p.lat, p.lng))
        return places[:limit]

    async def geocode(self, text: str) -> tuple[float, float, str] | None:
        """駅名などを座標にする（ローカルの駅データに見つからないときの予備）。"""
        body = {"textQuery": nfkc(text), "languageCode": "ja", "regionCode": "JP", "pageSize": 1}
        r = await self.client.post(self.URL, json=body, headers={
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": "places.displayName,places.location",
        })
        r.raise_for_status()
        places = r.json().get("places", [])
        if not places:
            return None
        loc = places[0]["location"]
        return loc["latitude"], loc["longitude"], places[0]["displayName"]["text"]
