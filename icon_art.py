"""icon_art.py - วาดไอคอนโปรแกรม (ใช้ทั้งที่ถาดระบบและตอนสร้างไฟล์ .exe)"""
from PIL import Image, ImageDraw, ImageFont

THAI_FONTS = ["tahoma.ttf", "LeelawUI.ttf", "leelawui.ttf", "Leelawad.ttf",
              "NotoSansThai-Regular.ttf", "Garuda.ttf", "Loma.ttf"]
LATIN_FONTS = ["arial.ttf", "DejaVuSans.ttf"]


def _font(size):
    for name in THAI_FONTS:
        try:
            return ImageFont.truetype(name, size), True
        except OSError:
            pass
    for name in LATIN_FONTS:
        try:
            return ImageFont.truetype(name, size), False
        except OSError:
            pass
    return ImageFont.load_default(), False


def make_icon_image(size=256):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 5, fill=(37, 99, 235, 255))
    font, thai = _font(int(size * 0.68))
    ch = "ก" if thai else "A"
    box = d.textbbox((0, 0), ch, font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), ch, font=font, fill="white")
    return img
