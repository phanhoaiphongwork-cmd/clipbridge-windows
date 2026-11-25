#!/usr/bin/env python3
"""
ClipBridge - chia sẻ clipboard (chữ + ảnh) giữa Windows và iPhone/iPad.

  python clipbridge.py                 # dùng trong cùng mạng Wi-Fi
  python clipbridge.py --tunnel        # dùng ở bất kỳ đâu (qua Internet, HTTPS)

Không cần cài thư viện nào. Ảnh được xử lý bằng GDI+ có sẵn trong Windows.
(Tuỳ chọn) in mã QR trong cửa sổ:  pip install qrcode
"""
import argparse
import atexit
import ctypes
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

MAX_UPLOAD = 40 * 1024 * 1024
FROZEN = getattr(sys, "frozen", False)  # đang chạy dạng .exe (PyInstaller)
APP_DIR = Path(sys.executable if FROZEN else __file__).resolve().parent
PNG_SIG = b"\x89PNG\r\n\x1a\n"


# ================================================================ ảnh (không cần Pillow)
def sniff_mime(data: bytes) -> str:
    h = data[:16]
    if h.startswith(PNG_SIG):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h.startswith(b"GIF8"):
        return "image/gif"
    if h.startswith(b"BM"):
        return "image/bmp"
    if h.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if h[:4] == b"RIFF" and h[8:12] == b"WEBP":
        return "image/webp"
    if h[4:8] == b"ftyp":
        return "image/heic"
    return ""


def encode_png(w: int, h: int, bgra: bytes) -> bytes:
    """Mã hoá PNG từ điểm ảnh BGRA (trên xuống dưới) - chỉ dùng zlib của Python."""
    n = w * h
    alpha = bgra[3::4]
    opaque = alpha.count(255) == n or alpha.count(0) == n  # DIB 32-bit hay để alpha = 0
    ch = 3 if opaque else 4
    px = bytearray(n * ch)
    px[0::ch], px[1::ch], px[2::ch] = bgra[2::4], bgra[1::4], bgra[0::4]
    if ch == 4:
        px[3::4] = alpha
    rl = w * ch
    raw = b"".join(b"\x00" + px[i * rl:(i + 1) * rl] for i in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2 if ch == 3 else 6, 0, 0, 0)
    return PNG_SIG + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def dib_to_bgra(dib: bytes):
    """CF_DIB 24/32-bit -> (w, h, BGRA trên xuống dưới). Định dạng khác -> ValueError."""
    hsize, w, h, _planes, bpp, comp = struct.unpack_from("<IiiHHI", dib, 0)
    if bpp not in (24, 32) or comp not in (0, 3) or w <= 0 or h == 0:
        raise ValueError("DIB không hỗ trợ")
    off = hsize
    if comp == 3:
        if struct.unpack_from("<III", dib, 40) != (0xFF0000, 0xFF00, 0xFF):
            raise ValueError("DIB bitfields lạ")
        if hsize == 40:
            off += 12
    off += struct.unpack_from("<I", dib, 32)[0] * 4  # biClrUsed
    top_down, h = h < 0, abs(h)
    stride, rowlen = ((w * bpp + 31) // 32) * 4, w * bpp // 8
    if off + stride * (h - 1) + rowlen > len(dib):
        raise ValueError("DIB bị cắt")
    rows = [dib[off + i * stride: off + i * stride + rowlen] for i in range(h)]
    if not top_down:
        rows.reverse()
    px = b"".join(rows)
    if bpp == 24:
        out = bytearray(w * h * 4)
        out[0::4], out[1::4], out[2::4] = px[0::3], px[1::3], px[2::3]
        out[3::4] = b"\xff" * (w * h)
        px = bytes(out)
    return w, h, px


def bgra_to_dib(w: int, h: int, bgra: bytes) -> bytes:
    """BGRA trên xuống dưới -> CF_DIB 32-bit (dưới lên trên)."""
    hdr = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 32, 0, w * h * 4, 2835, 2835, 0, 0)
    rl = w * 4
    return hdr + b"".join(bgra[i * rl:(i + 1) * rl] for i in range(h - 1, -1, -1))


def dib_to_bmp(dib: bytes) -> bytes:
    hsize = struct.unpack_from("<I", dib, 0)[0]
    comp = struct.unpack_from("<I", dib, 16)[0]
    bpp = struct.unpack_from("<H", dib, 14)[0]
    ncol = struct.unpack_from("<I", dib, 32)[0] or (1 << bpp if bpp <= 8 else 0)
    extra = 12 if (comp == 3 and hsize == 40) else 0
    off = 14 + hsize + extra + ncol * 4
    return b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, off) + dib


class Gdip:
    """Giải mã PNG/JPEG/GIF/BMP/TIFF bằng GDI+ có sẵn trong Windows."""

    def __init__(self):
        from ctypes import POINTER, Structure, c_int, c_uint, c_void_p
        from ctypes import wintypes as w

        class SI(Structure):
            _fields_ = [("ver", ctypes.c_uint32), ("cb", c_void_p), ("s1", w.BOOL), ("s2", w.BOOL)]

        class Rect(Structure):
            _fields_ = [("x", c_int), ("y", c_int), ("w", c_int), ("h", c_int)]

        class BitmapData(Structure):
            _fields_ = [("w", c_uint), ("h", c_uint), ("stride", c_int), ("fmt", c_int),
                        ("scan0", c_void_p), ("res", ctypes.c_size_t)]

        class PropItem(Structure):
            _fields_ = [("id", ctypes.c_uint32), ("len", ctypes.c_uint32),
                        ("type", ctypes.c_uint16), ("value", c_void_p)]

        self.Rect, self.BitmapData, self.PropItem = Rect, BitmapData, PropItem
        g = self.g = ctypes.WinDLL("gdiplus")
        sh = ctypes.WinDLL("shlwapi")
        sh.SHCreateMemStream.argtypes = [ctypes.c_char_p, c_uint]
        sh.SHCreateMemStream.restype = c_void_p
        self.mkstream = sh.SHCreateMemStream
        sig = {
            "GdiplusStartup": [POINTER(ctypes.c_size_t), c_void_p, c_void_p],
            "GdipCreateBitmapFromStream": [c_void_p, POINTER(c_void_p)],
            "GdipGetImageWidth": [c_void_p, POINTER(c_uint)],
            "GdipGetImageHeight": [c_void_p, POINTER(c_uint)],
            "GdipBitmapLockBits": [c_void_p, POINTER(Rect), c_uint, c_int, POINTER(BitmapData)],
            "GdipBitmapUnlockBits": [c_void_p, POINTER(BitmapData)],
            "GdipDisposeImage": [c_void_p],
            "GdipGetPropertyItemSize": [c_void_p, ctypes.c_uint32, POINTER(c_uint)],
            "GdipGetPropertyItem": [c_void_p, ctypes.c_uint32, c_uint, c_void_p],
            "GdipImageRotateFlip": [c_void_p, c_int],
        }
        for name, args in sig.items():
            f = getattr(g, name)
            f.argtypes, f.restype = args, c_int
        tok, si = ctypes.c_size_t(0), SI(1, None, False, False)
        if g.GdiplusStartup(ctypes.byref(tok), ctypes.byref(si), None) != 0:
            raise OSError("Không khởi động được GDI+")

    @staticmethod
    def _release(p):
        vtbl = ctypes.cast(ctypes.c_void_p(p), ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])(p)  # IUnknown::Release

    def _orient(self, img):  # xoay ảnh theo EXIF (ảnh chụp iPhone)
        size = ctypes.c_uint(0)
        if self.g.GdipGetPropertyItemSize(img, 0x0112, ctypes.byref(size)) != 0:
            return
        if size.value < ctypes.sizeof(self.PropItem):
            return
        buf = ctypes.create_string_buffer(size.value)
        if self.g.GdipGetPropertyItem(img, 0x0112, size, buf) != 0:
            return
        item = self.PropItem.from_buffer(buf)
        if item.value:
            o = ctypes.c_uint16.from_address(item.value).value
            t = {2: 4, 3: 2, 4: 6, 5: 5, 6: 1, 7: 7, 8: 3}.get(o)
            if t:
                self.g.GdipImageRotateFlip(img, t)

    def decode(self, data: bytes):
        """-> (w, h, BGRA trên xuống dưới)"""
        g = self.g
        stream = self.mkstream(data, len(data))
        if not stream:
            raise MemoryError("SHCreateMemStream thất bại")
        img = ctypes.c_void_p()
        try:
            if g.GdipCreateBitmapFromStream(stream, ctypes.byref(img)) != 0 or not img.value:
                raise ValueError("định dạng ảnh không hỗ trợ (hãy dùng PNG/JPEG)")
            try:
                self._orient(img)
                cw, ch = ctypes.c_uint(), ctypes.c_uint()
                g.GdipGetImageWidth(img, ctypes.byref(cw))
                g.GdipGetImageHeight(img, ctypes.byref(ch))
                w, h = cw.value, ch.value
                rect, bd = self.Rect(0, 0, w, h), self.BitmapData()
                if g.GdipBitmapLockBits(img, ctypes.byref(rect), 1, 0x26200A, ctypes.byref(bd)) != 0:
                    raise ValueError("không đọc được điểm ảnh")
                try:
                    if bd.stride == w * 4:
                        px = ctypes.string_at(bd.scan0, w * 4 * h)
                    else:
                        px = b"".join(ctypes.string_at(bd.scan0 + i * bd.stride, w * 4) for i in range(h))
                finally:
                    g.GdipBitmapUnlockBits(img, ctypes.byref(bd))
                return w, h, px
            finally:
                g.GdipDisposeImage(img)
        finally:
            self._release(stream)


# ================================================================ clipboard
class MemoryClipboard:
    """Dùng khi không chạy trên Windows (để thử nghiệm)."""

    def __init__(self):
        self._seq, self._val = 0, ("empty", None)

    def seq(self):
        return self._seq

    def read(self):
        return self._val

    def write_text(self, text):
        self._seq += 1
        self._val = ("text", text)

    def write_image(self, data):
        self._seq += 1
        self._val = ("image", data)

    def is_private(self):
        return False


class WindowsClipboard:
    CF_UNICODETEXT, CF_DIB, CF_HDROP = 13, 8, 15

    def __init__(self):
        from ctypes import wintypes as w

        self.u = u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.s = s = ctypes.WinDLL("shell32")
        u.OpenClipboard.argtypes = [w.HWND]
        u.OpenClipboard.restype = w.BOOL
        u.CloseClipboard.restype = w.BOOL
        u.EmptyClipboard.restype = w.BOOL
        u.GetClipboardData.argtypes = [w.UINT]
        u.GetClipboardData.restype = w.HANDLE
        u.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
        u.SetClipboardData.restype = w.HANDLE
        u.IsClipboardFormatAvailable.argtypes = [w.UINT]
        u.IsClipboardFormatAvailable.restype = w.BOOL
        u.GetClipboardSequenceNumber.restype = w.DWORD
        u.RegisterClipboardFormatW.argtypes = [w.LPCWSTR]
        u.RegisterClipboardFormatW.restype = w.UINT
        k.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = w.HGLOBAL
        k.GlobalLock.argtypes = [w.HGLOBAL]
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalUnlock.argtypes = [w.HGLOBAL]
        k.GlobalUnlock.restype = w.BOOL
        k.GlobalFree.argtypes = [w.HGLOBAL]
        k.GlobalFree.restype = w.HGLOBAL
        k.GlobalSize.argtypes = [w.HGLOBAL]
        k.GlobalSize.restype = ctypes.c_size_t
        s.DragQueryFileW.argtypes = [w.HANDLE, w.UINT, w.LPWSTR, w.UINT]
        s.DragQueryFileW.restype = w.UINT
        self.CF_PNG = u.RegisterClipboardFormatW("PNG")
        self._gdip = None

    @property
    def gdip(self):
        if self._gdip is None:
            self._gdip = Gdip()
        return self._gdip

    def seq(self):
        return self.u.GetClipboardSequenceNumber()

    def is_private(self):
        """Trình quản lý mật khẩu (1Password, KeePass, Bitwarden...) đánh dấu nội dung
        không được đồng bộ -> tôn trọng và bỏ qua."""
        avail, reg = self.u.IsClipboardFormatAvailable, self.u.RegisterClipboardFormatW
        for name in ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore"):
            if avail(reg(name)):
                return True
        for name in ("CanUploadToCloudClipboard", "CanIncludeInClipboardHistory"):
            f = reg(name)
            if avail(f):
                d = self._get(f)
                if d and len(d) >= 4 and struct.unpack_from("<I", d)[0] == 0:
                    return True
        return False

    def _open(self):
        for _ in range(40):  # clipboard có thể đang bị app khác giữ
            if self.u.OpenClipboard(None):
                return
            time.sleep(0.05)
        raise RuntimeError("Không mở được clipboard của Windows")

    def _get(self, fmt):
        """Đọc 1 định dạng thô -> bytes (hoặc None)."""
        self._open()
        try:
            h = self.u.GetClipboardData(fmt)
            if not h:
                return None
            p = self.k.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.string_at(p, self.k.GlobalSize(h))
            finally:
                self.k.GlobalUnlock(h)
        finally:
            self.u.CloseClipboard()

    def _files(self):
        self._open()
        try:
            h = self.u.GetClipboardData(self.CF_HDROP)
            if not h:
                return []
            out = []
            for i in range(self.s.DragQueryFileW(h, 0xFFFFFFFF, None, 0)):
                n = self.s.DragQueryFileW(h, i, None, 0)
                buf = ctypes.create_unicode_buffer(n + 1)
                self.s.DragQueryFileW(h, i, buf, n + 1)
                out.append(buf.value)
            return out
        finally:
            self.u.CloseClipboard()

    def read(self):
        avail = self.u.IsClipboardFormatAvailable
        # Ưu tiên chữ (Excel/Word copy kèm cả ảnh minh hoạ, nhưng người dùng muốn chữ)
        if avail(self.CF_UNICODETEXT):
            raw = self._get(self.CF_UNICODETEXT) or b""
            return "text", raw.decode("utf-16-le", "replace").split("\x00", 1)[0]
        if self.CF_PNG and avail(self.CF_PNG):  # Chrome, Office... (giữ nền trong suốt)
            png = self._get(self.CF_PNG)
            if png and png.startswith(PNG_SIG):
                return "image", png
        if avail(self.CF_DIB):  # Win+Shift+S, Paint...
            dib = self._get(self.CF_DIB)
            if dib:
                try:
                    return "image", encode_png(*dib_to_bgra(dib))
                except ValueError:  # DIB lạ -> để GDI+ đọc dưới dạng file BMP
                    return "image", encode_png(*self.gdip.decode(dib_to_bmp(dib)))
        if avail(self.CF_HDROP):  # copy file ảnh trong Explorer
            for f in self._files():
                try:
                    if os.path.getsize(f) > MAX_UPLOAD:
                        continue
                    data = Path(f).read_bytes()
                    if data.startswith(PNG_SIG):
                        return "image", data
                    if sniff_mime(data):
                        return "image", encode_png(*self.gdip.decode(data))
                except Exception:
                    continue
        return "empty", None

    def _set(self, items):
        self._open()
        try:
            self.u.EmptyClipboard()
            for fmt, data in items:
                h = self.k.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
                if not h:
                    raise MemoryError("GlobalAlloc thất bại")
                p = self.k.GlobalLock(h)
                ctypes.memmove(p, data, len(data))
                self.k.GlobalUnlock(h)
                if not self.u.SetClipboardData(fmt, h):
                    self.k.GlobalFree(h)
                    raise RuntimeError("SetClipboardData thất bại")
        finally:
            self.u.CloseClipboard()

    def write_text(self, text):
        self._set([(self.CF_UNICODETEXT, text.encode("utf-16-le") + b"\0\0")])

    def write_image(self, data):
        w, h, bgra = self.gdip.decode(data)
        items = [(self.CF_DIB, bgra_to_dib(w, h, bgra))]
        if data.startswith(PNG_SIG):  # giữ nền trong suốt cho app hỗ trợ PNG
            items.append((self.CF_PNG, data))
        self._set(items)


class Clip:
    """Bọc clipboard: khoá luồng + cache theo số thứ tự thay đổi."""

    def __init__(self):
        self.cb = WindowsClipboard() if sys.platform == "win32" else MemoryClipboard()
        self.lock = threading.Lock()
        self._cache_seq, self._cache = None, ("empty", None)

    def current(self):
        with self.lock:
            s = self.cb.seq()
            if s != self._cache_seq:
                try:
                    self._cache = self.cb.read()
                except Exception as e:
                    print(f"[lỗi đọc clipboard] {e}")
                    self._cache = ("empty", None)
                self._cache_seq = s
            return s, self._cache

    def write(self, kind, data):
        with self.lock:
            if kind == "image":
                self.cb.write_image(data)
            else:
                self.cb.write_text(data)


CLIP = None

# ================================================================ trang web
PAGE = r"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>ClipBridge</title><style>
:root{color-scheme:light dark;--bg:#f4f4f6;--card:#fff;--fg:#111;--mut:#777;--acc:#0a7cff}
@media (prefers-color-scheme:dark){:root{--bg:#000;--card:#1c1c1e;--fg:#f2f2f2;--mut:#999}}
*{box-sizing:border-box}body{margin:0;font:16px -apple-system,system-ui,sans-serif;background:var(--bg);color:var(--fg);padding:16px;max-width:640px;margin:auto}
h1{font-size:20px;margin:4px 0 12px}.card{background:var(--card);border-radius:14px;padding:14px;margin-bottom:14px}
.row{display:flex;justify-content:space-between;align-items:center}.tag{font-size:12px;color:var(--mut)}
textarea{width:100%;min-height:120px;font:inherit;border-radius:10px;border:1px solid #8884;padding:10px;background:transparent;color:inherit;margin-top:8px}
img{display:block;max-width:100%;max-height:50vh;margin:8px auto 0;border-radius:10px;-webkit-touch-callout:default}
button,.btn{display:block;text-align:center;text-decoration:none;width:100%;font:600 17px -apple-system,system-ui,sans-serif;padding:14px;border:0;border-radius:12px;background:var(--acc);color:#fff;margin-top:10px;cursor:pointer}
.sec{background:#8883!important;color:var(--fg)!important}small{color:var(--mut)}#st{min-height:1.2em;text-align:center;color:var(--mut)}
[hidden]{display:none!important}
</style></head><body>
<h1>📋 ClipBridge</h1>
<div class="card"><div class="row"><b>PC ➜ iPhone</b><span class="tag" id="kind"></span></div>
<small>Copy chữ hoặc ảnh trên Windows, nội dung tự hiện ở đây.</small>
<textarea id="pc" readonly></textarea><img id="pcimg" hidden alt="Ảnh từ PC">
<button id="copy">Copy vào iPhone</button>
<a class="btn sec" id="dl" hidden download="clipboard.png" target="_blank">Mở / lưu ảnh</a></div>
<div class="card"><b>iPhone ➜ PC</b><br>
<button id="sendclip" hidden>Gửi clipboard iPhone (chữ / ảnh)</button>
<textarea id="ph" placeholder="Hoặc dán chữ vào đây…"></textarea>
<button id="send">Gửi chữ sang PC</button>
<label class="btn sec">🖼️ Chọn ảnh gửi sang PC<input type="file" id="file" accept="image/*" hidden></label></div>
<div id="st"></div>
<script>
const T=new URLSearchParams(location.search).get('token')||'', Q='?token='+encodeURIComponent(T);
const $=id=>document.getElementById(id);
const st=m=>{$('st').textContent=m;clearTimeout(st.t);st.t=setTimeout(()=>$('st').textContent='',3000)};
const canClipRead=!!(window.isSecureContext&&navigator.clipboard&&navigator.clipboard.read);
$('sendclip').hidden=!canClipRead;
let sig=null,kind='empty';
const imgUrl=()=>'/api/image'+Q+'&s='+sig;
async function poll(){try{const r=await fetch('/api/status'+Q,{cache:'no-store'});
 if(r.status==401){$('st').textContent='Sai token';return}
 const s=await r.json();if(s.sig===sig&&s.kind===kind)return;sig=s.sig;kind=s.kind;
 const isImg=kind==='image';$('pc').hidden=isImg;$('pcimg').hidden=!isImg;$('dl').hidden=!isImg;
 $('kind').textContent={text:'chữ',image:'ảnh',empty:'trống'}[kind]||kind;
 if(isImg){$('pcimg').src=imgUrl();$('dl').href=imgUrl()}
 else $('pc').value=kind==='text'?await (await fetch('/api/text'+Q,{cache:'no-store'})).text():'';
}catch(e){$('st').textContent='Mất kết nối tới PC…'}}
setInterval(poll,1500);poll();
$('copy').onclick=async()=>{
 if(kind==='image'){
  if(window.ClipboardItem&&navigator.clipboard&&navigator.clipboard.write){
   try{await navigator.clipboard.write([new ClipboardItem({'image/png':fetch(imgUrl()).then(r=>r.blob())})]);return st('✅ Đã copy ảnh')}catch(e){}}
  return st('Giữ ngón tay vào ảnh ➜ Sao chép');}
 const t=$('pc').value;
 try{await navigator.clipboard.writeText(t);return st('✅ Đã copy')}catch(e){}
 const a=$('pc');a.removeAttribute('readonly');a.focus();a.setSelectionRange(0,t.length);
 const ok=document.execCommand('copy');a.setAttribute('readonly','');a.blur();st(ok?'✅ Đã copy':'Giữ ô chữ ➜ Chọn tất cả ➜ Sao chép')};
// Ảnh không phải PNG/JPEG/GIF (vd. HEIC) -> trình duyệt tự đổi sang PNG trước khi gửi
async function toPng(blob){const bm=await createImageBitmap(blob);const c=document.createElement('canvas');
 c.width=bm.width;c.height=bm.height;c.getContext('2d').drawImage(bm,0,0);return await new Promise(r=>c.toBlob(r,'image/png'))}
async function upload(body,type){st('Đang gửi…');
 if(type.startsWith('image/')&&!/^image\/(png|jpeg|gif|bmp)$/.test(type)){
  try{body=await toPng(body);type='image/png'}catch(e){return st('Không đọc được ảnh này')}}
 const r=await fetch('/api/clip'+Q,{method:'POST',body,headers:{'Content-Type':type}});
 st(r.ok?'✅ Đã gửi – Ctrl+V trên PC':'Lỗi: '+await r.text());if(r.ok)setTimeout(poll,300)}
$('send').onclick=()=>{const t=$('ph').value;if(!t)return st('Chưa có nội dung');upload(t,'text/plain;charset=utf-8')};
$('file').onchange=e=>{const f=e.target.files[0];if(f)upload(f,f.type||'application/octet-stream');e.target.value=''};
$('ph').addEventListener('paste',e=>{for(const it of e.clipboardData.items){if(it.type.startsWith('image/')){e.preventDefault();upload(it.getAsFile(),it.type);return}}});
$('sendclip').onclick=async()=>{try{const items=await navigator.clipboard.read();
 for(const it of items){const im=it.types.find(t=>t.startsWith('image/'));
  if(im)return upload(await it.getType(im),im);
  if(it.types.includes('text/plain'))return upload(await (await it.getType('text/plain')).text(),'text/plain;charset=utf-8')}
 st('Clipboard iPhone trống')}catch(e){st('Không đọc được clipboard: '+e.message)}};
</script></body></html>"""


# ================================================================ server
class Handler(BaseHTTPRequestHandler):
    token = ""
    protocol_version = "HTTP/1.1"

    def _authed(self, q):
        given = q.get("token", [""])[0] or self.headers.get("X-Token", "")
        ok = secrets.compare_digest(given.encode(), self.token.encode())
        if not ok:
            time.sleep(0.5)  # làm chậm dò token
        return ok

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if not u.path.startswith("/api/"):
            return self._send(404, "not found")
        if not self._authed(q):
            return self._send(401, "unauthorized")
        try:
            seq, (kind, val) = CLIP.current()
        except Exception as e:
            return self._send(500, str(e))
        if u.path == "/api/status":
            return self._send(200, json.dumps({"kind": kind, "sig": str(seq)}), "application/json")
        if u.path == "/api/text":
            return self._send(200, val if kind == "text" else "")
        if u.path in ("/api/image", "/api/clip"):  # /api/clip cho Phím tắt iOS: chữ hoặc ảnh
            if kind == "image":
                return self._send(200, val, sniff_mime(val) or "application/octet-stream")
            if u.path == "/api/image":
                return self._send(404, "no image")
            return self._send(200, val if kind == "text" else "")
        self._send(404, "not found")

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path != "/api/clip":
            return self._send(404, "not found")
        if not self._authed(q):
            return self._send(401, "unauthorized")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            return self._send(413, "quá lớn")
        raw = self.rfile.read(n)
        ctype = (self.headers.get("Content-Type") or "").lower()
        try:
            if ctype.startswith("image/") or sniff_mime(raw):
                CLIP.write("image", raw)
                print(f"[iPhone ➜ PC] ảnh {len(raw) // 1024} KB")
            else:
                text = raw.decode("utf-8", errors="replace")
                if "json" in ctype:  # {"text": "..."} từ Phím tắt iOS
                    try:
                        text = str(json.loads(text).get("text", ""))
                    except Exception:
                        pass
                CLIP.write("text", text)
                print(f"[iPhone ➜ PC] {len(text)} ký tự")
        except ValueError as e:
            return self._send(415, f"lỗi: {e}")
        except Exception as e:
            return self._send(500, f"lỗi: {e}")
        self._send(200, "ok")

    def log_message(self, *a):
        pass


# ================================================================ tunnel
def _download(url, dest):
    if sys.platform == "win32":
        # Dùng curl.exe / PowerShell có sẵn của Windows -> .exe không phải kèm OpenSSL (nhẹ hơn ~4 MB)
        q = str(dest).replace("'", "''")
        cmds = [
            ["curl.exe", "-fL", "--retry", "3", "-o", str(dest), url],
            ["powershell", "-NoProfile", "-Command",
             f"[Net.ServicePointManager]::SecurityProtocol='Tls12'; "
             f"Invoke-WebRequest -UseBasicParsing -Uri '{url}' -OutFile '{q}'"],
        ]
        for c in cmds:
            try:
                subprocess.run(c, check=True)
                return
            except (OSError, subprocess.CalledProcessError):
                continue
        raise RuntimeError("không tải được")
    import urllib.request

    urllib.request.urlretrieve(url, dest)


def find_cloudflared():
    exe = shutil.which("cloudflared")
    if exe:
        return exe
    local = APP_DIR / ("cloudflared.exe" if os.name == "nt" else "cloudflared")
    if local.exists():
        return str(local)
    m = platform.machine().lower()
    base = "https://github.com/cloudflare/cloudflared/releases/latest/download/"
    if sys.platform == "win32":
        name = "cloudflared-windows-386.exe" if m in ("x86", "i386", "i686") else "cloudflared-windows-amd64.exe"
    elif sys.platform.startswith("linux"):
        name = "cloudflared-linux-arm64" if m in ("aarch64", "arm64") else "cloudflared-linux-amd64"
    else:
        sys.exit("Hãy cài cloudflared thủ công (macOS: brew install cloudflared)")
    print(f"Đang tải cloudflared (~30 MB, chỉ lần đầu) về {local} ...")
    tmp = local.with_suffix(".part")
    try:
        _download(base + name, tmp)
    except Exception:
        sys.exit(f"Không tải được cloudflared. Hãy tự tải:\n  {base + name}\n"
                 f"đổi tên thành {local.name} và đặt vào: {APP_DIR}")
    tmp.replace(local)
    if os.name != "nt":
        local.chmod(0o755)
    return str(local)


def start_tunnel(port, on_url):
    exe = find_cloudflared()
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", creationflags=flags,
    )
    atexit.register(proc.terminate)

    def reader():
        found = False
        for line in proc.stdout:
            m = re.search(r"https://[-a-z0-9]+\.trycloudflare\.com", line)
            if m and not found:
                found = True
                on_url(m.group(0))
        print("!! cloudflared đã dừng (mất mạng?). Khởi động lại ClipBridge.")

    threading.Thread(target=reader, daemon=True).start()


# ================================================================ main
def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # không gửi gói tin thật
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def show(title, url):
    print(f"\n {title}:\n   {url}")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except Exception:
        pass


def main():
    global CLIP
    ap = argparse.ArgumentParser(description="ClipBridge: clipboard Windows <-> iOS")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", help="mã bí mật (mặc định: tự sinh ngẫu nhiên)")
    ap.add_argument("--tunnel", action="store_true", help="mở link HTTPS dùng được ở mọi nơi (Cloudflare)")
    ap.add_argument("--local-only", action="store_true", help="chỉ nghe trên 127.0.0.1 (khi dùng --tunnel)")
    a = ap.parse_args()

    # Double-click (không có tham số): hỏi chế độ + đọc token.txt cạnh file
    if len(sys.argv) == 1:
        print("=" * 45)
        print("  ClipBridge - Clipboard Windows <-> iPhone")
        print("=" * 45)
        print("  1. Cùng mạng Wi-Fi")
        print("  2. Dùng ở bất kỳ đâu (4G / Wi-Fi khác)")
        while True:
            c = input("\n  Chọn 1 hoặc 2: ").strip()
            if c in ("1", "2"):
                break
        a.tunnel = c == "2"
        tf = APP_DIR / "token.txt"
        if tf.exists():
            t = tf.read_text(encoding="utf-8-sig").strip()
            if t:
                a.token = t
        print()

    if a.token:
        if a.tunnel and len(a.token) < 16:
            sys.exit("Với chế độ tunnel, token phải dài ít nhất 16 ký tự (link mở ra Internet).")
        Handler.token = a.token
    else:
        Handler.token = secrets.token_urlsafe(16 if a.tunnel else 6)

    CLIP = Clip()
    host = "127.0.0.1" if a.local_only else "0.0.0.0"
    srv = ThreadingHTTPServer((host, a.port), Handler)
    srv.daemon_threads = True

    print("=" * 64)
    print(" ClipBridge đang chạy.  Ctrl+C (hoặc đóng cửa sổ) để dừng.")
    if sys.platform != "win32":
        print(" (Không phải Windows: dùng clipboard giả lập trong bộ nhớ)")
    if not a.local_only:
        ip = lan_ip()
        show("Cùng Wi-Fi - mở trên Safari", f"http://{ip}:{a.port}/?token={Handler.token}")
    if a.tunnel:
        print("\n Đang mở đường hầm Cloudflare...")
        start_tunnel(a.port, lambda url: (
            show("Mọi nơi (4G/Wi-Fi khác) - mở trên Safari", f"{url}/?token={Handler.token}"),
            print(f"   API cho Phím tắt iOS: {url}/api/clip?token={Handler.token}"),
            print("   (link này đổi mỗi lần khởi động lại)"),
        ))
    print("=" * 64)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if FROZEN and e.code not in (None, 0):
            print(e.code if isinstance(e.code, str) else "")
            input("\nNhấn Enter để thoát...")
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        if FROZEN:
            input("\nNhấn Enter để thoát...")
        sys.exit(1)
