# -*- coding: utf-8 -*-
"""
test_paid_draft.py

create_new_draft()の有料エリア挿入・価格設定の自動化を、
本物のレース予想を待たずに単体テストするためのスクリプト。

使い方:
    python test_paid_draft.py            # 下書き作成まで(投稿はしない、従来通り安全)
    python test_paid_draft.py --publish  # 実際に「投稿する」まで行う(本当に公開・課金対象になる)

ブラウザが見える状態(headless=False)で動くので、目視で確認しながら進められる。
--publish を付けたときだけ、実行前にターミナルで確認を求める
(本当に公開してよいテスト記事か、誤って本番用のタイトル・本文にしていないか等)。

実行後、note.comの記事一覧を開いて、有料ラインの位置・価格・
(--publish時は)公開状態になっているかを確認してください。
"""
import sys
import note_auto_draft

DO_PUBLISH = "--publish" in sys.argv

title = "【テスト】有料エリア自動配置・公開のテスト"

anchor = "本レースの厳選度は【S】です。具体的な買い目は、この続きの有料エリアでご確認いただけます。"

body = (
    "# テストレース\n\n"
    "これはテスト用の本文です。ここは無料で読める部分という想定です。\n\n"
    "テスト用の説明文がもう一段落あります。無料エリアの続きです。\n\n"
    f"{anchor}\n\n"
    "ここから先が有料エリアになるはずの段落です。実力評価ランキングなどが入る想定の場所です。\n\n"
    "## 買い目(テスト)\n\n"
    "- 1-2-3  推奨300円  優位度+5.0pt (予想10.0% / オッズ換算5.0%)\n"
    "- 3-2-1  推奨200円  優位度+3.0pt (予想8.0% / オッズ換算5.0%)\n\n"
    "---\n"
    "※本記事は投資・購入を推奨するものではありません。舟券の購入は自己責任でお願いします。"
)

if DO_PUBLISH:
    print("⚠ --publish が指定されています。このテストは実際に記事を公開します(300円の有料記事として)。")
    confirm = input("本当に実行しますか？ 'yes' と入力してください: ")
    if confirm.strip().lower() != "yes":
        print("中止しました。")
        sys.exit(0)

print(f"headless=Falseでブラウザを開き、テスト用の記事を作成します(publish={DO_PUBLISH})...")
url = note_auto_draft.create_new_draft(
    title, body, headless=False, paywall_anchor_text=anchor, price=300,
    auto_publish=DO_PUBLISH,
)
print(f"\n完了。URL: {url}")
if DO_PUBLISH:
    print("note.comを開いて、実際に公開された状態になっているか確認してください。")
    print("(テスト記事なので、確認後は手動で削除・非公開にしておくことをおすすめします)")
else:
    print("note.comを開いて、有料ラインの位置・価格(300円)・有料設定を目視確認してください。")

