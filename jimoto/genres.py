"""OSM の amenity / cuisine タグを日本語のジャンル名にする。キーワード検索にも使う。"""
from __future__ import annotations

FOOD_AMENITIES = {"restaurant", "cafe", "fast_food", "bar", "pub", "food_court", "ice_cream", "biergarten"}

AMENITY_JA = {
    "restaurant": "レストラン", "cafe": "カフェ", "fast_food": "ファストフード", "bar": "バー",
    "pub": "居酒屋", "food_court": "フードコート", "ice_cream": "アイスクリーム", "biergarten": "ビアガーデン",
}

CUISINE_JA = {
    "japanese": "和食", "ramen": "ラーメン", "sushi": "寿司", "soba": "蕎麦", "udon": "うどん",
    "noodle": "麺類", "tempura": "天ぷら", "tonkatsu": "とんかつ", "yakiniku": "焼肉", "yakitori": "焼き鳥",
    "okonomiyaki": "お好み焼き", "takoyaki": "たこ焼き", "oden": "おでん", "unagi": "うなぎ", "eel": "うなぎ",
    "gyoza": "餃子", "dumpling": "餃子", "izakaya": "居酒屋", "donburi": "丼", "beef_bowl": "牛丼",
    "curry": "カレー", "chinese": "中華", "korean": "韓国料理", "thai": "タイ料理", "indian": "インド料理",
    "vietnamese": "ベトナム料理", "italian": "イタリアン", "french": "フレンチ", "pizza": "ピザ",
    "pasta": "パスタ", "burger": "ハンバーガー", "steak_house": "ステーキ", "hamburger_steak": "ハンバーグ",
    "western": "洋食", "chicken": "チキン", "fried_chicken": "唐揚げ", "seafood": "海鮮", "fish": "海鮮",
    "coffee_shop": "カフェ", "tea": "お茶", "cake": "ケーキ", "dessert": "スイーツ", "bakery": "パン",
    "sandwich": "サンドイッチ", "kebab": "ケバブ", "barbecue": "バーベキュー", "regional": "郷土料理",
    "teishoku": "定食", "bento": "弁当", "crepe": "クレープ", "ice_cream": "アイスクリーム",
}


def genre_label(amenity: str | None, cuisine: str | None) -> str:
    """表示用の日本語ジャンル。cuisine を優先し、なければ amenity から決める。"""
    labels: list[str] = []
    for c in (cuisine or "").split(";"):
        c = c.strip().lower()
        ja = CUISINE_JA.get(c)
        if ja and ja not in labels:
            labels.append(ja)
    if not labels:
        labels.append(AMENITY_JA.get(amenity or "", "飲食店"))
    return "・".join(labels[:2])
