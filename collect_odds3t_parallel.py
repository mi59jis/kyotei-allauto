# -*- coding: utf-8 -*-
"""
collect_odds3t_parallel.py
3連単オッズを10並列で収集する版。
バッチ(10件)単位で同時リクエストし、バッチ内の失敗率が高ければ次のバッチ前に待機する。

使い方:
    構造確認(単発):
        python collect_odds3t_parallel.py --test 20260709 20 3

    本番収集:
        python collect_odds3t_parallel.py 20260101 20260722 3000
        (開始日 終了日 最大取得レース数)

出力:
    bulk_data_v2/odds3t_raw.json       ← オッズ本体(collect_odds3t.pyと共通形式・追記可)
    bulk_data_v2/odds3t_cancelled.json ← 中止レース記録(同上)
"""
import json, sys, time, os, re, random
import requests
import pandas as pd
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

STADIUM_CODE = {
    "桐生":1,"戸田":2,"江戸川":3,"平和島":4,"多摩川":5,"浜名湖":6,
    "蒲郡":7,"常滑":8,"津":9,"三国":10,"びわこ":11,"住之江":12,
    "尼崎":13,"鳴門":14,"丸亀":15,"児島":16,"宮島":17,"徳山":18,
    "下関":19,"若松":20,"芦屋":21,"福岡":22,"唐津":23,"大村":24,
}

RESULTS_PATH = "bulk_data_v2/results_raw.json"
OUT_PATH = "bulk_data_v2/odds3t_raw.json"
CANCELLED_PATH = "bulk_data_v2/odds3t_cancelled.json"
DEBUG_DIR = "debug_odds3t_failures"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}
N_WORKERS = 10
SLEEP_BETWEEN_BATCHES = 1.5   # バッチ間の基本待機(1リクエスト分に相当する程度の配慮)
BACKOFF_SEC = 20.0
TIMEOUT_SEC = 12


def fetch_html(date_str, jcd, rno):
    url = f"https://www.boatrace.jp/owpc/pc/race/odds3t?rno={rno}&jcd={jcd:02d}&hd={date_str}"
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
    resp.encoding = "utf-8"
    html = resp.text
    html = re.sub(r'^\s*<\?xml[^>]*\?>', '', html, count=1)
    return html


def parse_odds3t(html):
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


def fetch_one(key):
    """1レース分を取得してパースする。成功/失敗/中止の種別を含めて返す(スレッドから呼ばれる)"""
    date, stadium, race_no = key
    date_str = date.replace("-", "")
    t0 = time.time()
    try:
        html = fetch_html(date_str, stadium, race_no)
        odds_map = parse_odds3t(html)
        elapsed = time.time() - t0
        if odds_map:
            return {"key": key, "status": "ok", "odds_map": odds_map, "elapsed": elapsed}
        elif "中止になりました" in html:
            return {"key": key, "status": "cancelled", "elapsed": elapsed}
        else:
            return {"key": key, "status": "empty", "elapsed": elapsed, "html": html}
    except Exception as e:
        elapsed = time.time() - t0
        return {"key": key, "status": "network_fail", "elapsed": elapsed, "error": str(e)}


def load_race_keys():
    with open(RESULTS_PATH, encoding="utf-8") as f:
        results = json.load(f)
    return [(r["race_date"], r["race_stadium_number"], r["race_number"]) for r in results]


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--test":
        date_str, jcd, rno = sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
        html = fetch_html(date_str, int(jcd), int(rno))
        odds_map = parse_odds3t(html)
        print(f"取得結果: {len(odds_map)}件")
        if odds_map:
            for k, v in list(odds_map.items())[:5]:
                print(k, v)
        else:
            print("中止レースかどうか:", "中止になりました" in html)
            print(html[:1000])
        return

    if len(sys.argv) < 3:
        print("使い方:")
        print("  動作確認: python collect_odds3t_parallel.py --test 20260709 20 3")
        print("  本番実行: python collect_odds3t_parallel.py 開始日 終了日 [最大件数]")
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

    todo = [k for k in keys if k not in done_keys]
    print(f"今回処理する対象: {len(todo):,}レース  ({N_WORKERS}並列)")

    out = existing
    lock = Lock()
    os.makedirs(DEBUG_DIR, exist_ok=True)

    def save():
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)

    def save_cancelled():
        with open(CANCELLED_PATH, "w", encoding="utf-8") as f:
            json.dump(cancelled, f, ensure_ascii=False)

    n_new = 0
    t_start = time.time()
    batch_size = N_WORKERS
    i = 0
    extra_wait = 0.0

    try:
        with ThreadPoolExecutor(max_workers=N_WORKERS) as executor:
            while i < len(todo):
                batch = todo[i:i + batch_size]
                i += batch_size

                if extra_wait > 0:
                    print(f"  ⚠ 直前バッチで失敗が多かったため{extra_wait:.0f}秒待機します...", flush=True)
                    time.sleep(extra_wait)

                futures = {executor.submit(fetch_one, key): key for key in batch}
                batch_network_fails = 0
                for future in as_completed(futures):
                    res = future.result()
                    key = res["key"]
                    date, stadium, race_no = key
                    elapsed = res["elapsed"]

                    with lock:
                        if res["status"] == "ok":
                            for (b1, b2, b3), odds in res["odds_map"].items():
                                out.append({"date": date, "stadium": stadium, "race_no": race_no,
                                            "b1": b1, "b2": b2, "b3": b3, "odds": odds})
                            n_new += 1
                            print(f"[{i}/{len(todo)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  OK({len(res['odds_map'])}件)", flush=True)
                        elif res["status"] == "cancelled":
                            cancelled.append({"date": date, "stadium": stadium, "race_no": race_no})
                            print(f"[{i}/{len(todo)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  中止レース(スキップ)", flush=True)
                        elif res["status"] == "empty":
                            print(f"[{i}/{len(todo)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  空(取得失敗)", flush=True)
                            fname = f"{DEBUG_DIR}/{date.replace('-','')}_{stadium}_{race_no}.html"
                            with open(fname, "w", encoding="utf-8") as f:
                                f.write(res["html"])
                        else:  # network_fail
                            batch_network_fails += 1
                            print(f"[{i}/{len(todo)}] {date} 場{stadium} {race_no}R  {elapsed:.1f}秒  失敗: {res['error']}", flush=True)

                # バッチごとに保存
                with lock:
                    save()
                    save_cancelled()

                if n_new and n_new % 50 < batch_size:
                    total_elapsed = time.time() - t_start
                    print(f"  --- 経過時間{total_elapsed/60:.1f}分  新規取得{n_new}レース ---", flush=True)

                # このバッチの通信失敗率に応じて次バッチ前の待機を決める
                fail_ratio = batch_network_fails / len(batch) if batch else 0
                if fail_ratio >= 0.5:
                    extra_wait = min(extra_wait + BACKOFF_SEC, BACKOFF_SEC * 5) if extra_wait else BACKOFF_SEC
                elif fail_ratio > 0:
                    extra_wait = BACKOFF_SEC / 2
                else:
                    extra_wait = 0.0
                    time.sleep(SLEEP_BETWEEN_BATCHES)

    except KeyboardInterrupt:
        print("\n中断されました。ここまでの分は保存済みです。同じコマンドで再実行すると続きから再開します。", flush=True)
    finally:
        with lock:
            save()
            save_cancelled()
        print(f"\n現在の保存レコード数: {len(out):,}件 → {OUT_PATH}")
        print(f"中止レース記録数: {len(cancelled):,}件 → {CANCELLED_PATH}")


if __name__ == "__main__":
    main()
