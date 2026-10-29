"""kbfix_app.py - หน้าต่างโปรแกรม KbFix (ถาดระบบ + เปิดอัตโนมัติ + ปุ่มอัปเดต)

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
import webbrowser
from tkinter import messagebox, ttk

import kbfix_hotkey as core
import updater
from icon_art import make_icon_image

try:
    from kbfix_version import __repo__, __version__
except ImportError:
    __version__, __repo__ = "0.0.0", ""

APP_NAME = "KbFix"
APP_TITLE = "KbFix - แก้พิมพ์ผิดภาษา ไทย ⇄ อังกฤษ"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
SWITCH_CHOICES = ["windows+space", "alt+shift", "ctrl+shift"]
DEFAULTS = {
    "switch_hotkey": "windows+space",
    "switch_after_selection": True,
    "update_repo": "",      # ว่าง = ใช้ repo ที่ฝังมากับตัวโปรแกรม
    "auto_check": True,     # ตรวจอัปเดตเงียบ ๆ ตอนเปิดโปรแกรม
}


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


def log_dir():
    return os.path.dirname(settings_path())


def setup_logging():
    """บันทึก log ไว้ที่ %APPDATA%\\KbFix\\kbfix.log (ไม่เก็บตัวอักษรที่พิมพ์ เก็บแค่โหมด/จำนวน/ข้อผิดพลาด)"""
    import logging
    import logging.handlers

    try:
        h = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir(), "kbfix.log"), maxBytes=200_000, backupCount=1, encoding="utf-8")
    except OSError:
        return
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    core.log.addHandler(h)
    core.log.setLevel(logging.INFO)
    core.log.info("KbFix v%s started", __version__)


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


def acquire_instance(wait_sec=0):
    """True = เราเป็นตัวเดียวที่รันอยู่ (ถ้าเพิ่งอัปเดต จะรอให้ตัวเก่าปิดก่อนสูงสุด wait_sec วินาที)"""
    global _mutex
    if sys.platform != "win32":
        return True
    import ctypes

    k32 = ctypes.windll.kernel32
    deadline = time.time() + wait_sec
    while True:
        h = k32.CreateMutexW(None, False, "KbFixSingleInstance")
        if k32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            _mutex = h
            return True
        k32.CloseHandle(h)
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


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

    def start(self):
        if self.running:
            return
        import keyboard as kb
        import mouse

        tracker, selection, lock = self.tracker, self.selection, self.lock
        tracker.buf, tracker.paused = "", False
        tracker.held.clear()
        selection.active = False

        def fire():
            threading.Thread(
                target=core.run_fix, args=(kb, tracker, selection, lock), daemon=True
            ).start()

        flt = core.HotkeyFilter(tracker, selection, fire)

        def on_key(e):  # ฮุกแบบ suppress: คืน False = กลืนปุ่ม (ใช้กับ Shift+Backspace ของเรา)
            try:
                return flt.on_event(e.scan_code, e.event_type == "down")
            except Exception:
                core.log.exception("on_key failed")
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

        self._kb, self._mouse = kb, mouse
        try:
            self._key_h = kb.hook(on_key, suppress=True)
            self._mouse_h = mouse.hook(on_mouse)
            self.running = True
            core.log.info("engine started")
        except Exception:
            core.log.exception("engine start failed")
            self.stop()
            raise

    def stop(self):
        steps = []
        if self._key_h is not None:
            steps.append(lambda: self._kb.unhook(self._key_h))
        if self._mouse_h is not None:
            steps.append(lambda: self._mouse.unhook(self._mouse_h))
        for step in steps:
            try:
                step()
            except Exception:
                pass
        self._key_h = self._mouse_h = None
        self.running = False


# ---------------------------------------------------------------- หน้าต่างโปรแกรม
class App:
    def __init__(self, start_minimized, first_run, after_update=False):
        self.q = queue.Queue()
        self.tray = None
        self.latest = None
        self.busy_update = False
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

        if getattr(sys, "frozen", False):  # ลบไฟล์ .old ที่เหลือจากการอัปเดตรอบก่อน
            threading.Thread(target=updater.cleanup_old, daemon=True).start()
        if after_update:
            self.root.after(600, lambda: messagebox.showinfo(
                APP_NAME, f"อัปเดตเป็นเวอร์ชัน v{__version__} เรียบร้อยแล้ว"))
        elif self.settings.get("auto_check") and self.current_repo():
            self.root.after(4000, lambda: self.check_update(silent=True))
        if start_minimized:
            self.hide()

    # ---- UI
    def build_ui(self):
        r = self.root
        r.title(APP_TITLE)
        r.minsize(470, 0)
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

        up = ttk.LabelFrame(r, text="อัปเดตโปรแกรม")
        up.pack(fill="x", **pad)
        head = ttk.Frame(up)
        head.pack(fill="x", padx=10, pady=(6, 2))
        ttk.Label(head, text=f"เวอร์ชันปัจจุบัน: v{__version__}").pack(side="left")
        self.lbl_update = tk.Label(up, text="", anchor="w", justify="left", wraplength=420, fg="#666666")
        self.lbl_update.pack(fill="x", padx=10)
        row = ttk.Frame(up)
        row.pack(fill="x", padx=10, pady=4)
        ttk.Label(row, text="GitHub repo:").pack(side="left")
        self.var_repo = tk.StringVar(value=self.settings["update_repo"] or __repo__)
        ent = ttk.Entry(row, textvariable=self.var_repo, width=30)
        ent.pack(side="left", padx=8)
        for ev in ("<FocusOut>", "<Return>"):
            ent.bind(ev, lambda e: self.on_settings())
        self.var_chk = tk.BooleanVar(value=self.settings["auto_check"])
        ttk.Checkbutton(up, text="ตรวจอัปเดตอัตโนมัติตอนเปิดโปรแกรม",
                        variable=self.var_chk, command=self.on_settings).pack(anchor="w", padx=10)
        self.pb = ttk.Progressbar(up, maximum=100)
        self.btn_update = ttk.Button(up, text="ตรวจสอบอัปเดต", command=self.on_update_click)
        self.btn_update.pack(anchor="e", padx=10, pady=(4, 8))

        ttk.Label(
            r,
            text="ปิดหน้าต่าง = ย่อไปที่ถาดระบบ (ยังทำงานต่อ)\n"
                 "ถ้าโปรแกรมที่รันแบบ Administrator ไม่ตอบสนอง ให้เปิด KbFix ด้วย Run as administrator",
            wraplength=440, justify="left", foreground="#666666",
        ).pack(fill="x", **pad)
        bar = ttk.Frame(r)
        bar.pack(pady=6)
        ttk.Button(bar, text="เปิดโฟลเดอร์ log", command=self.open_log_dir).pack(side="left", padx=6)
        ttk.Button(bar, text="ออกจากโปรแกรม", command=self.quit).pack(side="left", padx=6)

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

    # ---- การทำงานหลัก
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
        repo_text = self.var_repo.get().strip()
        repo = updater.normalize_repo(repo_text) if repo_text else ""
        if repo_text and not repo:
            self.set_update_msg("รูปแบบ repo ไม่ถูกต้อง (ต้องเป็น เจ้าของ/ชื่อ repo)", "#dc2626")
            repo = self.settings.get("update_repo", "")
        elif repo:
            self.var_repo.set(repo)
        self.settings.update(
            switch_hotkey=hk,
            switch_after_selection=bool(self.var_sw.get()),
            update_repo=repo if repo != __repo__ else "",
            auto_check=bool(self.var_chk.get()),
        )
        apply_settings(self.settings)
        save_settings(self.settings)

    def on_test(self):
        res = core.convert_selection(self.var_in.get())
        self.var_out.set(res[0] if res else "")

    def open_log_dir(self):
        try:
            os.startfile(log_dir())  # Windows เท่านั้น
        except Exception:
            messagebox.showinfo(APP_NAME, f"ไฟล์ log อยู่ที่:\n{log_dir()}")

    def copy_out(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.var_out.get())

    # ---- อัปเดต
    def current_repo(self):
        text = self.var_repo.get().strip() if hasattr(self, "var_repo") else ""
        return updater.normalize_repo(text or self.settings.get("update_repo") or __repo__)

    def set_update_msg(self, text, color="#666666"):
        self.lbl_update.config(text=text, fg=color)

    def on_update_click(self):
        if self.latest and updater.is_newer(self.latest["tag"], __version__):
            self.offer_update(self.latest)
        else:
            self.check_update()

    def check_update(self, silent=False):
        repo = self.current_repo()
        if not repo:
            if not silent:
                messagebox.showinfo(APP_NAME, "ใส่ชื่อ GitHub repo ในช่อง (รูปแบบ เจ้าของ/ชื่อ repo) ก่อน")
            return
        if self.busy_update:
            return
        self.busy_update = True
        self.btn_update.config(state="disabled")
        self.set_update_msg("กำลังตรวจสอบ...")

        def work():
            try:
                info = updater.check_latest(repo)
            except updater.UpdateError as ex:
                self.q.put(lambda m=str(ex): self.on_check_failed(m, silent))
                return
            except Exception as ex:
                self.q.put(lambda m=f"เกิดข้อผิดพลาด: {ex}": self.on_check_failed(m, silent))
                return
            self.q.put(lambda: self.on_check_done(info, silent))

        threading.Thread(target=work, daemon=True).start()

    def on_check_failed(self, msg, silent):
        self.busy_update = False
        self.btn_update.config(state="normal")
        self.set_update_msg(msg, "#dc2626")
        if not silent:
            messagebox.showwarning(APP_NAME, msg)

    def on_check_done(self, info, silent):
        self.busy_update = False
        self.btn_update.config(state="normal")
        self.latest = info
        if not updater.is_newer(info["tag"], __version__):
            self.btn_update.config(text="ตรวจสอบอัปเดต")
            self.set_update_msg(f"เป็นเวอร์ชันล่าสุดแล้ว (v{__version__})", "#16a34a")
            if not silent:
                messagebox.showinfo(APP_NAME, f"คุณใช้เวอร์ชันล่าสุดอยู่แล้ว (v{__version__})")
            return
        self.set_update_msg(f"มีเวอร์ชันใหม่: {info['tag']}", "#2563eb")
        self.btn_update.config(text=f"อัปเดตเป็น {info['tag']}")
        if not silent:
            self.offer_update(info)

    def offer_update(self, info):
        notes = (info.get("notes") or "").strip()
        if len(notes) > 500:
            notes = notes[:500] + "…"
        msg = f"พบเวอร์ชันใหม่ {info['tag']} (ปัจจุบัน v{__version__})"
        if notes:
            msg += f"\n\n{notes}"
        if not getattr(sys, "frozen", False):
            if messagebox.askyesno(APP_NAME, msg + "\n\nกำลังรันจากสคริปต์ จึงอัปเดตอัตโนมัติไม่ได้ "
                                                   "เปิดหน้า Release เพื่อดาวน์โหลดไหม?"):
                webbrowser.open(info.get("page") or f"https://github.com/{self.current_repo()}/releases/latest")
            return
        if not info.get("url"):
            messagebox.showwarning(APP_NAME, msg + "\n\nRelease นี้ยังไม่มีไฟล์ KbFix.exe แนบอยู่ "
                                                   "(อาจกำลังสร้างอยู่) ลองใหม่อีกครั้งภายหลัง")
            return
        if messagebox.askyesno(APP_NAME, msg + "\n\nอัปเดตและเปิดโปรแกรมใหม่เลยไหม?"):
            self.start_download(info)

    def start_download(self, info):
        self.busy_update = True
        self.btn_update.config(state="disabled")
        self.pb["value"] = 0
        self.pb.pack(fill="x", padx=10, pady=(4, 0), before=self.btn_update)
        self.set_update_msg("กำลังดาวน์โหลด 0%", "#2563eb")
        exe = sys.executable
        tmp = exe + ".new"

        def progress(done, total):
            pct = int(done * 100 / total) if total else 0
            self.q.put(lambda: self.on_progress(pct))

        def work():
            try:
                sha = updater.parse_sha256(updater.fetch_text(info["sha_url"])) if info.get("sha_url") else None
                updater.download(info["url"], tmp, progress)
                updater.verify(tmp, info.get("size"), sha)
                updater.apply_update(tmp, exe)
            except updater.UpdateError as ex:
                updater.remove_file(tmp)
                self.q.put(lambda m=str(ex): self.on_update_failed(m))
                return
            except Exception as ex:
                updater.remove_file(tmp)
                self.q.put(lambda m=f"เกิดข้อผิดพลาด: {ex}": self.on_update_failed(m))
                return
            self.q.put(self.finish_update)

        threading.Thread(target=work, daemon=True).start()

    def on_progress(self, pct):
        self.pb["value"] = pct
        self.set_update_msg(f"กำลังดาวน์โหลด {pct}%", "#2563eb")

    def on_update_failed(self, msg):
        self.busy_update = False
        self.pb.pack_forget()
        self.btn_update.config(state="normal")
        self.set_update_msg(msg, "#dc2626")
        messagebox.showerror(APP_NAME, msg)

    def finish_update(self):
        self.set_update_msg("ติดตั้งแล้ว กำลังเปิดโปรแกรมใหม่...", "#16a34a")
        try:
            updater.relaunch(sys.executable, ["--after-update"])
        except Exception as ex:
            messagebox.showinfo(APP_NAME, f"อัปเดตเสร็จแล้ว แต่เปิดโปรแกรมใหม่เองไม่ได้ ({ex})\n"
                                          "กรุณาเปิด KbFix อีกครั้งด้วยตัวเอง")
        self.quit()

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

    def pump(self):  # รับคำสั่งจากเธรดอื่นมารันใน Tk
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
    setup_logging()
    after_update = "--after-update" in sys.argv
    if not acquire_instance(wait_sec=15 if after_update else 0):
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, "KbFix ทำงานอยู่แล้ว (ดูไอคอนที่ถาดระบบมุมขวาล่าง)")
        return
    first_run = not os.path.exists(settings_path())
    App("--minimized" in sys.argv, first_run, after_update).run()


if __name__ == "__main__":
    main()
