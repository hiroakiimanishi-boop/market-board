#!/usr/bin/env python3
"""国内個別株スキャナー（GitHub Actions が定期実行）。

1. JPX の東証上場銘柄一覧（data_j.xlsx）から内国株式の一覧を作る（universe.json、7日ごとに更新）
2. Yahoo Finance から 1 銘柄あたり 3 リクエストで
   - 株価・時価総額・PBR・PER・配当利回り・ROA・営業利益率・現金・有利子負債（quoteSummary）
   - 過去 5〜6 期の純利益・売上・営業利益・自己資本・総資産（fundamentals-timeseries）
   - 過去 10 年の月足高値（chart）
   を取得し、たーちゃん流シクリカルバリューの観点でスコア化して stocks.json に保存する
3. 1 回の実行で BATCH 銘柄（既定 500）だけ処理し、古い順に巡回する（全銘柄を数日で一巡）
"""
import json, os, sys, time, datetime as dt, random, urllib.parse, re
from fetch_data import http_get, yahoo_crumb

HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_PATH = os.path.join(HERE, "universe.json")
STOCKS_PATH = os.path.join(HERE, "stocks.json")
JPX_URL = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx"
BATCH = int(os.environ.get("BATCH", "500"))
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "40"))
MARKETS = ("プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）")

# 景気敏感（シクリカル）とみなす 33 業種
CYCLICAL = {
    "鉱業", "石油・石炭製品", "鉄鋼", "非鉄金属", "金属製品", "海運業", "空運業", "化学", "繊維製品",
    "パルプ・紙", "ガラス・土石製品", "機械", "電気機器", "輸送用機器", "精密機器", "建設業", "不動産業",
    "卸売業", "証券、商品先物取引業", "その他金融業", "銀行業", "倉庫・運輸関連業", "ゴム製品",
}


# ---------------- 銘柄一覧 ----------------
def load_universe():
    if os.path.exists(UNIVERSE_PATH):
        u = json.load(open(UNIVERSE_PATH, encoding="utf-8"))
        age = (dt.date.today() - dt.date.fromisoformat(u["date"])).days
        if age < 7 and len(u["stocks"]) > 1000:
            return u
    try:
        import openpyxl
        from curl_cffi import requests as cr
        r = cr.get(JPX_URL, impersonate="chrome", timeout=60)
        r.raise_for_status()
        tmp = os.path.join(HERE, "_data_j.xlsx")
        open(tmp, "wb").write(r.content)
        ws = openpyxl.load_workbook(tmp, read_only=True).active
        rows = ws.iter_rows(values_only=True)
        header = [str(c).strip() if c is not None else "" for c in next(rows)]
        idx = {name: header.index(name) for name in ("コード", "銘柄名", "市場・商品区分", "33業種区分", "規模区分")}
        stocks = {}
        for rec in rows:
            code = str(rec[idx["コード"]]).strip()
            market = str(rec[idx["市場・商品区分"]]).strip()
            if market not in MARKETS or not re.fullmatch(r"\d{4}", code):
                continue
            stocks[code] = {
                "name": str(rec[idx["銘柄名"]]).strip(),
                "market": market.replace("（内国株式）", ""),
                "sector": str(rec[idx["33業種区分"]]).strip(),
                "size": str(rec[idx["規模区分"]]).strip(),
            }
        os.remove(tmp)
        u = {"date": dt.date.today().isoformat(), "stocks": stocks}
        json.dump(u, open(UNIVERSE_PATH, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
        print(f"universe: JPX から {len(stocks)} 銘柄を取得")
        return u
    except Exception as e:
        print(f"universe: JPX 取得失敗 ({e})")
        if os.path.exists(UNIVERSE_PATH):
            return json.load(open(UNIVERSE_PATH, encoding="utf-8"))
        raise


# ---------------- Yahoo 取得 ----------------
def raw(d, *keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    if isinstance(d, dict):
        d = d.get("raw")
    return d if isinstance(d, (int, float)) else None


def quote_summary(sym):
    crumb = yahoo_crumb()
    url = (f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
           f"?modules=price,summaryDetail,defaultKeyStatistics,financialData&formatted=false"
           + (f"&crumb={urllib.parse.quote(crumb)}" if crumb else ""))
    d = json.loads(http_get(url))
    r = d["quoteSummary"]["result"][0]
    p, sd, ks, fd = (r.get(k) or {} for k in ("price", "summaryDetail", "defaultKeyStatistics", "financialData"))
    return {
        "price": raw(p, "regularMarketPrice"),
        "mcap": raw(p, "marketCap"),
        "pbr": raw(ks, "priceToBook"),
        "per": raw(sd, "trailingPE"),
        "fper": raw(sd, "forwardPE") or raw(ks, "forwardPE"),
        "div": raw(sd, "dividendYield"),          # 割合（0.03 = 3%）
        "roa": raw(fd, "returnOnAssets"),
        "roe": raw(fd, "returnOnEquity"),
        "opm": raw(fd, "operatingMargins"),
        "cash": raw(fd, "totalCash"),
        "debt": raw(fd, "totalDebt"),
        "ocf": raw(fd, "operatingCashflow"),
        "fcf": raw(fd, "freeCashflow"),
        "hi52": raw(sd, "fiftyTwoWeekHigh"),
        "lo52": raw(sd, "fiftyTwoWeekLow"),
    }


TS_TYPES = ["annualNetIncome", "annualTotalRevenue", "annualOperatingIncome", "annualStockholdersEquity",
            "annualTotalAssets", "annualCashAndCashEquivalents", "annualTotalDebt", "annualOperatingCashFlow"]


def timeseries(sym):
    now = int(time.time())
    url = (f"https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}"
           f"?symbol={sym}&type={','.join(TS_TYPES)}&period1={now - 7 * 366 * 86400}&period2={now}")
    d = json.loads(http_get(url))
    out = {}
    for res in d.get("timeseries", {}).get("result", []):
        t = (res.get("meta", {}).get("type") or [None])[0]
        if not t or t not in res:
            continue
        series = []
        for item in res[t]:
            if not item:
                continue
            v = raw(item, "reportedValue")
            if v is not None and item.get("asOfDate"):
                series.append([item["asOfDate"][:7], v])
        series.sort()
        out[t] = series[-6:]
    return out


def ten_year_high(sym):
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?range=10y&interval=1mo"
    d = json.loads(http_get(url))
    r = d["chart"]["result"][0]
    closes = [c for c in r["indicators"]["quote"][0].get("close", []) if c]
    highs = [c for c in r["indicators"]["quote"][0].get("high", []) if c]
    return (max(highs) if highs else (max(closes) if closes else None)), (r["timestamp"][0] if r.get("timestamp") else None)


# ---------------- 指標・スコア ----------------
def pct(a, b):
    return None if a is None or not b else a / b


def evaluate(info, q, ts, hi10):
    """スコアと主要指標をまとめる。閾値はたーちゃん流の目安。"""
    ni = [v for _, v in ts.get("annualNetIncome", [])]
    eq = ts.get("annualStockholdersEquity", [])
    ta = ts.get("annualTotalAssets", [])
    equity_ratio = None
    if eq and ta and eq[-1][0] == ta[-1][0] and ta[-1][1]:
        equity_ratio = eq[-1][1] / ta[-1][1]
    mcap = q.get("mcap")
    cash, debt = q.get("cash"), q.get("debt")
    if cash is None and ts.get("annualCashAndCashEquivalents"):
        cash = ts["annualCashAndCashEquivalents"][-1][1]
    if debt is None and ts.get("annualTotalDebt"):
        debt = ts["annualTotalDebt"][-1][1]
    net_cash = (cash - debt) if cash is not None and debt is not None else None
    net_cash_ratio = pct(net_cash, mcap)
    # 潜在収益力は「営業利益のピーク×0.7（税引後の概算）」を基本にする。
    # 純利益のピークは資産売却益などの一過性利益で膨らむことがあるため、営業利益が取れない場合のみ純利益を使う。
    oi = [v for _, v in ts.get("annualOperatingIncome", [])]
    peak_oi_after_tax = max(oi) * 0.7 if oi and max(oi) > 0 else None
    peak_ni_raw = max(ni) if ni else None
    peak_ni = peak_oi_after_tax if peak_oi_after_tax is not None else peak_ni_raw
    peak_per = (mcap / peak_ni) if mcap and peak_ni and peak_ni > 0 else None
    latest_ni = ni[-1] if ni else None
    loss_years = 0
    for v in reversed(ni):
        if v < 0:
            loss_years += 1
        else:
            break
    trough = None  # 直近利益 / ピーク利益（純利益ベース）
    if peak_ni_raw and peak_ni_raw > 0 and latest_ni is not None:
        trough = latest_ni / peak_ni_raw
    drawdown = (q["price"] / hi10 - 1) if q.get("price") and hi10 else None

    s = {}
    pbr = q.get("pbr")
    s["資産割安"] = 25 if pbr is not None and pbr <= 0.3 else 20 if pbr is not None and pbr <= 0.5 else \
        12 if pbr is not None and pbr <= 0.7 else 5 if pbr is not None and pbr <= 1.0 else 0
    s["底値圏"] = 20 if drawdown is not None and drawdown <= -0.7 else 15 if drawdown is not None and drawdown <= -0.5 else \
        8 if drawdown is not None and drawdown <= -0.3 else 0
    s["業績の谷"] = (10 if latest_ni is not None and latest_ni < 0 else 8 if trough is not None and trough <= 0.3 else
                 4 if trough is not None and trough <= 0.5 else 0) + (5 if loss_years >= 2 else 0)
    s["潜在収益力"] = 20 if peak_per is not None and peak_per <= 3 else 15 if peak_per is not None and peak_per <= 5 else \
        8 if peak_per is not None and peak_per <= 8 else 3 if peak_per is not None and peak_per <= 12 else 0
    s["生存力"] = (10 if equity_ratio is not None and equity_ratio >= 0.6 else 7 if equity_ratio is not None and equity_ratio >= 0.4 else
               3 if equity_ratio is not None and equity_ratio >= 0.2 else 0) + (5 if net_cash_ratio is not None and net_cash_ratio >= 0 else 0)
    s["配当"] = 5 if q.get("div") is not None and q["div"] >= 0.03 else 0
    score = min(100, sum(s.values()))

    per = q.get("per")
    flags = []
    if pbr is not None and pbr <= 0.5 and equity_ratio is not None and equity_ratio >= 0.6:
        flags.append("資産VAL")
    if (per is not None and per <= 15 and q.get("opm") is not None and q["opm"] >= 0.10
            and q.get("roa") is not None and q["roa"] >= 0.07 and mcap and mcap <= 300e8):
        flags.append("収益VAL")
    if info["sector"] in CYCLICAL:
        flags.append("景気敏感")
    if loss_years >= 2:
        flags.append("2期連続赤字")
    if net_cash_ratio is not None and net_cash_ratio >= 1:
        flags.append("ネットネット")  # ネットキャッシュが時価総額を上回る

    return {
        "n": info["name"], "m": info["market"], "sec": info["sector"], "sz": info["size"],
        "p": q.get("price"), "mc": mcap, "pbr": pbr, "per": per, "fper": q.get("fper"),
        "div": q.get("div"), "roa": q.get("roa"), "roe": q.get("roe"), "opm": q.get("opm"),
        "eqr": equity_ratio, "nc": net_cash, "ncr": net_cash_ratio, "ocf": q.get("ocf"),
        "ni": ts.get("annualNetIncome", []), "rev": ts.get("annualTotalRevenue", []), "oi": ts.get("annualOperatingIncome", []),
        "peakbase": "営業利益×0.7" if peak_oi_after_tax is not None else "純利益",
        "peakper": peak_per, "trough": trough, "loss": loss_years,
        "hi10": hi10, "dd": drawdown, "hi52": q.get("hi52"), "lo52": q.get("lo52"),
        "score": score, "sc": s, "flags": flags,
        "at": dt.date.today().isoformat(),
    }


# ---------------- メイン ----------------
def main():
    universe = load_universe()
    stocks = {}
    if os.path.exists(STOCKS_PATH):
        try:
            stocks = json.load(open(STOCKS_PATH, encoding="utf-8")).get("stocks", {})
        except Exception:
            stocks = {}
    # 上場廃止などで一覧から消えた銘柄を除く
    stocks = {c: v for c, v in stocks.items() if c in universe["stocks"]}

    def last_scan(code):
        v = stocks.get(code)
        return (v.get("at") or v.get("err_at") or "") if v else ""

    queue = sorted(universe["stocks"].keys(), key=lambda c: (last_scan(c), c))[:BATCH]
    print(f"scan: 対象 {len(universe['stocks'])} 銘柄中 {len(queue)} 銘柄を処理（最古 {last_scan(queue[0]) or '未取得'}）")

    t0 = time.time()
    ok = fail = 0
    consecutive_fail = 0
    for i, code in enumerate(queue):
        if (time.time() - t0) / 60 > MAX_MINUTES:
            print("scan: 時間上限に達したため中断")
            break
        info = universe["stocks"][code]
        sym = f"{code}.T"
        try:
            q = quote_summary(sym)
            ts = timeseries(sym)
            hi10, _ = ten_year_high(sym)
            if q.get("price") is None:
                raise RuntimeError("株価なし")
            stocks[code] = evaluate(info, q, ts, hi10)
            ok += 1
            consecutive_fail = 0
        except Exception as e:
            fail += 1
            consecutive_fail += 1
            msg = str(e)[:100]
            prev = stocks.get(code) or {}
            prev.update({"n": info["name"], "m": info["market"], "sec": info["sector"], "sz": info["size"],
                         "err": msg, "err_at": dt.date.today().isoformat()})
            stocks[code] = prev
            if consecutive_fail >= 15:
                print(f"scan: 連続失敗が続くため中断 ({msg})")
                break
        if (i + 1) % 50 == 0:
            print(f"  {i + 1}/{len(queue)} 済 (成功 {ok} / 失敗 {fail}) {int(time.time() - t0)}s")
            save(universe, stocks)
        time.sleep(0.3 + random.random() * 0.4)

    save(universe, stocks)
    print(f"done: 成功 {ok} / 失敗 {fail} / 累計 {sum(1 for v in stocks.values() if v.get('score') is not None)} 銘柄採点済み")
    return 0


def save(universe, stocks):
    scored = [v for v in stocks.values() if v.get("score") is not None]
    meta = {
        "updated": dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="minutes"),
        "universe": len(universe["stocks"]), "scanned": len(scored),
        "oldest": min((v["at"] for v in scored), default=None),
        "universe_date": universe["date"],
    }
    json.dump({"meta": meta, "stocks": stocks}, open(STOCKS_PATH, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    sys.exit(main())
