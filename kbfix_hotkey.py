"""kbfix_hotkey.py - แกนหลักของ KbFix: กด Shift+Backspace 1 ครั้งเพื่อแก้ข้อความที่พิมพ์ผิดภาษา

รันแบบคอนโซล (ไม่มีหน้าต่าง):
    pip install keyboard mouse pyperclip
    python kbfix_hotkey.py

สามโหมด (เลือกอัตโนมัติ):
  1) เอาเมาส์ลากคลุม (หรือดับเบิล/ทริปเปิลคลิก) แล้วกด Shift+Backspace -> แก้ข้อความที่คลุมไว้
  2) พิมพ์เสร็จแล้วกด Shift+Backspace เลย -> แก้คำล่าสุด
  3) กดซ้ำทันทีหลังแก้ -> สลับกลับ/สลับไปมาได้เรื่อย ๆ (จำว่าบนหน้าจอเป็นภาษาอะไรอยู่ ไม่ต้องพึ่งการตรวจแป้นภาษา)

หมายเหตุ: log จะไม่บันทึกตัวอักษรที่พิมพ์ บันทึกเฉพาะโหมด จำนวนตัวอักษร และข้อผิดพลาด
"""
import logging
import re
import sys
import threading
import time

from kbfix import EN2TH, EN_NORMAL, EN_SHIFT, TH2EN, english_score, thai_score

log = logging.getLogger("kbfix")
log.addHandler(logging.NullHandler())

# คีย์ลัดสลับภาษาของ Windows (ปรับตามเครื่องคุณ เช่น "alt+shift" หรือ "ctrl+shift")
SWITCH_HOTKEY = "windows+space"
# สลับภาษาแป้นให้ตรงกับข้อความที่แปลงออกมาหลังแก้ข้อความที่คลุม
SWITCH_AFTER_SELECTION = True

MAX_BUF = 200
DRAG_PX = 4            # ลากเกินกี่พิกเซลถึงนับว่าเป็นการคลุม
MULTI_CLICK_SEC = 0.5  # คลิกซ้ำเร็วกว่านี้ = ดับเบิล/ทริปเปิลคลิก
SHIFT_WAIT_SEC = 0.6   # รอให้ปล่อย Shift ก่อนเริ่มแก้ (ไม่งั้นปุ่มที่โปรแกรมกดเองจะกลายเป็น Shift+...)
AFTER_FIX_SEC = 0.3    # หน่วงหลังแก้เสร็จ กันเหตุการณ์ปุ่มที่โปรแกรมกดเองหลุดเข้ามานับ
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


def to_thai(s):
    return "".join(EN2TH.get(c, c) for c in s)


def to_english(s):
    return "".join(TH2EN.get(c, c) for c in s)


class Tracker:
    """เก็บปุ่มที่กดล่าสุด (เป็นตัวอักษร QWERTY) เพื่อรู้ว่า "คำล่าสุด" คืออะไร
    และจำผลการแก้ครั้งล่าสุด (last_fix) เพื่อให้กดซ้ำแล้วสลับกลับได้"""

    def __init__(self):
        self.buf = ""
        self.held = set()
        self.paused = False
        # (ตัวอักษร QWERTY ของข้อความ, ภาษาที่อยู่บนหน้าจอตอนนี้ 'th'|'en', สลับแป้นให้ไหม)
        self.last_fix = None

    def reset(self):
        self.buf = ""
        self.last_fix = None

    def key(self, scan, down):
        if not down:
            self.held.discard(scan)
            return
        self.held.add(scan)
        if scan in MODIFIERS:
            return
        self.last_fix = None  # พิมพ์/ย้ายเคอร์เซอร์ = ผลแก้ครั้งก่อนสลับกลับไม่ได้แล้ว
        shift = bool(self.held & SHIFT_KEYS)
        if scan == BACKSPACE:
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
    if scan in SCAN or scan in EDIT_KEYS:
        selection.active = False  # พิมพ์/ลบทับ = ไม่มีข้อความคลุมอยู่แล้ว


def has_target(tracker, selection):
    """มีอะไรให้แก้ไหม (ถ้าไม่มี ปล่อย Shift+Backspace ให้ทำงานตามปกติของโปรแกรม)"""
    return bool(selection.active or tracker.last_fix or tracker.segment())


class HotkeyFilter:
    """ตัวตัดสินว่าปุ่มไหนเป็น Shift+Backspace ของเรา (ทำเองแทนระบบฮอตคีย์ของไลบรารี keyboard
    เพราะของไลบรารีสถานะปุ่ม modifier เพี้ยนได้เมื่อมีการกดปุ่มจำลองซ้อน ทำให้ใช้ได้แค่ครั้งเดียว)"""

    HOLD_TIMEOUT = 2.0

    def __init__(self, tracker, selection, fire):
        self.tracker, self.selection, self.fire = tracker, selection, fire
        self._down = False
        self._t = 0.0

    def reset(self):
        self._down = False

    def on_event(self, scan, down, now=None):
        """คืน True = ปล่อยปุ่มไปถึงโปรแกรม, False = กลืนปุ่มไว้ (ใช้เป็นฮุกแบบ suppress)"""
        now = time.time() if now is None else now
        t = self.tracker
        # ต้องเช็กก่อน paused เสมอ ไม่งั้นปุ่ม Backspace ตอนปล่อยอาจหลุดมา แล้ว Backspace ปกติจะถูกกลืนค้าง
        if scan == BACKSPACE and self._down:
            if now - self._t <= self.HOLD_TIMEOUT:
                if not down:
                    self._down = False
                return False  # กลืนทั้งตอนกดค้าง (key repeat) และตอนปล่อย
            self._down = False  # ค้างนานผิดปกติ ล้างสถานะ
        if t.paused:
            return True
        if (scan == BACKSPACE and down and (t.held & SHIFT_KEYS)
                and has_target(t, self.selection)):
            self._down, self._t = True, now
            self.fire()
            return False
        handle_key(t, self.selection, scan, down)
        return True


# ---------- ตรวจแป้นภาษา / แปลงข้อความ ----------
def current_layout():
    """'th' / 'en' ตามแป้นที่ใช้อยู่ในหน้าต่างปัจจุบัน (Windows) หรือ None ถ้าตรวจไม่ได้"""
    if sys.platform != "win32":
        return None
    import ctypes

    u = ctypes.windll.user32
    tid = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
    return "th" if (u.GetKeyboardLayout(tid) & 0xFFFF) == 0x041E else "en"


def plan(segment, layout):
    """โหมดคำล่าสุด: layout = แป้นที่พิมพ์ไว้ตอนนั้น คืน (จำนวนตัวที่ต้องลบ, ข้อความใหม่, ภาษาของข้อความใหม่)"""
    if layout is None:  # เดาจากความเป็นภาษาจริงของแต่ละฝั่ง
        layout = "en" if thai_score(to_thai(segment)) > english_score(segment) else "th"
    if layout == "en":
        return len(segment), to_thai(segment), "th"
    return len(segment), to_english(segment), "en"  # to_english(ข้อความที่เป็นปุ่ม QWERTY) = เดิม


def convert_selection(text):
    """โหมดคลุมข้อความ: ดูว่าส่วนใหญ่เป็นภาษาไหน แล้วแปลงเป็นอีกภาษา
    คืน (ข้อความใหม่, 'th'|'en') หรือ None ถ้าไม่มีตัวอักษรให้แปลง"""
    th = len(re.findall(r"[ก-๙]", text))
    en = len(re.findall(r"[A-Za-z]", text))
    if th == 0 and en == 0:
        return None
    if th > en:
        return to_english(text), "en"
    return to_thai(text), "th"


def ensure_layout(kb, target):
    """สลับแป้นภาษาให้ตรงกับ target ('th'/'en') ถ้ายังไม่ตรง"""
    cur = current_layout()
    if cur is not None and cur != target:
        kb.send(SWITCH_HOTKEY)


def wait_shift_release(tracker, timeout=None):
    """รอให้ผู้ใช้ปล่อย Shift (ดูจากปุ่มจริงที่ฮุกเห็น) คืน True ถ้าปล่อยแล้ว"""
    deadline = time.time() + (SHIFT_WAIT_SEC if timeout is None else timeout)
    while tracker.held & SHIFT_KEYS:
        if time.time() >= deadline:
            return False
        time.sleep(0.01)
    return True


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
        saved = None
        try:
            released = wait_shift_release(tracker)
            tracker.paused = True  # ไม่ให้นับปุ่มที่โปรแกรมกดเอง
            if not released:  # ผู้ใช้กด Shift ค้างนานผิดปกติ: ปล่อย Shift ให้ (ไม่งั้นกลายเป็น Ctrl+Shift+C ฯลฯ)
                kb.release("left shift")
                kb.release("right shift")
                tracker.held.difference_update(SHIFT_KEYS)
                log.info("shift still held; released by program")

            # --- โหมด 1: ข้อความที่คลุมด้วยเมาส์ ---
            if selection.active:
                picked, saved = grab_selection(kb)
                picked = picked.replace("\r\n", "\n") if picked else picked
                result = convert_selection(picked) if picked else None
                if result:
                    text, target = result
                    kb.write(text)  # พิมพ์ทับข้อความที่คลุมอยู่
                    selection.active = False
                    tracker.buf = ""
                    tracker.last_fix = (text if target == "en" else picked, target, SWITCH_AFTER_SELECTION)
                    log.info("fix selection: %d chars -> %s", len(text), target)
                    if SWITCH_AFTER_SELECTION:
                        ensure_layout(kb, target)
                    return
                log.info("selection probe: nothing usable copied")

            # --- โหมด 3: กดซ้ำ -> สลับกลับ (อิงภาษาที่จำไว้ว่าอยู่บนหน้าจอ) ---
            if tracker.last_fix:
                phys, on_screen, switch = tracker.last_fix
                if on_screen == "th":
                    text, target = to_english(phys), "en"
                else:
                    text, target = to_thai(phys), "th"
                for _ in range(len(phys)):
                    kb.send("backspace")
                kb.write(text)
                tracker.last_fix = (phys, target, switch)
                log.info("toggle: %d chars -> %s", len(phys), target)
                if switch:
                    ensure_layout(kb, target)
                return

            # --- โหมด 2: คำล่าสุดที่เพิ่งพิมพ์ ---
            seg = tracker.segment()
            if seg:
                layout = current_layout()
                n, text, target = plan(seg, layout)
                for _ in range(n):
                    kb.send("backspace")
                kb.write(text)
                tracker.last_fix = (seg, target, True)
                log.info("fix last word: %d chars, typed layout=%s -> %s", n, layout, target)
                ensure_layout(kb, target)
        except Exception:
            log.exception("run_fix failed")
        finally:
            if saved is not None:
                try:
                    import pyperclip

                    pyperclip.copy(saved)
                except Exception:
                    pass
            time.sleep(AFTER_FIX_SEC)
            tracker.held.intersection_update(SHIFT_KEYS | OTHER_MODS)
            tracker.paused = False


def main():
    import keyboard as kb
    import mouse

    tracker, selection, lock = Tracker(), Selection(), threading.Lock()

    def fire():
        threading.Thread(target=run_fix, args=(kb, tracker, selection, lock), daemon=True).start()

    flt = HotkeyFilter(tracker, selection, fire)

    def on_key(e):
        try:
            return flt.on_event(e.scan_code, e.event_type == "down")
        except Exception:
            log.exception("on_key failed")
            return True

    def on_mouse(e):
        if tracker.paused or not isinstance(e, mouse.ButtonEvent) or e.button != "left":
            return
        pos = mouse.get_position()
        if e.event_type in ("down", "double"):
            selection.press(pos, time.time(), e.event_type == "double")
            tracker.reset()
        elif e.event_type == "up":
            selection.release(pos)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    kb.hook(on_key, suppress=True)
    mouse.hook(on_mouse)
    print("พร้อมใช้งาน: คลุมข้อความด้วยเมาส์ หรือพิมพ์เสร็จ แล้วกด Shift+Backspace (Ctrl+C เพื่อออก)")
    kb.wait()


if __name__ == "__main__":
    main()
