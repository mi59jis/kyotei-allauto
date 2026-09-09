"""
競艇予想 全自動パイプライン pipeline.py
「データ取得 → ML予想 → 結果記録 → 日次レポート」を1コマンドで実行

使い方:
    python pipeline.py predict          ← 本日の全場予想
    python pipeline.py predict 10       ← 三国のみ予想
    python pipeline.py predict 10 3     ← 三国3Rのみ予想
    python pipeline.py record           ← 昨日の結果を取得して的中判定
    python pipeline.py record 20260701  ← 指定日の結果を記録
    python pipeline.py report           ← 成績レポートを表示
    python pipeline.py all              ← 予想+昨日結果記録+レポートを一括実行

必要ファイル（同フォルダに置く）:
    model_new.pkl           ← ML予想モデル(retrain_final_v2.pyで作成)
    features_new.json       ← 特徴量リスト
    fan_data/fan*.txt        ← 公式FANデータ(6ファイル)
    fan_features.py, parse_fan.py, collect_odds.py ← 依存モジュール

予想ロジック:
    単勝オッズを取得し、モデルの予測確率と比較して「edge」(モデル確率-市場確率)を算出。
    3連単120通りのうちedge>=0.01の組み合わせだけを買い目として抽出する。
"""

import urllib.request
import json, csv, sys, os, pickle, itertools, glob, time
import numpy as np
from datetime import date, timedelta, datetime
from collections import defaultdict

sys.path.insert(0, ".")
from fan_features import load_fan_data, get_fan_features_single
from collect_odds import fetch_odds_page

# ============================================================
# 定数
# ============================================================
STADIUM_NAMES = {
    1:"桐生",2:"戸田",3:"江戸川",4:"平和島",5:"多摩川",6:"浜名湖",
    7:"蒲郡",8:"常滑",9:"津",10:"三国",11:"びわこ",12:"住之江",
    13:"尼崎",14:"鳴門",15:"丸亀",16:"児島",17:"宮島",18:"徳山",
    19:"下関",20:"若松",21:"芦屋",22:"福岡",23:"唐津",24:"大村",
}
WEATHER_NAMES = {1:"晴",2:"曇り",3:"雨",4:"雪",5:"霧",6:"台風"}
CLASS_NAMES   = {1:"A1",2:"A2",3:"B1",4:"B2"}
UPSET_RATE    = {1:29.5,2:40.2,3:60.2,4:66.8}
PICK_LABELS   = ["◎","◎","○","○","△","△","▲","▲","☆","☆"]

# 場別1コース勝率（実データから）
STADIUM_C1_RATE = {
    1:0.563,2:0.528,3:0.447,4:0.541,5:0.548,6:0.539,
    7:0.546,8:0.572,9:0.579,10:0.556,11:0.499,12:0.583,
    13:0.568,14:0.561,15:0.554,16:0.567,17:0.548,18:0.561,
    19:0.563,20:0.540,21:0.562,22:0.552,23:0.567,24:0.576,
}

RECORD_FILE = "prediction_record.csv"
MODEL_FILE  = "model_new.pkl"
FEAT_FILE   = "features_new.json"
EDGE_THRESHOLD = 0.01   # 毎日運用向け。厳選したい場合は0.02に上げる

# オッズ未確定時のリトライ設定(レースがまだ先すぎてオッズが出ていないケース対策)
ODDS_RETRY_MAX  = 4      # 最大リトライ回数(初回含めず)
ODDS_RETRY_WAIT = 45     # リトライ間隔(秒)

# 直前情報未公開時の待機設定
# boatraceopenapi(previews)は約30分間隔でしか更新されないため、
# 1レース指定で実行した場合に限り、来るまで待つ。
PREV_RETRY_MAX  = 10     # 最大リトライ回数(初回含めず)
PREV_RETRY_WAIT = 120    # リトライ間隔(秒) → 10回 x 2分 = 最大20分待機

_fan_cache = None
def get_fan_df():
    global _fan_cache
    if _fan_cache is None:
        fan_paths = sorted(glob.glob("fan_data/fan*.txt"))
        if not fan_paths:
            print("⚠ fan_data/ にFANファイルが見つかりません。FAN特徴量は空欄になります。")
            import pandas as pd
            _fan_cache = pd.DataFrame()
        else:
            _fan_cache = load_fan_data(fan_paths)
    return _fan_cache

# ============================================================
# 選手の直近成績キャッシュ(recent_form_score / recent_trend用)
# ============================================================
RACER_FORM_CACHE_FILE = "racer_recent_form.json"
RACER_FORM_KEEP_LAST_N = 10
_racer_form_cache = None

def load_racer_form_cache():
    global _racer_form_cache
    if _racer_form_cache is None:
        if os.path.exists(RACER_FORM_CACHE_FILE):
            try:
                with open(RACER_FORM_CACHE_FILE, encoding="utf-8") as f:
                    _racer_form_cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                _racer_form_cache = {}
        else:
            _racer_form_cache = {}
    return _racer_form_cache

def save_racer_form_cache(cache):
    global _racer_form_cache
    _racer_form_cache = cache
    with open(RACER_FORM_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)

def update_racer_form_cache(results, programs_idx):
    """1日分の結果(results)と、対応する番組表インデックス(programs_idx)から、
    選手ごとの直近成績キャッシュを更新する。record_results()から呼ばれる想定。"""
    cache = load_racer_form_cache()
    for res in results:
        key = (res["race_date"], res["race_stadium_number"], res["race_number"])
        prog = programs_idx.get(key)
        if not prog:
            continue
        prog_boats = {b["racer_boat_number"]: b for b in prog.get("boats", [])}
        for rb in res.get("boats", []):
            bno = rb.get("racer_boat_number")
            place = rb.get("racer_place_number")
            if place is None:
                continue
            pb = prog_boats.get(bno, {})
            racer_id = pb.get("racer_number")
            if not racer_id:
                continue
            try:
                place_f = float(place)
            except (TypeError, ValueError):
                continue
            k = str(racer_id)
            entries = cache.get(k, [])
            # 同じ日付+場+レースの重複登録を避ける
            entry_date = res["race_date"]
            if not any(e["date"] == entry_date for e in entries):
                entries.append({"date": entry_date, "place": place_f})
                entries.sort(key=lambda e: e["date"])
                cache[k] = entries[-RACER_FORM_KEEP_LAST_N:]
    save_racer_form_cache(cache)

def get_recent_form(racer_id, before_date):
    """
    指定選手の、before_dateより前の直近3走から recent_form_score, recent_trend を計算する。
    十分なデータが無ければ (None, None) を返す(呼び出し側でデフォルト値にフォールバックする)。
    """
    if not racer_id or not before_date:
        return None, None
    cache = load_racer_form_cache()
    entries = cache.get(str(racer_id), [])
    past = sorted([e for e in entries if e["date"] < before_date], key=lambda e: e["date"])
    if not past:
        return None, None
    last3 = past[-3:]
    avg_place = sum(e["place"] for e in last3) / len(last3)
    recent_form_score = 7 - min(max(avg_place, 1), 6)
    trend = (past[-1]["place"] - past[-3]["place"]) if len(past) >= 3 else 0.0
    return recent_form_score, trend

# ============================================================
# モデル読み込み
# ============================================================
def load_model():
    if os.path.exists(MODEL_FILE) and os.path.exists(FEAT_FILE):
        with open(MODEL_FILE,"rb") as f: model = pickle.load(f)
        with open(FEAT_FILE) as f:       feats  = json.load(f)
        return model, feats
    print(f"⚠ {MODEL_FILE} が見つかりません。ルールベースで予想します。")
    return None, None

# ============================================================
# データ取得
# ============================================================
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        return json.loads(res.read().decode("utf-8"))

def fetch_today():
    prog = fetch_json("https://boatraceopenapi.github.io/programs/v2/today.json")
    prev = fetch_json("https://boatraceopenapi.github.io/previews/v2/today.json")
    return prog, prev

def fetch_results(target_date=None):
    if target_date:
        url = f"https://boatraceopenapi.github.io/results/v2/{target_date[:4]}/{target_date}.json"
    else:
        url = "https://boatraceopenapi.github.io/results/v2/today.json"
    return fetch_json(url)

def fetch_programs(target_date=None):
    if target_date:
        url = f"https://boatraceopenapi.github.io/programs/v2/{target_date[:4]}/{target_date}.json"
    else:
        url = "https://boatraceopenapi.github.io/programs/v2/today.json"
    return fetch_json(url)

# ============================================================
# 特徴量計算
# ============================================================
def safe(v, d=0.0):
    try: return float(v) if v is not None else d
    except: return d

def _parse_st(val):
    """
    スタートタイミングの文字列を数値に変換する。
    'F.02'(フライング)は負の値として扱う。'L.05'(出遅れ)は正の値として扱う。
    通常は '.20' のような文字列、またはfloat/intそのもの。
    変換できない場合は None を返す。
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return None
    if s.upper().startswith("F"):
        try:
            return -float(s[1:])
        except ValueError:
            return None
    if s.upper().startswith("L"):
        try:
            return float(s[1:])
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def build_feature_vector(pb, pv, ex_rank, is_c1, c1_cls,
                          wind, wave, stadium_no, feats,
                          boat_no=None, course=None, date_str=None,
                          wind_direction=None, weather_number=None,
                          temperature=None, water_temperature=None,
                          fan_df=None, ex_relative=0.0, class_rank_in_race=None):
    age = safe(pb.get("racer_age"),35)
    st  = safe(pb.get("racer_average_start_timing"),0.18)
    m2  = safe(pb.get("racer_assigned_motor_top_2_percent"))
    b2  = safe(pb.get("racer_assigned_boat_top_2_percent"))
    n1  = safe(pb.get("racer_national_top_1_percent"))
    n2  = safe(pb.get("racer_national_top_2_percent"))
    cls = pb.get("racer_class_number",3)
    ex  = safe(pv.get("racer_exhibition_time") if pv else None)
    prev_st = _parse_st(pv.get("racer_start_timing") if pv else None)
    racer_id = pb.get("racer_number")

    age_score = 2 if 30<=age<=39 else (1 if 40<=age<=49 else 0)
    st_score  = 3 if st<=0.14 else 2 if st<=0.16 else 1 if st<=0.18 else 0

    # real_st_score（展示STを代用）
    rst = prev_st if prev_st is not None else 0.18
    real_st_score = (4 if rst<=0.10 else 3 if rst<=0.14 else
                     2 if rst<=0.18 else 1 if rst<=0.22 else 0)
    is_flying_val = 1 if (prev_st is not None and prev_st < 0) else 0

    c1r = STADIUM_C1_RATE.get(stadium_no, 0.55)

    # --- 進入コースのズレ(予想コース基準。本番の最終コースは締切直前まで確定しないため、
    #     直前情報のracer_course_numberを「実質的な進入コース」とみなして計算する。
    #     course_shift_from_previewは定義上0固定になる(学習時と違い、直前情報より後の
    #     "本当の最終着順コース"は予想時点では未知のため) ---
    course_val = course if course is not None else boat_no
    course_shift_from_boat = (course_val - boat_no) if (course_val is not None and boat_no is not None) else 0
    moved_inside  = 1 if course_shift_from_boat < 0 else 0
    moved_outside = 1 if course_shift_from_boat > 0 else 0
    course_shift_from_preview = 0   # 予想時点では計算不可(既知の限界)
    course_swap_from_preview = 0    # 同上

    # --- FANデータ由来の特徴量 ---
    fan_vals = {"ability_index_now": None, "ability_index_trend": None, "rank_trend_fan": None,
                "course_place_rate": None, "course_avg_st": None,
                "course_avg_st_rank": None, "course_entries": None}
    if fan_df is not None and len(fan_df) > 0 and racer_id and date_str:
        fan_vals = get_fan_features_single(fan_df, racer_id, date_str, course_val)

    # --- 天候・チルト・補重 ---
    tilt = safe(pv.get("racer_tilt_adjustment") if pv else None)
    weight_adj = safe(pv.get("racer_weight_adjustment") if pv else None)

    # --- 直近成績(選手ごとのキャッシュから計算。データが無ければデフォルト値) ---
    _recent_form, _recent_trend = get_recent_form(racer_id, date_str) if date_str else (None, None)
    if _recent_form is None:
        _recent_form = 3.0
    if _recent_trend is None:
        _recent_trend = 0.0

    val_map = {
        "class":           cls,
        "national_1st_pct":n1,
        "is_course1":      is_c1,
        "ex_rank":         ex_rank,
        "motor_2nd_pct":   m2,
        "boat_2nd_pct":    b2,
        "st_score":        st_score,
        "age_score":       age_score,
        "motor_strong":    int(m2>=40),
        "boat_strong":     int(b2>=50),
        "wind":            safe(wind),
        "wave":            safe(wave),
        "c1_class":        c1_cls,
        "ex_relative":     ex_relative,
        "c1_motor":        is_c1*m2,
        "is_a_class":      int(cls in [1,2]),
        "win_pct_x_c1":    n1*is_c1,
        "has_flying":      safe(pb.get("racer_flying_count"),0)>=1,
        "ex_rank_c1":      ex_rank if is_c1 else 0,
        "nat_2nd_pct":     n2,
        "class_rank_in_race": class_rank_in_race if class_rank_in_race is not None else cls,
        "weather_roughness": safe(wind)*safe(wave),
        "real_st_score":   real_st_score,
        "real_st_x_course": real_st_score * (1 if is_c1 else 3),
        "is_flying":       is_flying_val,
        "recent_form_score": _recent_form,
        "recent_trend":    _recent_trend,
        "ex_change_score": 0.0,
        "stadium_c1_win_rate": c1r,
        "c1_x_stadium_rate":   is_c1 * c1r,
        # 進入コースのズレ
        "course_shift_from_boat":    course_shift_from_boat,
        "course_shift_from_preview": course_shift_from_preview,
        "moved_inside":              moved_inside,
        "moved_outside":             moved_outside,
        "course_swap_from_preview":  course_swap_from_preview,
        # FANデータ由来
        "ability_index_now":   fan_vals.get("ability_index_now") or 0.0,
        "ability_index_trend": fan_vals.get("ability_index_trend") or 0.0,
        "rank_trend_fan":      fan_vals.get("rank_trend_fan") or 0.0,
        "course_place_rate":   fan_vals.get("course_place_rate") or 0.0,
        "course_avg_st":       fan_vals.get("course_avg_st") or 0.0,
        "course_avg_st_rank":  fan_vals.get("course_avg_st_rank") or 0.0,
        "course_entries":      fan_vals.get("course_entries") or 0.0,
        # 天候・チルト・補重
        "wind_direction":      safe(wind_direction),
        "weather_number":      safe(weather_number),
        "temperature":         safe(temperature),
        "water_temperature":   safe(water_temperature),
        "tilt_adjustment":     tilt,
        "weight_adjustment":   weight_adj,
        "wind_dir_x_course1":  safe(wind_direction) * is_c1,
        "tilt_x_course1":      tilt * is_c1,
    }
    return [val_map.get(f, 0.0) for f in feats]

# ============================================================
# 予想ロジック
# ============================================================
def fetch_odds_with_retry(date_compact, sno, rno, max_retries=ODDS_RETRY_MAX, wait_sec=ODDS_RETRY_WAIT):
    """
    単勝オッズを取得する。レースの締切がまだ先で6艇分揃わない場合、
    一定間隔で再試行する(レースが直前すぎず、オッズがまだ確定していないケース対策)。
    最終的に揃わなければ空(または不足)のdictを返す。
    """
    tansho = {}
    for attempt in range(max_retries + 1):
        try:
            tansho, _ = fetch_odds_page(date_compact, sno, rno)
        except Exception as e:
            print(f"  ⚠ オッズ取得エラー({sno}場{rno}R, 試行{attempt+1}): {e}")
            tansho = {}

        if len(tansho) == 6:
            return tansho

        if attempt < max_retries:
            print(f"  ⏳ オッズ未確定({sno}場{rno}R, {len(tansho)}/6艇分)。"
                  f"{wait_sec}秒後に再試行します({attempt+1}/{max_retries})...")
            time.sleep(wait_sec)

    return tansho


def harville(p, i, j, k):
    """Harvilleの公式で3連単確率を推定"""
    d1 = 1 - p[i]
    d2 = 1 - p[i] - p[j]
    if d1 <= 1e-9 or d2 <= 1e-9:
        return 0.0
    return p[i] * (p[j] / d1) * (p[k] / d2)


def judge_upset(c1_cls, c1_ex_rank, wind, wave):
    base = UPSET_RATE.get(c1_cls, 45.0)
    base += {1:-6,2:-3,3:0,4:4,5:6,6:8}.get(min(c1_ex_rank,6),0)
    if wind>=6: base+=5
    elif wind>=4: base+=2
    if wave>=6: base+=3
    elif wave>=3: base+=1
    if base>=60: return "🌊最高(大荒)",10
    elif base>=50: return "⚠高(荒)",10
    elif base>=40: return "△中",6
    else: return "✅低(堅)",6

def check_prev_ready(prev):
    """直前情報(展示タイム等)が6艇分きちんと公開されているかを判定する共通関数。"""
    if not prev:
        return False
    raw = prev.get("boats", {})
    if isinstance(raw, list):
        prev_boats = {b.get("racer_boat_number"): b for b in raw if b.get("racer_boat_number")}
    else:
        prev_boats = {int(k): v for k, v in raw.items()}
    return (
        len(prev_boats) >= 6 and
        sum(1 for pv in prev_boats.values() if safe(pv.get("racer_exhibition_time")) > 0) >= 6
    )


def predict_race(sno, rno, prog, prev, model, feats, date_str=None):
    if not prog: return None
    wind  = safe(prev.get("race_wind") if prev else None)
    wave  = safe(prev.get("race_wave") if prev else None)
    weather = WEATHER_NAMES.get(prev.get("race_weather_number",1),"不明") if prev else "不明"
    wind_direction   = prev.get("race_wind_direction_number") if prev else None
    weather_number   = prev.get("race_weather_number") if prev else None
    temperature      = prev.get("race_temperature") if prev else None
    water_temperature= prev.get("race_water_temperature") if prev else None

    prog_boats = {b["racer_boat_number"]:b for b in prog.get("boats",[])}
    prev_boats = {}
    if prev:
        raw = prev.get("boats",{})
        if isinstance(raw,list):
            prev_boats = {b.get("racer_boat_number"):b for b in raw if b.get("racer_boat_number")}
        else:
            prev_boats = {int(k):v for k,v in raw.items()}

    course_map = {bno:int(pv.get("racer_course_number",bno))
                  for bno,pv in prev_boats.items() if pv.get("racer_course_number")}

    # 直前情報(展示タイム等)が実際に公開されているかどうかの判定。
    # 通常、直前情報は締切のおおむね20分前頃から公開される。
    # これが未公開の時間帯に実行すると、天候・展示タイムだけでなくオッズも
    # まだ確定していないため、オッズ取得のリトライ自体が無意味になる。
    prev_ready = check_prev_ready(prev)

    ex_times = {bno:safe(pv.get("racer_exhibition_time"))
                for bno,pv in prev_boats.items()
                if safe(pv.get("racer_exhibition_time"))>0}
    ex_rank_map = {bno:i+1 for i,(bno,_) in enumerate(sorted(ex_times.items(),key=lambda x:x[1]))}

    # ex_relative: レース内の展示タイムの相対順位(平均との差を%で表す)。
    # 以前はライブ予想時に0.0固定だったが、直前情報が揃っていれば計算できるため修正。
    if len(ex_times) > 1:
        _ex_mean = sum(ex_times.values()) / len(ex_times)
        ex_relative_map = {bno: (_ex_mean - et) / _ex_mean * 100 for bno, et in ex_times.items()}
    else:
        ex_relative_map = {}

    # class_rank_in_race: レース内での級別順位(1が最上位)。
    # 以前はライブ予想時に自分の級番号をそのまま使っていた(近似値)が、
    # 6艇全体を比較した正しい順位を計算するよう修正。
    _class_map = {bno: pb.get("racer_class_number", 3) for bno, pb in prog_boats.items()}
    class_rank_map = {bno: 1 + sum(1 for vv in _class_map.values() if vv < c)
                       for bno, c in _class_map.items()}

    c1_bno  = next((bno for bno,c in course_map.items() if c==1),1)
    c1_cls  = prog_boats.get(c1_bno,{}).get("racer_class_number",3)
    c1_exr  = ex_rank_map.get(c1_bno,99)
    upset_label, _ = judge_upset(c1_cls, c1_exr, wind, wave)

    if not date_str:
        date_str = str(date.today())

    fan_df = get_fan_df()

    scored = []
    model_prob_raw = {}
    for bno, pb in prog_boats.items():
        pv   = prev_boats.get(bno)
        course = course_map.get(bno, bno)
        is_c1  = 1 if course==1 else 0
        exr    = ex_rank_map.get(bno, 99)
        name   = pb.get("racer_name", f"{bno}号艇")
        cls_n  = CLASS_NAMES.get(pb.get("racer_class_number"),"?")
        et     = safe(pv.get("racer_exhibition_time") if pv else None)

        if model and feats:
            fv   = build_feature_vector(pb, pv, exr, is_c1, c1_cls,
                                        wind, wave, sno, feats,
                                        boat_no=bno, course=course, date_str=date_str,
                                        wind_direction=wind_direction, weather_number=weather_number,
                                        temperature=temperature, water_temperature=water_temperature,
                                        fan_df=fan_df,
                                        ex_relative=ex_relative_map.get(bno, 0.0),
                                        class_rank_in_race=class_rank_map.get(bno, 3))
            prob = model.predict_proba([fv])[0][1]
            score = prob * 100
        else:
            prob = None
            score = (8-pb.get("racer_class_number",3))*3 + safe(pb.get("racer_national_top_1_percent"))*0.8
            if is_c1: score += 5

        if prob is not None:
            model_prob_raw[bno] = prob
        scored.append((bno, name, cls_n, score, et))

    scored.sort(key=lambda x:x[3], reverse=True)

    # --- 実オッズを取得してedgeベースで3連単を選定 ---
    picks = []
    edge_info = []
    tansho = {}
    if model_prob_raw and len(model_prob_raw) == 6:
        if not prev_ready:
            print(f"  🕒 直前情報未公開のためオッズ取得をスキップします({sno}場{rno}R)。"
                  f"締切20分前頃を目安に再実行してください。")
        else:
            date_compact = date_str.replace("-", "")
            tansho = fetch_odds_with_retry(date_compact, sno, rno)

        if len(tansho) == 6:
            total_raw = sum(model_prob_raw.values())
            model_prob = {b: v/total_raw for b, v in model_prob_raw.items()}
            implied = {b: 1.0/o for b, o in tansho.items() if o > 0}
            total_implied = sum(implied.values())
            market_prob = {b: v/total_implied for b, v in implied.items()} if total_implied > 0 else {}

            boats = list(model_prob.keys())
            combos = []
            skipped = 0
            for i, j, k in itertools.permutations(boats, 3):
                try:
                    m_prob = harville(model_prob, i, j, k)
                    mkt_prob = harville(market_prob, i, j, k) if market_prob else 0.0
                except KeyError:
                    # オッズ側に無い艇番(欠場・差し替え等)を含む組み合わせは計算できないためスキップ
                    skipped += 1
                    continue
                edge = m_prob - mkt_prob
                combos.append((i, j, k, m_prob, mkt_prob, edge))
            if skipped:
                print(f"  ⚠ オッズと予想対象の艇番が一致しない組み合わせを{skipped}件スキップしました"
                      f"({sno}場{rno}R)。オッズ艇番:{sorted(tansho.keys())} 予想艇番:{sorted(model_prob.keys())}")
            combos.sort(key=lambda x: -x[5])
            for i, j, k, m_prob, mkt_prob, edge in combos:
                if edge >= EDGE_THRESHOLD:
                    picks.append(f"{i}-{j}-{k}")
                    edge_info.append((i, j, k, m_prob, mkt_prob, edge))
        elif prev_ready:
            print(f"  ⚠ 単勝オッズが6艇分揃いませんでした({sno}場{rno}R): {tansho}")

    n_picks = len(picks)

    return {
        "date":date_str, "stadium":STADIUM_NAMES.get(sno,"不明"),
        "stadium_no":sno, "race_no":rno,
        "weather":weather, "wind":wind, "wave":wave,
        "upset_label":upset_label, "n_picks":n_picks,
        "ranked":scored, "picks":picks, "edge_info":edge_info,
        "c1_cls":CLASS_NAMES.get(c1_cls,"?"), "c1_ex_rank":c1_exr,
        "prev_ready":prev_ready,
    }

# ============================================================
# 表示
# ============================================================
def format_prediction(r):
    lines = [f"\n{'='*58}"]
    lines.append(f"【{r['stadium']} 第{r['race_no']}R】{r['date']}")
    lines.append(f"天候:{r['weather']} 風:{r['wind']}m 波:{r['wave']}cm")
    lines.append(f"荒れ目安:{r['upset_label']}(参考情報)")
    lines.append(f"1コース:{r['c1_cls']} 展示順位:{r['c1_ex_rank']}位")
    lines.append("")
    lines.append("MLスコアランキング:")
    for i,(bno,name,cls,score,et) in enumerate(r["ranked"][:4],1):
        et_s = f"展示{et:.2f}" if et>0 else "展示未計測"
        lines.append(f"  {i}位: {bno}号艇 {name}({cls}) {score:.1f}% {et_s}")
    lines.append("")
    if r["n_picks"] == 0:
        if not r.get("prev_ready", True):
            lines.append("買い目: 未算出(直前情報がまだ公開されていません。締切20分前頃に再実行してください)")
        else:
            lines.append("買い目: 該当なし(見送り推奨。edge>=0.01の組み合わせが見つかりませんでした)")
    else:
        lines.append(f"買い目(edge>={EDGE_THRESHOLD}, {r['n_picks']}点, 想定投資額{r['n_picks']*100}円):")
        for combo, (i,j,k,m_prob,mkt_prob,edge) in zip(r["picks"], r["edge_info"]):
            lines.append(f"  {combo}  edge={edge:+.3f} (モデル{m_prob*100:.1f}% vs 市場{mkt_prob*100:.1f}%)")
    return "\n".join(lines)

# ============================================================
# 記録（CSV保存）
# ============================================================
RECORD_FIELDS = [
    "date","stadium","race_no","upset","n_picks","picks","max_edge",
    "rank1","rank2","rank3","wind","wave",
    "result","hit","payout","profit","note"
]

def save_prediction(r):
    exists = os.path.exists(RECORD_FILE)
    ranked = r["ranked"]
    edge_info = r.get("edge_info") or []
    max_edge = max((e[5] for e in edge_info), default=0.0)
    new_row = {
        "date":r["date"], "stadium":r["stadium"],
        "race_no":r["race_no"], "upset":r["upset_label"],
        "n_picks":r["n_picks"], "picks":" / ".join(r["picks"]),
        "max_edge": f"{max_edge:.4f}",
        "rank1":f"{ranked[0][0]}号{ranked[0][1]}" if ranked else "",
        "rank2":f"{ranked[1][0]}号{ranked[1][1]}" if len(ranked)>1 else "",
        "rank3":f"{ranked[2][0]}号{ranked[2][1]}" if len(ranked)>2 else "",
        "wind":r["wind"], "wave":r["wave"],
        "result":"","hit":"","payout":"","profit":"","note":"",
    }

    key = (str(r["date"]), str(r["stadium"]), str(r["race_no"]))

    rows = []
    if exists:
        with open(RECORD_FILE, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))

    # 同じ日付・場・レース番号の既存行は削除してから新しい行を追加する(重複防止)
    before = len(rows)
    rows = [row for row in rows
            if (str(row.get("date")), str(row.get("stadium")), str(row.get("race_no"))) != key]
    removed = before - len(rows)
    rows.append(new_row)

    with open(RECORD_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RECORD_FIELDS)
        w.writeheader()
        w.writerows(rows)

    if removed:
        print(f"  ℹ 同じレースの既存予想({removed}件)を置き換えました。")

# ============================================================
# 結果記録（的中判定）
# ============================================================
def record_results(target_date=None):
    if not os.path.exists(RECORD_FILE):
        print("予想記録ファイルがありません。先に predict を実行してください。")
        return

    tdate = target_date or (date.today()-timedelta(days=1)).strftime("%Y%m%d")
    print(f"結果取得中: {tdate}...")
    try:
        data = fetch_results(tdate)
    except Exception as e:
        print(f"取得失敗: {e}"); return

    results = data.get("results",[])
    result_map = {}
    for r in results:
        sno = r["race_stadium_number"]
        rno = r["race_number"]
        boats = r.get("boats",[])
        if isinstance(boats,dict): boats = list(boats.values())
        placed = sorted(
            [b for b in boats if isinstance(b,dict)
             and b.get("racer_place_number") and int(b["racer_place_number"])<=3],
            key=lambda b: int(b["racer_place_number"])
        )
        if len(placed)<3: continue
        combo = f"{placed[0]['racer_boat_number']}-{placed[1]['racer_boat_number']}-{placed[2]['racer_boat_number']}"
        payouts = r.get("payouts",{})
        payout = None
        if isinstance(payouts, dict):
            trifecta_list = payouts.get("trifecta", [])
            if trifecta_list:
                payout = trifecta_list[0].get("payout")
        elif isinstance(payouts, list):
            # 念のための旧形式フォールバック
            t = next((p for p in payouts if isinstance(p,dict) and p.get("payout_type")=="3T"),None)
            if t: payout = t.get("payout_amount")
        result_map[(sno,rno)] = (combo, payout)

    # 選手ごとの直近成績キャッシュを更新する(番組表データが必要)
    try:
        prog_data = fetch_programs(tdate)
        programs_for_day = prog_data.get("programs", [])
        programs_idx_for_day = {
            (p["race_date"], p["race_stadium_number"], p["race_number"]): p
            for p in programs_for_day
        }
        update_racer_form_cache(results, programs_idx_for_day)
        print(f"  選手フォームキャッシュ更新完了")
    except Exception as e:
        print(f"  ⚠ 選手フォームキャッシュの更新に失敗しました: {e}")

    # CSV更新
    rows = []
    updated = hit = total = 0
    with open(RECORD_FILE,encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        if row["date"] != tdate[:4]+"-"+tdate[4:6]+"-"+tdate[6:]: continue
        sno = next((k for k,v in STADIUM_NAMES.items() if v==row["stadium"]),None)
        rno = int(row["race_no"])
        if not sno: continue
        key = (sno,rno)
        if key not in result_map: continue
        combo, payout = result_map[key]
        picks = row["picks"].split(" / ") if row["picks"] else []
        is_hit = combo in picks
        row["result"]  = combo
        row["hit"]     = "○" if is_hit else "×"
        row["payout"]  = payout or ""

        # 回収計算（edge方式は1点あたり均一100円。旧方式の段階的配点は使わない）
        # cost計算は picks欄の分割数ではなく、信頼できる n_picks列を使う
        # (過去、picks欄とn_picksが食い違っているレコードが見つかったため)
        n_picks_val = int(row["n_picks"]) if row["n_picks"] else len(picks)
        cost = n_picks_val * 100
        if is_hit:
            gain = (payout or 0) - cost
            row["profit"] = gain
        else:
            row["profit"] = -cost if cost else ""

        updated += 1
        if is_hit: hit += 1
        total += 1

    if updated==0:
        print("更新対象レースが見つかりませんでした"); return

    with open(RECORD_FILE,"w",encoding="utf-8-sig",newline="") as f:
        w = csv.DictWriter(f, fieldnames=RECORD_FIELDS)
        w.writeheader(); w.writerows(rows)

    print(f"結果記録完了: {updated}レース更新")
    print(f"的中率: {hit}/{total} = {hit/total*100:.1f}%")

# ============================================================
# 日次レポート
# ============================================================
def show_report(target_date=None):
    if not os.path.exists(RECORD_FILE):
        print("記録ファイルがありません"); return

    with open(RECORD_FILE,encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["hit"] in ["○","×"]]

    if target_date:
        tdate_fmt = target_date[:4]+"-"+target_date[4:6]+"-"+target_date[6:] if len(target_date)==8 else target_date
        rows = [r for r in rows if r["date"] == tdate_fmt]

    if not rows:
        print("結果記録がまだありません"); return

    total   = len(rows)
    hits    = sum(1 for r in rows if r["hit"]=="○")
    profits = [float(r["profit"]) for r in rows if r["profit"] not in ["",None]]
    total_profit = sum(profits)

    # 投資額計算（edge方式は1点あたり均一100円）
    total_bet = 0
    for r in rows:
        n = int(r["n_picks"]) if r["n_picks"] else 0
        total_bet += n * 100

    total_payout = total_bet + total_profit
    roi = total_payout / total_bet * 100 if total_bet > 0 else 0

    print("\n" + "="*50)
    print("📊 競艇予想 成績レポート" + (f" ({target_date})" if target_date else " (全期間)"))
    print("="*50)
    print(f"総レース数: {total}")
    print(f"的中数:     {hits}  ({hits/total*100:.1f}%)")
    print(f"目標的中率: 50.0%")
    print(f"")
    print(f"総投資額:   ¥{total_bet:,}")
    print(f"総払戻額:   ¥{int(total_payout):,}")
    print(f"損益:       ¥{int(total_profit):+,}")
    print(f"回収率:     {roi:.1f}%  (目標:150%)")
    print("="*50)

    # 直近10レース
    print("\n直近10レース:")
    for r in rows[-10:]:
        mk = "○" if r["hit"]=="○" else "×"
        pay = f"¥{int(float(r['payout'])):,}" if r["payout"] else "-"
        print(f"  {mk} {r['date']} {r['stadium']}{r['race_no']}R "
              f"{r.get('result','-')} 払戻:{pay}")

def show_edge_report(target_date=None, thresholds=None):
    """edge(max_edge)の閾値ごとに、対象レース数・的中率・回収率を比較表示する。
    note記事用にどの閾値で足切りするのが良いかを判断するためのコマンド。"""
    if not os.path.exists(RECORD_FILE):
        print("記録ファイルがありません"); return

    with open(RECORD_FILE, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["hit"] in ["○","×"]]

    if target_date:
        tdate_fmt = target_date[:4]+"-"+target_date[4:6]+"-"+target_date[6:] if len(target_date)==8 else target_date
        rows = [r for r in rows if r["date"] == tdate_fmt]

    if not rows:
        print("結果記録がまだありません"); return

    if thresholds is None:
        thresholds = [0.00, 0.01, 0.02, 0.03, 0.05]

    def get_edge(r):
        try:
            return float(r.get("max_edge") or 0.0)
        except ValueError:
            return 0.0

    has_edge_data = any(r.get("max_edge") not in (None, "") for r in rows)

    print("\n" + "="*74)
    print("📊 edge閾値別 回収率レポート" + (f" ({target_date})" if target_date else " (全期間)"))
    print("="*74)
    if not has_edge_data:
        print("⚠ max_edge が記録されているレースがありません(過去の古い予想のため)。")
        print("   新しく predict したレースから反映されます。\n")

    print(f"{'閾値':^8}|{'対象数':^8}|{'的中数':^8}|{'的中率':^9}|{'投資額':^12}|{'払戻額':^12}|{'回収率':^9}")
    print("-"*74)
    for th in thresholds:
        sub = [r for r in rows if get_edge(r) >= th]
        n = len(sub)
        if n == 0:
            print(f"{th:^8.2f}|{0:^8}|{'-':^8}|{'-':^9}|{'-':^12}|{'-':^12}|{'-':^9}")
            continue
        hits = sum(1 for r in sub if r["hit"] == "○")
        total_bet = sum((int(r["n_picks"]) if r["n_picks"] else 0) * 100 for r in sub)
        profits = [float(r["profit"]) for r in sub if r["profit"] not in ["", None]]
        total_profit = sum(profits)
        total_payout = total_bet + total_profit
        roi = total_payout / total_bet * 100 if total_bet > 0 else 0
        hit_rate = hits / n * 100
        print(f"{th:^8.2f}|{n:^8}|{hits:^8}|{hit_rate:^8.1f}%|"
              f"¥{total_bet:>9,} |¥{int(total_payout):>9,} |{roi:>7.1f}% ")
    print("="*74)
    print("※「対象数」はそのレースの最大edgeが閾値以上だったレース数(該当レースの買い目全点をまとめて集計)")
    print("※ note記事で使う閾値の目安に。回収率が高く、対象数もある程度確保できる閾値を選んでください。")


def show_kelly_backtest(target_date=None, strategies=None):
    """
    【簡易版】レース単位の max_edge を強さの指標として賭け金を傾斜させた場合、
    既に記録済みの結果(hit/payout)を使って過去データ上でどうなっていたかを
    バックテストする(現状の均等ベットとの比較)。

    的中判定・払戻オッズ自体は変えず、「その組み合わせに何円賭けていたら」を
    仮想的に再計算するだけなので、新しいデータ取得なしにすぐ試せる。

    【重要な注意】これは本来のKelly基準(買い目ごとの推定確率×オッズから最適
    賭け金比率を計算する方式)ではない。ここでの「傾斜」は、レース単位の
    max_edge(そのレースで一番edgeが高かった1点のスカラー値)を強さの目安として
    レース全体の賭け金を一律で増減させているだけで、買い目1点ごとの強弱には
    対応できていない。買い目ごとの確率・オッズを記録するようになれば、
    本格的なKelly計算に置き換えられる。
    """
    if not os.path.exists(RECORD_FILE):
        print("記録ファイルがありません"); return

    with open(RECORD_FILE, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["hit"] in ["○","×"]]

    if target_date:
        tdate_fmt = target_date[:4]+"-"+target_date[4:6]+"-"+target_date[6:] if len(target_date)==8 else target_date
        rows = [r for r in rows if r["date"] == tdate_fmt]

    if not rows:
        print("結果記録がまだありません"); return

    def get_edge(r):
        try:
            return float(r.get("max_edge") or 0.0)
        except ValueError:
            return 0.0

    # 傾斜配分の候補。各関数は edge(レース単位のmax_edge) を受け取り、
    # 「100円の何倍賭けるか」の倍率を返す。数値は仮の目安値なので、
    # 結果を見ながら調整すること。
    if strategies is None:
        strategies = {
            "均等(現状)":          lambda e: 1.0,
            "傾斜・控えめ":         lambda e: 1.0 + min(e, 0.10) * 10,   # edge0.05→1.5倍
            "傾斜・積極的":         lambda e: 1.0 + min(e, 0.10) * 30,   # edge0.05→2.5倍
            "階段(1/1.5/2/3/5倍)":  lambda e: (5.0 if e >= 0.05 else 3.0 if e >= 0.03 else
                                              2.0 if e >= 0.02 else 1.5 if e >= 0.01 else 1.0),
        }

    print("\n" + "="*78)
    print("📊 簡易Kelly風・傾斜配分バックテスト" + (f" ({target_date})" if target_date else " (全期間)"))
    print("   ※ レース単位のmax_edgeによる傾斜であり、買い目ごとの本格Kellyではない")
    print("="*78)
    print(f"{'戦略':^20}|{'投資額':^14}|{'払戻額':^14}|{'損益':^14}|{'回収率':^9}")
    print("-"*78)

    for name, f_mult in strategies.items():
        total_bet = 0.0
        total_payout = 0.0
        for r in rows:
            e = get_edge(r)
            mult = f_mult(e)
            n_picks = int(r["n_picks"]) if r["n_picks"] else 0
            unit = 100 * mult
            cost = n_picks * unit
            total_bet += cost
            if r["hit"] == "○" and r["payout"] not in ("", None):
                total_payout += float(r["payout"]) / 100 * unit
        total_profit = total_payout - total_bet
        roi = total_payout / total_bet * 100 if total_bet > 0 else 0
        print(f"{name:^20}|¥{total_bet:>11,.0f} |¥{total_payout:>11,.0f} |¥{total_profit:>+11,.0f} |{roi:>7.1f}% ")

    print("="*78)
    print(f"対象レース数: {len(rows)}")
    print("※ 均等ベット時に実際に的中した結果を使い、賭け金だけ仮想的に変えて再計算しています。")
    print("   的中判定・オッズ自体はどの戦略でも同じです。")

def repair_profit():
    """既存レコードのprofit値を、信頼できるn_picks列を使って再計算し直す。
    (過去、picks欄の分割数とn_picks列が食い違っているレコードがあり、投資額計算が誤っていたため)
    公式結果の再取得は不要。保存済みのpayout/hit/n_picksから再計算するだけ。"""
    if not os.path.exists(RECORD_FILE):
        print("記録ファイルがありません"); return

    with open(RECORD_FILE, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    fixed = 0
    for row in rows:
        if row.get("hit") not in ("○","×"):
            continue
        try:
            n_picks_val = int(row["n_picks"]) if row["n_picks"] else 0
        except ValueError:
            continue
        cost = n_picks_val * 100
        if cost == 0:
            continue

        try:
            payout = float(row["payout"]) if row["payout"] not in ("", None) else 0
        except ValueError:
            payout = 0

        if row["hit"] == "○":
            new_profit = payout - cost
        else:
            new_profit = -cost

        old_profit = row.get("profit")
        try:
            old_profit_val = float(old_profit) if old_profit not in ("", None) else None
        except ValueError:
            old_profit_val = None

        if old_profit_val is None or abs(old_profit_val - new_profit) > 0.5:
            row["profit"] = new_profit
            fixed += 1

    with open(RECORD_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RECORD_FIELDS)
        w.writeheader(); w.writerows(rows)

    print(f"修復完了: {fixed}件のprofit値を再計算しました。")

def diagnose_records():
    """profit値が理論上あり得ない範囲(-n_picks*100より下)になっている行を探す診断コマンド。"""
    if not os.path.exists(RECORD_FILE):
        print("記録ファイルがありません"); return

    with open(RECORD_FILE, encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r["hit"] in ["○","×"]]

    problems = []
    for r in rows:
        try:
            n = int(r["n_picks"]) if r["n_picks"] else 0
            cost = n * 100
            profit = float(r["profit"]) if r["profit"] not in ("", None) else None
        except ValueError:
            problems.append((r, "数値変換エラー"))
            continue
        if profit is None:
            continue
        if profit < -cost - 1:  # 誤差許容
            problems.append((r, f"profit({profit})が-cost({-cost})より下回っている"))
        if r["hit"] == "○" and profit <= -cost:
            problems.append((r, f"的中(○)なのにprofit({profit})が最大損失(-{cost})のまま=払戻額が反映されていない可能性"))

    print(f"総レコード数(結果あり): {len(rows)}")
    print(f"疑わしい行: {len(problems)}件\n")
    for r, reason in problems[:20]:
        print(f"  {r.get('date')} {r.get('stadium')}{r.get('race_no')}R "
              f"n_picks={r.get('n_picks')} payout={r.get('payout')!r} profit={r.get('profit')!r} hit={r.get('hit')}")
        print(f"    → {reason}")
    if len(problems) > 20:
        print(f"  ...他{len(problems)-20}件")

def dedupe_records():
    """既存のprediction_record.csvに溜まった重複行(同一日付・場・レース番号)を掃除する。
    結果(hit)が記録済みの行を優先して残し、無ければ最後に予想した行を残す。"""
    if not os.path.exists(RECORD_FILE):
        print("記録ファイルがありません"); return

    with open(RECORD_FILE, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    before = len(rows)
    best = {}
    order = []
    for row in rows:
        key = (row.get("date"), row.get("stadium"), row.get("race_no"))
        if key not in best:
            order.append(key)
            best[key] = row
        else:
            # 結果が記録済みの行を優先。無ければ後勝ち(最後に予想した行)で上書き。
            if row.get("hit") in ("○","×") or best[key].get("hit") not in ("○","×"):
                best[key] = row

    deduped = [best[k] for k in order]
    removed = before - len(deduped)

    with open(RECORD_FILE, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=RECORD_FIELDS)
        w.writeheader(); w.writerows(deduped)

    print(f"重複掃除完了: {before}行 → {len(deduped)}行 ({removed}件の重複を削除しました)")

# ============================================================
# メイン
# ============================================================
def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "predict"

    if cmd == "report":
        rdate = args[1] if len(args) > 1 else None
        show_report(rdate); return

    if cmd == "edge_report":
        edate = args[1] if len(args) > 1 else None
        custom_thresholds = [float(x) for x in args[2:]] if len(args) > 2 else None
        show_edge_report(edate, custom_thresholds); return

    if cmd == "kelly_backtest":
        kdate = args[1] if len(args) > 1 else None
        show_kelly_backtest(kdate); return

    if cmd == "dedupe":
        dedupe_records(); return

    if cmd == "diagnose":
        diagnose_records(); return

    if cmd == "repair":
        repair_profit(); return

    if cmd == "record":
        tdate = args[1] if len(args)>1 else None
        record_results(tdate); return

    if cmd in ("predict","all"):
        target_stadium = int(args[1]) if len(args)>1 and args[1].isdigit() else None
        target_race    = int(args[2]) if len(args)>2 and args[2].isdigit() else None

        model, feats = load_model()
        print("データ取得中...")
        try:
            prog_data, prev_data = fetch_today()
        except Exception as e:
            print(f"データ取得失敗: {e}"); return

        all_progs = prog_data.get("programs",[])
        prev_idx  = {(p["race_stadium_number"],p["race_number"]):p
                     for p in prev_data.get("previews",[])}

        targets = [
            (p["race_stadium_number"],p["race_number"])
            for p in all_progs
            if (not target_stadium or p["race_stadium_number"]==target_stadium)
            and (not target_race    or p["race_number"]==target_race)
        ]

        if not targets:
            print("対象レースが見つかりませんでした"); return

        prog_idx = {(p["race_stadium_number"],p["race_number"]):p for p in all_progs}

        # 1レースだけを指定した実行(締切直前運用)の場合のみ、
        # 直前情報がまだ公開されていなければ来るまで待つ。
        # 複数レース一括実行では待たず、従来通り即座に判定する。
        single_race_mode = (target_stadium is not None and target_race is not None and len(targets) == 1)
        if single_race_mode:
            sno0, rno0 = targets[0]
            prev0 = prev_idx.get((sno0, rno0))

            # B案: macour.jpから直前情報を直接取得する(優先)
            macour_ok = False
            try:
                import macour_scraper
                today_compact = date.today().strftime("%Y%m%d")
                print("  🌐 macour.jpから直前情報の取得を試みます...")
                driver = macour_scraper.make_driver(headless=True)
                try:
                    m_data = macour_scraper.fetch_beforeinfo(driver, today_compact, sno0, rno0)
                finally:
                    driver.quit()
                macour_prev = macour_scraper.build_prev_dict(m_data)
                if check_prev_ready(macour_prev):
                    prev0 = macour_prev
                    prev_idx[(sno0, rno0)] = macour_prev
                    macour_ok = True
                    print("  ✅ macour.jpから直前情報を取得しました。")
                else:
                    print("  ⚠ macour.jpからの取得に失敗、またはデータ不足です。")
            except Exception as e:
                print(f"  ⚠ macour.jp取得エラー: {e}")

            # A案: macourで取得できなかった場合、boatraceopenapiが来るまで待つ(フォールバック)
            if not macour_ok:
                print("  → A案(boatraceopenapi待機)にフォールバックします。")
                attempt = 0
                while not check_prev_ready(prev0) and attempt < PREV_RETRY_MAX:
                    print(f"  🕒 直前情報未公開({sno0}場{rno0}R)。"
                          f"{PREV_RETRY_WAIT}秒後に再取得します({attempt+1}/{PREV_RETRY_MAX})...")
                    time.sleep(PREV_RETRY_WAIT)
                    try:
                        prog_data, prev_data = fetch_today()
                        all_progs = prog_data.get("programs", [])
                        prev_idx  = {(p["race_stadium_number"],p["race_number"]):p
                                     for p in prev_data.get("previews",[])}
                        prog_idx  = {(p["race_stadium_number"],p["race_number"]):p for p in all_progs}
                        prev0 = prev_idx.get((sno0, rno0))
                    except Exception as e:
                        print(f"  ⚠ 再取得に失敗しました: {e}")
                    attempt += 1

        print(f"対象: {len(targets)}レース\n")
        results = []
        for sno,rno in sorted(targets):
            prog = prog_idx.get((sno,rno))
            prev = prev_idx.get((sno,rno))
            r = predict_race(sno, rno, prog, prev, model, feats)
            if r:
                print(format_prediction(r))
                save_prediction(r)
                results.append(r)

        print(f"\n✅ {len(results)}レースの予想完了")
        print(f"📝 {RECORD_FILE} に保存しました")

        if cmd == "all":
            print("\n--- 昨日の結果を記録中 ---")
            record_results()
            print("\n--- 成績レポート ---")
            show_report()

if __name__=="__main__":
    main()
