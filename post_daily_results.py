# -*- coding: utf-8 -*-
"""
post_daily_results.py

前日分の「本日の結果」記事(無料・信頼構築用、有料ラインなし)を生成し、
note下書きとして投稿する。record-resultsワークフローから、
pipeline.py record の後に呼び出す想定。

使い方:
    python post_daily_results.py             # 前日分(JST基準)
    python post_daily_results.py 20260903    # 指定日を明示

その日、note記事化されたレース(NOTE_EDGE_THRESHOLD以上)が1件も無ければ、
何もせず終了する(build_daily_results_article()が(None, None)を返す)。
"""
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import generate_note_article
import note_auto_draft


def main():
    if len(sys.argv) > 1:
        target_date = sys.argv[1]
    else:
        yesterday = datetime.now(ZoneInfo("Asia/Tokyo")) - timedelta(days=1)
        target_date = yesterday.strftime("%Y-%m-%d")

    print(f"{target_date} の結果まとめ記事を作成します...")
    title, body = generate_note_article.build_daily_results_article(target_date)

    if title is None:
        print(f"{target_date} は対象レース(note記事化された予想)が無かったため、投稿をスキップします。")
        return

    print(f"タイトル: {title}")
    driver = note_auto_draft.make_driver(headless=True)
    try:
        # paywall_anchor_textを渡さない = 無料記事として投稿する
        # auto_publish=True: 下書きで止めず、実際に公開する
        url = note_auto_draft.create_new_draft(title, body, driver=driver, auto_publish=True)
        print(f"✅ 完了: {url}")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()

#test
