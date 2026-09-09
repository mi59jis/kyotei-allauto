# -*- coding: utf-8 -*-
"""
boatrace_official_scraper.py
boatrace.jp(公式サイト)の「直前情報」ページから、
枠・選手名・体重・展示タイム・チルト・進入コース・ST・水面気象情報を取得するモジュール。

【macour_scraper.pyとの違い】
boatrace.jpの直前情報ページは完全に静的なHTML(サーバー側で描画済みのテーブル)で、
JavaScriptによる遅延読み込みが無い。そのため、Selenium(ブラウザ操作)を使わず、
requests + pandas.read_html だけで取得できる(collect_odds.pyと同じ方式)。
Seleniumのブラウザ起動・待機・スケルトン表示検知といった一連の複雑さが不要になる。

【重要:表の構造について】
1艇につき、実際のHTML上は複数行(rowspanで結合されたセルを含む)にまたがっている。
pandas.read_htmlはrowspanを展開してくれるため、同じ値が複数行に重複して現れる。
そのため、「枠番号(1〜6)が最初に現れた行」だけを採用することで、1艇1行分の
データとして抽出している。

pipeline.py から import して使う想定(macour_scraper.build_prev_dict()と
互換の形式を返すbuild_prev_dict()を用意している)。

使い方(単体テスト):
    python boatrace_official_scraper.py --test 20260828 14 1
    (日付 場コード レース番号)
"""
import re
import sys
import requests
import pandas as pd
from io import StringIO

STADIUM_NAME_TO_CODE = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,
    "蒲郡":7,"常滑":8,"津":9,"三国":10,"びわこ":11,"住之江":12,
    "尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,"徳山":18,
    "下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24,
}

CANCELLED_TEXT = "中止"
CANCELLED = "cancelled"  # fetch_beforeinfo()が中止検知時に返す特別な値

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
TIMEOUT_SEC = 12


def _safe_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_weather(html):
    """「水面気象情報」セクションから天候を抽出する。
    【2026-09-02 修正】実際のHTMLはラベルと数値が別々の<span>に分かれており
    (例: <span class="weather1_bodyUnitLabelTitle">風速</span> ... <span
    class="weather1_bodyUnitLabelData">3m</span>)、間に改行・インデントが
    挟まるため、ラベル直後に数字が来る前提の正規表現ではマッチしなかった。
    ラベルのspanから対応するbodyUnitLabelDataのspanまで(改行含む)飛んでから
    数値を拾うようにする。"""
    weather = {"wind": None, "temperature": None, "water_temperature": None, "wave": None}
    patterns = {
        "wind": r'風速</span>.*?bodyUnitLabelData">\s*(\d+(?:\.\d+)?)',
        "temperature": r'気温</span>.*?bodyUnitLabelData">\s*(\d+(?:\.\d+)?)',
        "water_temperature": r'水温</span>.*?bodyUnitLabelData">\s*(\d+(?:\.\d+)?)',
        "wave": r'波高</span>.*?bodyUnitLabelData">\s*(\d+(?:\.\d+)?)',
    }
    for key, pat in patterns.items():
        m = re.search(pat, html, re.S)
        if m:
            weather[key] = float(m.group(1))
    return weather


def _parse_start_display(html):
    """「スタート展示」表(進入コース・ST)を抽出する。
    戻り値: {艇番(int): {"course_order": int|None, "st_raw": str}}
    実際のHTML構造がまだ完全には確認できていないため、崩れた場合に備えて
    複数のパターンで抽出を試みる(暫定実装)。"""
    result = {}
    try:
        tables = pd.read_html(StringIO(html), flavor="lxml")
    except ValueError:
        return result

    # 「コース」「並び」「ST」を含む表を探す
    target = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        joined = " ".join(cols)
        if "ST" in joined and ("コース" in joined or "並び" in joined):
            target = t
            break
    if target is None:
        return result

    for idx, row in target.iterrows():
        row_text = " ".join(str(v) for v in row.values if pd.notna(v))
        # 行頭の数字をコース番号として拾う(例: "1 .15" → コース1、ST=.15)
        m = re.match(r'^\s*(\d)\s+(F?\.\d{2}|L|K)', row_text)
        if m:
            course = int(m.group(1))
            st_raw = m.group(2)
            result[course] = {"course_order": course, "st_raw": st_raw}
    return result


def fetch_beforeinfo(date_str, jcd, rno, debug=False):
    """
    date_str: 'YYYYMMDD'
    jcd: 場コード(1-24)
    rno: レース番号
    戻り値:
        {"boats": {枠番: {...}}, "weather": {...}}  ... 取得成功
        "cancelled"                                  ... レース中止を検知
        None                                          ... 取得失敗/直前情報まだ未公開
    """
    url = f"https://www.boatrace.jp/owpc/pc/race/beforeinfo?rno={rno}&jcd={jcd:02d}&hd={date_str}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
        resp.encoding = "utf-8"
        html = resp.text
        # 【重要】boatrace.jpのページはXML宣言(<?xml version="1.0" ...?>)付きで
        # 返ってくることがあり、これがあるとlxmlが
        # 「Unicode strings with encoding declaration are not supported」で
        # 解析を拒否する。collect_odds3t.py等で既に確立済みの対処と同じく、
        # 先頭のXML宣言を除去してから渡す。
        html = re.sub(r'^\s*<\?xml[^>]*\?>', '', html, count=1)
    except requests.RequestException as e:
        if debug:
            print(f"⚠ 通信エラー: {e}")
        return None

    if debug:
        print(f"URL: {url}")
        print(f"HTTPステータスコード: {resp.status_code}")
        print(f"最終的なURL(リダイレクトされた場合、元と異なる): {resp.url}")
        print(f"レスポンス本文の長さ: {len(html)}文字")
        print(f"レスポンス本文の先頭500文字:\n{html[:500]}")
        print("---")

    if CANCELLED_TEXT in html and "レースは中止" in html:
        return CANCELLED

    if debug:
        for kw in ("風速", "気温", "水温", "波高"):
            i = html.find(kw)
            if i == -1:
                print(f"[weather debug] キーワード『{kw}』が見つかりません")
            else:
                print(f"[weather debug] 『{kw}』周辺: ...{html[max(0,i-30):i+80]}...")
        print("---")

    try:
        tables = pd.read_html(StringIO(html), flavor="lxml")
    except ValueError as e:
        if debug:
            print(f"⚠ pandas.read_htmlでのパースに失敗しました: {e}")
        tables = []

    if debug:
        print(f"見つかった表の数: {len(tables)}")
        for i, t in enumerate(tables):
            print(f"--- table {i} --- shape={t.shape} columns={list(t.columns)}")

    # 展示タイム・体重・チルトを含む、選手一覧の表を探す
    target = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        joined = " ".join(cols)
        if "展示" in joined and ("体重" in joined or "チルト" in joined):
            target = t
            break

    if target is None:
        return None

    if debug:
        print(f"選手一覧の表として選んだ table の shape: {target.shape}")
        print(f"列名: {list(target.columns)}")
        print(target.to_string())
        print("---")

    # 【2026-09-01 修正】この表(写真列・部品交換列などを含む版)はヘッダーが
    # 2段(MultiIndex)で返ってくることがあり、文字列 "枠" 等での直接アクセスが
    # 期待通りに働かない(タプル列名と衝突する)ケースがあるため、まずトップ
    # レベルだけの通常のIndexへフラット化する。
    if isinstance(target.columns, pd.MultiIndex):
        target = target.copy()
        target.columns = target.columns.get_level_values(0)

    col_waku = "枠"
    col_name = "ボートレーサー"
    col_weight = "体重"
    col_exh = next((c for c in target.columns if "展示" in str(c)), None)
    col_tilt = "チルト"

    # rowspanで結合されていたセルは、行によっては最初の出現行にしか値が
    # 入っておらず、以降の行はNaNになることがある(以前は全行に値が複製
    # されると想定していたが、列構成が変わった影響でズレた可能性がある)。
    # 艇1〜6の判定に使う列だけ前方向に値を埋めておくことで、どちらの
    # 挙動でも安全に「枠番号が最初に現れた行」を拾えるようにする。
    for c in (col_waku, col_name, col_weight, col_exh, col_tilt):
        if c is not None and c in target.columns:
            target[c] = target[c].ffill()

    boats = {}
    for idx, row in target.iterrows():
        try:
            waku = int(row[col_waku])
        except (ValueError, TypeError):
            continue
        if waku in boats or not (1 <= waku <= 6):
            continue  # rowspan展開による重複行はスキップ(最初の出現だけ採用)

        name = str(row[col_name]).strip() if pd.notna(row.get(col_name)) else ""
        weight_raw = str(row[col_weight]) if pd.notna(row.get(col_weight)) else ""
        weight_match = re.search(r'(\d+\.\d+)', weight_raw)

        boats[waku] = {
            "name": name,
            "weight": _safe_float(weight_match.group(1)) if weight_match else None,
            "exhibition_time": _safe_float(row.get(col_exh)) if col_exh else None,
            "tilt": _safe_float(row.get(col_tilt)),
        }

    if debug:
        print(f"抽出できた艇: {len(boats)}艇 → 枠番{sorted(boats.keys())}")

    if len(boats) != 6:
        if debug:
            print(f"⚠ 6艇分そろいませんでした(取得できたのは{len(boats)}艇)")
        return None

    start_display = _parse_start_display(html)
    for waku, info in start_display.items():
        if waku in boats:
            boats[waku]["course_order"] = info["course_order"]
            boats[waku]["st_raw"] = info["st_raw"]
    for waku in boats:
        boats[waku].setdefault("course_order", None)
        boats[waku].setdefault("st_raw", "")

    weather = _parse_weather(html)

    return {"boats": boats, "weather": weather}


def build_prev_dict(result):
    """
    pipeline.py の predict_race が期待する形式に変換する。
    macour_scraper.build_prev_dict() と互換の出力形式。
    """
    if not result:
        return None
    boats_data = result.get("boats", {})
    weather = result.get("weather", {}) or {}
    if not boats_data:
        return None

    boats = []
    for waku, info in boats_data.items():
        boats.append({
            "racer_boat_number": waku,
            "racer_exhibition_time": info.get("exhibition_time"),
            "racer_course_number": info.get("course_order"),
            "racer_tilt_adjustment": info.get("tilt"),
            "racer_start_timing": info.get("st_raw", ""),
        })
    return {
        "boats": boats,
        "race_wind": weather.get("wind"),
        "race_wave": weather.get("wave"),
        "race_weather_number": None,
        "race_wind_direction_number": None,
        "race_temperature": weather.get("temperature"),
        "race_water_temperature": weather.get("water_temperature"),
    }


def main():
    args = sys.argv[1:]
    if len(args) >= 1 and args[0] == "--test":
        date_str, jcd, rno = args[1], int(args[2]), int(args[3])
        result = fetch_beforeinfo(date_str, jcd, rno, debug=True)
        print("\n=== 結果 ===")
        print(result)
        return
    print("使い方: python boatrace_official_scraper.py --test 20260828 14 1")


if __name__ == "__main__":
    main()
