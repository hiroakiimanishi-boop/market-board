#!/usr/bin/env python3
"""株価データを取得して data.json に書き出す（GitHub Actions が定期実行）。
取得順: Yahoo Finance（cookie/crumb 付き）→ FRED（米国指数・日経・ドル円のみ）→ Stooq。
すべて失敗した銘柄は前回のデータを保持する。"""
import json, os, sys, time, datetime as dt, csv, io, random
import urllib.request, urllib.error, urllib.parse, http.cookiejar

YEARS = 6
# key: (yahoo, fred, stooq)
SYMBOLS = {
    "n225":   ("^N225",     "NIKKEI225", "^nkx"),
    "topix":  ("^TPX",      None,        "^tpx"),
    "spx":    ("^GSPC",     "SP500",     "^spx"),
    "ndq":    ("^IXIC",     "NASDAQCOM", "^ndq"),
    "dji":    ("^DJI",      "DJIA",      "^dji"),
    "dax":    ("^GDAXI",    None,        "^dax"),
    "shc":    ("000001.SS", None,        "^shc"),
    "hsi":    ("^HSI",      None,        "^hsi"),
    "usdjpy": ("JPY=X",     "DEXJPUS",   "usdjpy"),
}
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# curl_cffi があればブラウザ(Chrome)になりすまして通信する（Yahoo の 429 対策）
try:
    from curl_cffi import requests as cffi_requests
    _session = cffi_requests.Session(impersonate="chrome")
    print("curl_cffi: 使用")
except Exception as _e:  # 未インストール時は標準ライブラリ
    _session = None
    print(f"curl_cffi: 未使用 ({_e})")
_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_jar))


def http_get(url, headers=None, retries=2):
    h = {"Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    if _session is None:
        h["User-Agent"] = UA
    if headers:
        h.update(headers)
    last = None
    for i in range(retries):
        try:
            if _session is not None:
                r = _session.get(url, headers=h, timeout=30, allow_redirects=True)
                if r.status_code >= 400:
                    raise urllib.error.HTTPError(url, r.status_code, "err", None, io.BytesIO(r.content[:200]))
                return r.text
            with _opener.open(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:120]
            last = RuntimeError(f"HTTP {e.code} {body!r}")
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:
            last = e
        time.sleep(3 * (i + 1) + random.random() * 2)
    raise last


def is_date(s):
    return len(s) == 10 and s[4] == "-" and s[7] == "-"


# ---------- Yahoo Finance ----------
_crumb = None


def yahoo_crumb():
    global _crumb
    if _crumb is not None:
        return _crumb
    try:
        http_get("https://fc.yahoo.com/", retries=1)
    except Exception:
        pass  # 404 でも cookie は付与される
    for host in ("query1", "query2"):
        try:
            c = http_get(f"https://{host}.finance.yahoo.com/v1/test/getcrumb", retries=2).strip()
            if c and "<" not in c:
                _crumb = c
                return c
        except Exception:
            pass
    _crumb = ""
    return ""


def from_yahoo(sym):
    crumb = yahoo_crumb()
    errs = []
    for host in ("query2", "query1"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
               f"?range={YEARS}y&interval=1d&includePrePost=false&events=div%2Csplits"
               + (f"&crumb={urllib.parse.quote(crumb)}" if crumb else ""))
        try:
            d = json.loads(http_get(url))
            r = d["chart"]["result"][0]
            ts, cl = r["timestamp"], r["indicators"]["quote"][0]["close"]
        except Exception as e:
            errs.append(f"{host}: {e}")
            continue
        rows = [[dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d"), round(float(v), 4)]
                for t, v in zip(ts, cl) if v is not None and v > 0]
        if len(rows) >= 20:
            return rows, None
        errs.append(f"{host}: データなし")
    return None, "yahoo: " + " / ".join(errs)


# ---------- FRED（セントルイス連銀）----------
def from_fred(series_id, since_date):
    if not series_id:
        return None, "fred: 対象外"
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={since_date}"
    try:
        body = http_get(url)
    except Exception as e:
        return None, f"fred: {e}"
    rows = []
    for rec in csv.reader(io.StringIO(body)):
        if len(rec) < 2 or not is_date(rec[0]):
            continue
        try:
            v = float(rec[1])
        except ValueError:
            continue  # 休場日は "." が入る
        if v > 0:
            rows.append([rec[0], v])
    return (rows, None) if len(rows) >= 20 else (None, "fred: データなし")


# ---------- Stooq ----------
def from_stooq(sym, since_yyyymmdd):
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&i=d&d1={since_yyyymmdd}&d2={dt.date.today():%Y%m%d}"
    try:
        body = http_get(url, retries=1)
    except Exception as e:
        return None, f"stooq: {e}"
    if "Exceeded" in body:
        return None, "stooq: 取得上限"
    if "<html" in body[:500].lower():
        return None, "stooq: アクセス制限"
    rows = []
    for rec in csv.reader(io.StringIO(body)):
        if len(rec) < 5 or not is_date(rec[0]):
            continue
        try:
            close = float(rec[4])
        except ValueError:
            continue
        if close > 0:
            rows.append([rec[0], close])
    return (rows, None) if len(rows) >= 20 else (None, "stooq: データなし")


def main():
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
    old = {}
    if os.path.exists(out_path):
        try:
            old = json.load(open(out_path, encoding="utf-8")).get("series", {})
        except Exception:
            pass
    since = dt.date.today() - dt.timedelta(days=365 * YEARS)
    jst = dt.timezone(dt.timedelta(hours=9))
    out = {"updated": dt.datetime.now(jst).isoformat(timespec="minutes"), "series": {}}

    for key, (ysym, fred_id, ssym) in SYMBOLS.items():
        errs = []
        rows, src = None, None
        for name, fn in (("yahoo", lambda: from_yahoo(ysym)),
                         ("fred", lambda: from_fred(fred_id, since.isoformat())),
                         ("stooq", lambda: from_stooq(ssym, since.strftime("%Y%m%d")))):
            rows, err = fn()
            if rows is not None:
                src = name
                break
            errs.append(err)
        if rows is not None:
            out["series"][key] = {"ok": True, "source": src, "rows": rows}
            print(f"{key}: {len(rows)} rows ({src}) 最新 {rows[-1]}")
        elif key in old and old[key].get("ok"):
            item = dict(old[key]); item["stale"] = True; item["error"] = " | ".join(errs)
            out["series"][key] = item
            print(f"{key}: 取得失敗、前回データを保持 ({' | '.join(errs)})")
        else:
            out["series"][key] = {"ok": False, "error": " | ".join(errs)}
            print(f"{key}: 取得失敗 ({' | '.join(errs)})")
        time.sleep(1.5 + random.random())

    json.dump(out, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    ok = sum(1 for v in out["series"].values() if v.get("ok"))
    print(f"done: {ok}/{len(SYMBOLS)} series")
    return 0


if __name__ == "__main__":
    sys.exit(main())
