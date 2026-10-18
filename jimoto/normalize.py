"""店名の正規化。

チェーン判定では「スターバックス コーヒー 静岡パルコ店」と「スターバックスコーヒー 渋谷店」を
同じブランドとして扱う必要がある。ここでは表記ゆれをならし、支店名を取り除く。
"""
from __future__ import annotations

import re
import unicodedata

_SPACE = re.compile(r"[\s　]+")
# 空白のあとに続く「〜店」「〜支店」などの支店名
_BRANCH = re.compile(r"[\s　]+[^\s　]*?(?:店|支店|本店|号店|営業所)$")
# 末尾の括弧書き「（静岡駅前）」など
_PAREN = re.compile(r"[\(（][^\)）]*[\)）]$")
# 名前の残りがこれで終わっていれば支店名とみなす
BRANCH_TAIL = re.compile(r"^(?:.*(?:店|支店|本店|号店|営業所)|[\(（].*)$")


def nfkc(s: str | None) -> str:
    """全角英数・半角カナなどを統一する。"""
    return unicodedata.normalize("NFKC", s or "").strip()


_KATA2HIRA = {c: c - 0x60 for c in range(ord("ァ"), ord("ヶ") + 1)}


def squash(s: str | None) -> str:
    """照合用のキー。NFKC にそろえ、空白を除き、英字を小文字に、カタカナをひらがなにする。

    >>> squash("8番ラーメン") == squash("8番らーめん")
    True
    """
    return _SPACE.sub("", nfkc(s)).lower().translate(_KATA2HIRA)


def drop_branch(name: str | None) -> str:
    """表示用：支店名と末尾の括弧書きを取り除く（空白は残す）。"""
    s = _PAREN.sub("", nfkc(name)).strip()
    return _BRANCH.sub("", s).strip()


def strip_branch(name: str | None) -> str:
    """支店名と末尾の括弧書きを取り除いた照合用キーを返す。

    >>> strip_branch("スターバックス コーヒー 静岡パルコ店")
    'スターバックスコーヒー'
    >>> strip_branch("鮨 まつ乃")
    '鮨まつ乃'
    """
    return squash(drop_branch(name))
