#!/usr/bin/env python3
"""ดึงราคาหุ้น อัตราแลกเปลี่ยน และข้อมูล valuation แล้วเขียนลง prices.json

อ่านรายชื่อจาก symbols.json:
  {"symbols": ["PTT.BK", "AAPL", "GC=F"], "currencies": ["USD", "EUR", "VND"]}

ราคา / อัตราแลกเปลี่ยน : Yahoo Finance (ทุกรอบที่รัน)
Valuation (มูลค่าตลาด, P/E, P/BV, ปันผล) : รีเฟรชไม่เกินวันละ 2 ครั้งต่อหุ้น
  - หุ้นไทย (.BK)  : set.or.th หน้า Factsheet (ล้านบาท, P/E, P/BV) ปรับตามราคาล่าสุด
                     ถ้า set.or.th ดึงไม่ได้ จะใช้ Yahoo Finance แทน
  - หุ้นต่างประเทศ : Yahoo Finance
  - ปันผลต่อปี (D/V): ผลรวมปันผลย้อนหลัง 12 เดือน หารด้วยราคา (จากข้อมูลของ Yahoo)

หลักการ: ส่วนไหนดึงไม่ได้ จะคงค่าเดิมไว้ ไม่ทำให้ราคาหรือทั้งไฟล์พัง
อัตราแลกเปลี่ยนทั้งหมดเก็บเป็น "1 หน่วยสกุลนั้น = กี่บาท"
"""
import html as htmlmod
import json
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
SET_FACTSHEET = "https://www.set.or.th/en/market/product/stock/quote/{}/factsheet"
# สกุลเงินที่ Yahoo รายงานเป็นหน่วยย่อย (เช่น เพนนี) -> (สกุลหลัก, ตัวหาร)
MINOR_UNITS = {"GBp": ("GBP", 100.0), "GBX": ("GBP", 100.0),
               "ZAc": ("ZAR", 100.0), "ILA": ("ILS", 100.0)}
MAX_ATTEMPTS = 3          # ลองต่อตัวกี่ครั้ง
BACKOFF = [6, 15]         # วินาทีที่รอก่อนลองใหม่ (ครั้งที่ 1, 2)
STOP_AFTER_BLOCKED = 3    # โดนบล็อกติดกันกี่ตัวแล้วหยุดรอบนี้
VAL_TTL = 12 * 3600       # valuation ของแต่ละตัวรีเฟรชไม่ถี่กว่านี้ (วินาที)


class HttpErr(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"HTTP {code} {msg}".strip())
        self.code = code


class Blocked(Exception):
    """Yahoo ปฏิเสธ (429/อื่นๆ) ต่อเนื่อง ไม่ใช่ความผิดของชื่อหุ้น"""


_session = None  # None = ยังไม่ลอง, False = ใช้ curl_cffi ไม่ได้


def get_session():
    """คืน session ของ curl_cffi (เลียนแบบ Chrome) หรือ None ถ้าใช้ไม่ได้"""
    global _session
    if _session is False:
        return None
    if _session is not None:
        return _session
    try:
        from curl_cffi import requests as cr
    except ImportError:
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "curl_cffi"],
                           check=True, timeout=240)
            from curl_cffi import requests as cr
        except Exception as e:  # noqa: BLE001
            print("ใช้ curl_cffi ไม่ได้ จะใช้ urllib แทน:", e, file=sys.stderr)
            _session = False
            return None
    try:
        s = cr.Session(impersonate="chrome")
        try:
            s.get("https://fc.yahoo.com", timeout=15)  # ขอ cookie เหมือนเบราว์เซอร์ (ตอบ 404 ได้ ไม่เป็นไร)
        except Exception:  # noqa: BLE001
            pass
        _session = s
        return s
    except Exception as e:  # noqa: BLE001
        print("เริ่ม curl_cffi ไม่ได้ จะใช้ urllib แทน:", e, file=sys.stderr)
        _session = False
        return None


def http_text(url, headers=None):
    hdr = {"Accept": "text/html,application/json,*/*", "Accept-Language": "en-US,en;q=0.9"}
    if headers:
        hdr.update(headers)
    s = get_session()
    if s is not None:
        r = s.get(url, headers=hdr, timeout=30)
        if r.status_code != 200:
            raise HttpErr(r.status_code)
        return r.text
    hdr["User-Agent"] = UA
    req = urllib.request.Request(url, headers=hdr)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise HttpErr(e.code, e.reason) from None


def http_json(host, path):
    return json.loads(http_text("https://" + host + path, {"Accept": "application/json,*/*"}))


def fetch_chart(symbol, query="interval=1d&range=5d"):
    path = "/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?" + query
    last = None
    for attempt in range(MAX_ATTEMPTS):
        host = HOSTS[attempt % len(HOSTS)]
        try:
            return http_json(host, path)
        except HttpErr as e:
            last = e
            if e.code == 404:
                raise RuntimeError(f"{symbol}: Yahoo ไม่รู้จักชื่อนี้ (404) ตรวจการสะกดหรือ .BK/.PA") from None
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < MAX_ATTEMPTS - 1:
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)] + random.uniform(0, 2))
    raise Blocked(f"{symbol}: {last}")


def parse_quote(data):
    """คืน dict ราคาหนึ่งตัว จากผลลัพธ์ chart API"""
    res = data["chart"]["result"][0]
    meta = res["meta"]
    price = meta.get("regularMarketPrice")
    if price is None:
        raise ValueError("no regularMarketPrice")
    closes = []
    try:
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
    except (KeyError, IndexError, TypeError):
        pass
    # ราคาปิดก่อนหน้าของรอบการซื้อขายล่าสุด = แท่งรองสุดท้าย
    prev = closes[-2] if len(closes) >= 2 else None
    cur = meta.get("currency") or "USD"
    if cur in MINOR_UNITS:
        cur, div = MINOR_UNITS[cur]
        price = price / div
        prev = prev / div if prev is not None else None
    q = {"price": round(float(price), 6), "currency": cur}
    if prev is not None:
        q["prevClose"] = round(float(prev), 6)
    t = meta.get("regularMarketTime")
    if t:
        q["t"] = int(t)
    nm = meta.get("longName") or meta.get("shortName")
    if nm:
        q["name"] = " ".join(str(nm).split())[:80]
    return q


def closes_by_date(data):
    """{'2026-10-21': 335.0, ...} ราคาปิดรายวันตามวันที่ท้องถิ่นของตลาด (ใช้ปรับ valuation ให้ตรงราคา)"""
    try:
        res = data["chart"]["result"][0]
        off = res["meta"].get("gmtoffset") or 0
        ts = res.get("timestamp") or []
        cl = res["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return {}
    out = {}
    for t, c in zip(ts, cl):
        if c is None:
            continue
        out[datetime.fromtimestamp(t + off, timezone.utc).date().isoformat()] = float(c)
    return out


# ---------------------------------------------------------------- อัตราแลกเปลี่ยน
def fx_usdthb():
    d = fetch_chart("USDTHB=X")["chart"]["result"][0]["meta"]
    return float(d["regularMarketPrice"])


def fx_to_thb(cur, usdthb):
    """คืนอัตรา 1 cur = กี่บาท"""
    if cur == "THB":
        return 1.0
    if cur == "USD":
        return usdthb
    try:
        d = fetch_chart(f"{cur}THB=X")["chart"]["result"][0]["meta"]
        return float(d["regularMarketPrice"])
    except Exception as e:  # noqa: BLE001
        print("fx direct failed", cur, e, file=sys.stderr)
    # สำรอง: คำนวณผ่านดอลลาร์ (USD/THB หารด้วย USD/cur)
    d = fetch_chart(f"USD{cur}=X")["chart"]["result"][0]["meta"]
    return usdthb / float(d["regularMarketPrice"])


# ---------------------------------------------------------------- valuation
def html_to_text(h):
    h = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", htmlmod.unescape(h))


def _num(tok):
    try:
        return float(tok.replace(",", ""))
    except (ValueError, AttributeError):
        return None


def parse_set_factsheet(page):
    """อ่าน Market Cap (ล้านบาท), P/E, P/BV และวันที่ข้อมูล จากหน้า Factsheet ของ set.or.th"""
    t = html_to_text(page)

    def grab(label):
        m = re.search(label + r"\s*:?\s*(-?[\d,]*\.?\d+|N/?A|-)(?![\w.])", t, re.I)
        return _num(m.group(1)) if m else None

    f = {
        "mcap": grab(r"Market\s*Cap\s*\((?:M\.?\s*Baht|ล้านบาท)\)"),
        "pe": grab(r"P/E\s*\(X\)"),
        "pbv": grab(r"P/BV\s*\(X\)"),
        "asof": None,
    }
    m = re.search(r"As of\s+(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})", t)
    if m:
        try:
            f["asof"] = datetime.strptime(re.sub(r"\s+", " ", m.group(1)), "%d %b %Y").date().isoformat()
        except ValueError:
            pass
    if f["mcap"] is None and f["pe"] is None and f["pbv"] is None:
        raise ValueError("หาตัวเลข Market Cap / P/E / P/BV ในหน้า SET ไม่เจอ (โครงหน้าเว็บอาจเปลี่ยน)")
    return f


def set_valuation(sym, quote, closes):
    """valuation หุ้นไทยจาก set.or.th ปรับตามราคาล่าสุดของ Yahoo"""
    code = sym[:-3]
    f = parse_set_factsheet(http_text(SET_FACTSHEET.format(urllib.parse.quote(code, safe=""))))
    k = 1.0
    adj = False
    asof = f.get("asof")
    if quote and asof and closes and closes.get(asof, 0) > 0:
        r = quote["price"] / closes[asof]
        if 0.7 < r < 1.4:   # กันค่าเพี้ยน
            k, adj = r, True
    e = {"cur": "THB", "src": "set", "asof": asof}
    e["mcap"] = round(f["mcap"] * 1e6 * k) if f["mcap"] is not None else None
    e["pe"] = round(f["pe"] * k, 2) if f["pe"] is not None else None
    e["pbv"] = round(f["pbv"] * k, 2) if f["pbv"] is not None else None
    if adj:
        e["adj"] = True
    return e


_crumb = None


def yahoo_fundamentals(symbols):
    """มูลค่าตลาด, P/E, P/BV จาก Yahoo (ต้องใช้ crumb) คืน {symbol: {...}}"""
    global _crumb
    s = get_session()
    if s is None:
        print("ข้ามข้อมูล valuation จาก Yahoo: ต้องใช้ curl_cffi", file=sys.stderr)
        return {}
    out = {}
    try:
        if _crumb is None:
            r = s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=20)
            c = r.text.strip()
            if r.status_code != 200 or not c or "<" in c or " " in c:
                raise HttpErr(r.status_code, "getcrumb")
            _crumb = c
        for i in range(0, len(symbols), 40):
            chunk = symbols[i:i + 40]
            url = ("https://query1.finance.yahoo.com/v7/finance/quote?symbols="
                   + urllib.parse.quote(",".join(chunk), safe=",") + "&crumb=" + urllib.parse.quote(_crumb))
            r = s.get(url, timeout=30)
            if r.status_code != 200:
                print("Yahoo quote HTTP", r.status_code, file=sys.stderr)
                continue
            for it in r.json().get("quoteResponse", {}).get("result", []):
                cur = it.get("currency") or ""
                cur = MINOR_UNITS[cur][0] if cur in MINOR_UNITS else cur
                out[str(it.get("symbol", "")).upper()] = {
                    "cur": cur,
                    "mcap": it.get("marketCap"),
                    "pe": round(float(it["trailingPE"]), 2) if it.get("trailingPE") else None,
                    "pbv": round(float(it["priceToBook"]), 2) if it.get("priceToBook") else None,
                }
            time.sleep(1)
    except Exception as e:  # noqa: BLE001
        print("ดึง valuation จาก Yahoo ไม่ได้:", e, file=sys.stderr)
    return out


def trailing_dividend_yield(sym, price):
    """% ปันผลย้อนหลัง 12 เดือน = ผลรวมปันผลที่จ่ายใน 1 ปี / ราคาปัจจุบัน"""
    data = fetch_chart(sym, "interval=1d&range=1y&events=div")
    res = data["chart"]["result"][0]
    divs = (res.get("events") or {}).get("dividends") or {}
    cur = (res.get("meta") or {}).get("currency")
    factor = MINOR_UNITS[cur][1] if cur in MINOR_UNITS else 1.0
    cutoff = time.time() - 365 * 86400
    total = sum(float(d.get("amount") or 0) for d in divs.values() if (d.get("date") or 0) >= cutoff) / factor
    if total <= 0 or not price:
        return None
    return round(total / price * 100, 2)


def pause():
    time.sleep(random.uniform(0.7, 1.4))


def build_valuation(symbols, quotes, closes_map, old_val, now=None):
    """รวม valuation ทุกตัว ใช้ค่าเดิมถ้ายังไม่ถึงเวลารีเฟรช ตัวไหนดึงไม่ได้คงค่าเดิมไว้"""
    now = int(now or time.time())
    val, todo = {}, []
    for s in symbols:
        o = old_val.get(s)
        if o:
            val[s] = o
        if not o or now - int(o.get("t", 0)) >= VAL_TTL:
            todo.append(s)
    if not todo:
        return val
    print(f"รีเฟรช valuation {len(todo)} ตัว:", ", ".join(todo))

    new = {}
    need_yahoo = set()
    for s in todo:
        if s.upper().endswith(".BK"):
            try:
                new[s] = set_valuation(s, quotes.get(s), closes_map.get(s))
                pause()
            except Exception as e:  # noqa: BLE001
                print("SET FAIL", s, "->", e, "(จะลองใช้ Yahoo แทน)", file=sys.stderr)
                need_yahoo.add(s)
        else:
            need_yahoo.add(s)
    if need_yahoo:
        yf = yahoo_fundamentals(sorted(need_yahoo))
        for s in need_yahoo:
            if s in yf:
                new[s] = dict(yf[s], src="yahoo")

    blocked = 0
    for s in todo:
        q = quotes.get(s)
        e = new.setdefault(s, {})
        if blocked >= STOP_AFTER_BLOCKED:
            break
        try:
            e["dy"] = trailing_dividend_yield(s, q["price"] if q else None)
            blocked = 0
        except Blocked as ex:
            blocked += 1
            print("DIV BLOCKED", ex, file=sys.stderr)
        except Exception as ex:  # noqa: BLE001
            print("DIV FAIL", s, ex, file=sys.stderr)
        pause()

    for s in todo:
        e, o = new.get(s, {}), old_val.get(s, {})
        keys = ("mcap", "pe", "pbv", "dy")
        if not any(e.get(k) is not None for k in keys):
            continue                        # รอบนี้ไม่ได้อะไรใหม่เลย: คงของเดิมไว้ทั้งก้อน แล้วลองใหม่รอบหน้า
        core_new = any(e.get(k) is not None for k in ("mcap", "pe", "pbv"))
        for k in keys:                      # ช่องไหนได้ค่าใหม่ใช้ค่าใหม่ ไม่ได้ก็คงของเดิม
            if e.get(k) is None and o.get(k) is not None:
                e[k] = o[k]
        if "src" not in e:                  # ได้แค่ปันผล: ที่มาของ P/E ฯลฯ ยังเป็นของเดิม
            for k in ("src", "asof", "adj"):
                if k in o:
                    e[k] = o[k]
        q = quotes.get(s)
        e.setdefault("cur", (q or {}).get("currency") or o.get("cur") or "")
        # ได้ตัวเลขหลักครบจึงนับว่ารีเฟรชแล้ว ถ้าได้แค่ปันผล ให้ลองใหม่อีกใน ~30 นาที
        e["t"] = now if core_new else (o.get("t") or now - VAL_TTL + 1800)
        val[s] = e
        print("VAL", s, "src=" + str(e.get("src")), "mcap=", e.get("mcap"), "pe=", e.get("pe"),
              "pbv=", e.get("pbv"), "dy=", e.get("dy"), "asof=", e.get("asof"))
    return val


def main():
    cfg = json.loads((ROOT / "symbols.json").read_text(encoding="utf-8"))
    symbols = []
    for s in cfg.get("symbols", []):
        s = str(s).strip()
        if s and s not in symbols:
            symbols.append(s)
    want_cur = {"USD"} | {str(c).strip().upper() for c in cfg.get("currencies", []) if str(c).strip()}

    out_path = ROOT / "prices.json"
    old = {}
    if out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            old = {}
    old_q = old.get("quotes", {})
    old_fx = old.get("fx", {})
    old_val = old.get("valuation", {})

    quotes, failed, fresh, closes_map = {}, [], set(), {}
    blocked_run = 0
    stopped = False
    for s in symbols:
        if stopped:
            failed.append(s)
            if s in old_q:
                quotes[s] = old_q[s]
            continue
        try:
            data = fetch_chart(s)
            quotes[s] = parse_quote(data)
            if "name" not in quotes[s] and s in old_q and old_q[s].get("name"):
                quotes[s]["name"] = old_q[s]["name"]   # ถ้ารอบนี้ Yahoo ไม่ส่งชื่อมา ใช้ชื่อเดิม
            closes_map[s] = closes_by_date(data)
            fresh.add(s)
            blocked_run = 0
        except Blocked as e:
            print("BLOCKED", e, file=sys.stderr)
            failed.append(s)
            if s in old_q:
                quotes[s] = old_q[s]
            blocked_run += 1
            if blocked_run >= STOP_AFTER_BLOCKED:
                print(f"โดน Yahoo ปฏิเสธติดกัน {blocked_run} ตัว หยุดรอบนี้ (ราคาเดิมยังอยู่ รอบหน้าลองใหม่)",
                      file=sys.stderr)
                stopped = True
        except Exception as e:  # noqa: BLE001
            print("FAIL", s, e, file=sys.stderr)
            failed.append(s)
            if s in old_q:
                quotes[s] = old_q[s]
        pause()

    if symbols and not fresh:
        print("ดึงราคาไม่ได้เลยสักตัวในรอบนี้ ไม่เขียนทับไฟล์เดิม", file=sys.stderr)
        # รอบแรกที่ยังไม่เคยได้ราคาเลย ให้ขึ้นแดงเพื่อให้รู้ว่ามีปัญหา
        # แต่ถ้าเคยได้แล้ว ถือว่าชั่วคราว ไม่ต้องแจ้งเตือนซ้ำทุก 30 นาที
        sys.exit(0 if old_q else 1)

    for q in quotes.values():
        want_cur.add(q["currency"])
    want_cur.discard("THB")

    fx = {"THB": 1.0}
    usdthb = None
    if not stopped:
        try:
            usdthb = fx_usdthb()
        except Exception as e:  # noqa: BLE001
            print("FAIL USDTHB", e, file=sys.stderr)
    if not usdthb:
        usdthb = old_fx.get("USD")
    if usdthb:
        fx["USD"] = round(usdthb, 6)
    for cur in sorted(want_cur - {"USD"}):
        try:
            if stopped or not usdthb:
                raise RuntimeError("ข้ามรอบนี้")
            fx[cur] = round(fx_to_thb(cur, usdthb), 8)
            pause()
        except Exception as e:  # noqa: BLE001
            print("FAIL fx", cur, e, file=sys.stderr)
            if cur in old_fx:
                fx[cur] = old_fx[cur]

    # valuation: ถ้าส่วนนี้พัง ต้องไม่ทำให้ราคาพังตามไปด้วย
    valuation = {s: v for s, v in old_val.items() if s in symbols}
    if not stopped:
        try:
            valuation = build_valuation([s for s in symbols if s in quotes], quotes, closes_map, old_val)
        except Exception as e:  # noqa: BLE001
            print("valuation ผิดพลาด (ราคายังอัปเดตตามปกติ):", e, file=sys.stderr)

    if (old.get("quotes") == quotes and old.get("fx") == fx
            and old.get("valuation", {}) == valuation):
        print("ข้อมูลไม่เปลี่ยน ไม่เขียนไฟล์")
        return

    result = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "fx": fx,
        "quotes": quotes,
        "valuation": valuation,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"เขียน prices.json แล้ว: ได้ราคาใหม่ {len(fresh)}/{len(symbols)} ตัว, "
          f"valuation {len(valuation)} ตัว, fx={sorted(fx)}")
    if failed:
        print("ตัวที่ดึงไม่ได้รอบนี้:", ", ".join(failed))


if __name__ == "__main__":
    main()
