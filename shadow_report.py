# -*- coding: utf-8 -*-
"""
シャドー運用の成績比較: 現行モデル(model_new) vs リークなしモデル(model_leakfree)。
prediction_record.csv のうち、両方の買い目が記録され、結果(result/payout)が入ったレースだけを、
同じレース・同じedge閾値で比べる。

使い方(PowerShell、prediction_record.csv のあるフォルダで):
  python shadow_report.py
  python shadow_report.py --since 2026-09-21
"""
import argparse
import csv

import numpy as np

TH = [0.03, 0.05, 0.07, 0.10]


def parse(s):
    """'1-2-3:0.0512 / 2-1-3:0.0311' → {'1-2-3':0.0512, ...}"""
    out = {}
    for part in (s or "").split(" / "):
        if ":" in part:
            c, e = part.rsplit(":", 1)
            try:
                out[c.strip()] = float(e)
            except ValueError:
                pass
    return out


def roi(cost_pay):
    cost = sum(c for c, _ in cost_pay)
    pay = sum(p for _, p in cost_pay)
    return cost, pay


def boot(cp, rng, n=2000):
    c = np.array([x[0] for x in cp], float)
    p = np.array([x[1] for x in cp], float)
    idx = rng.integers(0, len(c), size=(n, len(c)))
    bc, bp = c[idx].sum(1), p[idx].sum(1)
    ok = bc > 0
    r = bp[ok] / bc[ok] * 100
    return np.percentile(r, [2.5, 97.5]) if len(r) else (np.nan, np.nan)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="prediction_record.csv")
    ap.add_argument("--since", default=None)
    args = ap.parse_args()
    rng = np.random.default_rng(0)

    with open(args.file, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows = [r for r in rows if r.get("shadow_picks") is not None and r.get("shadow_version")]
    if args.since:
        rows = [r for r in rows if r["date"] >= args.since]
    done = [r for r in rows if r.get("result")]
    print(f"シャドー記録あり {len(rows)}レース / うち結果確定 {len(done)}レース")
    if len(done) < 30:
        print("まだ少なすぎます。数百レース以上たまってから見てください。")
        if not done:
            return

    print(f"\n{'閾値':>6} | {'現行: 点数':>9} {'ROI%':>7} {'95%CI':>15} | {'新: 点数':>8} {'ROI%':>7} {'95%CI':>15}")
    print("-" * 78)
    for t in TH:
        old_cp, new_cp = [], []
        for r in done:
            res = r["result"]
            pay = float(r["payout"]) if r.get("payout") not in (None, "") else 0.0
            for cp, picks in ((old_cp, parse(r.get("pick_detail"))), (new_cp, parse(r.get("shadow_picks")))):
                sel = [c for c, e in picks.items() if e >= t]
                cp.append((100 * len(sel), pay if res in sel else 0.0))
        line = f"{t:6.2f} |"
        for cp in (old_cp, new_cp):
            c, p = roi(cp)
            if c == 0:
                line += f" {'0':>9} {'-':>7} {'-':>15} |"
                continue
            lo, hi = boot(cp, rng)
            line += f" {int(c/100):9d} {p/c*100:7.1f} {lo:6.1f}-{hi:6.1f} |"
        print(line.rstrip("|"))

    print("\n読み方:")
    print(" ・同じレースで両モデルを比べています(現行の買い目は pick_detail、新モデルは shadow_picks)。")
    print(" ・基準線(全120点買い)は約60%。95%CIが狭くなるまで(数千点)は、差を断定しないでください。")
    print(" ・新モデルが現行を安定して上回るまでは、公開する買い目は現行のままにします。")


if __name__ == "__main__":
    main()
