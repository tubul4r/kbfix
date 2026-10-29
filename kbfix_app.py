"""kbfix_app.py - หน้าต่างโปรแกรม KbFix (ถาดระบบ + เปิดอัตโนมัติตอนเปิดเครื่อง)

รันตรง ๆ:     python kbfix_app.py
สร้าง .exe:   ดับเบิลคลิก build.bat  ->  dist\\KbFix.exe
"""
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

import kbfix_hotkey as core
from icon_art import make_icon_image

APP_NAME = "KbFix"
APP_TITLE = "KbFix - แก้พิมพ์ผิดภาษา ไทย ⇄ อังกฤษ"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SWITCH_CHOICES = ["windows+space", "alt+shift", "ctrl+shift"]
DEFAULTS = {"switch_hotkey": "windows+space", "switch_after_selection": True}


# ---------------------------------------------------------------- ตั้งค่า
def settings_path():
    d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_NAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(settings_path(), encoding="utf-8") as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def apply_settings(s):
    core.SWITCH_HOTKEY = (s.get("switch_hotkey") or DEFAULTS["switch_hotkey"]).strip().lower()
    core.SWITCH_AFTER_SELECTION = bool(s.get("switch_after_selection", True))


# ---------------------------------------------------------------- เปิดอัตโนมัติ (Windows)
def _winreg():
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def startup_command():
    if getattr(sys, "frozen", False):  # เป็นไฟล์ .exe
        return f'"{sys.executable}" --minimized'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")  # ไม่เปิดหน้าต่างดำ
    if os.path.exists(pyw):
        exe = pyw
    return f'"{exe}" "{os.path.abspath(sys.argv[0])}" --minimized'


def autostart_enabled():
    wr = _winreg()
    if not wr:
        return False
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY) as k:
            wr.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    wr = _winreg()
    if not wr:
        return False
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, RUN_KEY, 0, wr.KEY_SET_VALUE) as k:
            if on:
                wr.SetValueEx(k, APP_NAME, 0, wr.REG_SZ, startup_command())
            else:
                try:
                    wr.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- กันเปิดซ้อน
_mutex = None


def already_running():
    global _mutex
    if sys.platform != "win32":
        return False
    import ctypes

    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "KbFixSingleInstance")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def enable_dpi_awareness():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


# ---------------------------------------------------------------- ตัวดักคีย์/เมาส์
class Engine:
    def __init__(self):
        self.tracker = core.Tracker()
        self.selection = core.Selection()
        self.lock = threading.Lock()
        self.running = False
        self._kb = self._mouse = self._key_h = self._mouse_h = None
        self._hot = False

    def start(self):
        if self.running:
            return
        import keyboard as kb
        import mouse

        tracker, selection, lock = self.tracker, self.selection, self.lock
        tracker.buf, tracker.paused = "", False
        tracker.held.clear()
        selection.active = False

        def on_key(e):
            if not tracker.paused:
                core.handle_key(tracker, selection, e.scan_code, e.event_type == "down")

        def on_mouse(e):
            if tracker.paused or not isinstance(e, mouse.ButtonEvent) or e.button != "left":
                return
            pos = mouse.get_position()
            if e.event_type in ("down", "double"):
                selection.press(pos, time.time(), e.event_type == "double")
                tracker.buf = ""
            elif e.event_type == "up":
                selection.release(pos)

        def fire():
            threading.Thread(
                target=core.run_fix, args=(kb, tracker, selection, lock), daemon=True
            ).start()

        self._kb, self._mouse = kb, mouse
        try:
            self._key_h = kb.hook(on_key)
            self._mouse_h = mouse.hook(on_mouse)
            kb.add_hotkey("shift+backspace", fire, suppress=True)
            self._hot = True
            self.running = True
        except Exception:
            self.stop()
            raise

    def stop(self):
        steps = []
        if self._key_h is not None:
            steps.append(lambda: self._kb.unhook(self._key_h))
        if self._mouse_h is not None:
            steps.append(lambda: self._mouse.unhook(self._mouse_h))
        if self._hot:
            steps.append(lambda: self._kb.remove_hotkey("shift+backspace"))
        for step in steps:
            try:
                step()
            except Exception:
                pass
        self._key_h = self._mouse_h = None
        self._hot = False
        self.running = False


# ---------------------------------------------------------------- หน้าต่างโปรแกรม
class App:
    def __init__(self, start_minimized, first_run):
        self.q = queue.Queue()
        self.tray = None
        self.settings = load_settings()
        apply_settings(self.settings)
        if first_run:  # ครั้งแรก: เปิดอัตโนมัติตอนเปิดเครื่องให้เลย (ปิดได้ในหน้าต่าง)
            set_autostart(True)
            save_settings(self.settings)
        elif autostart_enabled():
            set_autostart(True)  # อัปเดตพาธ เผื่อย้ายไฟล์ .exe

        self.root = tk.Tk()
        self.engine = Engine()
        self.build_ui()
        self.start_engine()
        self.start_tray()
        self.root.protocol("WM_DELETE_WINDOW", self.hide)
        self.root.after(100, self.pump)
        if start_minimized:
            self.hide()

    # ---- UI
    def build_ui(self):
        r = self.root
        r.title(APP_TITLE)
        r.geometry("470x520")
        r.resizable(False, False)
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=("Tahoma", 10))
        pad = {"padx": 14, "pady": 6}

        top = ttk.Frame(r)
        top.pack(fill="x", **pad)
        self.status = tk.Label(top, font=("Tahoma", 14, "bold"))
        self.status.pack(side="left")
        self.btn = ttk.Button(top, width=14, command=self.toggle)
        self.btn.pack(side="right")

        ttk.Label(
            r,
            text="วิธีใช้: คลุมข้อความด้วยเมาส์ (หรือดับเบิลคลิก) หรือพิมพ์เสร็จ "
                 "แล้วกด Shift + Backspace 1 ครั้ง",
            wraplength=440, justify="left",
        ).pack(fill="x", **pad)

        box = ttk.LabelFrame(r, text="ตั้งค่า")
        box.pack(fill="x", **pad)
        self.var_auto = tk.BooleanVar(value=autostart_enabled())
        ttk.Checkbutton(box, text="เปิดโปรแกรมอัตโนมัติเมื่อเปิดเครื่อง",
                        variable=self.var_auto, command=self.on_autostart).pack(anchor="w", padx=10, pady=4)
        self.var_sw = tk.BooleanVar(value=self.settings["switch_after_selection"])
        ttk.Checkbutton(box, text="สลับภาษาแป้นให้หลังแก้ข้อความที่คลุม",
                        variable=self.var_sw, command=self.on_settings).pack(anchor="w", padx=10, pady=4)
        row = ttk.Frame(box)
        row.pack(fill="x", padx=10, pady=(4, 8))
        ttk.Label(row, text="คีย์ลัดสลับภาษาของ Windows:").pack(side="left")
        self.var_hk = tk.StringVar(value=self.settings["switch_hotkey"])
        cb = ttk.Combobox(row, textvariable=self.var_hk, values=SWITCH_CHOICES, width=16)
        cb.pack(side="left", padx=8)
        for ev in ("<<ComboboxSelected>>", "<FocusOut>", "<Return>"):
            cb.bind(ev, lambda e: self.on_settings())

        tb = ttk.LabelFrame(r, text="ลองแปลง (ลองกด Shift + Backspace ในช่องนี้ได้ด้วย)")
        tb.pack(fill="x", **pad)
        ttk.Label(tb, text="ข้อความที่พิมพ์ผิดภาษา:").pack(anchor="w", padx=10, pady=(6, 0))
        self.var_in, self.var_out = tk.StringVar(), tk.StringVar()
        ttk.Entry(tb, textvariable=self.var_in).pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(tb, text="ผลลัพธ์:").pack(anchor="w", padx=10)
        ttk.Entry(tb, textvariable=self.var_out, state="readonly").pack(fill="x", padx=10, pady=(0, 6))
        ttk.Button(tb, text="คัดลอกผลลัพธ์", command=self.copy_out).pack(anchor="e", padx=10, pady=(0, 8))
        self.var_in.trace_add("write", lambda *a: self.on_test())

        ttk.Label(
            r,
            text="ปิดหน้าต่าง = ย่อไปที่ถาดระบบ (ยังทำงานต่อ)\n"
                 "ถ้าโปรแกรมที่รันแบบ Administrator ไม่ตอบสนอง ให้เปิด KbFix ด้วย Run as administrator",
            wraplength=440, justify="left", foreground="#666666",
        ).pack(fill="x", **pad)
        ttk.Button(r, text="ออกจากโปรแกรม", command=self.quit).pack(pady=6)

    def refresh(self):
        if self.engine.running:
            self.status.config(text="● กำลังทำงาน", fg="#16a34a")
            self.btn.config(text="หยุดชั่วคราว")
        else:
            self.status.config(text="● หยุดทำงาน", fg="#9ca3af")
            self.btn.config(text="เริ่มทำงาน")
        if self.tray:
            try:
                self.tray.update_menu()
            except Exception:
                pass

    # ---- การทำงาน
    def start_engine(self):
        try:
            self.engine.start()
        except Exception as ex:
            messagebox.showerror(APP_NAME, f"เริ่มดักคีย์บอร์ด/เมาส์ไม่สำเร็จ:\n{ex}")
        self.refresh()

    def toggle(self):
        if self.engine.running:
            self.engine.stop()
            self.refresh()
        else:
            self.start_engine()

    def on_autostart(self):
        if not set_autostart(self.var_auto.get()):
            messagebox.showwarning(APP_NAME, "ตั้งค่าเปิดอัตโนมัติไม่สำเร็จ (ใช้ได้เฉพาะบน Windows)")
        self.var_auto.set(autostart_enabled())

    def on_settings(self):
        hk = self.var_hk.get().strip().lower() or DEFAULTS["switch_hotkey"]
        self.var_hk.set(hk)
        self.settings.update(switch_hotkey=hk, switch_after_selection=bool(self.var_sw.get()))
        apply_settings(self.settings)
        save_settings(self.settings)

    def on_test(self):
        res = core.convert_selection(self.var_in.get())
        self.var_out.set(res[0] if res else "")

    def copy_out(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.var_out.get())

    # ---- ถาดระบบ
    def start_tray(self):
        try:
            import pystray
        except ImportError:
            return
        menu = pystray.Menu(
            pystray.MenuItem("เปิดหน้าต่าง", lambda i, it: self.q.put(self.show), default=True),
            pystray.MenuItem("เปิดการทำงาน", lambda i, it: self.q.put(self.toggle),
                             checked=lambda it: self.engine.running),
            pystray.MenuItem("ออกจากโปรแกรม", lambda i, it: self.q.put(self.quit)),
        )
        try:
            self.tray = pystray.Icon(APP_NAME, make_icon_image(64), APP_TITLE, menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            self.tray = None

    def pump(self):  # รับคำสั่งจากเธรดของถาดระบบมารันใน Tk
        while True:
            try:
                self.q.get_nowait()()
            except queue.Empty:
                break
        self.root.after(100, self.pump)

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        if self.tray:
            self.root.withdraw()
        else:
            self.root.iconify()

    def quit(self):
        self.engine.stop()
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    enable_dpi_awareness()
    if already_running():
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, "KbFix ทำงานอยู่แล้ว (ดูไอคอนที่ถาดระบบมุมขวาล่าง)")
        return
    first_run = not os.path.exists(settings_path())
    App("--minimized" in sys.argv, first_run).run()


if __name__ == "__main__":
    main()
