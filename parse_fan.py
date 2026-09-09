# -*- coding: utf-8 -*-
"""
parse_fan.py
公式「モーターボートファン手帳」固定長データ(fanYYMM.txt, 1レコード=416バイト, Shift-JIS)を
パースしてDataFrame化する。

レイアウト仕様: https://www.boatrace.jp/owpc/pc/extra/data/layout.html
"""
import pandas as pd

ENC = "cp932"

# (フィールド名, バイト数) のリストを順番通りに定義
FIELD_SPEC = [
    ("racer_id", 4),
    ("name_kanji", 16),
    ("name_kana", 15),
    ("branch", 4),
    ("rank", 2),
    ("era", 1),
    ("birth_date", 6),
    ("sex", 1),
    ("age", 2),
    ("height", 3),
    ("weight", 2),
    ("blood_type", 2),
    ("win_rate", 4),
    ("place_rate", 4),
    ("wins_1st", 3),
    ("wins_2nd", 3),
    ("starts", 3),
    ("final_appearances", 2),
    ("championships", 2),
    ("avg_st", 3),
]

for c in range(1, 7):
    FIELD_SPEC += [
        (f"c{c}_entries", 3),
        (f"c{c}_place_rate", 4),
        (f"c{c}_avg_st", 3),
        (f"c{c}_avg_st_rank", 3),
    ]

FIELD_SPEC += [
    ("rank_prev", 2),
    ("rank_prev2", 2),
    ("rank_prev3", 2),
    ("index_prev", 4),
    ("index_now", 4),
    ("fan_year", 4),
    ("fan_term", 1),
    ("period_from", 8),
    ("period_to", 8),
    ("training_term", 3),
]

for c in range(1, 7):
    FIELD_SPEC += [
        (f"c{c}_1st", 3),
        (f"c{c}_2nd", 3),
        (f"c{c}_3rd", 3),
        (f"c{c}_4th", 3),
        (f"c{c}_5th", 3),
        (f"c{c}_6th", 3),
        (f"c{c}_F", 2),
        (f"c{c}_L0", 2),
        (f"c{c}_L1", 2),
        (f"c{c}_K0", 2),
        (f"c{c}_K1", 2),
        (f"c{c}_S0", 2),
        (f"c{c}_S1", 2),
        (f"c{c}_S2", 2),
    ]

FIELD_SPEC += [
    ("nocourse_L0", 2),
    ("nocourse_L1", 2),
    ("nocourse_K0", 2),
    ("nocourse_K1", 2),
    ("hometown", 6),
]

RECORD_LEN = sum(n for _, n in FIELD_SPEC)  # 416になるはず


def _to_num(raw: str, decimals: int = 0):
    """数字フィールドを int/float に変換。空白/非数字はNaN扱い"""
    raw = raw.strip()
    if raw == "" or not raw.replace(".", "").isdigit():
        # 全角/記号混じり(級 "A1" など)はそのまま文字列として返す
        return raw
    if decimals:
        try:
            return int(raw) / (10 ** decimals)
        except ValueError:
            return None
    try:
        return int(raw)
    except ValueError:
        return None


# 小数点変換が必要なフィールド(桁数指定あり)
DECIMAL_FIELDS = {
    "win_rate": 2, "place_rate": 1, "avg_st": 2,
    "index_prev": 2, "index_now": 2,
}
for c in range(1, 7):
    DECIMAL_FIELDS[f"c{c}_place_rate"] = 1
    DECIMAL_FIELDS[f"c{c}_avg_st"] = 2
    DECIMAL_FIELDS[f"c{c}_avg_st_rank"] = 2

STR_FIELDS = {"name_kanji", "name_kana", "branch", "rank", "era", "sex", "blood_type",
              "rank_prev", "rank_prev2", "rank_prev3", "fan_term", "hometown",
              "birth_date", "period_from", "period_to"}


def parse_file(path: str) -> pd.DataFrame:
    with open(path, "rb") as f:
        raw = f.read()

    # CRLFのみで分割(cp932の中にNEL(0x85)に誤認される2バイト文字が含まれるため、
    # 文字コードに依存しないバイト単位で厳密にCRLF分割する)
    lines = raw.split(b"\r\n")
    lines = [l for l in lines if len(l) >= RECORD_LEN - 4]  # 末尾の空行等を除外

    records = []
    for line in lines:
        if len(line) < RECORD_LEN:
            # 生年月日など末尾が欠けている壊れた行はスキップ
            continue
        pos = 0
        rec = {}
        for name, length in FIELD_SPEC:
            chunk = line[pos:pos + length]
            pos += length
            text = chunk.decode(ENC, errors="replace").strip().replace("\u3000", " ").strip()
            if name in STR_FIELDS:
                rec[name] = text
            else:
                rec[name] = _to_num(text, DECIMAL_FIELDS.get(name, 0))
        records.append(rec)

    return pd.DataFrame(records)


if __name__ == "__main__":
    import sys
    df = parse_file(sys.argv[1])
    print(f"レコード想定バイト数: {RECORD_LEN}")
    print(f"パース件数: {len(df)}")
    print(df[["racer_id", "name_kanji", "name_kana", "branch", "rank",
              "win_rate", "place_rate", "starts", "avg_st"]].head(10).to_string())
