"""利用者が直したチェーン判定。

- jimoto/overrides.json：リポジトリに入れて共有する修正
- data/reports.json：画面の「判定を直す」で記録した修正（各自のパソコンに保存）

キーはブランド名（ブランドがない店は支店名を除いた店名）を正規化したもの。
同じキーが両方にあれば reports.json を優先する。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .classify import Brand, Category
from .normalize import squash, strip_branch

SHIPPED = Path(__file__).with_name("overrides.json")
VALID: set[str] = {"chain", "local", "indie"}


def override_key(brand: Brand | None, place_name: str) -> str:
    return squash(brand.name) if brand else strip_branch(place_name)


def _load(path: Path) -> dict[str, dict]:
    """{"<キー>": {"category": ..., "name": ...}} を読む。"""
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for key, v in data.items():
        if isinstance(v, str):
            v = {"category": v}
        if v.get("category") in VALID:
            out[squash(key)] = v
    return out


class Overrides:
    def __init__(self, reports_path: Path | None, shipped_path: Path = SHIPPED):
        self.reports_path = reports_path
        self.shipped = _load(shipped_path)
        self.reports = _load(reports_path) if reports_path else {}
        self.lock = threading.Lock()

    def get(self, key: str) -> Category | None:
        v = self.reports.get(key) or self.shipped.get(key)
        return v["category"] if v else None

    def set(self, key: str, category: str | None, name: str = "") -> None:
        """category=None で記録を消す（自動の判定に戻す）。"""
        if self.reports_path is None:
            raise RuntimeError("判定の修正を保存する場所がありません")
        if category is not None and category not in VALID:
            raise ValueError(category)
        with self.lock:
            if category is None:
                self.reports.pop(key, None)
            else:
                self.reports[key] = {"category": category, "name": name}
            self.reports_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.reports_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.reports, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.reports_path)
