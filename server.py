#!/usr/bin/env python3
"""
台股盤中「整張 vs 零股」價差即時查詢伺服器
- 零第三方套件依賴（僅使用 Python 3 標準函式庫）
- 盤中優先呼叫證交所/櫃買中心基本市況報導 API (mis.twse.com.tw)
  - 整股: getStockInfo.jsp
  - 盤中零股: getOddInfo.jsp
- 盤後或 MIS 無法連線時，自動備援至 TWSE / TPEx 官方盤後 API
"""

import http.cookiejar
import http.server
import json
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

HOST = "127.0.0.1"
PORT = 8765
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
}

# MIS session with CookieJar
_mis_lock = threading.Lock()
_mis_cj = http.cookiejar.CookieJar()
_mis_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=SSL_CTX),
    urllib.request.HTTPCookieProcessor(_mis_cj),
)
_mis_cookie_ts = 0.0

# After-hours fallback cache
_after_lock = threading.Lock()
_after_cache = {
    "ts": 0.0,
    "date": "",
    "stocks": {},  # code -> dict
}
AFTER_CACHE_TTL = 300  # 5 minutes


def parse_float(val):
    if val is None:
        return None
    s = str(val).strip().replace(",", "")
    if not s or s in ("-", "--", "---", "N/A", "null", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_first_book_price(book_str):
    """Parse first price from MIS 'b' or 'a' field like '1050.0000_1045.0000_...'"""
    if not book_str:
        return None
    parts = [p for p in str(book_str).split("_") if p and p != "-"]
    if not parts:
        return None
    return parse_float(parts[0])


def parse_order_book(price_str, vol_str):
    """Parse MIS 5-level price & volume strings into [{'price': float, 'vol': int}, ...]."""
    if not price_str:
        return []
    prices = [p.strip() for p in str(price_str).split("_") if p.strip() != ""]
    vols = [v.strip() for v in str(vol_str or "").split("_") if v.strip() != ""]
    levels = []
    for i, p_raw in enumerate(prices[:5]):
        p = parse_float(p_raw)
        if p is None or p <= 0:
            continue
        v_raw = vols[i] if i < len(vols) else ""
        v_num = parse_float(v_raw)
        levels.append({
            "price": p,
            "vol": int(v_num) if v_num is not None else 0,
        })
    return levels


def ensure_mis_cookie(timeout=5):
    global _mis_cookie_ts
    now = time.time()
    if now - _mis_cookie_ts < 600 and len(_mis_cj) > 0:
        return
    req = urllib.request.Request(
        "https://mis.twse.com.tw/stock/index.jsp",
        headers={**HEADERS, "Referer": "https://mis.twse.com.tw/stock/index.jsp"},
    )
    with _mis_opener.open(req, timeout=timeout) as resp:
        resp.read(256)
    _mis_cookie_ts = time.time()


def fetch_mis_endpoint(endpoint, ex_ch_list, timeout=6):
    """Fetch getStockInfo.jsp or getOddInfo.jsp for a list of ex_ch channels."""
    if not ex_ch_list:
        return {}
    result = {}
    # Batch up to 60 channels per request
    batch_size = 60
    with _mis_lock:
        ensure_mis_cookie(timeout=timeout)
        for i in range(0, len(ex_ch_list), batch_size):
            batch = ex_ch_list[i : i + batch_size]
            ex_ch = "|".join(batch)
            qs = urllib.parse.urlencode({"ex_ch": ex_ch, "json": "1", "delay": "0"})
            url = f"https://mis.twse.com.tw/stock/api/{endpoint}?{qs}"
            req = urllib.request.Request(
                url,
                headers={**HEADERS, "Referer": "https://mis.twse.com.tw/stock/odd.jsp"},
            )
            with _mis_opener.open(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for item in data.get("msgArray", []):
                code = str(item.get("c", "")).strip()
                if code:
                    result[code] = item
    return result


def extract_mis_price(item):
    """Extract trade price, bid, ask, prev_close, volume, time, market from MIS item."""
    if not item:
        return None
    z = parse_float(item.get("z"))
    bids = parse_order_book(item.get("b"), item.get("g"))
    asks = parse_order_book(item.get("a"), item.get("f"))
    bid = bids[0]["price"] if bids else parse_first_book_price(item.get("b"))
    ask = asks[0]["price"] if asks else parse_first_book_price(item.get("a"))
    y = parse_float(item.get("y"))
    u = parse_float(item.get("u"))
    w = parse_float(item.get("w"))
    v = item.get("v", "-")
    t = item.get("t", "")
    d = item.get("d", "")
    ex = item.get("ex", "")  # 'tse' or 'otc'
    name = item.get("n", "")

    price = z
    price_type = "成交"
    if price is None or price <= 0:
        if bid and ask:
            price = round((bid + ask) / 2.0, 4)
            price_type = "委買賣均"
        elif bid:
            price = bid
            price_type = "委買"
        elif ask:
            price = ask
            price_type = "委賣"
        else:
            price = None
            price_type = "無成交"

    return {
        "name": name,
        "market": "上市" if ex == "tse" else ("上櫃" if ex == "otc" else ex),
        "price": price,
        "price_type": price_type,
        "trade_price": z,
        "bid": bid,
        "ask": ask,
        "bids": bids,
        "asks": asks,
        "prev_close": y,
        "limit_up": u,
        "limit_down": w,
        "volume": v,
        "time": t,
        "date": d,
    }


def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def load_after_hours_data():
    """Fallback: load latest TWSE + TPEx after-hours regular & odd-lot quotes."""
    now = time.time()
    with _after_lock:
        if now - _after_cache["ts"] < AFTER_CACHE_TTL and _after_cache["stocks"]:
            return _after_cache["stocks"], _after_cache["date"]

        stocks = {}
        data_date = ""
        today_str = datetime.now().strftime("%Y%m%d")

        # 1. TWSE MI_INDEX + TWT53U
        try:
            mi = fetch_json(
                f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={today_str}&type=ALLBUT0999&response=json"
            )
            if mi.get("stat") != "OK":
                # Try OpenAPI latest snapshot if today has no after-hours yet
                day_all = fetch_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
                for r in day_all:
                    code = str(r.get("Code", "")).strip()
                    if not code:
                        continue
                    data_date = data_date or str(r.get("Date", ""))
                    stocks[code] = {
                        "name": str(r.get("Name", "")).strip(),
                        "market": "上市",
                        "reg_price": parse_float(r.get("ClosingPrice")),
                        "reg_vol": r.get("TradeVolume", "-"),
                        "odd_price": None,
                        "odd_bid": None,
                        "odd_ask": None,
                        "odd_vol": "-",
                    }
            else:
                data_date = mi.get("date", today_str)
                tables = mi.get("tables", [])
                if len(tables) > 8:
                    for r in tables[8].get("data", []):
                        code = str(r[0]).strip()
                        stocks[code] = {
                            "name": str(r[1]).strip(),
                            "market": "上市",
                            "reg_price": parse_float(r[8]),
                            "reg_vol": r[2],
                            "odd_price": None,
                            "odd_bid": None,
                            "odd_ask": None,
                            "odd_vol": "-",
                        }
        except Exception:
            pass

        try:
            t53 = fetch_json(
                f"https://www.twse.com.tw/rwd/zh/afterTrading/TWT53U?date={today_str}&response=json"
            )
            if t53.get("stat") == "OK":
                for r in t53.get("data", []):
                    code = str(r[0]).strip()
                    if code not in stocks:
                        stocks[code] = {
                            "name": str(r[1]).strip(),
                            "market": "上市",
                            "reg_price": None,
                            "reg_vol": "-",
                        }
                    stocks[code]["odd_price"] = parse_float(r[5])
                    stocks[code]["odd_bid"] = parse_float(r[6])
                    stocks[code]["odd_bid_vol"] = parse_float(r[7])
                    stocks[code]["odd_ask"] = parse_float(r[8])
                    stocks[code]["odd_ask_vol"] = parse_float(r[9])
                    stocks[code]["odd_vol"] = r[2]
            else:
                t53_open = fetch_json("https://openapi.twse.com.tw/v1/exchangeReport/TWT53U")
                for r in t53_open:
                    code = str(r.get("Code", "")).strip()
                    if code in stocks:
                        stocks[code]["odd_price"] = parse_float(r.get("TradePrice"))
                        stocks[code]["odd_bid"] = parse_float(r.get("BestBidPrice"))
                        stocks[code]["odd_bid_vol"] = parse_float(r.get("BestBidVolume"))
                        stocks[code]["odd_ask"] = parse_float(r.get("BestAskPrice"))
                        stocks[code]["odd_ask_vol"] = parse_float(r.get("BestAskVolume"))
                        stocks[code]["odd_vol"] = r.get("TradeVolume", "-")
        except Exception:
            pass

        # 2. TPEx OpenAPI
        try:
            tpex_day = fetch_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
            for r in tpex_day:
                code = str(r.get("SecuritiesCompanyCode", "")).strip()
                if not code or (len(code) > 5 and not code.startswith("00")):
                    continue
                if not data_date:
                    data_date = str(r.get("Date", ""))
                stocks[code] = {
                    "name": str(r.get("CompanyName", "")).strip(),
                    "market": "上櫃",
                    "reg_price": parse_float(r.get("Close")),
                    "reg_bid": parse_float(r.get("LatestBidPrice")),
                    "reg_ask": parse_float(r.get("LatesAskPrice")),
                    "reg_vol": r.get("TradingShares", "-"),
                    "odd_price": None,
                    "odd_bid": None,
                    "odd_ask": None,
                    "odd_vol": "-",
                }
        except Exception:
            pass

        try:
            tpex_odd = fetch_json("https://www.tpex.org.tw/openapi/v1/tpex_odd_stock")
            for r in tpex_odd:
                code = str(r.get("SecuritiesCompanyCode", "")).strip()
                if code in stocks:
                    stocks[code]["odd_price"] = parse_float(r.get("Price"))
                    stocks[code]["odd_bid"] = parse_float(r.get("LastBestBidPrice"))
                    stocks[code]["odd_bid_vol"] = parse_float(r.get("LastBestBidVolume"))
                    stocks[code]["odd_ask"] = parse_float(r.get("LastBestAskPrice"))
                    stocks[code]["odd_ask_vol"] = parse_float(r.get("LastBestAskVolume"))
                    stocks[code]["odd_vol"] = r.get("TradeVolume", "-")
        except Exception:
            pass

        _after_cache["ts"] = time.time()
        _after_cache["date"] = data_date
        _after_cache["stocks"] = stocks
        return stocks, data_date


def query_stocks(codes, force_mode="auto"):
    """
    Query whole-lot vs odd-lot spread for a list of stock codes.
    force_mode: 'auto' (try MIS intraday first, fallback to after-hours), 'intraday', 'after'
    """
    clean_codes = []
    seen = set()
    for c in codes:
        c = str(c).strip().upper()
        if c and c not in seen:
            seen.add(c)
            clean_codes.append(c)

    if not clean_codes:
        return {"mode": "none", "timestamp": datetime.now().strftime("%H:%M:%S"), "items": []}

    mis_error = None
    if force_mode in ("auto", "intraday"):
        try:
            ex_ch_list = []
            for c in clean_codes:
                ex_ch_list.append(f"tse_{c}.tw")
                ex_ch_list.append(f"otc_{c}.tw")

            reg_raw = fetch_mis_endpoint("getStockInfo.jsp", ex_ch_list, timeout=6)
            odd_raw = fetch_mis_endpoint("getOddInfo.jsp", ex_ch_list, timeout=6)

            if reg_raw or odd_raw:
                items = []
                for c in clean_codes:
                    reg = extract_mis_price(reg_raw.get(c))
                    odd = extract_mis_price(odd_raw.get(c))
                    if not reg and not odd:
                        items.append({"code": c, "found": False, "error": "查無此代號"})
                        continue
                    reg_p = reg["price"] if reg else None
                    odd_p = odd["price"] if odd else None
                    diff = None
                    diff_pct = None
                    if reg_p and odd_p and reg_p > 0:
                        diff = round(odd_p - reg_p, 4)
                        diff_pct = round((odd_p - reg_p) / reg_p * 100.0, 2)

                    items.append({
                        "code": c,
                        "found": True,
                        "name": (reg or odd)["name"],
                        "market": (reg or odd)["market"],
                        "reg_price": reg_p,
                        "reg_price_type": reg["price_type"] if reg else "無",
                        "reg_bid": reg["bid"] if reg else None,
                        "reg_ask": reg["ask"] if reg else None,
                        "reg_bids": reg["bids"] if reg else [],
                        "reg_asks": reg["asks"] if reg else [],
                        "reg_vol": reg["volume"] if reg else "-",
                        "reg_time": reg["time"] if reg else "",
                        "odd_price": odd_p,
                        "odd_price_type": odd["price_type"] if odd else "無",
                        "odd_bid": odd["bid"] if odd else None,
                        "odd_ask": odd["ask"] if odd else None,
                        "odd_bids": odd["bids"] if odd else [],
                        "odd_asks": odd["asks"] if odd else [],
                        "odd_vol": odd["volume"] if odd else "-",
                        "odd_time": odd["time"] if odd else "",
                        "diff": diff,
                        "diff_pct": diff_pct,
                        "date": (reg or odd)["date"],
                    })
                return {
                    "mode": "intraday",
                    "mode_label": "盤中即時 (TWSE/TPEx MIS)",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "items": items,
                }
        except Exception as e:
            mis_error = str(e)
            if force_mode == "intraday":
                return {
                    "mode": "error",
                    "mode_label": "盤中即時連線失敗",
                    "error": f"無法連線至 mis.twse.com.tw（非盤中時段常會關閉或維護）：{mis_error}",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "items": [],
                }

    # Fallback to after-hours official API
    stocks, data_date = load_after_hours_data()
    items = []
    for c in clean_codes:
        info = stocks.get(c)
        if not info:
            items.append({"code": c, "found": False, "error": "查無此代號"})
            continue
        reg_p = info.get("reg_price")
        odd_p = info.get("odd_price")
        diff = None
        diff_pct = None
        if reg_p and odd_p and reg_p > 0:
            diff = round(odd_p - reg_p, 4)
            diff_pct = round((odd_p - reg_p) / reg_p * 100.0, 2)
        odd_bids = []
        if info.get("odd_bid"):
            odd_bids.append({"price": info["odd_bid"], "vol": int(info.get("odd_bid_vol") or 0)})
        odd_asks = []
        if info.get("odd_ask"):
            odd_asks.append({"price": info["odd_ask"], "vol": int(info.get("odd_ask_vol") or 0)})
        reg_bids = [{"price": info["reg_bid"], "vol": 0}] if info.get("reg_bid") else []
        reg_asks = [{"price": info["reg_ask"], "vol": 0}] if info.get("reg_ask") else []

        items.append({
            "code": c,
            "found": True,
            "name": info.get("name", ""),
            "market": info.get("market", ""),
            "reg_price": reg_p,
            "reg_price_type": "收盤" if reg_p else "無成交",
            "reg_bid": info.get("reg_bid"),
            "reg_ask": info.get("reg_ask"),
            "reg_bids": reg_bids,
            "reg_asks": reg_asks,
            "reg_vol": info.get("reg_vol", "-"),
            "reg_time": "13:30",
            "odd_price": odd_p,
            "odd_price_type": "盤後成交" if odd_p else "無成交",
            "odd_bid": info.get("odd_bid"),
            "odd_ask": info.get("odd_ask"),
            "odd_bids": odd_bids,
            "odd_asks": odd_asks,
            "odd_vol": info.get("odd_vol", "-"),
            "odd_time": "14:30",
            "diff": diff,
            "diff_pct": diff_pct,
            "date": data_date,
        })

    return {
        "mode": "after",
        "mode_label": f"盤後資料備援 ({data_date})",
        "notice": "目前非盤中或 MIS 伺服器無回應，已自動切換為官方盤後收盤/盤後零股資料。" if mis_error else "",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items,
    }


class RequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Keep console clean

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            html_path = os.path.join(BASE_DIR, "index.html")
            with open(html_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/quote":
            qs = urllib.parse.parse_qs(parsed.query)
            raw_codes = qs.get("codes", [""])[0]
            mode = qs.get("mode", ["auto"])[0]
            codes = [c.strip() for c in raw_codes.replace(" ", ",").split(",") if c.strip()]
            res = query_stocks(codes, force_mode=mode)
            self.send_json(res)
            return

        self.send_response(404)
        self.end_headers()


if __name__ == "__main__":
    server = http.server.ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"==========================================")
    print(f" 台股盤中「整張 vs 零股」價差監控已啟動！")
    print(f" 請用瀏覽器開啟： http://{HOST}:{PORT}")
    print(f" 關閉此視窗即可停止伺服器")
    print(f"==========================================")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止伺服器。")
