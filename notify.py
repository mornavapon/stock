"""ส่งสรุปหุ้นขึ้นแรง/ลงแรงของแต่ละกรอบ ไปที่แอป ntfy บนมือถือและแท็บเล็ต

MODE=am   -> ตลาดสหรัฐ (ส่งตอน 08:00 เวลาไทย)
MODE=pm   -> ตลาดเอเชียและหุ้นไทย (ส่งตอน 20:00 เวลาไทย)
MODE=ping -> ส่งข้อความทดสอบสั้นๆ
"""
import json
import os
import sys
import time
from urllib.request import Request, urlopen

SITE = "https://mornavapon.github.io/stock/"
ICON = SITE + "icon-192.png"
MAX_AGE_H = 14   # ข้อมูลที่ตลาดซื้อขายล่าสุดเก่ากว่านี้ (ชั่วโมง) ถือว่าตลาดปิดวันนี้ ไม่นำมาจัดอันดับ
TOP_N = 3        # ขึ้นแรง/ลงแรง กี่ตัวต่อกรอบ (กรอบที่มีหุ้นน้อย จะลดลงให้อัตโนมัติ)


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def make_boxes(groups, mode):
    """คืนรายการ (ชื่อกรอบ, [หุ้น]) ของรอบนี้"""
    boxes = []
    for g in groups:
        tag = g.get("notify")
        syms = g["symbols"]
        if tag == "am":
            # หุ้นต่างประเทศที่อยู่ในกรอบสหรัฐ (มีจุดในชื่อ เช่น 000660.KS) ย้ายไปแจ้งรอบเอเชีย
            us = [s for s in syms if "." not in s]
            asia = [s for s in syms if "." in s]
            if mode == "am":
                boxes.append((g["name"], us))
            elif mode == "pm" and asia:
                boxes.append((g["name"] + " (เอเชีย)", asia))
        elif tag == "pm" and mode == "pm":
            boxes.append((g["name"], syms))
    return boxes


def fmt(sym, q, up):
    mark = "🟢" if up else "🔴"
    return "%s %s %+.2f%%  (%s)" % (mark, sym, q["pct"], f'{q["p"]:,.2f}')


def build(boxes, quotes, force):
    now = time.time()
    sections = []
    for name, syms in boxes:
        rows = []
        for s in syms:
            q = quotes.get(s)
            if not q:
                continue
            t = q.get("t")
            if not force and (not t or now - t > MAX_AGE_H * 3600):
                continue
            rows.append((s, q))
        n = min(TOP_N, len(rows) // 2)
        if n == 0:
            continue
        rows.sort(key=lambda x: x[1]["pct"], reverse=True)
        ups = [r for r in rows[:n] if r[1]["pct"] > 0]
        downs = [r for r in reversed(rows[-n:]) if r[1]["pct"] < 0]
        if not ups and not downs:
            continue
        lines = ["【%s】" % name]
        lines += [fmt(s, q, True) for s, q in ups]
        lines += [fmt(s, q, False) for s, q in downs]
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def send(topic, title, message):
    body = json.dumps({
        "topic": topic, "title": title, "message": message,
        "click": SITE, "icon": ICON, "tags": ["chart_with_upwards_trend"],
        "priority": 4,  # 4 = สูง: Android จะเด้งเป็นป๊อปอัปบนจอ
    }, ensure_ascii=False).encode("utf-8")
    req = Request("https://ntfy.sh/", data=body,
                  headers={"Content-Type": "application/json; charset=utf-8"})
    last = None
    for _ in range(3):
        try:
            with urlopen(req, timeout=20) as r:
                r.read()
            return
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3)
    raise SystemExit("ส่งแจ้งเตือนไม่สำเร็จ: %s" % last)


def main():
    mode = os.environ.get("MODE", "ping")
    force = os.environ.get("FORCE", "") == "true"
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    dry = os.environ.get("DRY") == "1"
    if not topic and not dry:
        raise SystemExit("ยังไม่ได้ตั้งค่า NTFY_TOPIC (Secret) ใน GitHub")

    if mode == "ping":
        title, message = "ทดสอบแจ้งเตือน ✅", "ถ้าเห็นข้อความนี้ แปลว่าระบบแจ้งเตือนหุ้นของคุณใช้งานได้แล้ว"
    else:
        groups = load("tickers.json")["groups"]
        prices = load("prices.json")
        message = build(make_boxes(groups, mode), prices.get("quotes", {}), force)
        title = "ปิดตลาดสหรัฐ: ขึ้นแรง/ลงแรง" if mode == "am" else "ปิดตลาดเอเชีย: ขึ้นแรง/ลงแรง"
        if not message:
            print("ไม่มีตลาดที่เพิ่งปิด (อาจเป็นวันหยุด) จึงไม่ส่งแจ้งเตือน")
            return

    if dry:
        print(title)
        print(message)
        return
    send(topic, title, message)
    print("ส่งแล้ว:", title)


if __name__ == "__main__":
    sys.exit(main())
