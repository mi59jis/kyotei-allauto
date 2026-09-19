# -*- coding: utf-8 -*-
"""
シャドー運用の成績比較: 現行モデル(model_new) vs リークなしモデル(model_leakfree)。
prediction_record.csv のうち、両方の買い目が記録され、結果(result/payout)が入ったレースだけを、
同じレース・同じ条件で比べる。1点100円。

使い方(PowerShell、prediction_record.csv のあるフォルダで):
  python shadow_report.py
  python shadow_report.py --since 2026-09-21

比べるもの:
  (1) edge >= 閾値 の買い目(現行モデル / 新モデル)
  (2) 追加ルール「モデル確率/市場確率 >= 1.3 かつ 市場確率 >= 5%」(現行モデル / 新モデル)
      → 人気薄を避けて、市場より強いと見た買い目だけに絞る条件(2026-09-19の券種別検証で見つけた。
        事後に選んだ条件なので、ここで実測して確かめる)
"""
import argparse
import csv

import numpy as np

TH = [0.03, 0.05, 0.07, 0.10]
RULE_RATIO, RULE_Q = 1.3, 0.05


def parse(s):
    """'1-2-3:edge:m:q / ...' → {'1-2-3': (edge, m, q)} (古い記録は m,q が None)"""
    out = {}
    for part in (s or "").split(" / "):
        f = part.strip().split(":")
        if len(f) < 2:
            continue
        try:
            e = float(f[1])
            m = float(f[2]) if len(f) > 2 else None
            q = float(f[3]) if len(f) > 3 else None
        except ValueError:
            continue
        out[f[0]] = (e, m, q)
    return out


def boot(cp, rng, n=2000):
    c = np.array([x[0] for x in cp], float)
    p = np.array([x[1] for x in cp], float)
    idx = rng.integers(0, len(c), size=(n, len(c)))
    bc, bp = c[idx].sum(1), p[idx].sum(1)
    ok = bc > 0
    r = bp[ok] / bc[ok] * 100
    return np.percentile(r, [2.5, 97.5]) if len(r) else (np.nan, np.nan)


def eval_rule(done, key, pred, rng):
    cp = []
    for r in done:
        res = r["result"]
        pay = float(r["payout"]) if r.get("payout") not in (None, "") else 0.0
        sel = [c for c, (e, m, q) in parse(r.get(key)).items() if pred(e, m, q)]
        cp.append((100 * len(sel), pay if res in sel else 0.0))
    cost = sum(c for c, _ in cp)
    pay = sum(p for _, p in cp)
    if cost == 0:
        return None
    lo, hi = boot(cp, rng)
    return int(cost / 100), pay / cost * 100, lo, hi


def fmt(x):
    if x is None:
        return f"{'0':>8} {'-':>7} {'-':>15}"
    n, roi, lo, hi = x
    return f"{n:8,d} {roi:7.1f} {lo:6.1f}-{hi:6.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="prediction_record.csv")
    ap.add_argument("--since", default=None)
    args = ap.parse_args()
    rng = np.random.default_rng(0)

    with open(args.file, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get("shadow_version")]
    if args.since:
        rows = [r for r in rows if r["date"] >= args.since]
    done = [r for r in rows if r.get("result")]
    print(f"シャドー記録あり {len(rows)}レース / うち結果確定 {len(done)}レース")
    if not done:
        print("結果の入ったレースがまだありません。")
        return
    if len(done) < 300:
        print("※ まだ少ないです。数百〜数千点たまるまで、差を断定しないでください。")

    print("\n(1) edge >= 閾値")
    print(f"{'閾値':>6} | {'現行: 点数':>8} {'ROI%':>7} {'95%CI':>15} | {'新: 点数':>8} {'ROI%':>7} {'95%CI':>15}")
    print("-" * 80)
    for t in TH:
        a = eval_rule(done, "pick_detail", lambda e, m, q: e >= t, rng)
        b = eval_rule(done, "shadow_picks", lambda e, m, q: e >= t, rng)
        print(f"{t:6.2f} | {fmt(a)} | {fmt(b)}")

    print(f"\n(2) 追加ルール: モデル確率/市場確率 >= {RULE_RATIO} かつ 市場確率 >= {RULE_Q:.0%}")
    rule = lambda e, m, q: (m is not None and q is not None and q >= RULE_Q and q > 0 and m / q >= RULE_RATIO)
    a = eval_rule(done, "pick_detail", rule, rng)
    b = eval_rule(done, "shadow_picks", rule, rng)
    print(f"{'':>6} | {'現行: 点数':>8} {'ROI%':>7} {'95%CI':>15} | {'新: 点数':>8} {'ROI%':>7} {'95%CI':>15}")
    print(f"{'':>6} | {fmt(a)} | {fmt(b)}")

    print("\n読み方:")
    print(" ・同じレースで両モデルを比べています。基準線(全120点買い)は約60%。")
    print(" ・過去の検証(walk-forward)では、(2)のルールを新モデルに当てると 約85%(95%CI 79〜91%)でした。")
    print("   実測がそれに近ければ、検証が本番でも通用している目安です。")
    print(" ・95%CIが100%をまたぐ間は黒字と言い切れません。公開する買い目は、十分たまるまで変えません。")


if __name__ == "__main__":
    main()
