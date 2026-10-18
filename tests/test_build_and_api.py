import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jimoto.build import build
from jimoto.server import create_app

FIX = Path(__file__).parent / "fixtures"
# テストデータは2県しかないので、2県以上・3店以上を広域チェーンとみなす
RULES = {"min_stores": 3, "min_prefs": 2}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    out = tmp_path_factory.mktemp("data") / "jimoto.db"
    build(str(FIX / "sample.osm"), str(out), str(FIX / "prefectures.geojson"))
    return out


def test_build_brands(db):
    brands = {b["name"]: b for b in json.loads(db.with_name("brands.json").read_text())}
    sb = brands["スターバックス"]
    assert sb["stores"] == 4 and sb["prefs"] == 2  # wikidata のない1店も束ねられている
    sawa = brands["炭焼きレストランさわやか"]
    assert sawa["stores"] == 4 and sawa["prefs"] == 1  # 県内のタグなし1店は合流、愛知県の同名店は合流しない
    assert brands["居酒屋 花"]["source"] == "name"
    assert "鮨 まつ乃" not in brands


def test_station_search(db):
    c = TestClient(create_app(str(db), "osm"))
    d = c.get("/api/search", params={"station": "静岡駅", **RULES}).json()
    assert d["center"]["label"] == "静岡駅"
    cats = {r["name"]: r["category"] for r in d["results"]}
    assert cats["スターバックス コーヒー 静岡パルコ店"] == "chain"
    assert cats["炭焼きレストランさわやか 静岡両替町店"] == "local"
    assert cats["鮨 まつ乃"] == "indie"
    assert cats["洋食 ことり亭"] == "indie"  # way の店
    assert all(r["distance_m"] <= 1000 for r in d["results"])


def test_hide_excluded_and_exclude_local(db):
    c = TestClient(create_app(str(db), "osm"))
    d = c.get("/api/search", params={"station": "静岡", "hide_excluded": True, "exclude_local": True, **RULES}).json()
    assert {r["category"] for r in d["results"]} == {"indie"}


def test_current_location_and_keyword(db):
    c = TestClient(create_app(str(db), "osm"))
    d = c.get("/api/search", params={"lat": 34.7038, "lng": 137.7350, "q": "うなぎ"}).json()
    assert [r["name"] for r in d["results"]] == ["うなぎ 川瀬"]
    d = c.get("/api/search", params={"q": "寿司"}).json()  # 場所なしのキーワード
    assert [r["name"] for r in d["results"]] == ["鮨 まつ乃"]


def test_same_name_stations(db):
    c = TestClient(create_app(str(db), "osm"))
    d = c.get("/api/search", params={"station": "本町"}).json()
    assert d["needs_choice"] and len(d["stations"]) == 2
    sid = d["stations"][0]["id"]
    assert c.get("/api/search", params={"station_id": sid}).json()["center"]["label"] == "本町駅"


def test_report_overrides_category(db):
    c = TestClient(create_app(str(db), "osm"))
    params = {"station": "静岡", **RULES}
    r = next(x for x in c.get("/api/search", params=params).json()["results"] if x["name"] == "鮨 まつ乃")
    assert r["category"] == "indie" and r["override"] is None
    assert c.post("/api/report", json={"key": r["override_key"], "category": "chain"}).json()["ok"]
    r = next(x for x in c.get("/api/search", params=params).json()["results"] if x["name"] == "鮨 まつ乃")
    assert r["category"] == "chain" and r["excluded"] and r["override"] == "chain"
    # 新しく起動しても残る
    c2 = TestClient(create_app(str(db), "osm"))
    r = next(x for x in c2.get("/api/search", params=params).json()["results"] if x["name"] == "鮨 まつ乃")
    assert r["category"] == "chain"
    c2.post("/api/report", json={"key": r["override_key"], "category": None})
    r = next(x for x in c2.get("/api/search", params=params).json()["results"] if x["name"] == "鮨 まつ乃")
    assert r["category"] == "indie"
    assert c2.post("/api/report", json={"key": "x", "category": "foo"}).status_code == 400


def test_errors(db):
    c = TestClient(create_app(str(db), "osm"))
    assert c.get("/api/search").status_code == 400
    assert c.get("/api/search", params={"station": "存在しない駅"}).status_code == 404


def test_google_provider(db, monkeypatch):
    """Google の応答を模擬し、ローカルのブランド辞書で判定されることを確かめる。"""
    seen = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        seen.append(body)
        assert req.headers["X-Goog-Api-Key"] == "test-key"
        return httpx.Response(200, json={"places": [
            {"id": "a", "displayName": {"text": "スターバックスコーヒー 静岡駅南口店"},
             "location": {"latitude": 34.9705, "longitude": 138.3895}, "rating": 4.0,
             "primaryTypeDisplayName": {"text": "カフェ"}, "googleMapsUri": "https://maps.google.com/?cid=1"},
            {"id": "b", "displayName": {"text": "炭焼きレストランさわやか 新静岡セノバ店"},
             "location": {"latitude": 34.9760, "longitude": 138.3860}},
            {"id": "c", "displayName": {"text": "おでん屋 たけ"},
             "location": {"latitude": 34.9720, "longitude": 138.3880}},
            {"id": "d", "displayName": {"text": "遠すぎる店"},
             "location": {"latitude": 35.5, "longitude": 139.0}},
        ]})

    monkeypatch.setenv("GOOGLE_MAPS_API_KEY", "test-key")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c = TestClient(create_app(str(db), "google", google_client=client))
    d = c.get("/api/search", params={"station": "静岡", **RULES}).json()
    assert d["provider"] == "google"
    assert seen[0]["textQuery"] == "飲食店" and "locationBias" in seen[0]
    cats = {r["name"]: r["category"] for r in d["results"]}
    assert cats == {"スターバックスコーヒー 静岡駅南口店": "chain",
                    "炭焼きレストランさわやか 新静岡セノバ店": "local", "おでん屋 たけ": "indie"}
