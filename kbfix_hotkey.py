"""kbfix_hotkey.py - กด Shift+Backspace 1 ครั้ง เพื่อแก้ข้อความที่พิมพ์ผิดภาษา

ติดตั้ง:   pip install keyboard mouse pyperclip pythainlp
รัน:       python kbfix_hotkey.py      (วาง kbfix.py ไว้โฟลเดอร์เดียวกัน)
หยุด:      Ctrl+C ในหน้าต่างที่รันอยู่

สองโหมด (เลือกอัตโนมัติ):
  1) เอาเมาส์ลากคลุม (หรือดับเบิล/ทริปเปิลคลิก) แล้วกด Shift+Backspace -> แก้ข้อความที่คลุมไว้
  2) พิมพ์เสร็จแล้วกด Shift+Backspace เลย -> แก้คำล่าสุด (ไม่ต้องคลุม)
"""
import re
import sys
import threading
import time

from kbfix import EN2TH, EN_NORMAL, EN_SHIFT, TH2EN, english_score, thai_score

# คีย์ลัดสลับภาษาของ Windows (ปรับตามเครื่องคุณ เช่น "alt+shift" หรือ "ctrl+shift")
SWITCH_HOTKEY = "windows+space"
# โหมดคลุมข้อความ: แก้เสร็จแล้วสลับภาษาแป้นให้ตรงกับภาษาที่แปลงออกมา
SWITCH_AFTER_SELECTION = True

MAX_BUF = 200
DRAG_PX = 4            # ลากเกินกี่พิกเซลถึงนับว่าเป็นการคลุม
MULTI_CLICK_SEC = 0.5  # คลิกซ้ำเร็วกว่านี้ = ดับเบิล/ทริปเปิลคลิก
SENTINEL = "\u200b__kbfix_probe__\u200b"

# ---------- scan code (PC/AT set 1) -> ตัวอักษรบน QWERTY ----------
SCAN = {}
for _codes, _chars in [
    (range(0x02, 0x0E), "1234567890-="),
    (range(0x10, 0x1C), "qwertyuiop[]"),
    (range(0x1E, 0x29), "asdfghjkl;'"),
    (range(0x2C, 0x36), "zxcvbnm,./"),
]:
    SCAN.update(zip(_codes, _chars))
SCAN[0x29], SCAN[0x2B], SCAN[0x39] = "`", "\\", " "

SHIFT_MAP = dict(zip(EN_NORMAL, EN_SHIFT))
SHIFT_KEYS = {0x2A, 0x36}
OTHER_MODS = {0x1D, 0x38, 0x5B, 0x5C}  # ctrl, alt, win
MODIFIERS = SHIFT_KEYS | OTHER_MODS | {0x3A}  # + caps lock
BACKSPACE = 0x0E
EDIT_KEYS = {BACKSPACE, 0x53, 0x1C}  # backspace, delete, enter


class Tracker:
    """เก็บปุ่มที่กดล่าสุด (เป็นตัวอักษร QWERTY) เพื่อรู้ว่า "คำล่าสุด" คืออะไร"""

    def __init__(self):
        self.buf = ""
        self.held = set()
        self.paused = False

    def key(self, scan, down):
        if not down:
            self.held.discard(scan)
            return
        self.held.add(scan)
        if scan in MODIFIERS:
            return
        shift = bool(self.held & SHIFT_KEYS)
        if scan == BACKSPACE:
            if not shift:  # Shift+Backspace คือฮอตคีย์ ไม่นับเป็นการลบ
                self.buf = self.buf[:-1]
            return
        if self.held & OTHER_MODS:  # Ctrl/Alt/Win + ปุ่ม = ไม่ใช่การพิมพ์
            self.buf = ""
            return
        ch = SCAN.get(scan)
        if ch is None:  # Enter, Tab, Esc, ลูกศร ฯลฯ = เริ่มใหม่
            self.buf = ""
            return
        if shift:
            ch = SHIFT_MAP.get(ch, ch)
        self.buf = (self.buf + ch)[-MAX_BUF:]

    def segment(self):
        """คำสุดท้าย (รวมช่องว่างท้ายคำ ถ้ากดเว้นวรรคไปแล้ว)"""
        m = re.search(r"\S+\s*$", self.buf)
        return m.group(0) if m else ""


class Selection:
    """เดาว่าตอนนี้มีข้อความที่คลุมด้วยเมาส์อยู่ไหม"""

    def __init__(self):
        self.active = False
        self._down_pos = None
        self._last_press = 0.0
        self._multi = False

    def press(self, pos, now, double=False):
        self._multi = double or (now - self._last_press < MULTI_CLICK_SEC)
        self._last_press = now
        self._down_pos = pos
        self.active = False

    def release(self, pos):
        if self._down_pos is None:
            return
        dx = abs(pos[0] - self._down_pos[0])
        dy = abs(pos[1] - self._down_pos[1])
        self.active = self._multi or max(dx, dy) > DRAG_PX
        self._down_pos = None


def handle_key(tracker, selection, scan, down):
    """ประมวลผลการกดปุ่มหนึ่งครั้ง (แยกออกมาเพื่อให้ทดสอบได้)"""
    tracker.key(scan, down)
    if not down or scan in MODIFIERS or tracker.held & OTHER_MODS:
        return
    is_hotkey = scan == BACKSPACE and bool(tracker.held & SHIFT_KEYS)
    if (scan in SCAN or scan in EDIT_KEYS) and not is_hotkey:
        selection.active = False  # พิมพ์/ลบทับ = ไม่มีข้อความคลุมอยู่แล้ว


def current_layout():
    """'th' / 'en' ตามแป้นที่ใช้อยู่ในหน้าต่างปัจจุบัน (Windows) หรือ None ถ้าตรวจไม่ได้"""
    if sys.platform != "win32":
        return None
    import ctypes

    u = ctypes.windll.user32
    tid = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    return "th" if (u.GetKeyboardLayout(tid) & 0xFFFF) == 0x041E else "en"


def plan(segment, layout):
    """โหมดคำล่าสุด: คืน (จำนวนตัวที่ต้องลบ, ข้อความใหม่)"""
    as_en = segment
    as_th = "".join(EN2TH.get(c, c) for c in segment)
    if layout is None:  # เดาจากความเป็นภาษาจริงของแต่ละฝั่ง
        layout = "en" if thai_score(as_th) > english_score(as_en) else "th"
    return len(segment), (as_th if layout == "en" else as_en)


def convert_selection(text):
    """โหมดคลุมข้อความ: ดูว่าส่วนใหญ่เป็นภาษาไหน แล้วแปลงเป็นอีกภาษา
    คืน (ข้อความใหม่, 'th'|'en') หรือ None ถ้าไม่มีตัวอักษรให้แปลง"""
    th = len(re.findall(r"[ก-๙]", text))
    en = len(re.findall(r"[A-Za-z]", text))
    if th == 0 and en == 0:
        return None
    if th > en:
        return "".join(TH2EN.get(c, c) for c in text), "en"
    return "".join(EN2TH.get(c, c) for c in text), "th"


def grab_selection(kb):
    """ก๊อปข้อความที่คลุมอยู่ด้วย Ctrl+C คืน (ข้อความ|None, คลิปบอร์ดเดิม)"""
    import pyperclip

    try:
        saved = pyperclip.paste()
    except Exception:
        saved = None
    pyperclip.copy(SENTINEL)
    kb.send("ctrl+c")
    for _ in range(20):  # รอได้สูงสุดราว 0.5 วินาที
        time.sleep(0.025)
        cur = pyperclip.paste()
        if cur != SENTINEL:
            return cur, saved
    return None, saved


def run_fix(kb, tracker, selection, lock):
    with lock:
        tracker.paused = True  # ไม่ให้นับปุ่มที่โปรแกรมกดเอง
        saved = None
        try:
            kb.release("left shift")
            kb.release("right shift")

            # --- โหมด 1: ข้อความที่คลุมด้วยเมาส์ ---
            if selection.active:
                picked, saved = grab_selection(kb)
                result = convert_selection(picked) if picked else None
                if result:
                    text, target = result
                    kb.write(text)  # พิมพ์ทับข้อความที่คลุมอยู่
                    selection.active = False
                    tracker.buf = ""
                    if SWITCH_AFTER_SELECTION and current_layout() not in (None, target):
                        kb.send(SWITCH_HOTKEY)
                    return

            # --- โหมด 2: คำล่าสุดที่เพิ่งพิมพ์ ---
            seg = tracker.segment()
            if seg:
                n, text = plan(seg, current_layout())
                for _ in range(n):
                    kb.send("backspace")
                kb.write(text)
                kb.send(SWITCH_HOTKEY)
        finally:
            if saved is not None:
                try:
                    import pyperclip

                    pyperclip.copy(saved)
                except Exception:
                    pass
            time.sleep(0.15)
            tracker.held.clear()
            tracker.paused = False


def main():
    import keyboard as kb
    import mouse

    tracker, selection, lock = Tracker(), Selection(), threading.Lock()

    def on_key(e):
        if not tracker.paused:
            handle_key(tracker, selection, e.scan_code, e.event_type == "down")

    def on_mouse(e):
        if tracker.paused or not isinstance(e, mouse.ButtonEvent) or e.button != "left":
            return
        pos = mouse.get_position()
        if e.event_type in ("down", "double"):
            selection.press(pos, time.time(), e.event_type == "double")
            tracker.buf = ""  # เคอร์เซอร์ย้ายตำแหน่ง คำเดิมใช้ไม่ได้แล้ว
        elif e.event_type == "up":
            selection.release(pos)

    kb.hook(on_key)
    mouse.hook(on_mouse)
    kb.add_hotkey(
        "shift+backspace",
        lambda: threading.Thread(
            target=run_fix, args=(kb, tracker, selection, lock), daemon=True
        ).start(),
        suppress=True,
    )
    print("พร้อมใช้งาน: คลุมข้อความด้วยเมาส์ หรือพิมพ์เสร็จ แล้วกด Shift+Backspace (Ctrl+C เพื่อออก)")
    kb.wait()


if __name__ == "__main__":
    main()
