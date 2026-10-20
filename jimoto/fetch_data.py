"""GitHub のリリースに置いたデータベースを取ってくる（Render のビルドで使う）。

    python -m jimoto.fetch_data            # data/jimoto.db がなければ取得
    python -m jimoto.fetch_data --force    # あっても取り直す

取得元は環境変数 JIMOTO_DATA_URL で変えられる。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

DEFAULT_URL = "https://github.com/test90654/jimoto-meshi/releases/download/data-latest/jimoto.db"


def fetch(url: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".download")
    with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    # 壊れたファイルで起動しないよう、中身を確かめてから置き換える
    conn = sqlite3.connect(tmp)
    try:
        n = conn.execute("SELECT count(*) FROM brands").fetchone()[0]
        built = dict(conn.execute("SELECT k, v FROM meta").fetchall()).get("built_at", "?")
    finally:
        conn.close()
    tmp.replace(out)
    print(f"{url} → {out}（ブランド {n:,} 件、{built} 作成）", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="検索用データベースをダウンロードする")
    ap.add_argument("--out", default=os.environ.get("JIMOTO_DB", "data/jimoto.db"))
    ap.add_argument("--url", default=os.environ.get("JIMOTO_DATA_URL", DEFAULT_URL))
    ap.add_argument("--force", action="store_true", help="すでにあっても取り直す")
    args = ap.parse_args(argv)
    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"{out} はすでにあります（取り直すには --force）", file=sys.stderr)
        return 0
    fetch(args.url, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
