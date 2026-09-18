# -*- coding: utf-8 -*-
"""
学習(train_leakfree.py)と本番予想(pipeline.py)で「完全に同じ」特徴量を作るための共通モジュール。

ここに書いてある処理だけが特徴量の唯一の定義です。
学習側と本番側で別々に特徴量を組み立てると、入力の食い違い(train-serve skew)が起きます。
以前の model_new.pkl はそれが原因の1つでした(学習=結果確定後の実ST・実コース、本番=展示ST等)。

使う情報は「予想時点で既に分かっているもの」だけ:
  番組表(programs) + 直前情報(previews: 展示タイム/進入予想コース/チルト/風/波)
選手の直近成績(form5/win10)は、本番側のキャッシュ(racer_recent_form.json)が
664人・各1〜2走分しか無く学習時と同じ値を再現できないため、この版では使いません。
"""
import numpy as np
import pandas as pd

# 本番で再現できる特徴量だけ(直近成績 form5/win10/n_prev は除外)
FEATURES = [
    "boat_no", "course", "is_course1", "stadium",
    "class", "age", "nat1", "nat2", "loc1", "loc2", "motor2", "boat2", "avg_st", "flying",
    "ex_time_v", "ex_rank", "ex_rel", "tilt", "wt_adj", "wind", "wave", "wind_dir",
    "nat1_rk", "nat2_rk", "motor2_rk", "boat2_rk", "avg_st_rk", "class_rk",
    "nat1_dev", "motor2_dev", "c1_class", "c1_nat1", "nat1_x_c1",
]

RACE_KEY = ["date", "stadium", "race_no"]


def _prev_boat_dict(prev):
    """直前情報の boats(リスト形式/辞書形式どちらでも)を {艇番: dict} にする"""
    if not prev:
        return {}
    raw = prev.get("boats", {})
    if isinstance(raw, list):
        return {b.get("racer_boat_number"): b for b in raw if b.get("racer_boat_number")}
    return {int(k): v for k, v in raw.items()}


def make_rows(prog, prev):
    """1レース分(番組表prog + 直前情報prev)→ 艇ごとの行(list of dict)。
    place(着順)はここでは入れない(学習側で結果から付ける)。"""
    date = prog.get("race_date")
    stadium = prog.get("race_stadium_number")
    race_no = prog.get("race_number")
    vb = _prev_boat_dict(prev)
    rows = []
    for p in prog.get("boats", []):
        bno = p.get("racer_boat_number")
        v = vb.get(bno, {})
        rows.append({
            "date": date, "stadium": stadium, "race_no": race_no, "boat_no": bno,
            "racer_id": p.get("racer_number"),
            "class": p.get("racer_class_number"), "age": p.get("racer_age"),
            "nat1": p.get("racer_national_top_1_percent"),
            "nat2": p.get("racer_national_top_2_percent"),
            "loc1": p.get("racer_local_top_1_percent"),
            "loc2": p.get("racer_local_top_2_percent"),
            "motor2": p.get("racer_assigned_motor_top_2_percent"),
            "boat2": p.get("racer_assigned_boat_top_2_percent"),
            "avg_st": p.get("racer_average_start_timing"),
            "flying": p.get("racer_flying_count"),
            "ex_time": v.get("racer_exhibition_time"),
            "course": v.get("racer_course_number"),
            "tilt": v.get("racer_tilt_adjustment"),
            "wt_adj": v.get("racer_weight_adjustment"),
            "wind": prev.get("race_wind") if prev else None,
            "wave": prev.get("race_wave") if prev else None,
            "wind_dir": prev.get("race_wind_direction_number") if prev else None,
        })
    return rows


def rows_to_frame(rows):
    df = pd.DataFrame(rows)
    for c in df.columns:
        if c != "date":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["course"] = df["course"].fillna(df["boat_no"])
    return df


def add_race_features(df):
    """レース内の相対特徴量。df は 1レース以上(RACE_KEY で groupby)。"""
    df = df.sort_values(["date", "race_no", "stadium", "boat_no"]).reset_index(drop=True)
    g = df.groupby(RACE_KEY)
    df["is_course1"] = (df["course"] == 1).astype(int)
    df["ex_time_v"] = df["ex_time"].where(df["ex_time"] > 0)
    df["ex_rank"] = g["ex_time_v"].rank(method="min")
    m = g["ex_time_v"].transform("mean")
    df["ex_rel"] = (m - df["ex_time_v"]) / m * 100
    for col in ["nat1", "nat2", "motor2", "boat2", "avg_st", "class"]:
        asc = col in ("avg_st", "class")  # 小さいほど良い
        df[f"{col}_rk"] = g[col].rank(method="min", ascending=asc)
    df["nat1_dev"] = df["nat1"] - g["nat1"].transform("mean")
    df["motor2_dev"] = df["motor2"] - g["motor2"].transform("mean")
    c1 = df[df["boat_no"] == 1][RACE_KEY + ["class", "nat1"]]
    c1.columns = RACE_KEY + ["c1_class", "c1_nat1"]
    df = df.merge(c1, on=RACE_KEY, how="left")
    df["nat1_x_c1"] = df["nat1"] * df["is_course1"]
    return df


def race_frame(prog, prev):
    """本番用: 1レース分の特徴量DataFrame(艇番順)"""
    return add_race_features(rows_to_frame(make_rows(prog, prev)))


def predict_win_probs(artifact, df):
    """artifact: train_leakfree.py が保存した dict。戻り値: df と同じ並びの勝率(較正済み・未正規化)"""
    feats = artifact["features"]
    raw = artifact["model"].predict_proba(df[feats])[:, 1]
    return artifact["iso"].predict(raw)
