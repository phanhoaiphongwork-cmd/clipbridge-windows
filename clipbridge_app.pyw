#!/usr/bin/env python3
"""
ClipBridge - app Windows có giao diện (không cần cửa sổ terminal).

- Nhấp đúp file này (hoặc lối tắt "ClipBridge" trên Desktop) để mở.
- Đóng cửa sổ (X) -> ClipBridge vẫn chạy ngầm; mở lại app để hiện cửa sổ.
- Xem lịch sử copy của cả PC và iPhone, bấm "Copy" để copy lại.
- Ghép đôi iPhone bằng mã QR, bật/tắt tự khởi động cùng Windows.

Dùng chung lõi đồng bộ với clipbridge_cloud.py (bản dòng lệnh).
"""
import base64
import ctypes
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipbridge as cb  # noqa: E402
import clipbridge_cloud as cc  # noqa: E402

WIN = sys.platform == "win32"
FROZEN = getattr(sys, "frozen", False)
ICON_PNG = (Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parent) / "icon.png"
SHOW_EVENT = "Local\\ClipBridgeShowWindow"
NO_WINDOW = 0x08000000 if WIN else 0  # CREATE_NO_WINDOW

events = queue.Queue()  # luồng nền -> giao diện


# ================================================================ nhật ký
def app_log(msg):
    line = time.strftime("[%H:%M:%S] ") + msg
    try:
        with open(cc.LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    events.put(("log", line))


cc.log = app_log  # mọi thông báo của lõi đồng bộ -> khung "Hoạt động"


# ================================================================ một phiên duy nhất
def _k32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateEventW.restype = k.OpenEventW.restype = ctypes.c_void_p
    k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
    k.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k.SetEvent.argtypes = k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    return k


def signal_existing_app():
    """Nếu app đã mở (kể cả đang ẩn) -> bảo nó hiện cửa sổ. True nếu đã báo được."""
    if not WIN:
        return False
    k = _k32()
    h = k.OpenEventW(0x0002, False, SHOW_EVENT)  # EVENT_MODIFY_STATE
    if not h:
        return False
    k.SetEvent(h)
    k.CloseHandle(h)
    return True


def listen_show_requests():
    if not WIN:
        return
    k = _k32()
    h = k.CreateEventW(None, False, False, SHOW_EVENT)

    def loop():
        while True:
            if k.WaitForSingleObject(h, 0xFFFFFFFF) == 0:
                events.put(("show", None))

    threading.Thread(target=loop, daemon=True).start()


# ================================================================ tự khởi động & lối tắt
def app_command(hidden):
    if FROZEN:
        cmd = f'"{sys.executable}"'
    else:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        cmd = f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"'
    return cmd + (" --hidden" if hidden else "")


def autostart_enabled():
    if not WIN:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cc.RUN_KEY) as key:
            return winreg.QueryValueEx(key, cc.RUN_NAME)[0] == app_command(True)
    except OSError:
        return False


def set_autostart(on):
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, cc.RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, cc.RUN_NAME, 0, winreg.REG_SZ, app_command(True))
        else:
            try:
                winreg.DeleteValue(key, cc.RUN_NAME)
            except FileNotFoundError:
                pass


def make_ico():
    """icon.ico (chứa ảnh PNG) cho lối tắt - Windows Vista trở lên đọc được."""
    ico = cc.APP_DIR / "icon.ico"
    png = ICON_PNG.read_bytes()
    w, h = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    head = (b"\0\0\1\0\1\0" + bytes([w % 256, h % 256, 0, 0]) + (1).to_bytes(2, "little")
            + (32).to_bytes(2, "little") + len(png).to_bytes(4, "little") + (22).to_bytes(4, "little"))
    ico.write_bytes(head + png)
    return ico


def _guid(s):
    import uuid

    return (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(s).bytes_le)


def _com_call(obj, index, *args, argtypes=()):
    """Gọi hàm thứ `index` trong vtable của đối tượng COM `obj`."""
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])
    hr = fn(obj, *args)
    if hr < 0:
        raise OSError(f"COM lỗi 0x{hr & 0xFFFFFFFF:08X}")


def create_shortcuts():
    """Lối tắt ClipBridge trên Desktop và Start Menu.

    Gọi thẳng IShellLink của Windows qua ctypes (không dùng PowerShell/VBScript: Smart App
    Control có thể khoá PowerShell ở chế độ Constrained Language và chặn WScript.Shell).
    """
    if FROZEN:
        target, args = sys.executable, ""
    else:
        exe = Path(sys.executable)
        pyw = exe.with_name("pythonw.exe")
        target, args = str(pyw if pyw.exists() else exe), f'"{Path(__file__).resolve()}"'
    ico = str(make_ico())
    ole, sh = ctypes.OleDLL("ole32"), ctypes.WinDLL("shell32")
    ctypes.WinDLL("ole32").CoInitialize(None)  # bỏ qua kết quả: có thể đã khởi tạo sẵn
    W, P = ctypes.c_wchar_p, ctypes.c_void_p
    for csidl in (0x10, 0x02):  # CSIDL_DESKTOPDIRECTORY, CSIDL_PROGRAMS (Start Menu)
        buf = ctypes.create_unicode_buffer(260)
        if sh.SHGetFolderPathW(None, csidl, None, 0, buf) != 0:
            continue
        link = P()
        ole.CoCreateInstance(_guid("00021401-0000-0000-C000-000000000046"), None, 1,  # CLSID_ShellLink
                             _guid("000214F9-0000-0000-C000-000000000046"), ctypes.byref(link))  # IShellLinkW
        try:
            _com_call(link, 20, target, argtypes=(W,))                # SetPath
            _com_call(link, 11, args, argtypes=(W,))                  # SetArguments
            _com_call(link, 9, str(cc.APP_DIR), argtypes=(W,))        # SetWorkingDirectory
            _com_call(link, 17, ico, 0, argtypes=(W, ctypes.c_int))   # SetIconLocation
            _com_call(link, 7, "ClipBridge - clipboard Windows <-> iPhone", argtypes=(W,))  # SetDescription
            pf = P()
            _com_call(link, 0, _guid("0000010B-0000-0000-C000-000000000046"), ctypes.byref(pf),  # IPersistFile
                      argtypes=(P, P))
            try:
                _com_call(pf, 6, os.path.join(buf.value, "ClipBridge.lnk"), 1, argtypes=(W, ctypes.c_int))  # Save
            finally:
                _com_call(pf, 2)  # Release
        finally:
            _com_call(link, 2)


# ================================================================ ảnh thu nhỏ
def thumbnail_png(data, box=96):
    """Ảnh bất kỳ (PNG/JPEG/…) -> PNG nhỏ cho Tk. Dùng GDI+ của Windows, không cần Pillow."""
    if not WIN:
        return data if data.startswith(cb.PNG_SIG) else None
    w, h, bgra = cb.Gdip().decode(data)
    step = max(1, -(-max(w, h) // box))
    px = memoryview(bgra).cast("I")
    rows = [px[y * w:(y + 1) * w:step].tobytes() for y in range(0, h, step)]
    tw = len(rows[0]) // 4
    return cb.encode_png(tw, len(rows), b"".join(rows))


# ================================================================ QR
def qr_matrix(text):
    try:
        import qrcode
    except ImportError:
        # Cài qrcode tự động (một lần) bằng python.exe đi kèm
        if FROZEN:
            return None
        py = Path(sys.executable).with_name("python.exe")
        try:
            subprocess.run([str(py if py.exists() else sys.executable), "-m", "pip", "install", "-q",
                            "--disable-pip-version-check", "qrcode"],
                           creationflags=NO_WINDOW, capture_output=True, timeout=120)
            import qrcode
        except Exception:
            return None
    q = qrcode.QRCode(border=2)
    q.add_data(text)
    q.make(fit=True)
    return q.get_matrix()


# ================================================================ giao diện
SRC_NAMES = {"me": "PC này", "pc": "PC khác", "ios": "iPhone", "?": "Thiết bị khác"}


def source_of(sender, me):
    if sender == me:
        return "me"
    if sender.startswith("pc-"):
        return "pc"
    if sender.startswith("ios-"):
        return "ios"
    return "?"


class App:
    def __init__(self, root, hidden):
        self.root = root
        self.cfg = cc.load_cfg()
        self.sync = None
        self.items = []
        self.thumbs = {}  # id -> PhotoImage (giữ tham chiếu kẻo Tk xoá ảnh)
        self.hist_busy = self.hist_again = False
        self.hist_timer = None
        self.pair_win = None

        root.title("ClipBridge")
        root.geometry("480x660")
        root.minsize(400, 480)
        try:
            self.icon = tk.PhotoImage(file=str(ICON_PNG))
            root.iconphoto(True, self.icon)
        except Exception:
            pass
        root.protocol("WM_DELETE_WINDOW", self.hide)
        self.build()

        first_run = not self.cfg.get("code")
        self.cfg.setdefault("server", cc.DEFAULT_SERVER)
        if first_run:
            self.cfg["code"] = cc.new_code()
        self.cfg.setdefault("sender", "pc-" + secrets.token_hex(6))
        cc.save_cfg(self.cfg)
        cc.PID_FILE.write_text(str(os.getpid()))

        self.start_sync()
        self.root.after(150, self.poll_events)
        self.root.after(1000, self.tick_status)
        if hidden:
            root.withdraw()
        elif first_run:
            root.after(300, self.show_pairing)

    # ---------- dựng giao diện ----------
    def build(self):
        st = ttk.Style()
        if "vista" in st.theme_names():
            st.theme_use("vista")
        st.configure("Head.TLabel", font=("Segoe UI", 15, "bold"))
        st.configure("Src.TLabel", font=("Segoe UI", 9, "bold"))
        st.configure("Mut.TLabel", foreground="#777")
        st.configure("Sec.TLabel", font=("Segoe UI", 10, "bold"))

        top = ttk.Frame(self.root, padding=(14, 12, 14, 4))
        top.pack(fill="x")
        ttk.Label(top, text="ClipBridge", style="Head.TLabel").pack(side="left")
        self.status = tk.Label(top, text="● đang kết nối…", fg="#999", font=("Segoe UI", 9))
        self.status.pack(side="right")

        bar = ttk.Frame(self.root, padding=(14, 0, 14, 6))
        bar.pack(fill="x")
        self.paused = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Tạm dừng tự gửi sang iPhone", variable=self.paused,
                        command=self.toggle_pause).pack(side="left")

        # Lịch sử (thu gọn được)
        self.hist_open = tk.BooleanVar(value=self.cfg.get("ui_history_open", True))
        hh = ttk.Frame(self.root, padding=(14, 6, 14, 2))
        hh.pack(fill="x")
        self.hist_btn = ttk.Button(hh, width=3, command=self.toggle_history)
        self.hist_btn.pack(side="left")
        ttk.Label(hh, text="Lịch sử (4 giờ qua)", style="Sec.TLabel").pack(side="left", padx=(4, 0))
        self.hist_count = ttk.Label(hh, text="", style="Mut.TLabel")
        self.hist_count.pack(side="left", padx=6)
        ttk.Button(hh, text="Làm mới", command=lambda: self.refresh_history(0)).pack(side="right")

        self.hist_frame = ttk.Frame(self.root, padding=(14, 0, 14, 0))
        self.canvas = tk.Canvas(self.hist_frame, highlightthickness=0, bg="white")
        sb = ttk.Scrollbar(self.hist_frame, orient="vertical", command=self.canvas.yview)
        self.list = tk.Frame(self.canvas, bg="white")
        self.list.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.list_win = self.canvas.create_window((0, 0), window=self.list, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.list_win, width=e.width))
        self.canvas.configure(yscrollcommand=sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self.on_wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-2, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(2, "units"))

        # Hoạt động
        self.log_frame = ttk.Frame(self.root, padding=(14, 8, 14, 0))
        ttk.Label(self.log_frame, text="Hoạt động", style="Sec.TLabel").pack(anchor="w")
        self.log = tk.Text(self.log_frame, height=6, wrap="word", font=("Consolas", 9), relief="flat",
                           bg="#f4f4f6", padx=6, pady=4, state="disabled")
        self.log.pack(fill="both", expand=True, pady=(2, 0))

        bottom = ttk.Frame(self.root, padding=(14, 8, 14, 12))
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="Ghép đôi iPhone (QR)", command=self.show_pairing).pack(side="left")
        ttk.Button(bottom, text="Thoát hẳn", command=self.quit).pack(side="right")
        if WIN:
            ttk.Button(bottom, text="Tạo lối tắt", command=self.make_shortcuts).pack(side="right", padx=6)
        opts = ttk.Frame(self.root, padding=(14, 4, 14, 0))
        opts.pack(side="bottom", fill="x")
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        if WIN:
            ttk.Checkbutton(opts, text="Tự chạy ngầm khi đăng nhập Windows", variable=self.autostart,
                            command=self.toggle_autostart).pack(side="left")
        self.log_frame.pack(side="bottom", fill="x")
        self.apply_history_open()

    def on_wheel(self, e):
        if self.hist_open.get():
            self.canvas.yview_scroll(int(-e.delta / 120) * 2, "units")

    def apply_history_open(self):
        on = self.hist_open.get()
        self.hist_btn.configure(text="▼" if on else "▶")
        if on:
            self.hist_frame.pack(fill="both", expand=True, after=self.hist_btn.master)
            self.log_frame.pack_configure(fill="x", expand=False)
        else:
            self.hist_frame.pack_forget()
            self.log_frame.pack_configure(fill="both", expand=True)

    def toggle_history(self):
        self.hist_open.set(not self.hist_open.get())
        self.cfg["ui_history_open"] = self.hist_open.get()
        cc.save_cfg(self.cfg)
        self.apply_history_open()
        if self.hist_open.get():
            self.refresh_history(0)

    # ---------- đồng bộ ----------
    def start_sync(self):
        if self.sync:
            self.sync.stop.set()
        self.sync = None
        self.gen = getattr(self, "gen", 0) + 1
        c, gen = dict(self.cfg), self.gen

        def run():
            sync = cc.start_sync_with_retry(c, background=True)
            sync.on_change = lambda: events.put(("changed", None))
            events.put(("synced", (gen, sync)))

        threading.Thread(target=run, daemon=True).start()

    def tick_status(self):
        s = self.sync
        if s is None:
            self.status.configure(text="● đang kết nối…", fg="#999")
        elif self.paused.get():
            self.status.configure(text="● đã tạm dừng tự gửi", fg="#c80")
        elif s.realtime:
            self.status.configure(text="● đã kết nối", fg="#1a9b4b")
        else:
            self.status.configure(text="● đã kết nối (chậm hơn)", fg="#c80")
        self.root.after(1000, self.tick_status)

    def toggle_pause(self):
        if self.sync:
            self.sync.paused = self.paused.get()
        app_log("Đã TẠM DỪNG tự gửi." if self.paused.get() else "Tiếp tục tự gửi.")

    def poll_events(self):
        try:
            while True:
                kind, val = events.get_nowait()
                if kind == "log":
                    self.add_log(val)
                elif kind == "synced":
                    gen, sync = val
                    if gen != self.gen:  # mã đã đổi trong lúc kết nối -> bỏ bản cũ
                        sync.stop.set()
                        continue
                    sync.paused = self.paused.get()
                    self.sync = sync
                    app_log("Đang chạy. Copy trên PC sẽ tự gửi sang iPhone.")
                    self.refresh_history(0)
                elif kind == "changed":
                    self.refresh_history()
                elif kind == "history":
                    self.render_history(*val)
                elif kind == "show":
                    self.show()
                elif kind == "call":
                    val()
        except queue.Empty:
            pass
        self.root.after(150, self.poll_events)

    def add_log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        n = int(self.log.index("end-1c").split(".")[0])
        if n > 200:
            self.log.delete("1.0", f"{n - 200}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- lịch sử ----------
    def refresh_history(self, delay=500):
        if self.hist_timer:
            self.root.after_cancel(self.hist_timer)
        self.hist_timer = self.root.after(delay, self._load_history)

    def _load_history(self):
        self.hist_timer = None
        if not self.sync or not self.hist_open.get() or not self.root.winfo_viewable():
            return
        if self.hist_busy:
            self.hist_again = True
            return
        self.hist_busy = True
        sync = self.sync

        def work():
            try:
                items = sync.history()
                thumbs = {}
                for it in items:
                    if it["kind"] == "image" and "payload" in it and it["id"] not in self.thumbs:
                        try:
                            thumbs[it["id"]] = thumbnail_png(it["payload"])
                        except Exception:
                            thumbs[it["id"]] = None
                events.put(("history", (items, thumbs, None)))
            except Exception as e:
                events.put(("history", ([], {}, str(e))))

        threading.Thread(target=work, daemon=True).start()

    def render_history(self, items, thumbs, error):
        self.hist_busy = False
        if self.hist_again:
            self.hist_again = False
            self.refresh_history(0)
        if error:
            app_log(f"Lỗi tải lịch sử: {error}")
            return
        for i, png in thumbs.items():
            if png:
                try:
                    self.thumbs[i] = tk.PhotoImage(data=base64.b64encode(png).decode())
                except tk.TclError:
                    pass
        keep = {it["id"] for it in items}
        self.thumbs = {k: v for k, v in self.thumbs.items() if k in keep}
        self.items = items
        for w in self.list.winfo_children():
            w.destroy()
        self.hist_count.configure(text=f"· {len(items)} mục" if items else "")
        if not items:
            tk.Label(self.list, text="Chưa có gì trong 4 giờ qua.", bg="white", fg="#888",
                     pady=24).pack(fill="x")
            return
        me = self.cfg["sender"]
        for idx, it in enumerate(items):
            row = tk.Frame(self.list, bg="white", padx=4, pady=8)
            row.pack(fill="x")
            if idx:
                tk.Frame(self.list, bg="#e5e5ea", height=1).pack(fill="x", before=row)
            head = tk.Frame(row, bg="white")
            head.pack(fill="x")
            try:
                at = datetime.fromisoformat(it["at"].replace("Z", "+00:00")).astimezone().strftime("%H:%M")
            except Exception:
                at = ""
            src = SRC_NAMES[source_of(it["sender"], me)]
            tk.Label(head, text=src, bg="white", font=("Segoe UI", 9, "bold")).pack(side="left")
            meta = f"  ·  {'ảnh' if it['kind'] == 'image' else 'chữ'}  ·  {at}" + ("  ·  mới nhất" if idx == 0 else "")
            tk.Label(head, text=meta, bg="white", fg="#888", font=("Segoe UI", 9)).pack(side="left")
            if "error" in it:
                tk.Label(row, text=f"Không mở được: {it['error']}", bg="white", fg="#c33", anchor="w",
                         justify="left").pack(fill="x", pady=(4, 0))
                continue
            ttk.Button(head, text="Copy", width=8, command=lambda it=it: self.copy(it)).pack(side="right")
            if it["kind"] == "text":
                lines = it["payload"].splitlines() or [""]
                shown = [ln[:90] + ("…" if len(ln) > 90 else "") for ln in lines[:3]]
                if len(lines) > 3:
                    shown[-1] += "  …"
                tk.Label(row, text="\n".join(shown), bg="white", anchor="w", justify="left",
                         font=("Segoe UI", 10), wraplength=380).pack(fill="x", pady=(4, 0))
            else:
                ph = self.thumbs.get(it["id"])
                txt = f"[ảnh {len(it['payload']) // 1024} KB]"
                if ph:
                    tk.Label(row, image=ph, bg="white", anchor="w").pack(anchor="w", pady=(4, 0))
                else:
                    tk.Label(row, text=txt, bg="white", fg="#888", anchor="w").pack(fill="x", pady=(4, 0))
        self.canvas.yview_moveto(0)

    def copy(self, it):
        try:
            self.sync.copy_local(it["kind"], it["payload"])
            app_log("Đã copy lại từ lịch sử - Ctrl+V để dán.")
        except Exception as e:
            messagebox.showerror("ClipBridge", f"Không copy được: {e}")

    # ---------- ghép đôi ----------
    def show_pairing(self):
        if self.pair_win and self.pair_win.winfo_exists():
            self.pair_win.lift()
            return
        w = self.pair_win = tk.Toplevel(self.root)
        w.title("Ghép đôi iPhone")
        w.resizable(False, False)
        w.configure(padx=18, pady=14)
        url = f"{self.cfg['server']}/#p={self.cfg['code']}"
        ttk.Label(w, text="Mở Camera iPhone và quét mã QR này:", style="Sec.TLabel").pack()
        cv = tk.Canvas(w, width=300, height=300, bg="white", highlightthickness=0)
        cv.pack(pady=10)
        cv.create_text(150, 150, text="Đang tạo mã QR…", fill="#888")

        def draw(m):
            if not cv.winfo_exists():
                return
            cv.delete("all")
            if not m:
                cv.create_text(150, 150, width=260, fill="#888",
                               text="Chưa có thư viện qrcode.\nHãy nhập mã bên dưới trên iPhone.")
                return
            n = len(m)
            s = 300 // n
            off = (300 - s * n) // 2
            for y, r in enumerate(m):
                for x, v in enumerate(r):
                    if v:
                        cv.create_rectangle(off + x * s, off + y * s, off + (x + 1) * s, off + (y + 1) * s,
                                            fill="black", width=0)

        def work():
            m = qr_matrix(url)
            events.put(("call", lambda: draw(m)))

        threading.Thread(target=work, daemon=True).start()
        ttk.Label(w, text=f"Hoặc mở {self.cfg['server']} trên iPhone và nhập mã:").pack()
        code = tk.Entry(w, font=("Consolas", 16, "bold"), justify="center", relief="flat", width=26,
                        readonlybackground="#f4f4f6")
        code.insert(0, cc.pretty_code(self.cfg["code"]))
        code.configure(state="readonly")
        code.pack(pady=8, ipady=4)
        ttk.Label(w, text="Giữ bí mật mã này: ai có mã đều xem được clipboard của bạn.",
                  foreground="#c33").pack()
        bb = ttk.Frame(w)
        bb.pack(fill="x", pady=(12, 0))
        ttk.Button(bb, text="Tạo mã mới", command=self.new_code).pack(side="left")
        ttk.Button(bb, text="Đóng", command=w.destroy).pack(side="right")

    def new_code(self):
        if not messagebox.askyesno("ClipBridge", "Tạo mã mới sẽ ngắt kết nối mọi iPhone đang ghép đôi.\n"
                                   "Bạn sẽ phải quét lại mã QR mới. Tiếp tục?", parent=self.pair_win):
            return
        self.cfg["code"] = cc.new_code()
        cc.save_cfg(self.cfg)
        self.pair_win.destroy()
        app_log("Đã tạo mã ghép đôi mới.")
        self.start_sync()
        self.show_pairing()

    # ---------- tuỳ chọn ----------
    def toggle_autostart(self):
        try:
            set_autostart(self.autostart.get())
            app_log("Đã bật tự chạy ngầm khi đăng nhập Windows." if self.autostart.get()
                    else "Đã tắt tự khởi động.")
        except Exception as e:
            self.autostart.set(not self.autostart.get())
            messagebox.showerror("ClipBridge", f"Không đổi được tự khởi động: {e}")

    def make_shortcuts(self):
        try:
            create_shortcuts()
            messagebox.showinfo("ClipBridge", "Đã tạo lối tắt ClipBridge trên Desktop và Start Menu.")
        except Exception as e:
            messagebox.showerror("ClipBridge", f"Không tạo được lối tắt: {e}")

    # ---------- cửa sổ ----------
    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()
        self.refresh_history(0)

    def hide(self):
        self.root.withdraw()
        if not self.cfg.get("ui_hide_tip"):
            self.cfg["ui_hide_tip"] = True
            cc.save_cfg(self.cfg)
            messagebox.showinfo("ClipBridge", "ClipBridge vẫn chạy ngầm và tự đồng bộ clipboard.\n\n"
                                "Mở lại app (lối tắt ClipBridge) để hiện cửa sổ.\n"
                                "Muốn tắt hẳn: bấm \"Thoát hẳn\".")

    def quit(self):
        if self.sync:
            self.sync.stop.set()
        cc.PID_FILE.unlink(missing_ok=True)
        self.root.destroy()


def main():
    hidden = "--hidden" in sys.argv
    if signal_existing_app():
        return  # app đã mở -> nó tự hiện cửa sổ
    if cc.is_running():
        # bản dòng lệnh đang chạy ngầm -> chuyển sang app (tránh gửi trùng)
        if hidden:
            return
        r = tk.Tk()
        r.withdraw()
        ok = messagebox.askyesno("ClipBridge", "ClipBridge (bản dòng lệnh) đang chạy ngầm.\n"
                                 "Dừng nó và dùng app này thay thế?")
        r.destroy()
        if not ok or not cc.stop_background():
            return
    cc.acquire_single_instance()
    listen_show_requests()
    root = tk.Tk()
    App(root, hidden)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback

        err = traceback.format_exc()
        try:
            with open(cc.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(err)
        except OSError:
            pass
        cc.message_box("ClipBridge gặp lỗi:\n" + err[-800:], error=True)
        sys.exit(1)
