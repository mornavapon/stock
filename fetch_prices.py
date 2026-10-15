"""ดึงราคาหุ้นจาก Yahoo Finance ตามรายชื่อใน tickers.json แล้วเก็บลง prices.json"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote
from urllib.request import Request, urlopen

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}


def fetch(symbol):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           + quote(symbol) + "?interval=1d&range=5d")
    for _ in range(3):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=20) as r:
                data = json.load(r)
            res = data["chart"]["result"][0]
            price = res["meta"]["regularMarketPrice"]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            if len(closes) >= 2:
                prev = closes[-2]
            else:
                prev = res["meta"].get("chartPreviousClose") or price
            change = price - prev
            pct = (change / prev * 100) if prev else 0.0
            q = {"p": round(price, 4), "c": round(change, 4), "pct": round(pct, 4)}
            t = res["meta"].get("regularMarketTime")
            if t:
                q["t"] = int(t)  # เวลาที่ตลาดซื้อขายล่าสุด (วินาที) ใช้เช็กว่าข้อมูลเก่าไหม
            return symbol, q
        except Exception:
            continue
    return symbol, None


def main():
    with open("tickers.json", encoding="utf-8") as f:
        groups = json.load(f)["groups"]
    symbols = []
    for g in groups:
        for s in g["symbols"]:
            if s not in symbols:
                symbols.append(s)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(fetch, symbols))

    quotes = {s: q for s, q in results if q}
    missing = [s for s, q in results if not q]
    print("ได้ราคา", len(quotes), "ตัว จาก", len(symbols))
    if missing:
        print("ไม่ได้ราคา:", ", ".join(missing))
    if not quotes:
        print("ดึงราคาไม่ได้เลย จึงไม่แก้ไฟล์ prices.json")
        sys.exit(1)

    out = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "quotes": quotes,
    }
    with open("prices.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    main()
