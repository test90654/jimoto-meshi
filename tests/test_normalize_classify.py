from jimoto.classify import Brand, BrandIndex, Rules, classify, is_excluded
from jimoto.normalize import squash, strip_branch


def test_strip_branch():
    assert strip_branch("スターバックス コーヒー 静岡パルコ店") == squash("スターバックスコーヒー")
    assert strip_branch("ｽﾀｰﾊﾞｯｸｽ 渋谷店") == squash("スターバックス")
    assert strip_branch("鮨 まつ乃") == "鮨まつ乃"
    assert strip_branch("マクドナルド（静岡駅前）") == squash("マクドナルド")
    assert squash("8番ラーメン") == squash("8番らーめん")


SB = Brand("wd:Q37158", "スターバックス", 1900, 47, 1500, "wikidata", "Q37158", ["スターバックス", "スターバックスコーヒー"])
SAWA = Brand("b:さわやか", "炭焼きレストランさわやか", 34, 1, 120, "brand", None, ["炭焼きレストランさわやか"])
SUKI = Brand("b:すき家", "すき家", 1900, 47, 1500, "brand", None, ["すき家"])
INDEX = BrandIndex([SB, SAWA, SUKI])


def test_match():
    assert INDEX.match("スターバックス コーヒー 静岡パルコ店") is SB
    assert INDEX.match("すき家静岡駅南口店") is SUKI  # 空白なし
    assert INDEX.match("すき家族") is None  # 残りが支店名ではない
    assert INDEX.match("炭焼きレストランさわやか 浜松鍛冶町店") is SAWA
    assert INDEX.match("鮨 まつ乃") is None


def test_classify():
    r = Rules()
    assert classify(SB, r) == "chain"
    assert classify(SAWA, r) == "local"
    assert classify(None, r) == "indie"
    assert is_excluded("chain", r) and not is_excluded("local", r)
    assert is_excluded("local", Rules(exclude_local=True))


def test_spread_fallback_without_prefectures():
    b = Brand("b:x", "X", 30, None, 400, "brand")
    assert classify(b, Rules()) == "chain"
    assert classify(Brand("b:y", "Y", 30, None, 60, "brand"), Rules()) == "local"


def test_many_stores_in_small_area_is_chain():
    komoro = Brand("n:小諸そば", "小諸そば", 43, 3, 38, "name")
    assert classify(komoro, Rules()) == "chain"
    assert classify(komoro, Rules(min_stores_any=100)) == "local"


def test_name_group_needs_enough_stores():
    b = Brand("n:居酒屋花", "居酒屋 花", 2, 2, 140, "name")
    assert classify(b, Rules(min_stores=2, min_prefs=2)) == "indie"
