"""ブランド辞書とチェーン判定。

判定は2つの軸で行う。
- 店舗数：ブランドの全国の店舗数
- 広がり：展開している都道府県の数（都道府県データがない場合は店舗の分布範囲 km）

両方がしきい値以上なら「広域チェーン」として除外し、そうでなければ「地元チェーン」として残す。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Literal

from .normalize import BRANCH_TAIL, squash, strip_branch

Category = Literal["indie", "local", "chain"]


@dataclass
class Brand:
    key: str
    name: str
    stores: int
    prefs: int | None
    spread_km: float
    source: str  # "wikidata" | "brand" | "name"
    wikidata: str | None = None
    aliases: list[str] = field(default_factory=list)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("aliases")
        return d


@dataclass
class Rules:
    min_stores: int = 20
    min_prefs: int = 4
    min_spread_km: float = 150.0  # 都道府県データがない場合に使う
    exclude_local: bool = False  # 地元チェーンも除外するか
    min_name_group: int = 3  # 店名だけで束ねたグループは、この店舗数以上でないとチェーン扱いしない


def classify(brand: Brand | None, rules: Rules) -> Category:
    if brand is None:
        return "indie"
    if brand.source == "name" and brand.stores < rules.min_name_group:
        return "indie"
    if brand.prefs is not None:
        wide = brand.prefs >= rules.min_prefs
    else:
        wide = brand.spread_km >= rules.min_spread_km
    return "chain" if brand.stores >= rules.min_stores and wide else "local"


def is_excluded(category: Category, rules: Rules) -> bool:
    return category == "chain" or (rules.exclude_local and category == "local")


class BrandIndex:
    """ブランド辞書。店名からブランドを引く。"""

    MIN_PREFIX = 2  # 前方一致に使う別名の最短文字数

    def __init__(self, brands: list[Brand]):
        self.by_key: dict[str, Brand] = {b.key: b for b in brands}
        self.alias: dict[str, str] = {}
        # 店舗数の多いブランドを優先して別名を登録する
        for b in sorted(brands, key=lambda b: -b.stores):
            for a in {squash(b.name), *map(squash, b.aliases)}:
                if a and a not in self.alias:
                    self.alias[a] = b.key

    @classmethod
    def from_db(cls, conn: sqlite3.Connection) -> "BrandIndex":
        rows = conn.execute(
            "SELECT key, name, stores, prefs, spread_km, source, wikidata, aliases FROM brands"
        ).fetchall()
        return cls([Brand(r[0], r[1], r[2], r[3], r[4], r[5], r[6], json.loads(r[7] or "[]")) for r in rows])

    def __len__(self) -> int:
        return len(self.by_key)

    def get(self, key: str | None) -> Brand | None:
        return self.by_key.get(key) if key else None

    def match(self, name: str) -> Brand | None:
        """店名に一致するブランドを返す。見つからなければ None。

        1. 支店名を除いた名前が別名と完全一致するか
        2. 名前の先頭が別名と一致し、残りが「〜店」や括弧書きだけか
           （「すき家静岡駅南口店」のように空白のない店名に対応）
        """
        key = self.alias.get(strip_branch(name))
        if key:
            return self.by_key[key]
        s = squash(name)
        for n in range(len(s) - 1, self.MIN_PREFIX - 1, -1):
            key = self.alias.get(s[:n])
            if key and BRANCH_TAIL.match(s[n:]):
                return self.by_key[key]
        return None
