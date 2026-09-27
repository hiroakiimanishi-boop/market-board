#!/usr/bin/env python3
"""株価データを取得して data.json に書き出す（GitHub Actions が定期実行）。
Stooq を優先し、失敗したら Yahoo Finance に切り替える。"""
import json, os, sys, time, datetime as dt, urllib.request, urllib.parse, csv, io

YEARS = 6
SYMBOLS = {  # key: (stooq, yahoo)
    "n225":   ("^nkx",   "^N225"),
    "topix":  ("^tpx",   "^TPX"),
    "spx":    ("^spx",   "^GSPC"),
    "ndq":    ("^ndq",   "^IXIC"),
    "dji":    ("^dji",   "^DJI"),
    "dax":    ("^dax",   "^GDAXI"),
    "shc":    ("^shc",   "000001.SS"),
    "hsi":    ("^hsi",   "^HSI"),
    "usdjpy": ("usdjpy", "JPY=X"),
}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def from_stooq(sym, since):
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&i=d&d1={since}&d2={dt.date.today():%Y%m%d}"
    try:
        body = http_get(url)
    except Exception as e:
        return None, f"stooq: {e}"
    if "Exceeded" in body:
        return None, "stooq: 1日の取得上限に達しました"
    rows = []
    for rec in csv.reader(io.StringIO(body)):
        if len(rec) < 5 or not (len(rec[0]) == 10 and rec[0][4] == "-"):
            continue
        try:
            close = float(rec[4])
        except ValueError:
            continue
        if close > 0:
            rows.append([rec[0], close])
    return (rows, None) if len(rows) >= 20 else (None, "stooq: データなし")


def from_yahoo(sym):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}?range={YEARS}y&interval=1d"
    try:
        d = json.loads(http_get(url))
    except Exception as e:
        return None, f"yahoo: {e}"
    try:
        r = d["chart"]["result"][0]
        ts, cl = r["timestamp"], r["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return None, "yahoo: データなし"
    rows = []
    for t, v in zip(ts, cl):
        if v is not None and v > 0:
            rows.append([dt.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), round(float(v), 4)])
    return (rows, None) if len(rows) >= 20 else (None, "yahoo: データなし")


def main():
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    old = {}
    if os.path.exists(out_path):
        try:
            old = json.load(open(out_path, encoding="utf-8")).get("series", {})
        except Exception:
            pass
    since = (dt.date.today() - dt.timedelta(days=365 * YEARS)).strftime("%Y%m%d")
    out = {"updated": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes"), "series": {}}
    for key, (stooq, yahoo) in SYMBOLS.items():
        rows, err = from_stooq(stooq, since)
        src = "stooq"
        if rows is None:
            rows, err2 = from_yahoo(yahoo)
            src = "yahoo"
            err = f"{err} / {err2}" if rows is None else None
        if rows is not None:
            out["series"][key] = {"ok": True, "source": src, "rows": rows}
            print(f"{key}: {len(rows)} rows ({src})")
        elif key in old and old[key].get("ok"):
            item = dict(old[key]); item["stale"] = True
            out["series"][key] = item
            print(f"{key}: 取得失敗、前回データを保持 ({err})")
        else:
            out["series"][key] = {"ok": False, "error": err}
            print(f"{key}: 取得失敗 ({err})")
        time.sleep(1)
    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    ok = sum(1 for v in out["series"].values() if v.get("ok"))
    print(f"done: {ok}/{len(SYMBOLS)} series")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
