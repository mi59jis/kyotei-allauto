# -*- coding: utf-8 -*-
"""
fan_features.py
公式期別成績(FANファイル)をパースし、レース行(racer_id + 日付 + コース)に
対応する期のコース別成績・能力指数などを付与するモジュール。

使い方:
    from fan_features import load_fan_data, attach_fan_features
    fan_df = load_fan_data(['fan2204.txt', 'fan2210.txt', ...])
    df = attach_fan_features(df, fan_df)   # df には racer_id, date, result_course 列が必要
"""
import glob
import pandas as pd
import numpy as np
from datetime import datetime
from parse_fan import parse_file

RANK_SCORE = {"A1": 4, "A2": 3, "B1": 2, "B2": 1}


def _parse_date(s):
    """'20131101' 形式の文字列をdatetimeに変換"""
    try:
        return datetime.strptime(str(s).strip(), "%Y%m%d")
    except Exception:
        return pd.NaT


def load_fan_data(paths):
    """複数のFANファイルパスを受け取り、1本のDataFrameに統合する"""
    if isinstance(paths, str):
        paths = glob.glob(paths)

    dfs = []
    for p in paths:
        d = parse_file(p)
        dfs.append(d)
    fan = pd.concat(dfs, ignore_index=True)

    fan["racer_id"] = pd.to_numeric(fan["racer_id"], errors="coerce")
    fan["period_from_dt"] = fan["period_from"].apply(_parse_date)
    fan["period_to_dt"]   = fan["period_to"].apply(_parse_date)
    fan["rank_score"]     = fan["rank"].map(RANK_SCORE)
    fan["rank_prev_score"] = fan["rank_prev"].map(RANK_SCORE)

    # 級の推移: 今期 - 前期 (正=昇級, 負=降級, 0=維持)
    fan["rank_trend"] = fan["rank_score"] - fan["rank_prev_score"]

    # 能力指数の伸び (今期 - 前期)
    fan["index_trend"] = fan["index_now"] - fan["index_prev"]

    fan = fan.sort_values(["racer_id", "period_from_dt"]).reset_index(drop=True)
    return fan


def _course_cols(course):
    """コース番号(1-6)に対応する複勝率・平均ST・平均順位・進入回数のカラム名を返す"""
    c = int(course)
    if c < 1 or c > 6:
        return None
    return {
        "course_place_rate": f"c{c}_place_rate",
        "course_avg_st":     f"c{c}_avg_st",
        "course_avg_st_rank":f"c{c}_avg_st_rank",
        "course_entries":    f"c{c}_entries",
    }


def attach_fan_features(df: pd.DataFrame, fan: pd.DataFrame) -> pd.DataFrame:
    """
    df: 'racer_id'(選手登録番号), 'date'(YYYY-MM-DD文字列 or datetime), 'result_course' を含むレースデータ
    fan: load_fan_data() の返り値
    戻り値: 元のdfに ability_index_now, ability_index_trend, rank_trend_fan,
            course_place_rate, course_avg_st, course_avg_st_rank, course_entries
            を追加したDataFrame

    実装: merge_asof(by=racer_id, direction=backward) で「レース日付時点で
    有効な最新の期」のFANレコードを選手ごとに突き合わせる（ベクトル化により高速）。
    """
    left = df.copy()
    left["_date_dt"] = pd.to_datetime(left["date"])
    left["racer_id_num"] = pd.to_numeric(left["racer_id"], errors="coerce")
    left["_orig_order"] = np.arange(len(left))

    fan_cols_needed = ["racer_id", "period_from_dt", "index_now", "index_trend", "rank_trend"] + \
        [f"c{c}_place_rate" for c in range(1, 7)] + \
        [f"c{c}_avg_st" for c in range(1, 7)] + \
        [f"c{c}_avg_st_rank" for c in range(1, 7)] + \
        [f"c{c}_entries" for c in range(1, 7)]
    right = fan[fan_cols_needed].dropna(subset=["racer_id", "period_from_dt"]).copy()
    right["racer_id"] = right["racer_id"].astype("float64")
    right = right.sort_values("period_from_dt")

    left_sorted = left.sort_values("_date_dt")

    merged = pd.merge_asof(
        left_sorted, right,
        left_on="_date_dt", right_on="period_from_dt",
        left_by="racer_id_num", right_by="racer_id",
        direction="backward",
    )
    merged = merged.sort_values("_orig_order").reset_index(drop=True)

    course = pd.to_numeric(merged["result_course"], errors="coerce")
    place_rate  = np.full(len(merged), np.nan)
    avg_st      = np.full(len(merged), np.nan)
    avg_st_rank = np.full(len(merged), np.nan)
    entries     = np.full(len(merged), np.nan)
    for c in range(1, 7):
        m = (course == c).values
        place_rate[m]  = merged.loc[m, f"c{c}_place_rate"].values
        avg_st[m]      = merged.loc[m, f"c{c}_avg_st"].values
        avg_st_rank[m] = merged.loc[m, f"c{c}_avg_st_rank"].values
        entries[m]     = merged.loc[m, f"c{c}_entries"].values

    out = df.copy()
    out["ability_index_now"]   = merged["index_now"].values
    out["ability_index_trend"] = merged["index_trend"].values
    out["rank_trend_fan"]      = merged["rank_trend"].values
    out["course_place_rate"]   = place_rate
    out["course_avg_st"]       = avg_st
    out["course_avg_st_rank"]  = avg_st_rank
    out["course_entries"]      = entries
    return out


def get_fan_features_single(fan: pd.DataFrame, racer_id, date_str: str, course):
    """
    リアルタイム予想用: 1選手・1日付・1コースぶんのFAN特徴量を取得する。
    date_str: 'YYYY-MM-DD' 形式
    戻り値: dict (ability_index_now, ability_index_trend, rank_trend_fan,
                  course_place_rate, course_avg_st, course_avg_st_rank, course_entries)
            該当データが無い場合は全てNoneのdictを返す
    """
    out_keys = ["ability_index_now", "ability_index_trend", "rank_trend_fan",
                "course_place_rate", "course_avg_st", "course_avg_st_rank", "course_entries"]
    empty = {k: None for k in out_keys}

    try:
        rid = int(racer_id)
    except (TypeError, ValueError):
        return empty

    g = fan[fan["racer_id"] == rid]
    if g.empty:
        return empty

    date = pd.to_datetime(date_str)
    cand = g[g["period_from_dt"] <= date]
    if cand.empty:
        return empty
    rec = cand.sort_values("period_from_dt").iloc[-1]

    result = {
        "ability_index_now": rec.get("index_now"),
        "ability_index_trend": rec.get("index_trend"),
        "rank_trend_fan": rec.get("rank_trend"),
    }
    cc = _course_cols(course) if course and not pd.isna(course) else None
    if cc:
        result["course_place_rate"]  = rec.get(cc["course_place_rate"])
        result["course_avg_st"]      = rec.get(cc["course_avg_st"])
        result["course_avg_st_rank"] = rec.get(cc["course_avg_st_rank"])
        result["course_entries"]     = rec.get(cc["course_entries"])
    else:
        result.update({"course_place_rate": None, "course_avg_st": None,
                        "course_avg_st_rank": None, "course_entries": None})
    return result


if __name__ == "__main__":
    import sys
    fan = load_fan_data(sys.argv[1:])
    print(f"FAN統合レコード数: {len(fan):,}")
    print(fan[["racer_id", "name_kanji", "fan_year", "fan_term",
               "period_from", "period_to", "index_now", "rank", "rank_prev"]].head(10).to_string())
