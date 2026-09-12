#!/usr/bin/env python3
"""
TOPIX 段階的ウエイト低減 — 保有窓 × 推定量の比較（予行演習 v2）

第1段階 第8〜10回について、実施日 D からの営業日オフセットで区切った保有窓ごとに
相対効果を計算する。ベンチマークを2通り並べて、推定量のノイズ差を可視化する。

  (A) ETFベンチマーク  : 対象銘柄の等加重 − 1306(TOPIX連動ETF)
  (B) 対照群マッチ      : 対象銘柄の等加重 − 同じ規模区分・同じ33業種の非対象銘柄の等加重

(B) は小型株ファクターと業種要因を落とすので残差が小さくなる。
判定に使うのは (B) の S/N。

J-Quants 無料プランのみで動く（指数データは使わない）。

使い方:
    export JQUANTS_REFRESH_TOKEN="..."
    python event_windows.py --targets targets.csv
"""
import argparse, csv, os, sys, time, json, statistics as st
from datetime import date, timedelta
from collections import defaultdict
import urllib.request, urllib.parse, urllib.error

API = "https://api.jquants.com/v1"
EVENTS = {8: date(2024, 7, 31), 9: date(2024, 10, 31), 10: date(2025, 1, 31)}
WINDOWS = [(-3, 0), (-1, 0), (0, 1), (0, 3), (0, 5), (0, 10), (0, 13), (0, 20)]
ETF = "13060"  # 1306 TOPIX連動型上場投信


def _get(path, token, **params):
    url = f"{API}{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    for a in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503) and a < 4:
                time.sleep(2 ** a); continue
            sys.exit(f"[HTTP {e.code}] {path} {params}\n{e.read()[:400].decode(errors='replace')}")
    sys.exit("retry exhausted")


def auth():
    rt = os.environ.get("JQUANTS_REFRESH_TOKEN")
    if not rt:
        sys.exit("環境変数 JQUANTS_REFRESH_TOKEN を設定してください")
    req = urllib.request.Request(
        f"{API}/token/auth_refresh?refreshtoken={urllib.parse.quote(rt)}", data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["idToken"]
    except urllib.error.HTTPError as e:
        sys.exit(f"認証失敗 [HTTP {e.code}]: {e.read()[:400].decode(errors='replace')}")


def business_days(token, frm, to):
    js = _get("/markets/trading_calendar", token, **{"from": frm.isoformat(), "to": to.isoformat()})
    return sorted(date.fromisoformat(d["Date"]) for d in js.get("trading_calendar", [])
                  if str(d.get("HolidayDivision")) == "1")


def paged(path, token, **params):
    key = None
    while True:
        p = dict(params)
        if key:
            p["pagination_key"] = key
        js = _get(path, token, **p)
        yield js
        key = js.get("pagination_key")
        if not key:
            return


def closes_on(token, d):
    out = {}
    for js in paged("/prices/daily_quotes", token, date=d.isoformat()):
        for q in js.get("daily_quotes", []):
            c = q.get("AdjustmentClose")
            if c is not None:
                out[q["Code"]] = float(c)
    return out


def listed_on(token, d):
    """その時点の上場銘柄情報 {code: (ScaleCategory, Sector33Code)} — 事後情報を使わない"""
    out = {}
    for js in paged("/listed/info", token, date=d.isoformat()):
        for r in js.get("info", []):
            out[r["Code"]] = (r.get("ScaleCategory") or "", r.get("Sector33Code") or "")
    return out


def norm(c):
    c = str(c).strip()
    return c if len(c) == 5 else c + "0"


def summarize(vals):
    m = sum(vals) / len(vals)
    sd = st.stdev(vals) if len(vals) > 1 else float("nan")
    return m, sd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--targets", required=True)
    ap.add_argument("--etf", default=ETF)
    args = ap.parse_args()

    with open(args.targets, encoding="utf-8-sig") as f:
        targets = {norm(r["code"]) for r in csv.DictReader(f) if r.get("code")}
    if not targets:
        sys.exit("targets.csv に code がありません")
    print(f"対象銘柄 {len(targets)} 件\n")

    token = auth()
    offsets = sorted({o for w in WINDOWS for o in w})
    rows = []

    for rnd, D in EVENTS.items():
        cal = business_days(token, D - timedelta(days=40), D + timedelta(days=60))
        if D not in cal:
            sys.exit(f"第{rnd}回: {D} が営業日として取得できません")
        i = cal.index(D)
        px = {}
        for o in offsets:
            j = i + o
            if not (0 <= j < len(cal)):
                sys.exit(f"第{rnd}回: オフセット {o} がカレンダー範囲外")
            px[o] = closes_on(token, cal[j])
            print(f"  第{rnd}回 D{o:+d} = {cal[j]}  ({len(px[o])}銘柄)")

        # 対照群: 実施日前日時点の規模区分・33業種で、対象外の銘柄を割り当てる
        info = listed_on(token, cal[i - 1])
        buckets = defaultdict(list)
        for code, (scale, sec) in info.items():
            if code not in targets and scale and sec:
                buckets[(scale, sec)].append(code)
        tgt_bucket = {c: info[c] for c in targets if c in info}
        used = sorted({b for b in tgt_bucket.values() if b in buckets})
        controls = sorted({c for b in used for c in buckets[b]})
        print(f"  対照群 {len(controls)} 件 / マッチした区分 {len(used)} 通り\n")

        for a, b in WINDOWS:
            pa, pb = px[a], px[b]

            def ret(codes):
                return [pb[c] / pa[c] - 1 for c in codes if c in pa and c in pb and pa[c] > 0]

            tr = ret(sorted(targets))
            if len(tr) < 5:
                rows.append((rnd, a, b, None, None, None, None, len(tr))); continue

            # (A) ETF基準
            if args.etf not in pa or args.etf not in pb:
                sys.exit(f"ETF {args.etf} の価格が取得できません")
            etf_r = pb[args.etf] / pa[args.etf] - 1
            mA, sdA = summarize([r - etf_r for r in tr])

            # (B) 対照群マッチ（区分ごとに対照平均を引く）
            ctrl_mean = {}
            for bk in used:
                rs = ret(buckets[bk])
                if rs:
                    ctrl_mean[bk] = sum(rs) / len(rs)
            relB = [pb[c] / pa[c] - 1 - ctrl_mean[tgt_bucket[c]]
                    for c in sorted(targets)
                    if c in pa and c in pb and pa[c] > 0
                    and c in tgt_bucket and tgt_bucket[c] in ctrl_mean]
            if len(relB) < 5:
                rows.append((rnd, a, b, mA, sdA, None, None, len(tr))); continue
            mB, sdB = summarize(relB)
            rows.append((rnd, a, b, mA, sdA, mB, sdB, len(tr)))

    hdr = f"{'回':>3} {'窓':>10} | {'(A)ETF基準':>11} {'SD':>8} {'S/N':>6} | {'(B)対照群':>10} {'SD':>8} {'S/N':>6} | {'N':>4}"
    print("=" * len(hdr)); print(hdr); print("-" * len(hdr))
    for rnd, a, b, mA, sdA, mB, sdB, n in rows:
        w = f"D{a:+d}→D{b:+d}"
        if mA is None:
            print(f"{rnd:>3} {w:>10} | {'データ不足':>11}"); continue
        snA = abs(mA) / sdA if sdA == sdA and sdA else float("nan")
        if mB is None:
            print(f"{rnd:>3} {w:>10} | {mA*100:>10.3f}% {sdA*100:>7.3f}% {snA:>6.2f} | {'—':>10}")
            continue
        snB = abs(mB) / sdB if sdB == sdB and sdB else float("nan")
        mark = "  ← 現行" if (a, b) == (-1, 0) else ("  ← 論文の利益側" if (a, b) == (0, 13) else "")
        print(f"{rnd:>3} {w:>10} | {mA*100:>10.3f}% {sdA*100:>7.3f}% {snA:>6.2f} |"
              f" {mB*100:>9.3f}% {sdB*100:>7.3f}% {snB:>6.2f} | {n:>4}{mark}")
    print("=" * len(hdr))
    print("\n(A) 対象銘柄の等加重 − 1306(TOPIX連動ETF)")
    print("(B) 対象銘柄の等加重 − 同じ規模区分・同じ33業種の非対象銘柄の等加重")
    print("SD  は個別銘柄の相対リターンの横断的ばらつき（＝残差の大きさ）")
    print("判定は (B) の S/N を見る。(B) の SD が (A) より明確に小さくなければ、")
    print("対照群マッチが効いていないので設計を見直すこと。")


if __name__ == "__main__":
    main()
