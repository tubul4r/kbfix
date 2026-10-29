"""kbfix.py - แก้ข้อความที่พิมพ์ผิดภาษา (ลืมสลับแป้น ไทย <-> อังกฤษ)

ใช้งาน:
    python kbfix.py "l;ylfu"      -> สวัสดี
    python kbfix.py "้ำสสน"       -> hello

ติดตั้งเพิ่ม (ไม่บังคับ แต่แม่นขึ้นมาก):  pip install pythainlp
"""
import os
import re
import sys

# ---------- ตารางแป้นพิมพ์ (ไทยเกษมณี <-> US QWERTY) ----------
EN_NORMAL = "`1234567890-=qwertyuiop[]\\asdfghjkl;'zxcvbnm,./"
TH_NORMAL = "_ๅ/-ภถุึคตจขชๆไำพะัีรนยบลฃฟหกดเ้่าสวงผปแอิืทมใฝ"
EN_SHIFT = "~!@#$%^&*()_+QWERTYUIOP{}|ASDFGHJKL:\"ZXCVBNM<>?"
TH_SHIFT = "%+๑๒๓๔ู฿๕๖๗๘๙๐\"ฎฑธํ๊ณฯญฐ,ฅฤฆฏโฌ็๋ษศซ.()ฉฮฺ์?ฒฬฦ"

EN2TH = dict(zip(EN_NORMAL + EN_SHIFT, TH_NORMAL + TH_SHIFT))
TH2EN = {t: e for e, t in zip(EN_NORMAL + EN_SHIFT, TH_NORMAL + TH_SHIFT)}


def convert(text: str, table: dict) -> str:
    return "".join(table.get(ch, ch) for ch in text)


# ---------- ตัวตรวจว่าผลลัพธ์ "เป็นภาษาจริง" ไหม ----------
try:
    from pythainlp.corpus import thai_words
    from pythainlp.tokenize import word_tokenize
    _TH_DICT = thai_words()
except Exception:  # ไม่มี pythainlp ก็ใช้ heuristic แทน
    _TH_DICT = None

_EN_DICT = None
for _p in ("/usr/share/dict/words", "/usr/share/dict/american-english"):
    if os.path.exists(_p):
        with open(_p, encoding="utf-8", errors="ignore") as f:
            _EN_DICT = {w.strip().lower() for w in f}
        break

# ตัวอักษรที่ห้ามขึ้นต้นคำไทย (สระบน/ล่าง วรรณยุกต์ ฯลฯ)
_TH_BAD_START = re.compile(r"(^|\s)[ะัาำิีึืฺุู็่้๊๋์ํๆฯ]")


def thai_score(text: str) -> float:
    """0..1 ยิ่งสูงยิ่งเหมือนภาษาไทยจริง"""
    chars = re.findall(r"[ก-ฮ]", text)
    if not chars:
        return 0.0
    if _TH_BAD_START.search(text):
        return 0.0
    if _TH_DICT:
        toks = [t for t in word_tokenize(text, engine="newmm") if t.strip()]
        known = sum(len(t) for t in toks if t in _TH_DICT and len(t) > 1)
        return known / max(len(text.replace(" ", "")), 1)
    return 0.5  # ไม่มีพจนานุกรม: ผ่านแค่กฎโครงสร้าง


def english_score(text: str) -> float:
    words = re.findall(r"[A-Za-z]+", text)
    if not words:
        return 0.0
    if _EN_DICT:
        return sum(w.lower() in _EN_DICT for w in words) / len(words)
    ok = sum(
        bool(re.search(r"[aeiouy]", w, re.I)) and not re.search(r"[^aeiouy]{5,}", w, re.I)
        for w in words
    )
    return ok / len(words)


# ---------- ตัวตัดสินใจหลัก ----------
def fix(text: str) -> str:
    th = len(re.findall(r"[ก-๙]", text))
    en = len(re.findall(r"[A-Za-z]", text))

    if en >= th:  # ส่วนใหญ่เป็นตัวอังกฤษ -> ลองแปลงเป็นไทย
        as_thai = convert(text, EN2TH)
        # ถ้าข้อความเดิมเป็นอังกฤษที่ถูกอยู่แล้ว ไม่ต้องแก้
        if english_score(text) >= 0.6 or thai_score(as_thai) < 0.3:
            return text
        return as_thai
    else:  # ส่วนใหญ่เป็นไทย -> ลองแปลงเป็นอังกฤษ
        as_en = convert(text, TH2EN)
        if thai_score(text) >= 0.6 or english_score(as_en) < 0.5:
            return text
        return as_en


if __name__ == "__main__":
    src = " ".join(sys.argv[1:]) or sys.stdin.read().strip()
    print(fix(src))
