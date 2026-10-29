"""updater.py - ตรวจและติดตั้งอัปเดตจาก GitHub Releases (ไม่ต้องใช้แพ็กเกจเพิ่ม)"""
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API_BASE = "https://api.github.com"
EXE_NAME = "KbFix.exe"
USER_AGENT = "KbFix-updater"


class UpdateError(Exception):
    pass


def normalize_repo(text):
    """'https://github.com/owner/repo' หรือ 'owner/repo' -> 'owner/repo' (ไม่ถูกต้อง -> None)"""
    m = re.search(r"(?:github\.com[/:])?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", (text or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def parse_version(text):
    nums = [int(n) for n in re.findall(r"\d+", text or "")[:4]]
    return tuple(nums + [0] * (4 - len(nums)))


def is_newer(latest, current):
    return parse_version(latest) > parse_version(current)


def parse_sha256(text):
    m = re.search(r"\b[0-9a-fA-F]{64}\b", text or "")
    return m.group(0).lower() if m else None


def _open(url, timeout, accept=None):
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout)


def check_latest(repo, api_base=API_BASE, trusted_prefix=None, timeout=15):
    """ดึง Release ล่าสุด คืน dict: tag, notes, page, url, size, sha_url"""
    url = f"{api_base}/repos/{repo}/releases/latest"
    try:
        with _open(url, timeout, "application/vnd.github+json") as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("ไม่พบ Release (repo ต้องเป็น Public และมี Release อย่างน้อย 1 รายการ)")
        if e.code in (403, 429):
            raise UpdateError("GitHub จำกัดจำนวนการเรียก ลองใหม่ภายหลัง")
        raise UpdateError(f"GitHub ตอบกลับ HTTP {e.code}")
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError(f"เชื่อมต่อ GitHub ไม่ได้: {e}")

    prefix = trusted_prefix or f"https://github.com/{repo}/releases/download/"
    assets = {str(a.get("name", "")).lower(): a for a in data.get("assets", [])}

    def pick(name):  # รับเฉพาะไฟล์ที่มาจาก repo นี้เท่านั้น
        a = assets.get(name.lower())
        return a if a and str(a.get("browser_download_url", "")).startswith(prefix) else None

    exe, sha = pick(EXE_NAME), pick(EXE_NAME + ".sha256")
    return {
        "tag": data.get("tag_name", ""),
        "notes": data.get("body") or "",
        "page": data.get("html_url", ""),
        "url": exe["browser_download_url"] if exe else None,
        "size": exe.get("size") if exe else None,
        "sha_url": sha["browser_download_url"] if sha else None,
    }


def fetch_text(url, timeout=15):
    try:
        with _open(url, timeout) as r:
            return r.read(4096).decode("utf-8", "ignore")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"ดาวน์โหลดไฟล์ตรวจสอบ (checksum) ไม่สำเร็จ: {e}")


def remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


def download(url, dest, progress=None, timeout=30):
    try:
        with _open(url, timeout) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except (urllib.error.URLError, OSError) as e:
        remove_file(dest)
        raise UpdateError(f"ดาวน์โหลดไม่สำเร็จ: {e}")


def verify(path, size=None, sha256=None):
    if size and os.path.getsize(path) != size:
        raise UpdateError("ขนาดไฟล์ที่ดาวน์โหลดไม่ตรงกับที่ประกาศไว้ (ไฟล์อาจไม่สมบูรณ์)")
    with open(path, "rb") as f:
        if f.read(2) != b"MZ":
            raise UpdateError("ไฟล์ที่ได้ไม่ใช่โปรแกรม Windows (.exe)")
    if sha256:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != sha256.lower():
            raise UpdateError("ค่า checksum ไม่ตรง ไฟล์อาจเสียหาย จึงไม่ติดตั้งให้")


def apply_update(new_path, exe=None):
    """Windows ห้ามเขียนทับ .exe ที่กำลังรัน แต่เปลี่ยนชื่อได้ จึงย้ายตัวเก่าเป็น .old แล้วใส่ตัวใหม่แทน"""
    exe = exe or sys.executable
    old = exe + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
        os.replace(exe, old)
    except OSError as e:
        raise UpdateError(f"แทนที่ไฟล์ไม่ได้ (โฟลเดอร์อาจไม่อนุญาตให้เขียน ลองย้าย KbFix.exe ไปไว้ในโฟลเดอร์ของคุณ): {e}")
    try:
        os.replace(new_path, exe)
    except OSError as e:
        try:
            os.replace(old, exe)  # ย้อนกลับ
        except OSError:
            pass
        raise UpdateError(f"ติดตั้งไฟล์ใหม่ไม่สำเร็จ: {e}")
    return exe


def cleanup_old(exe=None, tries=20):
    """ลบ .old ที่เหลือจากการอัปเดตรอบก่อน (ตัวเก่าอาจยังปิดไม่สนิท จึงลองซ้ำ)"""
    old = (exe or sys.executable) + ".old"
    for _ in range(tries):
        if not os.path.exists(old):
            return
        try:
            os.remove(old)
            return
        except OSError:
            time.sleep(1)


def relaunch(exe, args=()):
    import subprocess

    env = dict(os.environ)
    env.pop("_MEIPASS2", None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"  # กัน PyInstaller onefile ใช้โฟลเดอร์ชั่วคราวของตัวเก่า
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED | NEW_PROCESS_GROUP
    subprocess.Popen([exe, *args], env=env, close_fds=True, creationflags=flags)
