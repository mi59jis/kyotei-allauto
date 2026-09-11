# -*- coding: utf-8 -*-
"""
auto_predict_cycle.py
Windowsタスクスケジューラ / GitHub Actionsから10〜15分おきに実行される想定のスクリプト。
1回の実行で以下を行う:
    1. 各場について、直前情報取得を試みるレースを絞り込む。
       【2026-08-25/26 設計変更、2026-08-27 さらに見直し】fetch_race_schedule.py が
       朝6時に取得する締切時刻表(race_schedule.json)があれば、それを使って
       「締切SCHEDULE_WINDOW_NEAR_MIN分前〜SCHEDULE_WINDOW_FAR_MIN分前のレースだけ」を
       対象にする【狙い撃ちモード】。
       一時期、レースごとにcronで正確なタイミングを狙う方式(1レース1〜2トリガー)を
       試みたが、1日150〜200レース分のトリガーが必要になり、月2,000分の無料枠と
       根本的に相性が悪いことが判明したため撤回した。実行自体はpredict_cycle.yml側の
       固定間隔ポーリング(土日10分おき・平日20分おき)に戻し、その実行間隔でも
       窓を絶対に素通りしないよう、窓の幅を実行間隔以上(20分)に広く取っている。
       1レース目からの総当たりが不要になり、note記事を締切に間に合わせやすくなる。
       時刻表が無い場(取得失敗等)は、従来通り進捗(race_progress.json)の続きから
       順番に確認する【フォールバックモード】に自動的に切り替わる。
       どちらのモードでも、以下の安全弁を維持している:
       - すでに予想記録がある(prediction_record.csv) → 一瞬でスキップ(Selenium不要)
       - すでに結果が出ている(Boatrace Open API) → 一瞬でスキップ(Selenium不要)
       - macour.jp上で「中止になりました」を検知 → スキップして次のレースへ
       - macour.jpが読み込み中のまま応答しない(スケルトン表示)を検知 → 即座に諦める
       - 1つの場・1サイクルあたりの実アクセス回数に上限(WARMUP_ATTEMPT_LIMIT)を設け、
         無限に時間を溶かさないようにする
    2. 予想できたレースのうち、edgeがNOTE_EDGE_THRESHOLD以上のものだけ、
       そのレース単体でnote下書きを新規作成する(1レース1記事方式)

このスクリプト自体は1回実行して終了する(ループしない)。
繰り返し実行はタスクスケジューラ/GitHub Actionsに任せる設計。

【重要】日本時間(JST)の扱いについて:
    GitHub Actionsのランナーはシステム時刻がUTC(協定世界時)で動いている。
    Windowsのローカル実行ではシステム時刻が日本時間のため問題にならなかったが、
    クラウド実行では「今何時か」「今日は何日か」を必ずJSTで明示的に計算する必要がある。
    このスクリプトでは zoneinfo を使って全ての日付・時刻判定をJST基準に統一している。

使い方:
    python auto_predict_cycle.py

ログ・状態ファイル:
    auto_cycle_log.txt      実行結果を追記
    race_progress.json      場ごとの進捗(フォールバックモード用、日付が変わると自動リセット)
    race_schedule.json      場ごとの締切時刻表(fetch_race_schedule.py が生成、当日分のみ使用)
"""
import sys
import os
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")

JST = ZoneInfo("Asia/Tokyo")
PROGRESS_FILE = "race_progress.json"
RACE_SCHEDULE_FILE = "race_schedule.json"

# 締切時刻表(race_schedule.json)を使って「締切が近いレースだけ」を狙い撃ちする際の時間窓。
# 【2026-08-27 設計変更】レースごとにcronで正確なタイミングを狙う方式は、月2,000分の
# 無料枠と根本的に相性が悪い(1日150〜200レース×複数トリガーだと、そもそも
# トリガー回数だけで予算を大幅に超える)ことが判明したため撤回。
# 固定間隔のポーリング(predict_cycle.yml側で土日10分おき・平日20分おき)に戻し、
# その実行間隔でも窓を絶対に素通りしないよう、窓の幅を実行間隔以上(20分)に
# 広く取る方式にしている。
# 窓の近い側(締切に最も近い側)は、note記事化してから読者が舟券を買うまでの
# 猶予を確保するため、締切10分前より内側には踏み込まない。
SCHEDULE_WINDOW_NEAR_MIN = 10   # 締切のこの分数前より内側(直前)は対象外にする
SCHEDULE_WINDOW_FAR_MIN = 30    # 締切のこの分数前より外側(まだ早い)は対象外にする


def now_jst():
    """現在時刻を必ず日本時間で返す(実行環境のシステムタイムゾーンに依存しない)"""
    return datetime.now(JST)


def today_jst_str():
    """今日の日付(日本時間基準)をYYYY-MM-DD文字列で返す"""
    return now_jst().strftime("%Y-%m-%d")


def today_jst_compact():
    """今日の日付(日本時間基準)をYYYYMMDD文字列で返す(macour.jp等への問い合わせ用)"""
    return now_jst().strftime("%Y%m%d")


# ============================================================
# 設定: まずは小規模でテストする(慣れてきたら増やす)
# ============================================================
STADIUM_TARGETS = list(range(1, 25))  # 全24場を対象

# 場ごとのおおよその開催時間帯(24時間表記、日本時間。この時間帯以外はチェックをスキップして無駄なアクセスを減らす)
# 出典: https://kcbn.jp/race-start/ (開門〜レース終了時刻の目安)を元に、前後1時間程度の余裕を持たせてある。
# 実際の開催日・グレードによって前後するので、ズレがあれば随時調整してください。
# ミッドナイト開催(下関・若松・大村、月1回程度)の場合は終了が21:40〜22:40頃になるため、
# 該当3場は終了時刻を長めに取ってあります。
STADIUM_TIME_WINDOWS = {
    # smiyajiさん提供の実際の開催時刻表(開門/レース開始/レース終了)を元に、
    # 「レース開始1時間前」〜「レース終了1時間後」で再計算(2026-08-13更新)
    1:  (14, 21),  # 桐生     開始15:20 終了20:40
    2:  (9, 17),   # 戸田     開始10:50 終了16:30
    3:  (10, 17),  # 江戸川   開始11:15 終了16:30
    4:  (10, 17),  # 平和島   開始11:00 終了16:35
    5:  (11, 18),  # 多摩川   開始12:00 終了17:40
    6:  (10, 18),  # 浜名湖   開始11:30 終了17:10
    7:  (14, 21),  # 蒲郡     開始15:00 終了20:45
    8:  (10, 17),  # 常滑     開始11:00 終了16:50
    9:  (9, 17),   # 津       開始10:30 終了16:15
    10: (7, 15),   # 三国     開始8:40  終了14:20
    11: (9, 17),   # びわこ   開始10:30 終了16:30
    12: (14, 21),  # 住之江   開始15:20 終了20:45
    13: (9, 17),   # 尼崎     開始10:40 終了16:30
    14: (7, 15),   # 鳴門     開始8:40  終了14:20
    15: (14, 21),  # 丸亀     開始15:20 終了20:35
    16: (9, 17),   # 児島     開始10:40 終了16:40
    17: (9, 17),   # 宮島     開始10:50 終了16:50
    18: (7, 15),   # 徳山     開始8:40  終了14:20
    19: (14, 22),  # 下関     開始15:20 終了20:45(ミッドナイト有りのため終了を長めに)
    20: (14, 22),  # 若松     開始15:20 終了20:35(ミッドナイト有りのため終了を長めに)
    21: (7, 15),   # 芦屋     開始8:40  終了14:30
    22: (11, 19),  # 福岡     開始12:30 終了18:00
    23: (7, 15),   # 唐津     開始8:45  終了14:30
    24: (14, 23),  # 大村     開始15:30 終了20:45(ミッドナイト有りのため終了を長めに)
}
DEFAULT_TIME_WINDOW = (8, 21)  # STADIUM_TIME_WINDOWSに無い場のデフォルト(通常は全場網羅済みのため使われない想定)

# note記事化する基準(1レース1記事方式)。このedge以上のレースだけ、その場で新規下書きを作る。
NOTE_EDGE_THRESHOLD = 0.03
# 【2026-09-11追加】Trueにすると、note下書き作成後に実際の「投稿する」まで自動で行う
# (=有料記事として即座に公開・販売される)。何か問題が起きたときは、まずここをFalseに
# 戻せば、以前と同じ「下書きまでは自動、公開は手動」の安全な状態に即座に戻せる。
AUTO_PUBLISH_NOTE = True

LOG_FILE = "auto_cycle_log.txt"


def is_in_time_window(stadium_no, now=None):
    now = now or now_jst()
    start_h, end_h = STADIUM_TIME_WINDOWS.get(stadium_no, DEFAULT_TIME_WINDOW)
    return start_h <= now.hour < end_h


def log(msg):
    line = f"[{now_jst().strftime('%Y-%m-%d %H:%M:%S')} JST] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()


def _safe_quit(driver, name, timeout=15):
    """driver.quit()にタイムアウトを設けて呼び出す。
    withブロックを使うと後片付け時に固まったスレッドの終了を待ってしまうため、
    ここでもwithを使わずshutdown(wait=False)で手放す(make_driver呼び出しと同じ理由)。"""
    if driver is None:
        return
    t0 = time.time()
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(driver.quit)
    try:
        future.result(timeout=timeout)
        log(f"  ⏱ {name}を終了しました({time.time()-t0:.1f}秒)")
    except TimeoutError:
        log(f"  ⚠ {name}の終了処理が{timeout}秒以内に完了しませんでした(処理は続行します)")
    executor.shutdown(wait=False)


def load_progress(today_str):
    """場ごとに『何レースまで確認済みか』を保存した進捗ファイルを読み込む。
    日付が変わっていれば自動的にリセットする。ファイルが壊れている/無い場合も
    空の進捗として扱う(進捗はあくまでヒントであり、無くても既存の安全弁で動作するため)。"""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("date") == today_str:
                return state.get("progress", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_progress(today_str, progress):
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump({"date": today_str, "progress": progress}, f, ensure_ascii=False)
    except OSError as e:
        log(f"  ⚠ 進捗ファイルの保存に失敗しました: {e}")


def load_race_schedule(today_str):
    """fetch_race_schedule.pyが朝に生成した締切時刻表を読み込む。
    ファイルが無い/日付が違う/壊れている場合は空dictを返す
    (呼び出し側は、その場のスケジュールが空なら従来の進捗ベース方式にフォールバックする)。"""
    if not os.path.exists(RACE_SCHEDULE_FILE):
        return {}
    try:
        with open(RACE_SCHEDULE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("date") == today_str:
            return data.get("schedule", {})
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def get_candidate_race_numbers(stadium_schedule, total_races, now):
    """締切時刻表から、『今まさに狙い撃ちすべきレース番号』のリストを返す。
    締切のSCHEDULE_WINDOW_NEAR_MIN分前〜SCHEDULE_WINDOW_FAR_MIN分前の範囲にあるレースを対象とする
    (固定間隔ポーリングの実行間隔以上の幅を持たせ、素通りしないようにしている)。"""
    candidates = []
    for rno in range(1, total_races + 1):
        t_str = stadium_schedule.get(str(rno))
        if not t_str:
            continue
        try:
            hh, mm = map(int, t_str.split(":"))
        except (ValueError, AttributeError):
            continue
        close_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta_min = (close_dt - now).total_seconds() / 60  # 締切まであと何分か(正=まだ先、負=過ぎた)
        if SCHEDULE_WINDOW_NEAR_MIN <= delta_min <= SCHEDULE_WINDOW_FAR_MIN:
            candidates.append(rno)
    return candidates


def main():
    log("="*60)
    log("auto_predict_cycle 開始")
    t_start = time.time()

    import pipeline  # pipeline.py の関数を再利用する

    # 【高速化】モデル読み込み・当日データ取得(2件)・結果データ取得は互いに独立しているため、
    # 順番に待たず並列実行する。ネットワーク通信が遅い場合、直列だと最悪45秒+モデル読み込み分
    # 待つことになるが、並列化により最悪でも一番遅い1件分(15秒程度)で済むようになる。
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_model = executor.submit(pipeline.load_model)
        future_prog = executor.submit(
            pipeline.fetch_json, "https://boatraceopenapi.github.io/programs/v2/today.json")
        future_prev = executor.submit(
            pipeline.fetch_json, "https://boatraceopenapi.github.io/previews/v2/today.json")
        future_results = executor.submit(pipeline.fetch_results)

        try:
            model, feats = future_model.result()
        except Exception as e:
            log(f"⚠ モデル読み込みでエラー: {e}")
            return
        if model is None:
            log("⚠ モデルが読み込めませんでした。中止します。")
            return
        log(f"  ⏱ モデル読み込み完了({time.time()-t_start:.1f}秒経過)")

        try:
            prog_data = future_prog.result()
            prev_data = future_prev.result()
        except Exception as e:
            log(f"⚠ 当日データ取得に失敗しました: {e}")
            return
        log(f"  ⏱ 当日データ取得完了({time.time()-t_start:.1f}秒経過)")

        try:
            results_data = future_results.result()
        except Exception as e:
            log(f"  ℹ 本日の結果データ取得に失敗しました(スキップ判定なしで続行): {e}")
            results_data = {}
        log(f"  ⏱ 結果データ取得完了({time.time()-t_start:.1f}秒経過)")

    all_progs = prog_data.get("programs", [])
    prog_idx = {(p["race_stadium_number"], p["race_number"]): p for p in all_progs}

    today_str = today_jst_str()
    progress = load_progress(today_str)
    race_schedule = load_race_schedule(today_str)
    n_stadiums_with_schedule = sum(1 for v in race_schedule.values() if v)
    log(f"  ⏱ 進捗・時刻表読み込み完了({time.time()-t_start:.1f}秒経過、"
        f"進捗記録済み{len(progress)}場、時刻表あり{n_stadiums_with_schedule}場)")

    # 場ごとの本日の総レース数を数える(個数ではなく最大レース番号を使う。
    # レース番号が歯抜けになっていても最後のレースまで処理できるようにするため)
    races_by_stadium = {}
    for (sno, rno) in prog_idx.keys():
        races_by_stadium.setdefault(sno, set()).add(rno)
    max_race_no = {sno: max(nos) for sno, nos in races_by_stadium.items()}

    # 既に記録済みのレースを調べる(状態ファイルが無い/ズレた場合の安全網)。
    # 【高速化】csv.DictReaderで1行ずつPythonループするのではなく、pandasのCパーサーで
    # 読み込む。日々蓄積されるprediction_record.csvが大きくなるほど効果が大きい。
    already_done_keys = set()
    if os.path.exists(pipeline.RECORD_FILE):
        try:
            import pandas as pd
            df = pd.read_csv(
                pipeline.RECORD_FILE,
                usecols=["date", "stadium", "race_no"],
                dtype=str,
                encoding="utf-8-sig",
            )
            today_df = df[df["date"] == today_str]
            already_done_keys = set(zip(today_df["stadium"], today_df["race_no"].astype(str)))
        except Exception as e:
            log(f"  ⚠ 予想記録CSV読み込みでエラー: {e}(既存スキップ判定なしで続行)")
    log(f"  ⏱ 予想記録CSV読み込み完了({time.time()-t_start:.1f}秒経過、"
        f"本日分{len(already_done_keys)}件)")

    # 本日すでに結果が出ているレースを把握しておく(Boatrace Open APIの結果データを利用)。
    # これが分かれば、すでに終わったレースにmacour.jp経由のSelenium取得を試みる無駄を省ける。
    # (結果データの更新は約30分間隔で遅延があるため、無かった場合のフォールバックは後段の
    #  「ウォームアップ」ロジックで別途カバーする)
    #
    # 【重要】結果データは当日の全レース分の「枠」が最初から用意されており、
    # まだ確定していないレースは racer_place_number(着順)が空のまま入っている。
    # そのため「resultsリストに存在するか」ではなく、record_results()と同じ基準
    # (上位3艇の着順が実際に埋まっているか)で判定する必要がある。
    finished_keys = set()
    raw_results = (results_data or {}).get("results", [])
    for r in raw_results:
        boats = r.get("boats", [])
        if isinstance(boats, dict):
            boats = list(boats.values())
        n_placed = 0
        for b in boats:
            if not isinstance(b, dict):
                continue
            try:
                if b.get("racer_place_number") and int(b["racer_place_number"]) <= 3:
                    n_placed += 1
            except (TypeError, ValueError):
                continue
        if n_placed >= 3:
            # 【重要】結果APIのrace_stadium_number/race_numberが文字列型で返る場合と
            # 数値型で返る場合があり得るため、必ずintに正規化してからキーに使う。
            # 型が食い違ったまま照合すると、setに存在していても一致せず、
            # 終了済みのレースを毎回実アクセスでチェックしてしまう不具合につながる
            # (2026-08-20、ウォームアップ空振りが多発した際に発覚)。
            try:
                key = (int(r["race_stadium_number"]), int(r["race_number"]))
                finished_keys.add(key)
            except (TypeError, ValueError, KeyError):
                continue
    log(f"  ⏱ 結果データ集計完了({time.time()-t_start:.1f}秒経過、"
        f"API取得件数{len(raw_results)}件中、終了済み{len(finished_keys)}件)")

    n_success = 0
    n_skipped_done = 0
    n_skipped_not_ready = 0
    n_skipped_finished_no_predict = 0
    n_skipped_cancelled = 0
    n_error = 0
    n_skipped_out_of_window = 0
    n_silent_not_ready = 0  # ウォームアップ中(まだ生きているレースに追いついていない間)の空振り件数

    # このサイクルで実際にチェックが必要な場があるか先に判定し、必要な場合のみ
    # Chromeを1つだけ起動して使い回す(起動・終了のたびに数秒かかるため)
    need_check = any(
        is_in_time_window(sno) for sno in STADIUM_TARGETS if max_race_no.get(sno, 0) > 0
    )
    log(f"  ⏱ 時間帯判定完了({time.time()-t_start:.1f}秒経過、要チェック={need_check})")

    # 【2026-08-28 設計変更(未検証)】直前情報の取得元をmacour.jp(Selenium必須)から
    # boatrace.jp公式サイト(requests+pandasのみ、Selenium不要)に切り替えた。
    # これにより、ブラウザ起動・スケルトン表示検知・stale element対策・タイムアウト等、
    # Selenium固有の複雑さが丸ごと不要になった。
    # 【重要】boatrace_official_scraper.pyはまだ実データでの動作確認が完了していない。
    # 本番投入前に、必ず単体で `python boatrace_official_scraper.py --test <日付> <場> <R>`
    # を実行し、6艇分のデータが正しく取れることを確認すること。
    note_driver = None
    note_driver_broken = False  # 【2026-09-03追加】note投稿が一度失敗したら、このサイクル中は
                                 # 毎レース再試行せず諦める(Chromeプロフィールのロック等、
                                 # 環境要因の失敗は毎回リトライしても直らず、後続レースの予想が
                                 # 遅れるだけなので)。
    # 【2026-09-03 設計変更】note下書き作成を予想ループから切り離した。
    # 以前はレースごとに即座にnote下書きを作ろうとしていたが、note.com側が重い/
    # 失敗する場合、その待ち時間がそのまま次のレースの予想開始を遅らせてしまい、
    # レース開始に間に合わなくなるリスクがあった。
    # そこで、このサイクルで予想が完了したレースのうちnote投稿対象になったものは
    # いったんここに貯めておき、全レースの予想が終わった後にまとめて投稿する。
    # これにより「note投稿が遅い/失敗する」ことが、他のレースの予想タイミングに
    # 一切影響しなくなる。
    pending_note_posts = []
    import boatrace_official_scraper as race_scraper

    try:
        cycle_now = now_jst()  # 狙い撃ち判定に使う現在時刻(サイクル内で使い回す)

        for sno in STADIUM_TARGETS:
            stadium_name = pipeline.STADIUM_NAMES.get(sno, "")
            total_races = max_race_no.get(sno, 0)
            if total_races == 0:
                continue  # 本日この場は開催が無い

            if not is_in_time_window(sno):
                n_skipped_out_of_window += total_races
                continue

            last_done = progress.get(str(sno), 0)
            stadium_schedule = race_schedule.get(str(sno), {})

            if stadium_schedule:
                # 【狙い撃ちモード】締切時刻表がある場合は、締切が近いレースだけを対象にする。
                # 1レース目からの総当たりが不要になるため、大幅に高速・確実になる。
                rno_list = get_candidate_race_numbers(stadium_schedule, total_races, cycle_now)
                if not rno_list:
                    # 今このサイクルでは、どのレースも締切から遠い(または全て過ぎた) → 何もしない
                    continue
            else:
                # 【フォールバックモード】締切時刻表が無い場(取得失敗等)は、
                # 従来通り進捗の続きから順番に確認する(安全弁は維持したまま)。
                start_rno = last_done + 1
                if start_rno > total_races:
                    continue
                rno_list = list(range(start_rno, total_races + 1))

            found_ready_this_stadium = False
            n_warmup_attempts_this_stadium = 0
            WARMUP_ATTEMPT_LIMIT = 2  # 1つの場・1サイクルあたり、空振り(未公開)を許容する最大実アクセス回数
            current_last_done = last_done  # 「解決済み」と確定したレース番号(進捗として保存する)
            for rno in rno_list:
                prog = prog_idx.get((sno, rno))
                if not prog:
                    # このレース番号が番組表に存在しない(中止等で欠番の可能性)。
                    # 未公開とは違い、後のレースは開催されている可能性があるのでスキップして次へ進む
                    current_last_done = max(current_last_done, rno)
                    continue

                if (stadium_name, str(rno)) in already_done_keys:
                    n_skipped_done += 1
                    current_last_done = max(current_last_done, rno)
                    continue

                if (int(sno), int(rno)) in finished_keys:
                    # 結果データ上すでに終了しているレース(かつ予想記録も無い)。
                    # 締切に間に合わなかった等の理由で予想の機会を逃したレースなので、
                    # macourへの無駄なアクセスはせず、記録も残さずそのまま次に進む。
                    log(f"  ⏭ {stadium_name}{rno}R: すでに結果が出ているため予想せずスキップします。")
                    n_skipped_finished_no_predict += 1
                    current_last_done = max(current_last_done, rno)
                    continue

                if not found_ready_this_stadium and n_warmup_attempts_this_stadium >= WARMUP_ATTEMPT_LIMIT:
                    # 【安全対策】終了済み判定が何らかの理由で機能していない場合でも、
                    # 1つの場に何十回も実アクセスして時間を溶かさないよう上限を設ける。
                    # (2026-08-20、finished_keysの型不一致によりこの上限が無いと
                    #  1サイクルで10分近く消費する事象が発生したため)
                    # 【重要】ここで諦めたレースは「未解決」のままなので、current_last_doneは
                    # 更新しない(次回のサイクルで同じレースから再挑戦する)。
                    log(f"  ⚠ {stadium_name}: ウォームアップの空振りが{WARMUP_ATTEMPT_LIMIT}回に達したため、"
                        f"このサイクルでは諦めます(次回のサイクルで再挑戦されます)。")
                    break

                try:
                    today_compact = today_jst_compact()
                    m_data = race_scraper.fetch_beforeinfo(today_compact, sno, rno)
                except Exception as e:
                    log(f"  ⚠ {stadium_name}{rno}R: boatrace.jp取得エラー: {e}")
                    n_error += 1
                    break  # 通信エラー等は今回はここで打ち切り、次サイクルで再挑戦

                if m_data == race_scraper.CANCELLED:
                    # 中止と判明。found_ready_this_stadiumの状態に関わらず、
                    # 中止は「未来のレースがまだ来ていない」ことの証拠にはならないので打ち切らず次に進む。
                    log(f"  🚫 {stadium_name}{rno}R: 中止のためスキップします。")
                    n_skipped_cancelled += 1
                    current_last_done = max(current_last_done, rno)
                    continue

                race_prev = race_scraper.build_prev_dict(m_data)

                if not pipeline.check_prev_ready(race_prev):
                    if found_ready_this_stadium:
                        n_skipped_not_ready += 1
                        break  # 生きているレースに追いついた後の未公開 → 本当にまだ来ていないので打ち切り
                    else:
                        # まだ今回のサイクルでこの場の「生きている」レースに追いついていない可能性が高い
                        # (未処理のまま終了した古いレース等)。打ち切らず次に進む。
                        # 【可視化】ここは以前ログもカウントも無く「見えない処理」になっていたため、
                        # 件数だけは集計してサイクル結果に含めるようにした(2026-08-19)。
                        # 【重要】未解決のままなのでcurrent_last_doneは更新しない。
                        n_silent_not_ready += 1
                        n_warmup_attempts_this_stadium += 1
                        continue

                found_ready_this_stadium = True
                current_last_done = max(current_last_done, rno)

                try:
                    r = pipeline.predict_race(sno, rno, prog, race_prev, model, feats)
                    if r:
                        pipeline.save_prediction(r)
                        log(f"  ✅ {stadium_name}{rno}R: 予想完了 (n_picks={r['n_picks']})")
                        n_success += 1

                        # edge条件を満たしていれば、このレース単体でnote記事を作る対象にする
                        # (実際の投稿は全レースの予想が終わった後にまとめて行う。下のpending_note_posts参照)
                        edge_info = r.get("edge_info") or []
                        max_edge = max((e[5] for e in edge_info), default=0.0)
                        if max_edge >= NOTE_EDGE_THRESHOLD:
                            try:
                                import generate_note_article
                                title, body, paywall_anchor_text = generate_note_article.build_article_for_race(r, today_str)

                                # 投稿前の安全チェック(買い目が空・edgeが異常値・本文が短すぎる等を検知)。
                                # NGの場合は下書き作成自体をスキップし、理由をログに残す。
                                ok, reason = generate_note_article.validate_race_article(
                                    r, title, body, paywall_anchor_text)
                                if not ok:
                                    log(f"  🛑 {stadium_name}{rno}R: 安全チェックNGのためnote下書きをスキップ({reason})")
                                else:
                                    pending_note_posts.append({
                                        "stadium_name": stadium_name, "rno": rno,
                                        "title": title, "body": body, "max_edge": max_edge,
                                        "paywall_anchor_text": paywall_anchor_text,
                                    })
                                    log(f"  📝 note下書き対象としてキューに追加しました({stadium_name}{rno}R, edge={max_edge:+.3f})")
                            except Exception as e:
                                log(f"  ⚠ note記事の生成でエラー({stadium_name}{rno}R): {e}")
                                log(traceback.format_exc())
                except Exception as e:
                    log(f"  ⚠ {stadium_name}{rno}R: 予想処理エラー: {e}")
                    log(traceback.format_exc())
                    n_error += 1
                    break

            progress[str(sno)] = current_last_done

        # 【2026-09-03追加】全レースの予想が終わった後、まとめてnote下書きを投稿する。
        # ここでの遅延・失敗は、もう他のレースの予想タイミングに一切影響しない。
        if pending_note_posts:
            log(f"  📮 note下書き投稿フェーズ開始({len(pending_note_posts)}件)")
            import note_auto_draft
            for item in pending_note_posts:
                stadium_name, rno = item["stadium_name"], item["rno"]
                if note_driver_broken:
                    log(f"  ⏭ note下書きをスキップ(このサイクルではnote投稿を無効化済み): {stadium_name}{rno}R")
                    continue
                try:
                    if note_driver is None:
                        note_driver = note_auto_draft.make_driver(headless=True)
                    url = note_auto_draft.create_new_draft(
                        item["title"], item["body"], driver=note_driver, log=log,
                        paywall_anchor_text=item.get("paywall_anchor_text"),
                        auto_publish=AUTO_PUBLISH_NOTE)
                    if url:
                        log(f"  📤 note処理が完了しました({stadium_name}{rno}R, edge={item['max_edge']:+.3f}): {url}")
                    else:
                        log(f"  ⚠ note下書き作成に失敗しました({stadium_name}{rno}R)")
                except Exception as e:
                    log(f"  ⚠ note投稿処理でエラー({stadium_name}{rno}R): {e}")
                    log(traceback.format_exc())
                    note_driver_broken = True
                    note_driver = None
                    log(f"  🛑 note投稿でエラーが出たため、このサイクルの残りのnote投稿をスキップします")
    finally:
        # 【重要】driver.quit()自体が原因不明に長時間固まる事象が確認されたため、
        # ここにもタイムアウトを設ける。GitHub Actionsのランナーはジョブ終了時に
        # まるごと破棄されるので、仮にブラウザプロセスが残っても実害はない。
        _safe_quit(note_driver, "noteブラウザ")

    save_progress(today_str, progress)

    log(f"サイクル結果: 成功{n_success} / 既存スキップ{n_skipped_done} / "
        f"未公開で打ち切り{n_skipped_not_ready} / 終了済みスキップ{n_skipped_finished_no_predict} / "
        f"中止スキップ{n_skipped_cancelled} / エラー{n_error} / "
        f"時間帯外スキップ{n_skipped_out_of_window} / ウォームアップ空振り{n_silent_not_ready}")

    log("auto_predict_cycle 終了")


if __name__ == "__main__":
    main()

# test
