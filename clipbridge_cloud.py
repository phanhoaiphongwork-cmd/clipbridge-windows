#!/usr/bin/env python3
"""
ClipBridge Cloud - app Windows: tự đồng bộ clipboard (chữ + ảnh) với iPhone qua Internet.

- Ghép đôi bằng mã 20 ký tự / QR, không cần tài khoản.
- Mã hoá đầu-cuối AES-256-GCM: server (Vercel + Supabase) chỉ thấy dữ liệu đã mã hoá.
- Copy trên PC -> tự gửi; iPhone gửi sang -> tự vào clipboard PC (Ctrl+V là dán).

Chạy:  python clipbridge_cloud.py      (hoặc ClipBridge.exe)
"""
import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import clipbridge as cb  # clipboard Windows + xử lý ảnh (dùng chung với bản LAN)

APP_DIR = cb.APP_DIR
CONFIG = APP_DIR / "clipbridge_config.json"
ALPH = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32
SALT = b"clipbridge-v1"
MAX_BYTES = 25 * 1024 * 1024
UA = "ClipBridge/2.0"
DEFAULT_SERVER = "https://clipbridge-weld.vercel.app"


def log(msg):
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


# ================================================================ mã ghép đôi & khoá
def new_code():
    return "".join(secrets.choice(ALPH) for _ in range(20))


def norm_code(s):
    s = s.upper().replace("O", "0").replace("I", "1").replace("L", "1").replace("U", "V")
    return "".join(c for c in s if c in ALPH)


def pretty_code(c):
    return "-".join(c[i:i + 5] for i in range(0, len(c), 5))


def hkdf(ikm: bytes, info: bytes, n=32):
    prk = hmac.new(SALT, ikm, hashlib.sha256).digest()
    out, t, i = b"", b"", 1
    while len(out) < n:
        t = hmac.new(prk, t + info + bytes([i]), hashlib.sha256).digest()
        out += t
        i += 1
    return out[:n]


class AesGcm:
    """AES-256-GCM qua libcrypto (OpenSSL) đi kèm Python - không cần cài thêm gì."""

    def __init__(self, key: bytes):
        self.key = key
        L = self.lib = self._load()
        vp, ip = ctypes.c_void_p, ctypes.c_int
        L.EVP_CIPHER_CTX_new.restype = vp
        L.EVP_CIPHER_CTX_free.argtypes = [vp]
        L.EVP_aes_256_gcm.restype = vp
        for f in (L.EVP_EncryptInit_ex, L.EVP_DecryptInit_ex):
            f.argtypes, f.restype = [vp, vp, vp, ctypes.c_char_p, ctypes.c_char_p], ip
        for f in (L.EVP_EncryptUpdate, L.EVP_DecryptUpdate):
            f.argtypes, f.restype = [vp, vp, ctypes.POINTER(ip), ctypes.c_char_p, ip], ip
        for f in (L.EVP_EncryptFinal_ex, L.EVP_DecryptFinal_ex):
            f.argtypes, f.restype = [vp, vp, ctypes.POINTER(ip)], ip
        L.EVP_CIPHER_CTX_ctrl.argtypes, L.EVP_CIPHER_CTX_ctrl.restype = [vp, ip, ip, vp], ip

    @staticmethod
    def _load():
        import _ssl  # nạp libcrypto vào tiến trình

        if sys.platform == "win32":
            names = ["libcrypto-3-x64", "libcrypto-3", "libcrypto-1_1-x64", "libcrypto-1_1"]
            dirs = [getattr(sys, "_MEIPASS", ""), os.path.dirname(_ssl.__file__),
                    os.path.join(sys.base_prefix, "DLLs"), os.path.dirname(sys.executable)]
            for d in dirs:
                for n in names:
                    p = os.path.join(d, n + ".dll")
                    if d and os.path.exists(p):
                        return ctypes.CDLL(p)
            for n in names:
                try:
                    return ctypes.CDLL(n)
                except OSError:
                    pass
        else:
            for n in (ctypes.util.find_library("crypto"), "libcrypto.so.3", "libcrypto.so"):
                if n:
                    try:
                        return ctypes.CDLL(n)
                    except OSError:
                        pass
        raise OSError("Không tìm thấy libcrypto (OpenSSL)")

    def _run(self, encrypt, data, aad, iv, tag=None):
        L = self.lib
        ctx = L.EVP_CIPHER_CTX_new()
        if not ctx:
            raise MemoryError
        try:
            init, upd, fin = ((L.EVP_EncryptInit_ex, L.EVP_EncryptUpdate, L.EVP_EncryptFinal_ex) if encrypt
                              else (L.EVP_DecryptInit_ex, L.EVP_DecryptUpdate, L.EVP_DecryptFinal_ex))
            n = ctypes.c_int(0)
            if init(ctx, L.EVP_aes_256_gcm(), None, self.key, iv) != 1:
                raise ValueError("init")
            if upd(ctx, None, ctypes.byref(n), aad, len(aad)) != 1:
                raise ValueError("aad")
            out = ctypes.create_string_buffer(len(data) + 16)
            if upd(ctx, out, ctypes.byref(n), data, len(data)) != 1:
                raise ValueError("update")
            total = n.value
            if not encrypt:
                tb = ctypes.create_string_buffer(tag, 16)
                L.EVP_CIPHER_CTX_ctrl(ctx, 0x11, 16, tb)  # EVP_CTRL_GCM_SET_TAG
            if fin(ctx, ctypes.addressof(out) + total, ctypes.byref(n)) != 1:
                raise ValueError("Giải mã thất bại (sai mã ghép đôi?)")
            total += n.value
            res = out.raw[:total]
            if encrypt:
                tb = ctypes.create_string_buffer(16)
                L.EVP_CIPHER_CTX_ctrl(ctx, 0x10, 16, tb)  # EVP_CTRL_GCM_GET_TAG
                res += tb.raw
            return res
        finally:
            L.EVP_CIPHER_CTX_free(ctx)

    def seal(self, data: bytes, kind: str) -> bytes:
        iv = os.urandom(12)
        return b"\x01" + iv + self._run(True, data, kind.encode(), iv)

    def open(self, blob: bytes, kind: str) -> bytes:
        if len(blob) < 29 or blob[0] != 1:
            raise ValueError("Dữ liệu hỏng / phiên bản không hỗ trợ")
        return self._run(False, blob[13:-16], kind.encode(), blob[1:13], blob[-16:])


# ================================================================ HTTP
def http(method, url, body=None, ctype=None, timeout=60):
    headers = {"User-Agent": UA}
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        msg = e.read()[:300].decode("utf-8", "replace")
        try:
            msg = json.loads(msg).get("error", msg)
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code}: {msg}") from None


class Api:
    def __init__(self, server):
        self.server = server.rstrip("/")

    def get(self, path, **q):
        return json.loads(http("GET", f"{self.server}{path}?{urlencode(q)}" if q else self.server + path))

    def post(self, path, obj):
        return json.loads(http("POST", self.server + path, json.dumps(obj).encode(), "application/json"))


# ================================================================ WebSocket tối giản (cho Supabase Realtime)
class WebSocket:
    def __init__(self, url, timeout=20):
        u = urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        # Hỗ trợ proxy (biến HTTPS_PROXY hoặc cài đặt proxy của Windows)
        proxy = urllib.request.getproxies().get("https" if u.scheme == "wss" else "http")
        if proxy and not urllib.request.proxy_bypass(u.hostname):
            pu = urlparse(proxy if "://" in proxy else "http://" + proxy)
            sock = socket.create_connection((pu.hostname, pu.port or 8080), timeout=timeout)
            auth = ""
            if pu.username:
                cred = base64.b64encode(f"{pu.username}:{pu.password or ''}".encode()).decode()
                auth = f"Proxy-Authorization: Basic {cred}\r\n"
            sock.sendall(f"CONNECT {u.hostname}:{port} HTTP/1.1\r\nHost: {u.hostname}:{port}\r\n{auth}\r\n".encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("Proxy đóng kết nối")
                resp += chunk
            if b" 200" not in resp.split(b"\r\n", 1)[0]:
                raise ConnectionError("Proxy từ chối: " + resp.split(b"\r\n", 1)[0].decode("latin1"))
        else:
            sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        self.sock, self.buf = sock, b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        host = u.hostname + (f":{u.port}" if u.port else "")
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {UA}\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket handshake bị đóng")
            self.buf += chunk
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("WebSocket bị từ chối: " + head.split(b"\r\n", 1)[0].decode("latin1"))
        self.lock = threading.Lock()

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("WebSocket đã đóng")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text, opcode=1):
        data = text.encode() if isinstance(text, str) else text
        n = len(data)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
        with self.lock:
            self.sock.sendall(hdr + mask + masked)

    def recv(self, wait):
        """Trả về chuỗi tin nhắn, hoặc None nếu hết `wait` giây không có gì."""
        msg, first_op = b"", None
        while True:
            if not self.buf:
                self.sock.settimeout(wait)
                try:
                    chunk = self.sock.recv(65536)
                except (socket.timeout, TimeoutError):
                    return None
                finally:
                    self.sock.settimeout(30)
                if not chunk:
                    raise ConnectionError("WebSocket đã đóng")
                self.buf += chunk
            b1, b2 = self._read(2)
            op, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i & 3] for i, b in enumerate(data))
            if op == 8:
                raise ConnectionError("Server đóng WebSocket")
            if op == 9:
                self.send(data, opcode=10)
                continue
            if op == 10:
                continue
            if op in (1, 2):
                first_op, msg = op, data
            elif op == 0:
                msg += data
            if b1 & 0x80 and first_op:
                return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


# ================================================================ đồng bộ
class Sync:
    def __init__(self, server, code, sender):
        self.api = Api(server)
        self.code, self.sender = code, sender
        ikm = code.encode()
        self.room = hkdf(ikm, b"clipbridge room").hex()
        self.aes = AesGcm(hkdf(ikm, b"clipbridge key"))
        self.clip = cb.WindowsClipboard() if sys.platform == "win32" else cb.MemoryClipboard()
        self.lock = threading.Lock()
        self.last_id = 0
        self.own_seq = None
        self.paused = False
        self.realtime = False
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.on_change = None  # gọi khi có mục mới (app giao diện dùng để làm mới lịch sử)
        self._hist_cache = {}  # id -> nội dung đã giải mã (khỏi tải lại ảnh)

    def _changed(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception:
                pass

    # ---------- gửi lên ----------
    def push(self, kind, payload):
        if kind == "text":
            data = base64.b64encode(self.aes.seal(payload.encode("utf-8"), "text")).decode()
            self.api.post("/api/push", {"room": self.room, "sender": self.sender, "kind": "text", "data": data})
            log(f"PC ➜ iPhone  chữ ({len(payload)} ký tự)")
            self._changed()
        else:
            sealed = self.aes.seal(payload, "image")
            if len(sealed) > MAX_BYTES:
                log("Bỏ qua: ảnh quá lớn (tối đa 25 MB)")
                return
            r = self.api.post("/api/upload-url", {"room": self.room, "sender": self.sender})
            http("PUT", r["signedUrl"], sealed, "application/octet-stream", timeout=120)
            self.api.post("/api/push", {"room": self.room, "sender": self.sender, "kind": "image",
                                        "id": r["id"], "size": len(sealed)})
            log(f"PC ➜ iPhone  ảnh ({len(payload) // 1024} KB)")
            self._changed()

    def watch_loop(self):
        """Theo dõi clipboard Windows; có thay đổi do người dùng copy -> gửi lên."""
        last = self.clip.seq()
        while not self.stop.is_set():
            time.sleep(0.3)
            s = self.clip.seq()
            if s == last:
                continue
            time.sleep(0.2)  # chờ app nguồn ghi xong mọi định dạng
            with self.lock:
                s = self.clip.seq()
                last = s
                if s == self.own_seq or self.paused:
                    continue
                try:
                    if self.clip.is_private():
                        log("Bỏ qua nội dung riêng tư (trình quản lý mật khẩu)")
                        continue
                    kind, payload = self.clip.read()
                except Exception as e:
                    log(f"Lỗi đọc clipboard: {e}")
                    continue
            if kind not in ("text", "image") or not payload:
                continue
            if kind == "text" and len(payload.encode("utf-8")) > 1_000_000:
                log("Bỏ qua: chữ quá dài (> 1 MB)")
                continue
            try:
                self.push(kind, payload)
            except Exception as e:
                log(f"Lỗi gửi: {e}")

    # ---------- nhận về ----------
    def pull(self, apply=True):
        q = {"room": self.room, "not": self.sender}
        if self.last_id:
            q["since"] = self.last_id
        clip = self.api.get("/api/pull", **q).get("clip")
        if not clip:
            return
        self.last_id = max(self.last_id, int(clip["id"]))
        if not apply:
            return
        if clip["kind"] == "text":
            text = self.aes.open(base64.b64decode(clip["data"]), "text").decode("utf-8")
            with self.lock:
                self.clip.write_text(text)
                self.own_seq = self.clip.seq()
            log(f"iPhone ➜ PC  chữ ({len(text)} ký tự) - Ctrl+V để dán")
            self._changed()
        else:
            img = self.aes.open(http("GET", clip["url"], timeout=120), "image")
            with self.lock:
                self.clip.write_image(img)
                self.own_seq = self.clip.seq()
            log(f"iPhone ➜ PC  ảnh ({len(img) // 1024} KB) - Ctrl+V để dán")
            self._changed()

    # ---------- lịch sử ----------
    def history(self):
        """Các mục gần đây của mọi thiết bị (mới nhất trước): [{id, sender, kind, at, payload | error}]."""
        out, cache = [], {}
        for c in self.api.get("/api/history", room=self.room).get("clips", []):
            item = {"id": c["id"], "sender": c.get("sender", ""), "kind": c["kind"], "at": c["created_at"]}
            try:
                if c["id"] in self._hist_cache:
                    item["payload"] = self._hist_cache[c["id"]]
                elif c["kind"] == "text":
                    item["payload"] = self.aes.open(base64.b64decode(c["data"]), "text").decode("utf-8")
                else:
                    if not c.get("url"):
                        raise RuntimeError("ảnh đã hết hạn")
                    item["payload"] = self.aes.open(http("GET", c["url"], timeout=120), "image")
                cache[c["id"]] = item["payload"]
            except Exception as e:
                item["error"] = str(e)
            out.append(item)
        self._hist_cache = cache
        return out

    def copy_local(self, kind, payload):
        """Ghi vào clipboard PC mà không gửi lại sang iPhone."""
        with self.lock:
            if kind == "text":
                self.clip.write_text(payload)
            else:
                self.clip.write_image(payload)
            self.own_seq = self.clip.seq()

    def pull_loop(self):
        while not self.stop.is_set():
            # realtime báo ngay; ngoài ra kiểm tra định kỳ phòng khi lỡ tin
            self.wake.wait(30 if self.realtime else 5)
            self.wake.clear()
            try:
                self.pull()
            except Exception as e:
                log(f"Lỗi nhận: {e}")

    def realtime_loop(self, cfg):
        url = (cfg["supabaseUrl"].replace("https://", "wss://").replace("http://", "ws://")
               + "/realtime/v1/websocket?apikey=" + quote(cfg["anonKey"]) + "&vsn=1.0.0")
        backoff = 1
        while not self.stop.is_set():
            ws = None
            try:
                ws = WebSocket(url)
                ws.send(json.dumps({"topic": f"realtime:{self.room}", "event": "phx_join", "ref": "1", "join_ref": "1",
                                    "payload": {"config": {"broadcast": {"self": False}, "presence": {"key": ""},
                                                           "postgres_changes": [], "private": False}}}))
                hb, ref = time.time(), 2
                while not self.stop.is_set():
                    raw = ws.recv(1.0)
                    if time.time() - hb > 25:
                        ws.send(json.dumps({"topic": "phoenix", "event": "heartbeat", "payload": {}, "ref": str(ref)}))
                        hb, ref = time.time(), ref + 1
                    if raw is None:
                        continue
                    m = json.loads(raw)
                    ev = m.get("event")
                    if ev == "phx_reply" and m.get("ref") == "1":
                        if m.get("payload", {}).get("status") != "ok":
                            raise ConnectionError(f"join bị từ chối: {m.get('payload')}")
                        if not self.realtime:
                            log("Realtime: đã kết nối ✓")
                        self.realtime, backoff = True, 1
                        self.wake.set()
                    elif ev == "broadcast":
                        p = (m.get("payload") or {}).get("payload") or {}
                        if p.get("sender") != self.sender:
                            self.wake.set()
                    elif ev in ("phx_error", "phx_close"):
                        raise ConnectionError(ev)
            except Exception as e:
                if self.realtime:
                    log(f"Realtime mất kết nối ({e}), đang thử lại…")
                self.realtime = False
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                if ws:
                    ws.close()

    def start(self):
        cfg = self.api.get("/api/config")
        if not cfg.get("supabaseUrl") or not cfg.get("anonKey"):
            raise RuntimeError("Server chưa cấu hình SUPABASE_URL / SUPABASE_ANON_KEY trên Vercel")
        self.pull(apply=False)  # bỏ qua nội dung cũ, chỉ nhận cái mới từ giờ
        for fn, args in ((self.watch_loop, ()), (self.pull_loop, ()), (self.realtime_loop, (cfg,))):
            threading.Thread(target=fn, args=args, daemon=True).start()


# ================================================================ giao diện console
def load_cfg():
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_cfg(c):
    CONFIG.write_text(json.dumps(c, indent=2), encoding="utf-8")


def show_pairing(server, code):
    url = f"{server}/#p={code}"
    print("\n" + "=" * 60)
    print("  GHÉP ĐÔI iPHONE")
    print("  Mở Camera iPhone và quét mã QR này:")
    try:
        import qrcode

        q = qrcode.QRCode(border=1)
        q.add_data(url)
        q.print_ascii(invert=True)
    except Exception:
        print("  (cài 'qrcode' để hiện mã QR)")
    print(f"  Hoặc mở {server} trên iPhone và nhập mã:")
    print(f"\n      {pretty_code(code)}\n")
    print("  ⚠ Giữ bí mật mã này: ai có mã đều xem được clipboard của bạn.")
    print("=" * 60)


def source_label(sender, me):
    if sender == me:
        return "PC này"
    if sender.startswith("pc-"):
        return "PC khác"
    if sender.startswith("ios-"):
        return "iPhone"
    return "khác"


def print_history(items, me):
    if not items:
        print("  (Chưa có gì trong 4 giờ qua)")
        return
    print("\n  LỊCH SỬ (4 giờ qua, mới nhất trước) - gõ số để copy lại:")
    for i, it in enumerate(items, 1):
        try:
            from datetime import datetime

            at = datetime.fromisoformat(it["at"].replace("Z", "+00:00")).astimezone().strftime("%H:%M")
        except Exception:
            at = "--:--"
        src = source_label(it["sender"], me)
        if "error" in it:
            desc = f"⚠ không mở được ({it['error']})"
        elif it["kind"] == "text":
            t = " ".join(it["payload"].split())
            desc = f"\"{t[:60]}{'…' if len(t) > 60 else ''}\""
        else:
            desc = f"[ảnh {len(it['payload']) // 1024} KB]"
        print(f"  {i:>2}. {at}  {src:<7} {desc}")
    print()


def history_mode(c):
    """--history: xem lịch sử và copy lại (dùng được cả khi ClipBridge đang chạy ngầm)."""
    sync = Sync(c["server"], c["code"], c["sender"])
    while True:
        items = sync.history()
        print_history(items, c["sender"])
        try:
            cmd = input("  Số để copy, Enter = tải lại, q = thoát: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return
        if cmd == "q":
            return
        if cmd.isdigit() and 1 <= int(cmd) <= len(items) and "error" not in items[int(cmd) - 1]:
            it = items[int(cmd) - 1]
            sync.copy_local(it["kind"], it["payload"])
            print(f"  ✓ Đã copy mục {cmd} vào clipboard PC - Ctrl+V để dán")


# ================================================================ chạy ngầm & tự khởi động
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "ClipBridge"
MUTEX_NAME = "Local\\ClipBridgeCloud"
PID_FILE = APP_DIR / "clipbridge.pid"
LOG_FILE = APP_DIR / "clipbridge.log"
_mutex = None


def acquire_single_instance():
    """True nếu đây là phiên duy nhất; False nếu ClipBridge đã chạy (ngầm hoặc cửa sổ khác)."""
    global _mutex
    if sys.platform != "win32":
        return True
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    _mutex = k.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def is_running():
    if sys.platform != "win32":
        return False
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenMutexW.restype = ctypes.c_void_p
    k.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    h = k.OpenMutexW(0x00100000, False, MUTEX_NAME)  # SYNCHRONIZE
    if h:
        k.CloseHandle(h)
    return bool(h)


def message_box(text, error=False):
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, text, "ClipBridge", 0x10 if error else 0x40)
    else:
        print(text)


def background_command():
    """Lệnh để Windows chạy ngầm: pythonw.exe (đã ký số -> Smart App Control không chặn)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --background'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}" --background'


def start_background():
    import shlex
    import subprocess

    args = shlex.split(background_command(), posix=False)
    args = [a.strip('"') for a in args]
    flags = 0x00000008 | 0x00000200 if sys.platform == "win32" else 0  # DETACHED | NEW_PROCESS_GROUP
    subprocess.Popen(args, cwd=str(APP_DIR), creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_background():
    if not is_running():
        PID_FILE.unlink(missing_ok=True)
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 15)
    except Exception as e:
        print(f"Không dừng được: {e}")
        return False
    for _ in range(30):
        if not is_running():
            break
        time.sleep(0.1)
    PID_FILE.unlink(missing_ok=True)
    return True


def install():
    if sys.platform != "win32":
        sys.exit("Chỉ hỗ trợ Windows.")
    c = load_cfg()
    if not c.get("code") or not c.get("server"):
        sys.exit("Hãy chạy 'py clipbridge_cloud.py' một lần để ghép đôi iPhone trước, rồi mới --install.")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, background_command())
    if not is_running():
        start_background()
        time.sleep(1.5)
    print("✓ Đã cài: ClipBridge sẽ tự chạy ngầm mỗi khi bạn đăng nhập Windows.")
    print("✓ ClipBridge đang chạy ngầm" if is_running() else "⚠ Chưa thấy ClipBridge chạy - xem clipbridge.log")
    print(f"  Nhật ký: {LOG_FILE}")
    print("  Dừng:     py clipbridge_cloud.py --stop")
    print("  Gỡ bỏ:    py clipbridge_cloud.py --uninstall")


def uninstall():
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_NAME)
        except FileNotFoundError:
            pass
    stopped = stop_background()
    print("✓ Đã gỡ tự khởi động." + (" Đã dừng ClipBridge đang chạy ngầm." if stopped else ""))


def start_sync_with_retry(c, background):
    """Lúc mới bật máy có thể chưa có mạng -> thử lại tới khi kết nối được."""
    delay = 5
    while True:
        try:
            sync = Sync(c["server"], c["code"], c["sender"])
            sync.start()
            return sync
        except Exception as e:
            log(f"Chưa kết nối được server ({e}), thử lại sau {delay}s…")
            if not background and delay >= 40:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 300)


def run_background():
    # pythonw không có cửa sổ: ghi nhật ký ra file (cắt bớt khi > 1 MB)
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
    except OSError:
        pass
    sys.stdout = sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    if not acquire_single_instance():
        return  # đã có một ClipBridge khác đang chạy
    c = load_cfg()
    if not c.get("code") or not c.get("server"):
        message_box("ClipBridge chưa được ghép đôi.\nHãy chạy 'py clipbridge_cloud.py' một lần trước.", error=True)
        return
    PID_FILE.write_text(str(os.getpid()))
    log("=== ClipBridge chạy ngầm ===")
    start_sync_with_retry(c, background=True)
    log("Đang chạy ngầm. Copy trên PC sẽ tự gửi sang iPhone.")
    while True:
        time.sleep(3600)


def main():
    import argparse

    ap = argparse.ArgumentParser(description="ClipBridge Cloud - đồng bộ clipboard Windows <-> iPhone")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--install", action="store_true", help="tự chạy ngầm cùng Windows (và bật ngay)")
    g.add_argument("--uninstall", action="store_true", help="gỡ tự khởi động và dừng chạy ngầm")
    g.add_argument("--stop", action="store_true", help="dừng ClipBridge đang chạy ngầm")
    g.add_argument("--history", action="store_true", help="xem lịch sử clipboard (PC + iPhone) và copy lại")
    g.add_argument("--background", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.background:
        return run_background()
    if os.name == "nt":
        os.system("")  # bật màu/ký tự Unicode trên console cũ
    if a.install:
        return install()
    if a.uninstall:
        return uninstall()
    if a.stop:
        print("✓ Đã dừng ClipBridge chạy ngầm." if stop_background() else "ClipBridge không chạy ngầm.")
        return

    c = load_cfg()
    if a.history:
        if not c.get("code") or not c.get("server"):
            sys.exit("Hãy chạy 'py clipbridge_cloud.py' một lần để ghép đôi iPhone trước.")
        return history_mode(c)
    print("ClipBridge Cloud - đồng bộ clipboard Windows <-> iPhone\n")
    if is_running():
        # Đã chạy ngầm -> không chạy thêm bản thứ 2 (sẽ gửi trùng), chỉ hiện lại mã ghép đôi
        print("ClipBridge đang chạy ngầm rồi. Dưới đây là mã để ghép thêm iPhone/iPad:")
        show_pairing(c["server"], c["code"])
        print("\n  Xem lịch sử:     py clipbridge_cloud.py --history")
        print("  Dừng chạy ngầm:  py clipbridge_cloud.py --stop")
        input("\nNhấn Enter để đóng…")
        return
    if not c.get("server"):
        while True:
            s = input(f"Địa chỉ web Vercel [Enter = {DEFAULT_SERVER}]: ").strip().rstrip("/") or DEFAULT_SERVER
            if not s.startswith("http"):
                s = "https://" + s
            try:
                Api(s).get("/api/config")
                break
            except Exception as e:
                print(f"  Không kết nối được: {e}")
        c["server"] = s
    if not c.get("code"):
        c["code"] = new_code()
    c.setdefault("sender", "pc-" + secrets.token_hex(6))
    save_cfg(c)
    acquire_single_instance()

    show_pairing(c["server"], c["code"])
    sync = start_sync_with_retry(c, background=False)
    log("Đang chạy. Copy trên PC sẽ tự gửi sang iPhone.")
    print("  Lệnh:  h = lịch sử (gõ số để copy lại)   p = tạm dừng/tiếp tục tự gửi")
    print("         m = hiện lại mã   n = tạo mã mới   q = thoát")
    print("  Muốn tự chạy ngầm cùng Windows: thoát (q) rồi chạy  py clipbridge_cloud.py --install\n")
    hist = []
    while True:
        try:
            cmd = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd == "q":
            break
        if cmd == "h":
            try:
                hist = sync.history()
                print_history(hist, c["sender"])
            except Exception as e:
                log(f"Lỗi tải lịch sử: {e}")
        elif cmd.isdigit() and hist:
            i = int(cmd)
            if 1 <= i <= len(hist) and "error" not in hist[i - 1]:
                sync.copy_local(hist[i - 1]["kind"], hist[i - 1]["payload"])
                log(f"Đã copy mục {i} trong lịch sử - Ctrl+V để dán")
            else:
                print("  Số không hợp lệ - gõ h để xem lại lịch sử")
        elif cmd == "p":
            sync.paused = not sync.paused
            log("Đã TẠM DỪNG tự gửi." if sync.paused else "Tiếp tục tự gửi.")
        elif cmd == "m":
            show_pairing(c["server"], c["code"])
        elif cmd == "n":
            if input("Tạo mã mới sẽ ngắt kết nối iPhone hiện tại. Chắc chắn? (y/n): ").strip().lower() == "y":
                c["code"] = new_code()
                save_cfg(c)
                print("Đã tạo mã mới - khởi động lại ClipBridge để dùng mã mới.")
                break
    sync.stop.set()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        if "--background" in sys.argv:
            message_box("ClipBridge gặp lỗi:\n" + traceback.format_exc()[-800:], error=True)
        elif getattr(sys, "frozen", False):
            input("\nNhấn Enter để thoát...")
        sys.exit(1)
