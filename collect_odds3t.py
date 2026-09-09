# -*- coding: utf-8 -*-
"""
collect_odds3t.py
公式サイトの3連単オッズページ(締切時オッズ)を収集する。
単勝オッズ(collect_odds.py)と同じ仕組みだが、1レース120通りの組み合わせを扱う。

使い方:
    まず構造確認:
        python collect_odds3t.py --explore 20260709 20 3
        (取得したテーブルの形をすべて表示します。想定通りでなければこの出力を送ってください)

    本番収集:
        python collect_odds3t.py 20260101 20260722 1500
        (開始日 終了日 最大取得レース数)
        最大件数を指定できるのは3連単オッズは点数が多く時間がかかるため。
        ランダムに間引いて指定件数だけ収集します。

出力:
    bulk_data_v2/odds3t_raw.json
"""
import json, sys, time, os, re, random
import requests
import pandas as pd
from io import StringIO

STADIUM_CODE = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,
    "蒲郡":7,"常滑":8,"津":9,"三国":10,"びわこ":11,"住之江":12,
    "尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,"徳山":18,
    "下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24,
}

RESULTS_PATH = "bulk_data_v2/results_raw.json"
OUT_PATH = "bulk_data_v2/odds3t_raw.json"
CANCELLED_PATH = "bulk_data_v2/odds3t_cancelled.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
SLEEP_SEC = 3.0
BACKOFF_SEC = 20.0
TIMEOUT_SEC = 12


def fetch_html(date_str, jcd, rno):
    url = f"https://www.boatrace.jp/owpc/pc/race/odds3t?rno={rno}&jcd={jcd:02d}&hd={date_str}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
    resp.encoding = "utf-8"
    html = resp.text
    html = re.sub(r'^\s*<\?xml[^>]*\?>', '', html, count=1)
    return html


DEBUG_DIR = "debug_odds3t_failures"


def explore(date_str, jcd, rno):
    """テーブル構造を確認するためのデバッグ用関数"""
    html = fetch_html(date_str, jcd, rno)
    try:
        tables = pd.read_html(StringIO(html), flavor="lxml")
    except ValueError as e:
        print(f"read_html失敗: {e}")
        return
    print(f"見つかった表の数: {len(tables)}\n")
    for i, t in enumerate(tables):
        print(f"--- table {i} --- shape={t.shape}")
        print(f"columns: {t.columns.tolist()}")
        print(t.head(10).to_string())
        print()


def parse_odds3t(html):
    """
    3連単オッズ表をパースして {(1着,2着,3着): オッズ} の辞書を返す。
    ページ構造: 6ブロック(1着ごと)が横に並び、各ブロック3列(2着,3着,オッズ)×20行。
    各ブロックの1列目の列名がその1着艇番号の文字列になっている。
    """
    try:
        tables = pd.read_html(StringIO(html), flavor="lxml")
    except ValueError:
        return {}

    target = None
    for t in tables:
        if t.shape[1] >= 15 and t.shape[0] >= 15:
            target = t
            break
    if target is None:
        return {}

    result = {}
    n_blocks = target.shape[1] // 3
    for b in range(n_blocks):
        col_start = b * 3
        b1_str = str(target.columns[col_start]).strip()
        try:
            b1 = int(b1_str)
        except ValueError:
            continue

        b2_col = pd.to_numeric(target.iloc[:, col_start], errors='coerce')
        b3_col = pd.to_numeric(target.iloc[:, col_start + 1], errors='coerce')
        odds_col = pd.to_numeric(target.iloc[:, col_start + 2], errors='coerce')

        for idx in range(len(target)):
            b2 = b2_col.iloc[idx]
            b3 = b3_col.iloc[idx]
            odds = odds_col.iloc[idx]
            if pd.notna(b2) and pd.notna(b3) and pd.notna(odds):
                result[(b1, int(b2), int(b3))] = float(odds)
    return result


def load_race_keys():
    with open(RESULTS_PATH, encoding="utf-8") as f:
        results = json.load(f)
    keys = [(r["race_date"], r["race_stadium_number"], r["race_number"]) for r in results]
    return keys


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--explore":
        date_str, jcd, rno = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
        explore(date_str, jcd, rno)
        return

    if len(sys.argv) < 3:
        print("使い方:")
        print("  構造確認: python collect_odds3t.py --explore 20260709 20 3")
        print("  本番実行: python collect_odds3t.py 開始日 終了日 [最大件数]")
        return

    date_from, date_to = sys.argv[1], sys.argv[2]
    max_races = int(sys.argv[3]) if len(sys.argv) > 3 else None

    keys = load_race_keys()
    keys = [k for k in keys if date_from <= k[0].replace("-", "") <= date_to]
    print(f"対象レース数(絞り込み前): {len(keys):,}")

    if max_races and len(keys) > max_races:
        random.seed(42)
        keys = random.sample(keys, max_races)
        keys.sort()
        print(f"ランダムに{max_races}件に絞り込みました")

    existing = []
    done_keys = set()
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            existing = json.load(f)
        done_keys = {(e["date"], e["stadium"], e["race_no"]) for e in existing}
        print(f"既存データ: {len(done_keys):,}レース分（スキップします）")

    cancelled = []
    if os.path.exists(CANCELLED_PATH):
        with open(CANCELLED_PATH, encoding="utf-8") as f:
            cancelled = json.load(f)
        cancelled_keys = {(c["date"], c["stadium"], c["race_no"]) for c in cancelled}
        done_keys |= cancelled_keys
        print(f"中止レース記録: {len(cancelled_keys):,}レース分（スキップします）")

    out = existing
    n_new = 0
    t_start = time.time()

    def save():
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)

    def save_cancelled():
        with open(CANCELLED_PATH, "w", encoding="utf-8") as f:
            json.dump(cancelled, f, ensure_ascii=False)

    try:
        consecutive_fails = 0
        os.makedirs(DEBUG_DIR, exist_ok=True)
        for i, (date, stadium, race_no) in enumerate(keys):
            key = (date, stadium, race_no)
            if key in done_keys:
                continue
            date_str = date.replace("-", "")
            t0 = time.time()
            failure_type = None  # None / "network" / "empty"
            try:
                html = fetch_html(date_str, stadium, race_no)
                odds_map = parse_odds3t(html)
                elapsed = time.time() - t0
                if odds_map:
                    for (b1, b2, b3), odds in odds_map.items():
                        out.append({
                            "date": date, "stadium": stadium, "race_no": race_no,
                            "b1": b1, "b2": b2, "b3": b3, "odds": odds,
                        })
                    n_new += 1
                    print(f"[{i+1}/{len(keys)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  OK({len(odds_map)}件)", flush=True)
                elif "中止になりました" in html:
                    print(f"[{i+1}/{len(keys)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  中止レース(スキップ)", flush=True)
                    cancelled.append({"date": date, "stadium": stadium, "race_no": race_no})
                    save_cancelled()
                else:
                    failure_type = "empty"
                    print(f"[{i+1}/{len(keys)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  空(取得失敗)", flush=True)
                    # デバッグ用にHTMLを保存(後で構造を確認できるように)
                    fname = f"{DEBUG_DIR}/{date_str}_{stadium}_{race_no}.html"
                    with open(fname, "w", encoding="utf-8") as f:
                        f.write(html)
            except Exception as e:
                elapsed = time.time() - t0
                failure_type = "network"
                print(f"[{i+1}/{len(keys)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  失敗: {e}", flush=True)

            save()
            if n_new % 20 == 0 and n_new > 0:
                total_elapsed = time.time() - t_start
                print(f"  --- 経過時間{total_elapsed/60:.1f}分  新規取得{n_new}レース ---", flush=True)

            if failure_type == "network":
                # 本物の通信エラー/タイムアウトはブロックの可能性があるので長めに待つ
                consecutive_fails += 1
                wait = BACKOFF_SEC * min(consecutive_fails, 5)
                print(f"  ⚠ 通信失敗が続いています({consecutive_fails}回連続)。{wait:.0f}秒待機します...", flush=True)
                time.sleep(wait)
            elif failure_type == "empty":
                # ページ自体は取得できているのでブロックではない可能性が高く、長く待たずに次へ
                consecutive_fails = 0
                time.sleep(SLEEP_SEC)
            else:
                consecutive_fails = 0
                time.sleep(SLEEP_SEC)
    except KeyboardInterrupt:
        print("\n中断されました。ここまでの分は保存済みです。同じコマンドで再実行すると続きから再開します。", flush=True)
    finally:
        save()
        print(f"\n現在の保存レコード数: {len(out):,}件 → {OUT_PATH}")


if __name__ == "__main__":
    main()
