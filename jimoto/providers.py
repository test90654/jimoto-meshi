"""飲食店のデータ源。

- OSMLocalProvider：build で作った SQLite を検索する。APIキー不要で、開発や試験に使える。
- GooglePlacesProvider：Google Places API (New) の Text Search を呼ぶ。情報の鮮度と網羅性が高い。
- HotpepperProvider：ホットペッパーグルメ Webサービスのグルメサーチ API。無料（要 API キー）。
- CombinedProvider：複数のデータ源を同時に呼び、同じ店をまとめる（OSM + ホットペッパーなど）。

どれも同じ Place の一覧を返し、チェーン判定はサーバー側でまとめて行う。
"""
from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
from dataclasses import dataclass, field

import httpx

from .geo import haversine_km
from .normalize import nfkc, squash, strip_branch

log = logging.getLogger(__name__)


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
    source: str = ""  # どのデータ源から来たか（osm / hotpepper / google）
    extra: dict = field(default_factory=dict)  # データ源ごとの追加情報（最寄り駅、予算など）


def osm_url(osm_id: str) -> str:
    kind = {"n": "node", "w": "way"}.get(osm_id[:1], "node")
    return f"https://www.openstreetmap.org/{kind}/{osm_id[1:]}"


class OSMLocalProvider:
    name = "osm"
    attribution = "© OpenStreetMap contributors"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    async def search(self, q: str | None, lat: float | None, lng: float | None,
                     radius_m: int = 1000, limit: int = 300) -> list[Place]:
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
                  url=osm_url(r[0]), source="osm")
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


class HotpepperProvider:
    """ホットペッパーグルメ Webサービスのグルメサーチ API。

    API キーは https://webservice.recruit.co.jp/register/ で取得する。
    利用規約により、画面に「Powered by ホットペッパーグルメ Webサービス」の表示が必要。
    """

    name = "hotpepper"
    attribution = "Powered by ホットペッパーグルメ Webサービス"
    attribution_url = "https://webservice.recruit.co.jp/"
    URL = "https://webservice.recruit.co.jp/hotpepper/gourmet/v1/"
    # range パラメータ：1=300m 2=500m 3=1000m 4=2000m 5=3000m
    RANGES = [(300, 1), (500, 2), (1000, 3), (2000, 4), (3000, 5)]
    PAGE = 100  # count の最大

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None, max_pages: int = 3):
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=10)
        self.max_pages = max_pages

    @classmethod
    def range_code(cls, radius_m: int) -> int:
        return next((code for m, code in cls.RANGES if m >= radius_m), 5)

    async def search(self, q: str | None, lat: float | None, lng: float | None,
                     radius_m: int = 1000, limit: int = 300) -> list[Place]:
        params: dict = {"key": self.api_key, "format": "json", "count": self.PAGE}
        if q:
            params["keyword"] = nfkc(q)
        if lat is not None and lng is not None:
            params.update(lat=lat, lng=lng, range=self.range_code(radius_m))
        elif not q:
            return []
        shops: list[dict] = []
        for page in range(self.max_pages):
            params["start"] = page * self.PAGE + 1
            r = await self.client.get(self.URL, params=params)
            r.raise_for_status()
            res = (r.json() or {}).get("results") or {}
            if res.get("error"):
                msg = "; ".join(str(e.get("message", e)) for e in res["error"])
                raise RuntimeError(f"ホットペッパー API のエラー：{msg}")
            got = res.get("shop") or []
            shops += got
            if not got or len(shops) >= int(res.get("results_available") or 0) or len(shops) >= limit:
                break
        places = []
        for sh in shops:
            try:
                plat, plng = float(sh["lat"]), float(sh["lng"])
            except (KeyError, TypeError, ValueError):
                continue
            places.append(Place(
                id="hp:" + str(sh.get("id", "")), name=nfkc(sh.get("name", "")), lat=plat, lng=plng,
                genre=(sh.get("genre") or {}).get("name", ""), address=sh.get("address", ""),
                url=(sh.get("urls") or {}).get("pc", ""), source="hotpepper",
                extra={k: v for k, v in {
                    "station": sh.get("station_name"), "budget": (sh.get("budget") or {}).get("name"),
                    "catch": sh.get("catch"),
                }.items() if v},
            ))
        if lat is not None and lng is not None:
            places = [p for p in places if haversine_km(lat, lng, p.lat, p.lng) * 1000 <= radius_m]
            places.sort(key=lambda p: haversine_km(lat, lng, p.lat, p.lng))
        return places[:limit]

    async def geocode(self, text: str) -> tuple[float, float, str] | None:
        return None


def same_shop(a: Place, b: Place, within_m: float = 80) -> bool:
    """別のデータ源にある同じ店か。名前（支店名を除く）が一致または一方が他方を含み、近くにあれば同じとみなす。"""
    if haversine_km(a.lat, a.lng, b.lat, b.lng) * 1000 > within_m:
        return False
    ka, kb = strip_branch(a.name), strip_branch(b.name)
    fa, fb = squash(a.name), squash(b.name)
    if not ka or not kb:
        return False
    return ka == kb or (len(ka) >= 2 and ka in fb) or (len(kb) >= 2 and kb in fa)


class CombinedProvider:
    """複数のデータ源を同時に呼んでまとめる。一つが失敗しても、ほかの結果は返す。"""

    def __init__(self, providers: list):
        self.providers = providers
        self.name = "+".join(p.name for p in providers)
        self.attribution = " / ".join(p.attribution for p in providers)

    async def search(self, q, lat, lng, radius_m: int = 1000, limit: int = 400) -> list[Place]:
        return (await self.search_detailed(q, lat, lng, radius_m, limit))[0]

    async def search_detailed(self, q, lat, lng, radius_m: int = 1000,
                              limit: int = 400) -> tuple[list[Place], list[str]]:
        """（店の一覧, 取得に失敗したデータ源の説明）を返す。"""
        results = await asyncio.gather(
            *(p.search(q, lat, lng, radius_m) for p in self.providers), return_exceptions=True)
        warnings: list[str] = []
        merged: list[Place] = []
        for prov, res in zip(self.providers, results):
            if isinstance(res, Exception):
                log.warning("%s の検索に失敗: %s", prov.name, res)
                warnings.append(f"{prov.name} から取得できませんでした")
                continue
            for p in res:
                dup = next((m for m in merged if same_shop(m, p)), None)
                if dup is None:
                    merged.append(p)
                    continue
                # 同じ店：足りない情報を補う（ブランドは OSM、詳しいページはホットペッパーなど）
                dup.brand_key = dup.brand_key or p.brand_key
                dup.address = dup.address or p.address
                dup.genre = dup.genre or p.genre
                if p.source == "hotpepper" and p.url:
                    dup.url = p.url
                dup.extra = {**p.extra, **dup.extra}
                dup.source = dup.source + "+" + p.source if p.source not in dup.source else dup.source
        if lat is not None and lng is not None:
            merged.sort(key=lambda p: haversine_km(lat, lng, p.lat, p.lng))
        return merged[:limit], warnings

    async def geocode(self, text: str):
        for p in self.providers:
            g = await p.geocode(text)
            if g:
                return g
        return None
