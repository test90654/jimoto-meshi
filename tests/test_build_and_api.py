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
    # 県内のタグなし店（「さわやか 長田店」、英語名の「Sawayaka」、空白なしの「さわやか静岡インター店」）は合流。
    # 遠い愛知県の短い同名店は合流しない
    assert sawa["stores"] == 6 and sawa["prefs"] == 1
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


def test_public_mode_does_not_store_reports(db):
    c = TestClient(create_app(str(db), "osm", public=True))
    assert c.get("/api/health").json()["reports"] == "browser"
    assert c.post("/api/report", json={"key": "鮨まつ乃", "category": "chain"}).status_code == 403
    assert not db.with_name("reports.json").exists() or "鮨まつ乃" not in db.with_name("reports.json").read_text()


def test_server_runs_without_build_packages():
    """Render ではビルド用の osmium / shapely を入れないので、サーバーはそれなしで読み込めること。"""
    import subprocess, sys
    code = (
        "import builtins; real = builtins.__import__\n"
        "def guard(n, *a, **k):\n"
        "    if n.split('.')[0] in ('osmium', 'shapely'): raise ImportError(n)\n"
        "    return real(n, *a, **k)\n"
        "builtins.__import__ = guard\n"
        "import jimoto.server\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).parent.parent)


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


def _hotpepper_mock(seen, shops, error=None):
    def handler(req: httpx.Request):
        seen.append(dict(req.url.params))
        if error:
            return httpx.Response(200, json={"results": {"api_version": "1.30", "error": [{"code": 2000, "message": error}]}})
        start = int(req.url.params.get("start", 1))
        page = shops[start - 1:start - 1 + 100]
        return httpx.Response(200, json={"results": {
            "api_version": "1.30", "results_available": len(shops), "results_returned": str(len(page)),
            "results_start": start, "shop": page}})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


HP_SHOPS = [
    # OSM にもある店（同じ店としてまとめる）
    {"id": "J001", "name": "鮨 まつ乃", "lat": 34.97305, "lng": 138.38505, "genre": {"name": "和食"},
     "address": "静岡県静岡市葵区", "urls": {"pc": "https://www.hotpepper.jp/strJ001/"}, "station_name": "静岡"},
    # OSM にない個人店
    {"id": "J002", "name": "酒場 しずまえ", "lat": "34.9722", "lng": "138.3881", "genre": {"name": "居酒屋"},
     "urls": {"pc": "https://www.hotpepper.jp/strJ002/"}, "budget": {"name": "3001～4000円"}},
    # OSM にないチェーンの支店（ブランド辞書で広域チェーンと判定される）
    {"id": "J003", "name": "スターバックスコーヒー 静岡駅前店", "lat": 34.9712, "lng": 138.3889,
     "genre": {"name": "カフェ・スイーツ"}, "urls": {"pc": "https://www.hotpepper.jp/strJ003/"}},
    # 半径の外
    {"id": "J004", "name": "遠い店", "lat": 35.2, "lng": 138.9, "genre": {"name": "洋食"}},
]


def test_hotpepper_is_merged_with_osm(db):
    seen = []
    c = TestClient(create_app(str(db), "osm", hotpepper_key="hp-key", hotpepper_client=_hotpepper_mock(seen, HP_SHOPS)))
    d = c.get("/api/search", params={"station": "静岡", **RULES}).json()
    assert seen[0]["key"] == "hp-key" and seen[0]["range"] == "3" and seen[0]["count"] == "100"
    by_name = {r["name"]: r for r in d["results"]}
    # 同じ店は1件にまとまり、ホットペッパーのページと最寄り駅が付く
    assert [r["name"] for r in d["results"]].count("鮨 まつ乃") == 1
    assert by_name["鮨 まつ乃"]["url"].startswith("https://www.hotpepper.jp/")
    assert by_name["鮨 まつ乃"]["source"] == "osm+hotpepper"
    assert by_name["酒場 しずまえ"]["category"] == "indie" and by_name["酒場 しずまえ"]["source"] == "hotpepper"
    assert by_name["スターバックスコーヒー 静岡駅前店"]["category"] == "chain"
    assert "遠い店" not in by_name
    assert {"name": "hotpepper", "text": "Powered by ホットペッパーグルメ Webサービス",
            "url": "https://webservice.recruit.co.jp/"} in d["credits"]


def test_hotpepper_keyword_and_paging(db):
    seen = []
    many = [{"id": f"K{i}", "name": f"店{i}", "lat": 34.9717, "lng": 138.389} for i in range(150)]
    c = TestClient(create_app(str(db), "osm", hotpepper_key="k", hotpepper_client=_hotpepper_mock(seen, many)))
    c.get("/api/search", params={"station": "静岡", "q": "うなぎ"})
    assert [p["start"] for p in seen] == ["1", "101"] and seen[0]["keyword"] == "うなぎ"


def test_hotpepper_failure_still_returns_osm(db):
    seen = []
    c = TestClient(create_app(str(db), "osm", hotpepper_key="bad",
                              hotpepper_client=_hotpepper_mock(seen, [], error="APIキーが不正です")))
    d = c.get("/api/search", params={"station": "静岡", **RULES}).json()
    assert d["results"] and d["warnings"] == ["hotpepper から取得できませんでした"]
