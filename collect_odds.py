# -*- coding: utf-8 -*-
"""
collect_odds.py
公式サイトの単勝オッズページ(締切時オッズ)を過去日付にさかのぼって収集する。
サンドボックスからはboatrace.jpにアクセスできないため、必ずご自身のPCで実行してください。

使い方:
    python collect_odds.py 20260101 20260722
    (開始日 終了日 を指定。既存のbulk_data_v2/results_raw.jsonにあるレースのみ対象にする)

出力:
    bulk_data_v2/odds_raw.json  ← {date, stadium, race_no, boat_no, tansho_odds, fukusho_odds_low, fukusho_odds_high} のリスト

注意:
    - 1レースにつき1リクエスト。54,180レース全件だと数時間かかります。
    - サーバー負荷軽減のため、必ず sleep を入れています（削除しないでください）。
    - 途中で中断しても再開時に「取得済みの日付」はスキップするようにキャッシュしています。
"""
import json, sys, time, os
import requests
import re
from datetime import datetime, timedelta

STADIUM_CODE = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,
    "蒲郡":7,"常滑":8,"津":9,"三国":10,"びわこ":11,"住之江":12,
    "尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,"徳山":18,
    "下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24,
}

RESULTS_PATH = "bulk_data_v2/results_raw.json"
OUT_PATH = "bulk_data_v2/odds_raw.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
SLEEP_SEC = 3.0    # 公式サイトへの配慮。短くしすぎないこと
BACKOFF_SEC = 20.0 # 失敗(タイムアウト等)が起きた直後に待つ延長時間
TIMEOUT_SEC = 12   # requestsのconnect/readタイムアウト(これを超えたら諦めて次へ)


import pandas as pd
from io import StringIO


def fetch_odds_page(date_str, jcd, rno):
    """単勝・複勝オッズページを取得してパースする。pandas.read_htmlで表を抽出。"""
    url = f"https://www.boatrace.jp/owpc/pc/race/oddstf?rno={rno}&jcd={jcd:02d}&hd={date_str}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
    resp.encoding = "utf-8"
    html = resp.text

    tansho = {}
    # ページ先頭の<?xml ... ?>宣言があるとlxmlが解析エラーを起こすため除去する
    html = re.sub(r'^\s*<\?xml[^>]*\?>', '', html, count=1)
    try:
        tables = pd.read_html(StringIO(html), flavor="lxml")
    except ValueError:
        tables = []

    target = None
    for t in tables:
        cols = [str(c) for c in t.columns]
        # 「単勝オッズ」という列名を含む表を探す
        if any("単勝オッズ" in c for c in cols):
            target = t
            break
    if target is None:
        # フォールバック: 6行かつ2〜3列で、数値オッズらしき列を含む表を探す
        for t in tables:
            if len(t) == 6 and t.shape[1] in (2, 3):
                last_col = t.iloc[:, -1]
                try:
                    pd.to_numeric(last_col)
                    target = t
                    break
                except (ValueError, TypeError):
                    continue

    if target is not None:
        boat_col = target.columns[0]
        odds_col = target.columns[-1]
        for _, row in target.iterrows():
            try:
                boat_no = int(row[boat_col])
                odds = float(row[odds_col])
                tansho[boat_no] = odds
            except (ValueError, TypeError):
                continue

    return tansho, html


def load_race_keys():
    """results_raw.jsonから (date, stadium, race_no) の一覧を作る"""
    with open(RESULTS_PATH, encoding="utf-8") as f:
        results = json.load(f)
    keys = [(r["race_date"], r["race_stadium_number"], r["race_number"]) for r in results]
    return keys


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--test":
        # 動作確認モード: python collect_odds.py --test 20260709 20 3
        date_str, jcd, rno = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
        tansho, html = fetch_odds_page(date_str, jcd, rno)
        print("取得結果(艇番: 単勝オッズ):")
        print(tansho)
        if not tansho:
            print("\n⚠ 取得できませんでした。以下はデバッグ用にHTMLの先頭2000文字です:")
            print(html[:2000])
        return

    if len(sys.argv) < 3:
        print("使い方:")
        print("  動作確認: python collect_odds.py --test 20260709 20 3   (日付 場コード レース番号)")
        print("  本番実行: python collect_odds.py 開始日(YYYYMMDD) 終了日(YYYYMMDD)")
        return
    date_from = sys.argv[1]
    date_to = sys.argv[2]

    keys = load_race_keys()
    keys = [k for k in keys if date_from <= k[0].replace("-", "") <= date_to]
    print(f"対象レース数: {len(keys):,}")

    existing = []
    done_keys = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            existing = json.load(f)
        done_keys = {(e["date"], e["stadium"], e["race_no"]) for e in existing}
        print(f"既存データ: {len(existing):,}件（スキップします）")

    out = existing
    n_new = 0
    t_start = time.time()

    def save():
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)

    try:
        consecutive_fails = 0
        for i, (date, stadium, race_no) in enumerate(keys):
            key = (date, stadium, race_no)
            if key in done_keys:
                continue
            date_str = date.replace("-", "")
            t0 = time.time()
            failed = False
            try:
                tansho, _ = fetch_odds_page(date_str, stadium, race_no)
                elapsed = time.time() - t0
                for boat_no, odds in tansho.items():
                    out.append({
                        "date": date, "stadium": stadium, "race_no": race_no,
                        "boat_no": boat_no, "tansho_odds": odds,
                    })
                n_new += 1
                status = "OK" if tansho else "空(取得失敗)"
                failed = not tansho
                print(f"[{i+1}/{len(keys)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  {status}", flush=True)
            except Exception as e:
                elapsed = time.time() - t0
                failed = True
                print(f"[{i+1}/{len(keys)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  失敗: {e}", flush=True)

            # 1レース取得するごとに保存（ログアウト等で強制終了しても最小限のロスで再開できる）
            save()
            if n_new % 20 == 0 and n_new > 0:
                total_elapsed = time.time() - t_start
                print(f"  --- 経過時間{total_elapsed/60:.1f}分  新規取得{n_new}件 ---", flush=True)

            if failed:
                consecutive_fails += 1
                wait = BACKOFF_SEC * min(consecutive_fails, 5)  # 失敗が続くほど待機時間を延ばす(最大5倍)
                print(f"  ⚠ 失敗が続いています({consecutive_fails}回連続)。{wait:.0f}秒待機します...", flush=True)
                time.sleep(wait)
            else:
                consecutive_fails = 0
                time.sleep(SLEEP_SEC)
    except KeyboardInterrupt:
        print("\n中断されました。ここまでの分は保存済みです。同じコマンドで再実行すると続きから再開します。", flush=True)
    finally:
        save()
        print(f"\n現在の保存件数: {len(out):,}件 → {OUT_PATH}")


if __name__ == "__main__":
    main()
