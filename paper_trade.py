#!/usr/bin/env python3
"""模擬売買（ペーパートレード）用の個別株 日足データ取得（GitHub Actions が定期実行）。

paper_universe.json に並べた銘柄コードについて Yahoo Finance の chart API から
直近 RANGE（既定 6mo）の日足（始値・高値・安値・終値・出来高）を取得し、
paper_prices.json に書き出す。失敗した銘柄は前回のデータを保持する。"""
import json, os, sys, time, random, datetime as dt
from fetch_data import http_get

HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_PATH = os.path.join(HERE, "paper_universe.json")
OUT_PATH = os.path.join(HERE, "paper_prices.json")
RANGE = os.environ.get("RANGE", "6mo")


def fetch_daily(code):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{code}.T?range={RANGE}&interval=1d"
    d = json.loads(http_get(url))
    r = d["chart"]["result"][0]
    q = r["indicators"]["quote"][0]
    ts = r.get("timestamp") or []
    rows = []
    for i, t in enumerate(ts):
        c = q["close"][i] if i < len(q["close"]) else None
        if c is None:
            continue
        day = dt.datetime.fromtimestamp(t, dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%d")
        rows.append([day,
                     round(q["open"][i] or c, 1), round(q["high"][i] or c, 1),
                     round(q["low"][i] or c, 1), round(c, 1), int(q["volume"][i] or 0)])
    return rows


def main():
    uni = json.load(open(UNIVERSE_PATH, encoding="utf-8"))
    prev = {}
    if os.path.exists(OUT_PATH):
        try:
            prev = json.load(open(OUT_PATH, encoding="utf-8")).get("stocks", {})
        except Exception:
            prev = {}
    out = {}
    ok = fail = 0
    for s in uni["stocks"]:
        code = s["code"]
        try:
            rows = fetch_daily(code)
            if len(rows) < 20:
                raise RuntimeError("データ不足")
            out[code] = {"name": s["name"], "group": s["group"], "sector": s.get("sector", ""), "rows": rows}
            ok += 1
            print(f"{code} {s['name']}: {len(rows)} 日 ({rows[0][0]}〜{rows[-1][0]})")
        except Exception as e:
            print(f"{code} {s['name']}: 失敗 {e}", file=sys.stderr)
            if code in prev:
                out[code] = prev[code]
            fail += 1
        time.sleep(0.8 + random.random() * 0.6)
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).strftime("%Y-%m-%dT%H:%M+09:00")
    json.dump({"updated": now, "range": RANGE, "ok": ok, "failed": fail, "stocks": out},
              open(OUT_PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"完了: 成功 {ok} / 失敗 {fail}")


if __name__ == "__main__":
    main()
