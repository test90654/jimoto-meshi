# ジモト飯

旅行先などで飲食店を探すときに、**全国に広く展開するチェーンだけを除外**し、個人店と**地元チェーン**（県内中心で展開する店）を残して表示する検索アプリです。

現在地・駅名・キーワードの3通りで探せます。

## しくみ

```
OSM の全国データ ──(jimoto.build)──▶ data/jimoto.db
                                      ├ brands   ブランド辞書（店舗数・展開都道府県数・分布範囲）
                                      ├ pois     飲食店（OSM をデータ源にする場合）
                                      └ stations 駅（駅名検索に使う）

ブラウザ ──▶ jimoto.server ──▶ データ源（OSM ローカル / Google Places）
                │
                └ 取得した店をブランド辞書と照合して「個人店／地元チェーン／広域チェーン」に分類
```

### チェーン判定

ブランドごとに次の2つを見ます。

| 軸 | 内容 | 既定のしきい値 |
| --- | --- | --- |
| 店舗数 | 全国の店舗数 | 20店以上 |
| 広がり | 展開している都道府県の数 | 4都道府県以上 |

両方を満たすと **広域チェーン**（除外）、満たさなければ **地元チェーン**（残す）です。

ただし店舗数が **40店以上** のブランドは、首都圏などに集中していても広域チェーンとします（小諸そばなど）。都道府県の境界データを使わずにビルドした場合は、店舗の分布範囲（既定 150km 以上）で広がりを判定します。

### ブランドの名寄せ

店をブランドに束ねる根拠は、優先順に次のとおりです。

1. OSM の `brand:wikidata` タグ
2. OSM の `brand` タグ（同じ名前で wikidata を持つ店があれば、そちらに合流）
3. 支店名を除いた店名（「居酒屋 花 静岡店」→「居酒屋花」）。偶然の同名を避けるため、3店以上ある場合だけブランドとみなします

タグのない店も、店名がブランド名と一致すれば合流させます（「日高屋」とだけ登録された店を日高屋として数える）。偶然の同名の店を混ぜないよう、合流させるのは全国に店のあるブランド、5文字以上の名前、または100km以内に同じブランドの店がある場合だけです。カタカナとひらがなの違い（「8番ラーメン」と「8番らーめん」）は同じ名前とみなします。

Google Places など別のデータ源の店は、店名を正規化してブランド辞書の別名と照合します。「すき家静岡駅南口店」のように空白のない店名は、先頭がブランド名と一致し、残りが「〜店」だけの場合に一致とみなします。

## はじめかた

Python 3.10 以上が必要です。

```bash
pip install -r requirements-build.txt
```

### 1. データを用意する

```bash
mkdir -p data
# 都道府県の境界（展開都道府県数を数えるのに使う）
curl -L -o data/japan.geojson https://raw.githubusercontent.com/dataofjapan/land/master/japan.geojson
# OSM データ。全国版は約2GB。まず地方版で試すと早い
curl -L -o data/chubu-latest.osm.pbf https://download.geofabrik.de/asia/japan/chubu-latest.osm.pbf
```

全国版は `https://download.geofabrik.de/asia/japan-latest.osm.pbf`（約2GB）です。**ブランドの店舗数・都道府県数を正しく数えるには全国版が必要**です（地方版だと全国チェーンも「地元チェーン」に見えます）。ビルドはファイルを2回読み、必要な点の位置だけを取り出す方式なので、全国版でもメモリの使用量を抑えられます。

#### Windows（PowerShell）の場合

PowerShell の `curl` は別のコマンドなので `curl.exe` と書き、パスの区切りは `\` にします。フォルダ名に日本語が入らない場所（例：`C:\dev`）に置くと安全です。

```powershell
mkdir data
curl.exe -L -o data\japan.geojson https://raw.githubusercontent.com/dataofjapan/land/master/japan.geojson
curl.exe -L -o data\japan-latest.osm.pbf https://download.geofabrik.de/asia/japan-latest.osm.pbf
python -m jimoto.build data\japan-latest.osm.pbf --prefectures data\japan.geojson --out data\jimoto.db
python -m uvicorn jimoto.server:app
```

### 2. データベースを作る

```bash
python -m jimoto.build data/japan-latest.osm.pbf --prefectures data/japan.geojson --out data/jimoto.db
```

`data/brands.json` にブランド辞書の中身も書き出されるので、判定結果の確認に使えます。

### 3. サーバーを起動する

```bash
uvicorn jimoto.server:app --reload
```

http://localhost:8000 を開きます。現在地検索はブラウザの位置情報を使うため、`localhost` か HTTPS で開いてください。

データベースを自分で作らずに、GitHub Actions が作ったものを使うこともできます（下の「Render で公開する」を参照）。

```bash
pip install -r requirements.txt
python -m jimoto.fetch_data
uvicorn jimoto.server:app
```

## Render で公開する

パソコンを起動しておかなくても、スマホなどからいつでも使えるようにできます。

```
GitHub Actions（毎週月曜）─▶ 全国版 OSM からデータベースを作る ─▶ リリース data-latest に置く
                                                                  │
Render ─(デプロイのたび)─▶ jimoto.db を取得 ─▶ サーバーを起動 ◀────┘
```

### 1. データベースを用意する

GitHub の **Actions** タブ →「データベースを作る」→ **Run workflow** で実行します（5〜10分）。終わるとリリース `data-latest` に `jimoto.db` が置かれます。以後は毎週月曜に自動で作り直します。`jimoto/build.py` などの判定の仕組みを変えたときも自動で作り直します。

### 2. Render にサービスを作る

1. https://render.com に GitHub アカウントでログインする
2. **New** → **Blueprint** を選び、このリポジトリを選ぶ
3. `render.yaml` の内容（無料プラン、公開用の設定）が表示されるので、そのまま作成する

数分でデプロイが終わり、`https://jimoto-meshi-xxxx.onrender.com` のようなアドレスで使えるようになります。HTTPS なので、スマホでも現在地検索が使えます。

### 3. データの更新を自動で反映する（任意）

Render のサービスの **Settings** → **Deploy Hook** の URL をコピーし、GitHub のリポジトリの **Settings** → **Secrets and variables** → **Actions** に `RENDER_DEPLOY_HOOK` という名前で登録します。データベースを作り直すたびに Render が新しいデータで再起動します。

### 公開版での注意

- **無料プランは15分使わないと休止します。** 休止中に開くと、表示まで1分ほどかかります
- **「判定を直す」は、公開版では各自のブラウザに保存されます**（`JIMOTO_PUBLIC=1`）。他の人の判定には影響しません。みんなに反映したい修正は `jimoto/overrides.json` に書いて push してください
- 地図の画像は OpenStreetMap のタイルを使っています。[タイルの利用方針](https://operations.osmfoundation.org/policies/tiles/)では大量のアクセスが禁止されているので、多くの人に公開する場合は別の地図タイルの提供元に切り替えてください

## ホットペッパーの店も表示する

OSM の飲食店は全国で約19万件で、Google マップよりかなり少なめです。ホットペッパーグルメ Webサービス（無料）の API キーを設定すると、ホットペッパーの店も合わせて表示します。同じ店は1件にまとめ、チェーンの判定は同じブランド辞書で行います。

1. https://webservice.recruit.co.jp/register/ でメールアドレスを登録し、API キーを受け取る
2. 環境変数 `HOTPEPPER_API_KEY` に設定する
   - 自分のパソコン（PowerShell）：`$env:HOTPEPPER_API_KEY="xxxx"` のあと `python -m uvicorn jimoto.server:app`
   - Render：サービスの **Environment** で `HOTPEPPER_API_KEY` に入力して保存（自動で再起動します）

ホットペッパーの利用規約により、画面に「Powered by ホットペッパーグルメ Webサービス」を表示しています。消さないでください。ホットペッパーに掲載している店が中心なので、居酒屋やレストランは多く、小さな個人店は少なめです。ホットペッパーの API が使えないときは、OSM の店だけを表示します。

## データ源の切り替え

| `JIMOTO_PROVIDER` | 内容 | 必要なもの |
| --- | --- | --- |
| `osm`（既定） | ビルドしたデータベースを検索する | なし |
| `google` | Google Places API (New) の Text Search を使う | `GOOGLE_MAPS_API_KEY` |

```bash
export JIMOTO_PROVIDER=google
export GOOGLE_MAPS_API_KEY=xxxxxxxx
uvicorn jimoto.server:app
```

Google を使う場合も、チェーン判定と駅名検索にはビルドしたデータベースを使います。

**Google Places を使う前に、Google Maps Platform の利用規約を確認してください。** 特に、取得データの保存期間、Google 以外の地図への表示、帰属表示の扱いに決まりがあります。このサーバーは Google の結果を保存せず、受け取ったその場で判定して返します。

## API

### `GET /api/search`

| パラメータ | 内容 |
| --- | --- |
| `lat`, `lng` | 現在地で検索 |
| `station` | 駅名で検索（「静岡」「静岡駅」どちらでも可） |
| `station_id` | 同名の駅が複数あるときに選んだ駅 |
| `q` | キーワード。場所と組み合わせるか、単独で使う |
| `radius` | 検索半径（m、既定 1000） |
| `min_stores`, `min_prefs`, `min_spread_km` | 判定のしきい値 |
| `min_stores_any` | この店舗数以上なら範囲に関係なく広域チェーン（既定 40） |
| `exclude_local` | `true` で地元チェーンも除外 |
| `hide_excluded` | `true` で除外した店を結果から取り除く |

同名の駅が複数あるときは `{"needs_choice": true, "stations": [...]}` を返します。

各結果には `category`（`indie` / `local` / `chain`）、`excluded`、`brand`（店舗数・都道府県数）が付きます。画面ではこの `brand` を使い、スライダーを動かしたときに再検索せずに判定し直しています。

### `POST /api/report`

判定の修正を記録します。`{"key": "<結果の override_key>", "category": "chain"}`。`category` を `null` にすると自動の判定に戻ります。

### `GET /api/stations?q=静岡`

駅名の候補を返します。

## テスト

```bash
pip install -r requirements-build.txt
pytest
```

`tests/fixtures/` の小さな OSM データで、辞書づくりから検索 API までを通して確かめます。Google Places は応答を模擬しています。

### 判定を直す

画面の各店にある「判定を直す」から、そのブランドを「広域チェーン／地元チェーン／個人店」に固定できます。記録は `data/reports.json` に保存され、同じブランドの全店舗に効き、自動の判定より優先されます。みんなで共有したい修正は `jimoto/overrides.json` に書いてください。

## 今後の課題

- 店舗数がまだ少ない新興チェーンへの対応（OSM の更新を定期的に取り込む）
- ホットペッパーグルメ API など、ほかのデータ源の追加
- OSM の飲食店データは地域によって網羅性に差があるため、Google などとの併用

## データのライセンス

- OpenStreetMap のデータは ODbL です。表示するときは「© OpenStreetMap contributors」の帰属表示が必要です
- 都道府県の境界データは、取得元のライセンスを確認して使ってください
