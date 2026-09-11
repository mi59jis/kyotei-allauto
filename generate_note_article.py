# -*- coding: utf-8 -*-
"""
generate_note_article.py
prediction_record.csv から指定日の予想データを読み込み、
note投稿用の記事(タイトル+本文)を自動生成する。

使い方:
    python generate_note_article.py                       # 今日の日付、閾値0.02(厳選)で生成
    python generate_note_article.py 20260802               # 日付を指定
    python generate_note_article.py 20260802 0.01          # 閾値も指定(0.01=毎日運用向け、0.02=厳選)

出力:
    note_articles/note_article_YYYYMMDD.txt
    (1行目: タイトル / 3行目以降: 本文)

【2026-08-14 改訂】
一般読者向けに文言を全面的に見直した。
- 「edge」という専門用語をそのまま出さず、「優位度」という言葉に統一し、
  意味を平易な日本語で説明する形にした。
- 的中率ではなく回収率を前面に押し出す構成にした(このモデルの狙いは
  「当てにいく」ことではなく「回収率を最大化する」ことなので、そこを
  一般読者にも伝わるように明示する)。
- 選手・モーター・ボート・当日の直前情報(展示タイム等)を総合的に分析した上で
  その日の全レースから厳選していることを、無料エリアで明記するようにした。
- edgeの数値をそのまま出す代わりに、厳選度A/S/SSの3段階表示に変換した
  (記事化される時点で既にedge>=0.02の下限をクリアしているため、
  最低ラインを「A」=十分良い評価として扱う設計)。
- 「本日の結果」を独立した信頼構築用の無料記事として自動投稿できるよう、
  build_daily_results_article() を新設した(全レース終了後に1日1回投稿する想定)。
"""
import csv
import sys
import os
from datetime import date

RECORD_FILE = "prediction_record.csv"
OUT_DIR = "note_articles"
DEFAULT_NOTE_EDGE_THRESHOLD = 0.03  # note販売用は厳選側をデフォルトにする(2026-09-04: 実データのedge_reportで0.02=80%→0.03=87.1%だったため引き上げ)

# 「edge」を読者向けにどう呼ぶか。内部の変数名・ロジックは変えず、表示文言だけをここで統一管理する。
ADVANTAGE_LABEL = "優位度"

# 厳選度ランクの閾値。DEFAULT_NOTE_EDGE_THRESHOLD(0.02)未満のレースはそもそも記事化されないため、
# 「A」を記事化される最低ラインとして設計している(=Aでも十分厳選された評価、という位置づけ)。
# A: 0.02以上0.025未満(回収の可能性が比較的高い、日常的に出現する水準)
# S: 0.025以上0.03未満
# SS: 0.03以上(出現頻度は低いが、特に優位性が大きいと判断したレース)
RANK_THRESHOLDS = [
    (0.03,  "SS"),
    (0.025, "S"),
    (0.0,   "A"),
]


def _edge_to_rank(edge_value):
    """edgeの数値を A/S/SS の厳選度ランクに変換する"""
    for threshold, rank in RANK_THRESHOLDS:
        if edge_value >= threshold:
            return rank
    return "A"


# 無料エリアで繰り返し使う「総合分析していること」の説明文。表現をここで一元管理する。
ANALYSIS_DESCRIPTION = (
    "選手の実力・モーターとボートの調子・当日の直前情報(展示タイム、進入コース、天候など)を"
    "総合的に分析したうえで、その日開催される全レースの中から、"
    "オッズ(＝みんなの人気)に対して評価が見合っていない=市場でまだ気づかれていないと判断した"
    "レースだけを厳選して記事化しています。"
)


def load_rows_for_date(target_date):
    """指定日(YYYY-MM-DD or YYYYMMDD)のレコードを読み込む"""
    if not os.path.exists(RECORD_FILE):
        print(f"エラー: {RECORD_FILE} が見つかりません。先に pipeline.py predict を実行してください。")
        sys.exit(1)

    # 日付表記のゆれを吸収 (2026-08-02 / 20260802 どちらでも一致するようにする)
    norm = target_date.replace("-", "")

    rows = []
    with open(RECORD_FILE, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("date", "").replace("-", "") == norm:
                rows.append(row)
    return rows


def format_date_jp(target_date):
    norm = target_date.replace("-", "")
    return f"{norm[0:4]}年{int(norm[4:6])}月{int(norm[6:8])}日"


def _get_max_edge(row):
    try:
        return float(row.get("max_edge") or 0.0)
    except ValueError:
        return 0.0


def _compute_roi(picks_rows):
    """厳選レース群の的中数・投資額・払戻額・回収率をまとめて計算する共通処理"""
    hit_rows = [r for r in picks_rows if r.get("hit") == "○"]
    hit_count = len(hit_rows)
    total_picks = len(picks_rows)
    payouts_yen = sum(float(r["payout"]) for r in hit_rows if r.get("payout") not in (None, "", "None"))
    total_bet = sum(int(r.get("n_picks") or 0) for r in picks_rows) * 100
    roi = (payouts_yen / total_bet * 100) if total_bet else 0
    return {
        "hit_count": hit_count,
        "total_picks": total_picks,
        "payouts_yen": payouts_yen,
        "total_bet": total_bet,
        "roi": roi,
    }


def build_article(rows, target_date, edge_threshold=DEFAULT_NOTE_EDGE_THRESHOLD):
    date_jp = format_date_jp(target_date)

    all_picks_rows = [r for r in rows if int(r.get("n_picks") or 0) > 0]
    # note販売用: max_edgeが閾値以上のレースだけに厳選する
    picks_rows   = [r for r in all_picks_rows if _get_max_edge(r) >= edge_threshold]
    excluded_rows = [r for r in all_picks_rows if _get_max_edge(r) < edge_threshold]

    # 結果が既に記録されているか(hit列に○/×が入っているか)で「結果報告」を含めるか判定
    has_results = any(r.get("hit") in ("○", "×") for r in rows)

    title = f"【競艇予想】{date_jp} 回収率重視の厳選レース({len(picks_rows)}レース)"

    lines = []
    lines.append(f"# {date_jp} の予想")
    lines.append("")
    lines.append(
        "「当てにいく」予想ではなく「回収率を上げる」ことを目的に作っています。"
        "オッズと、計算した実際の勝ちやすさを見比べて、"
        "人気の割に評価が低すぎる組み合わせだけを狙い撃ちする方式です。"
    )
    lines.append("")
    lines.append(ANALYSIS_DESCRIPTION)
    lines.append("")
    lines.append(
        f"過去のレースで検証したところ、この方式で厳選した組み合わせ(本記事の基準)は"
        "回収率170%前後という結果が出ています(※過去データでの検証結果であり、"
        "将来の成績を保証するものではありません)。"
    )
    lines.append("")

    if picks_rows:
        lines.append(f"## 本日の狙い目レース({len(picks_rows)}レース)")
        lines.append("")
        for r in sorted(picks_rows, key=_get_max_edge, reverse=True):
            rank = _edge_to_rank(_get_max_edge(r))
            lines.append(f"### {r['stadium']} {r['race_no']}R [厳選度: {rank}]")
            lines.append(f"- 買い目: {r['picks']}")
            lines.append(f"- 1位評価: {r['rank1']} / 2位評価: {r['rank2']} / 3位評価: {r['rank3']}")
            lines.append(f"- 天候: 風{r['wind']}m 波{r['wave']}cm")
            lines.append("")
    else:
        lines.append("## 本日の狙い目レース")
        lines.append("")
        lines.append("本日は基準を満たすレースがありませんでした。無理に狙わない、というのもこの予想の方針です。")
        lines.append("")

    if excluded_rows:
        lines.append(f"(このほか{len(excluded_rows)}レースは基準に届かず、見送り対象としました)")
        lines.append("")

    if has_results:
        stats = _compute_roi(picks_rows)
        lines.append("## 本日の結果")
        lines.append("")
        lines.append(f"- 対象レース数: {stats['total_picks']}")
        lines.append(f"- 的中数: {stats['hit_count']}")
        lines.append(f"- 投資額: {stats['total_bet']}円")
        lines.append(f"- 払戻額: {stats['payouts_yen']:.0f}円")
        lines.append(f"- 回収率: {stats['roi']:.1f}%")
        lines.append("")

    lines.append("---")
    lines.append("※本記事は投資・購入を推奨するものではありません。舟券の購入は自己責任でお願いします。")

    body = "\n".join(lines)
    return title, body


def build_article_for_race(r, target_date):
    """
    pipeline.predict_race() の戻り値(1レース分)から、そのレース単体のnote記事を作る。
    CSV経由ではなく、予想直後にその場で呼び出す想定。

    無料エリア: 分析方針の説明・厳選度の予告(誰でも読める部分。ランキングや天候などの
                詳細分析結果は含めず、買う価値があるかどうかの判断材料に絞る)
    有料エリア: 詳細な分析結果(実力評価ランキング・天候)・具体的な買い目・優位度スコア

    【2026-09-04変更】以前は本文中に「▼▼▼ ここから有料ライン...▼▼▼」という
    プレースホルダー文字を埋め込み、人間が目で見てnote上で有料エリアを手動設定する
    運用だった。これを廃止し、代わりに戻り値として「有料ラインを置くべき段落の
    直前のテキスト(paywall_anchor_text)」を返すようにした。
    note_auto_draft.py側は、投稿画面でこのテキストを含む段落を探し、その直後に
    有料ラインをドラッグで移動させる(実装は別途)。
    本文には目印の文字列は一切含まれない。

    戻り値: (title, body, paywall_anchor_text)
      paywall_anchor_text: この段落の直後に有料ラインを置く、という目印テキスト。
      本文(body)中に一字一句そのまま含まれる。
    """
    edge_info = r.get("edge_info") or []
    max_edge = max((e[5] for e in edge_info), default=0.0)
    rank = _edge_to_rank(max_edge)

    # 当日の予想であることは自明なため、タイトルには日付もAIという語も入れない
    title = f"【厳選度{rank}競艇予想】{r['stadium']}{r['race_no']}R"

    lines = []
    lines.append(f"# {r['stadium']} 第{r['race_no']}R")
    lines.append("")

    # ---------- 無料エリア ----------
    lines.append(
        "選手の実力・モーターとボートの調子・当日の直前情報(展示タイム、進入コース、天候など)を"
        "総合的に分析し、全レースの中から、オッズに対して評価が見合っていないレースを厳選して予想しています。"
    )
    lines.append("")
    lines.append(
        "「当てにいく」予想ではなく、**回収率を上げる**ことを目的とした競艇予想です。"
    )
    lines.append("")
    paywall_anchor_text = (
        f"本レースの厳選度は【{rank}】です。具体的な買い目は、この続きの有料エリアでご確認いただけます。"
    )
    lines.append(paywall_anchor_text)
    lines.append("")

    # ---------- 有料エリア(この続きが有料ラインより下になる想定) ----------
    lines.append("総合的に分析した結果、人気と実力のズレが大きい組み合わせで予想を出します。")
    lines.append("")
    lines.append("## 実力評価ランキング")
    for i, (bno, name, cls, score, et) in enumerate(r["ranked"][:4], 1):
        et_s = f"展示タイム{et:.2f}" if et > 0 else "展示タイム未計測"
        lines.append(f"{i}位: {bno}号艇 {name}({cls}) 実力スコア{score:.1f}% {et_s}")
    lines.append("")
    lines.append(f"天候: 風{r['wind']}m 波{r['wave']}cm")
    lines.append("")
    lines.append(f"## 買い目({r['n_picks']}点、厳選度{rank})")
    lines.append("")
    lines.append(
        f"※{ADVANTAGE_LABEL}とは、計算した勝率が、実際のオッズが示す確率よりどれだけ高いかを表す数値です。"
        "数値が大きいほど、市場がまだ気づいていない可能性が高いことを意味します。"
    )
    lines.append(
        f"※推奨購入金額は、1レースの予算{RACE_BUDGET:,}円を、{ADVANTAGE_LABEL}の大きさに応じて配分したものです"
        "(自信度が高い買い目ほど多く、低い買い目ほど少なく配分しています)。"
    )
    lines.append("")
    stakes = compute_stake_allocation(edge_info)
    for combo, (i, j, k, m_prob, mkt_prob, edge), stake in zip(r["picks"], edge_info, stakes):
        lines.append(
            f"- {combo}  推奨{stake:,}円  {ADVANTAGE_LABEL}+{edge*100:.1f}pt "
            f"(予想{m_prob*100:.1f}% / オッズ換算{mkt_prob*100:.1f}%)"
        )
    lines.append("")
    lines.append("---")
    lines.append("※本記事は投資・購入を推奨するものではありません。舟券の購入は自己責任でお願いします。")

    body = "\n".join(lines)
    return title, body, paywall_anchor_text


def build_daily_results_article(target_date, edge_threshold=DEFAULT_NOTE_EDGE_THRESHOLD):
    """
    その日の全レース終了後に投稿する、信頼構築用の「本日の結果」記事を作る。
    有料ラインなし・完全無料公開が前提(実績を包み隠さず公開することで信頼を得るための記事)。
    その日にnote記事化された(=NOTE_EDGE_THRESHOLD以上の)レースの回収率実績をまとめる。

    戻り値: (title, body)。対象レースが1件も無い場合は (None, None) を返す。
    """
    rows = load_rows_for_date(target_date)
    if not rows:
        return None, None

    date_jp = format_date_jp(target_date.replace("-", ""))
    all_picks_rows = [r for r in rows if int(r.get("n_picks") or 0) > 0]
    picks_rows = [r for r in all_picks_rows if _get_max_edge(r) >= edge_threshold]

    if not picks_rows:
        return None, None

    stats = _compute_roi(picks_rows)

    title = f"【本日の結果】{date_jp} 回収率{stats['roi']:.0f}%"

    lines = []
    lines.append(f"# {date_jp} 本日の成績")
    lines.append("")
    lines.append(
        "本日note記事でお伝えした狙い目レースの結果を、包み隠さずすべて公開します。"
        "「回収率」を目的に設計しているので、当たり外れよりもこの数字を重視して見ていただけると幸いです。"
    )
    lines.append("")
    lines.append("## 本日の成績まとめ")
    lines.append("")
    lines.append(f"- 対象レース数: {stats['total_picks']}レース")
    lines.append(f"- 的中数: {stats['hit_count']}レース")
    lines.append(f"- 投資額: {stats['total_bet']:,}円(1点100円換算)")
    lines.append(f"- 払戻額: {stats['payouts_yen']:,.0f}円")
    lines.append(f"- 回収率: {stats['roi']:.1f}%")
    lines.append("")

    lines.append("## レースごとの結果")
    lines.append("")
    lines.append("|レース|点数|結果|回収率|")
    lines.append("|---|---|---|---|")
    for r in sorted(picks_rows, key=lambda r: (r["stadium"], int(r["race_no"]))):
        rank = _edge_to_rank(_get_max_edge(r))
        race_label = f"{r['stadium']}{r['race_no']}R [厳選度{rank}]"
        n_picks = int(r.get("n_picks") or 0)
        points_label = f"{n_picks}点"

        if r.get("hit") == "○" and r.get("payout") not in (None, "", "None"):
            payout = float(r["payout"])
            bet = n_picks * 100
            race_roi = (payout / bet * 100) if bet else 0
            result_label = f"的中(払戻{int(payout):,}円)"
            roi_label = f"{race_roi:.0f}%"
        elif r.get("hit") == "×":
            result_label = "不的中"
            roi_label = "-"
        else:
            result_label = "結果未反映"
            roi_label = "-"

        lines.append(f"|{race_label}|{points_label}|{result_label}|{roi_label}|")
    lines.append("")

    lines.append(
        "選手・モーター・ボート・直前情報を総合的に分析したうえで厳選する、という方針を"
        "毎日続けて結果を公開していきますので、気になる方はぜひ日々の記事もチェックしてみてください。"
    )
    lines.append("")
    lines.append("---")
    lines.append("※本記事は投資・購入を推奨するものではありません。舟券の購入は自己責任でお願いします。")

    body = "\n".join(lines)
    return title, body


def validate_race_article(r, title, body, paywall_anchor_text=None):
    """
    レース単体記事を実際に投稿する前の安全チェック。
    ここでNGと判定された場合、呼び出し側(auto_predict_cycle.py)は
    note下書きの作成自体をスキップし、ログに理由を残す想定。

    paywall_anchor_text: build_article_for_race()が返す、有料ラインを置く目印テキスト。
      渡された場合、本文中に一字一句そのまま含まれているかを確認する
      (含まれていなければ、有料ライン自動設定の目印を見失うため投稿前に検知する)。

    戻り値: (ok: bool, reason: str)  ok=Falseの場合、reasonに理由が入る
    """
    edge_info = r.get("edge_info") or []
    picks = r.get("picks") or []

    # 買い目が空なのに記事を作ろうとしていないか
    if not picks or not edge_info:
        return False, "買い目(picks)が空です"

    # picksとedge_infoの点数が一致しているか(ズレはロジック不整合のサイン)
    if len(picks) != len(edge_info):
        return False, f"picks({len(picks)}件)とedge_info({len(edge_info)}件)の件数が一致しません"

    # edge値が常識的な範囲か(0未満やあり得ない大きさは異常値の可能性)
    edges = [e[5] for e in edge_info]
    if any(e < 0 for e in edges):
        return False, f"edgeが負の値です: {edges}"
    if any(e > 1.0 for e in edges):
        return False, f"edgeが異常に大きい値です: {edges}"

    # 確率(モデル・市場)が0〜1の範囲に収まっているか
    for (i, j, k, m_prob, mkt_prob, edge) in edge_info:
        if not (0 <= m_prob <= 1) or not (0 <= mkt_prob <= 1):
            return False, f"確率が0〜1の範囲外です: モデル{m_prob} 市場{mkt_prob}"

    # タイトル・本文が極端に短くないか(生成ロジックが壊れて空同然になっていないか)
    if not title or len(title) < 5:
        return False, f"タイトルが短すぎます: {title!r}"
    if not body or len(body) < 100:
        return False, f"本文が短すぎます({len(body) if body else 0}文字)"

    # 有料ラインを置く目印テキストが本文に含まれているか
    # (含まれていなければ、投稿時に有料ラインをどこに置けばよいか判別できない)
    if paywall_anchor_text and paywall_anchor_text not in body:
        return False, "有料ラインの目印テキストが本文に見つかりません"

    return True, ""


# ============================================================
# 賭け金配分(Kelly基準)
# ============================================================
# 予想ロジック(どの組み合わせを買うか)には一切触れず、
# 「いくら賭けるか」だけをedge(優位度)に応じて最適化する。
# edge_infoに含まれるモデル確率(m_prob)と市場確率(mkt_prob、オッズの逆数に相当)から、
# 各買い目のKelly基準の賭け金比率を計算する。
#
# 【注意】3連単は同一レース内で複数の買い目を同時に購入する「互いに排反な賭け」のため、
# 厳密には各買い目を独立に扱う単純Kelly公式は理論上の近似に過ぎない(複数排反事象への
# 同時ベットの厳密な最適化ではない)。実務上の簡便法として、各買い目のKelly比率を
# 算出したうえで正規化し、按分する方式を採用している。
# また、フルKellyは分散(振れ幅)が大きすぎるため、KELLY_DAMPING(デフォルト0.5=ハーフKelly)
# で割り引いて使う。
KELLY_DAMPING = 0.5
STAKE_UNIT = 100        # 舟券の購入単位(円)
MIN_STAKE = 100          # 1点あたりの最低賭け金(円)
RACE_BUDGET = 3000       # 1レースあたりの固定予算(円)。点数に関わらずこの金額をedgeに応じて配分する


def _kelly_fraction(m_prob, mkt_prob):
    """
    1点の買い目についてKelly基準の賭け金比率(バンクロールに対する割合)を計算する。
    mkt_prob(市場確率)からオッズを逆算し、b=オッズ-1、p=モデル確率、q=1-pとして
    f* = (b*p - q) / b を計算する。負値になった場合は0とする(理論上は賭けるべきでない)。
    """
    if mkt_prob is None or mkt_prob <= 0 or mkt_prob >= 1:
        return 0.0
    b = 1.0 / mkt_prob - 1.0
    if b <= 0:
        return 0.0
    p = m_prob
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f)


def compute_stake_allocation(edge_info, total_yen=None, damping=KELLY_DAMPING,
                              unit=STAKE_UNIT, min_stake=MIN_STAKE):
    """
    edge_info(pipeline.predict_race()が返す(i,j,k,m_prob,mkt_prob,edge)のリスト)から、
    各買い目のKelly基準に基づく推奨賭け金(円)のリストを返す。

    total_yen: そのレース全体の投資総額。省略時は RACE_BUDGET(1レース3,000円固定)を使う。
               点数(n_picks)に関わらず、1レースあたりの予算は常にこの金額で統一し、
               その中でedgeに応じてメリハリをつける(点数が多いレースほど1点あたりが
               薄まるのは従来と同じ考え方だが、予算そのものは点数非依存で一定にする)。

    戻り値: [各買い目の推奨賭け金(円), ...]  edge_infoと同じ順番・同じ件数
    """
    if not edge_info:
        return []

    if total_yen is None:
        total_yen = RACE_BUDGET

    fractions = [_kelly_fraction(e[3], e[4]) * damping for e in edge_info]
    total_fraction = sum(fractions)

    if total_fraction <= 0:
        # 全てのKelly比率が0以下(理論上どれも賭けるべきでない)の場合は、
        # 均等配分にフォールバックする(edge条件を満たして記事化されている以上、
        # 何かしらは提示する必要があるため)
        n = len(edge_info)
        base = round(total_yen / n / unit) * unit
        return [max(min_stake, base)] * n

    raw_stakes = [total_yen * (f / total_fraction) for f in fractions]
    # unit(100円)単位に丸め、最低min_stake円は確保する
    stakes = [max(min_stake, round(s / unit) * unit) for s in raw_stakes]
    return stakes


def main():
    args = sys.argv[1:]
    target_date = args[0] if len(args) > 0 else date.today().strftime("%Y%m%d")
    edge_threshold = float(args[1]) if len(args) > 1 else DEFAULT_NOTE_EDGE_THRESHOLD

    rows = load_rows_for_date(target_date)
    if not rows:
        print(f"警告: {target_date} のレコードが prediction_record.csv に見つかりませんでした。")
        sys.exit(1)

    title, body = build_article(rows, target_date, edge_threshold)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, f"note_article_{target_date.replace('-','')}.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(title + "\n\n" + body)

    print(f"✅ 記事を生成しました: {out_path}")
    print(f"\n--- タイトル ---\n{title}")
    print(f"\n--- 本文(先頭300文字) ---\n{body[:300]}...")


if __name__ == "__main__":
    main()

# test