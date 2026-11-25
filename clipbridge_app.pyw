#!/usr/bin/env python3
"""
ClipBridge - app Windows có giao diện (không cần cửa sổ terminal).

- Nhấp đúp file này (hoặc lối tắt "ClipBridge" trên Desktop) để mở.
- Đóng cửa sổ (X) -> ClipBridge vẫn chạy ngầm; mở lại app để hiện cửa sổ.
- Xem lịch sử copy của cả PC và iPhone, bấm "Copy" (hoặc nhấp đúp) để copy lại.
- Ghép đôi iPhone bằng mã QR; tự khởi động, lối tắt, gỡ cài đặt trong nút Cài đặt.
- Giao diện customtkinter (tự cài lần đầu), theo chế độ sáng/tối của Windows.

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
from tkinter import messagebox

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


def shortcut_files():
    """Đường dẫn các lối tắt ClipBridge.lnk (Desktop, Start Menu)."""
    out = []
    if not WIN:
        return out
    sh = ctypes.WinDLL("shell32")
    for csidl in (0x10, 0x02):
        buf = ctypes.create_unicode_buffer(260)
        if sh.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
            out.append(Path(buf.value) / "ClipBridge.lnk")
    return out


def uninstall_files():
    """Tắt tự khởi động, xoá lối tắt, mã ghép đôi, nhật ký. Trả về danh sách lỗi (nếu có)."""
    errors = []
    if WIN:
        try:
            set_autostart(False)
        except Exception as e:
            errors.append(f"Tự khởi động: {e}")
    for f in shortcut_files() + [cc.CONFIG, cc.LOG_FILE, cc.APP_DIR / "icon.ico"]:
        try:
            f.unlink(missing_ok=True)
        except OSError as e:
            errors.append(f"{f}: {e}")
    return errors


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


# ================================================================ thư viện phụ
def ensure_package(name, pip_name=None):
    """Import thư viện; thiếu thì tự cài (một lần) bằng python.exe đi kèm. Lỗi -> None."""
    import importlib

    try:
        return importlib.import_module(name)
    except ImportError:
        pass
    if FROZEN:
        return None
    py = Path(sys.executable).with_name("python.exe")
    try:
        subprocess.run([str(py if py.exists() else sys.executable), "-m", "pip", "install", "-q",
                        "--disable-pip-version-check", pip_name or name],
                       creationflags=NO_WINDOW, capture_output=True, timeout=300)
        importlib.invalidate_caches()
        return importlib.import_module(name)
    except Exception:
        return None


def qr_matrix(text):
    qrcode = ensure_package("qrcode")
    if not qrcode:
        return None
    q = qrcode.QRCode(border=2)
    q.add_data(text)
    q.make(fit=True)
    return q.get_matrix()


# ================================================================ giao diện (customtkinter)
ctk = ensure_package("customtkinter")
if ctk is None:
    cc.message_box("ClipBridge cần thư viện customtkinter nhưng không tự cài được (máy chưa có mạng?).\n\n"
                   "Mở Terminal và chạy:\n    py -m pip install customtkinter qrcode\nrồi mở lại ClipBridge.",
                   error=True)
    sys.exit(1)

# Màu (sáng, tối) - chủ đạo xanh ngọc. PC = xanh dương, iPhone = cam.
BG = ("#F1F6F5", "#0B1211")
CARD = ("#FFFFFF", "#151C1B")
FIELD = ("#F6F8F8", "#1C2524")
LINE = ("#E3E8E7", "#2A3433")
FG = ("#111827", "#ECF1F0")
MUT = ("#5B6472", "#9AA5A3")
CHIP = ("#EEF1F1", "#222B2A")
CHIP_HOVER = ("#E2E7E6", "#2C3635")
ACC = ("#0F766E", "#2DD4BF")
ACC_HOVER = ("#115E59", "#5EEAD4")
ACC_FG = ("#FFFFFF", "#042F2B")
ACC_SOFT = ("#DDF3EF", "#123A35")
PC_C, PC_SOFT = ("#1E40C4", "#93AEFF"), ("#E8EEFF", "#1B2647")
IOS_C, IOS_SOFT = ("#9A3412", "#FDBA8C"), ("#FFF1E8", "#3A2214")
OK, OK_SOFT = ("#16A34A", "#22C55E"), ("#E7F6EC", "#12301E")
WARN, WARN_SOFT, WARN_INK = ("#D97706", "#FBBF24"), ("#FFF7ED", "#3A2A14"), ("#7C2D12", "#FDBA74")
BAD = ("#B91C1C", "#FCA5A5")
SWITCH_OFF = ("#CBD5D3", "#3A4645")
TOAST_BG, TOAST_FG = ("#111827", "#ECF1F0"), ("#FFFFFF", "#0B1211")

# Biểu tượng: font Segoe Fluent Icons (Win 11) / Segoe MDL2 Assets (Win 10); máy khác dùng chữ.
GLYPHS = {"pc": ("\uE7F8", "PC"), "ios": ("\uE8EA", "iP"), "copy": ("\uE8C8", "Copy"), "check": ("\uE73E", "✓"),
          "gear": ("\uE713", "⚙"), "qr": ("\uED14", "QR"), "search": ("\uE721", ""), "image": ("\uEB9F", "Ảnh")}


def pick_fonts(root):
    import tkinter.font as tkfont

    fam = set(tkfont.families(root))
    ui = next((f for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "DejaVu Sans") if f in fam), None)
    icons = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in fam), None)
    mono = next((f for f in ("Cascadia Mono", "Consolas", "DejaVu Sans Mono") if f in fam), "Courier")
    return ui, icons, mono


DEVICES = {"me": ("PC này", "pc"), "pc": ("PC khác", "pc"), "ios": ("iPhone", "ios"), "?": ("Thiết bị khác", "pc")}


def source_of(sender, me):
    if sender == me:
        return "me"
    if sender.startswith("pc-"):
        return "pc"
    if sender.startswith("ios-"):
        return "ios"
    return "?"


def hhmm(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except Exception:
        return ""


class App:
    def __init__(self, root, hidden):
        self.root = root
        self.cfg = cc.load_cfg()
        self.sync = None
        self.gen = 0
        self.items = []
        self.thumbs = {}  # id -> PhotoImage (giữ tham chiếu kẻo Tk xoá ảnh)
        self.rows = []
        self.filter = "all"
        self.hist_busy = self.hist_again = False
        self.hist_timer = self.toast_timer = None
        self.pair_win = self.log_win = None
        self.logs = []
        self.last_event = ""

        ctk.set_appearance_mode("system")
        self.ui_font, self.icon_font, self.mono_font = pick_fonts(root)
        root.title("ClipBridge")
        root.geometry("560x720")
        root.minsize(440, 520)
        root.configure(fg_color=BG)
        try:
            self.icon = tk.PhotoImage(file=str(ICON_PNG))
            root.iconphoto(True, self.icon)
        except Exception:
            pass
        root.protocol("WM_DELETE_WINDOW", self.hide)

        first_run = not self.cfg.get("code")
        self.cfg.setdefault("server", cc.DEFAULT_SERVER)
        if first_run:
            self.cfg["code"] = cc.new_code()
        self.cfg.setdefault("sender", "pc-" + secrets.token_hex(6))
        cc.save_cfg(self.cfg)
        cc.PID_FILE.write_text(str(os.getpid()))

        self.build()
        self.start_sync()
        root.after(150, self.poll_events)
        root.after(1000, self.tick_status)
        root.bind_all("<Control-f>", lambda e: self.search.focus_set())
        if hidden:
            root.withdraw()
        elif first_run:
            root.after(400, self.show_pairing)

    # ---------- tiện ích ----------
    def font(self, size=13, weight="normal"):
        return ctk.CTkFont(family=self.ui_font, size=size, weight=weight) if self.ui_font else \
            ctk.CTkFont(size=size, weight=weight)

    def glyph(self, name):
        g, fallback = GLYPHS[name]
        return (g, ctk.CTkFont(family=self.icon_font, size=14)) if self.icon_font else (fallback, self.font(12, "bold"))

    # ---------- dựng giao diện ----------
    def build(self):
        r = self.root
        top = ctk.CTkFrame(r, fg_color=CARD, corner_radius=0)
        top.pack(fill="x")
        row1 = ctk.CTkFrame(top, fg_color="transparent")
        row1.pack(fill="x", padx=18, pady=(16, 12))
        left = ctk.CTkFrame(row1, fg_color="transparent")
        left.pack(side="left")
        line = ctk.CTkFrame(left, fg_color="transparent")
        line.pack(anchor="w")
        self.dot = ctk.CTkLabel(line, text="●", font=self.font(12), text_color=MUT, width=14)
        self.dot.pack(side="left")
        self.status = ctk.CTkLabel(line, text="Đang kết nối…", font=self.font(15, "bold"), text_color=FG)
        self.status.pack(side="left", padx=(4, 0))
        self.sub = ctk.CTkLabel(left, text="Copy trên PC sẽ tự gửi sang iPhone", font=self.font(12), text_color=MUT)
        self.sub.pack(anchor="w")
        self.auto = ctk.BooleanVar(value=True)
        self.switch = ctk.CTkSwitch(row1, text="Tự gửi", variable=self.auto, command=self.toggle_pause,
                                    font=self.font(13, "bold"), progress_color=ACC, fg_color=SWITCH_OFF,
                                    button_color=("#FFFFFF", "#ECF1F0"), button_hover_color=("#F3F4F6", "#FFFFFF"),
                                    border_width=2, border_color=ACC, text_color=FG, switch_width=44, switch_height=24)
        self.switch.pack(side="right")

        row2 = ctk.CTkFrame(top, fg_color="transparent")
        row2.pack(fill="x", padx=18, pady=(0, 14))
        self.search = ctk.CTkEntry(row2, placeholder_text="Tìm trong lịch sử  (Ctrl+F)", height=36, corner_radius=8,
                                   border_width=0, fg_color=CHIP, text_color=FG, placeholder_text_color=MUT,
                                   font=self.font(13))
        self.search.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.search.bind("<KeyRelease>", lambda e: self.search_soon())
        self.search.bind("<Escape>", lambda e: (self.search.delete(0, "end"), self.render_history()))
        self.seg = ctk.CTkSegmentedButton(row2, values=["Tất cả", "PC", "iPhone"], command=self.set_filter,
                                          height=36, corner_radius=8, font=self.font(13, "bold"), fg_color=CHIP,
                                          selected_color=CARD, selected_hover_color=CARD, unselected_color=CHIP,
                                          unselected_hover_color=CHIP_HOVER, text_color=FG)
        self.seg.set("Tất cả")
        self.seg.pack(side="right")
        ctk.CTkFrame(r, fg_color=LINE, height=1, corner_radius=0).pack(fill="x")

        bottom = ctk.CTkFrame(r, fg_color=CARD, corner_radius=0, height=56)
        bottom.pack(side="bottom", fill="x")
        ctk.CTkFrame(r, fg_color=LINE, height=1, corner_radius=0).pack(side="bottom", fill="x")
        self.last = ctk.CTkLabel(bottom, text="Đang khởi động…", font=self.font(12), text_color=MUT, cursor="hand2",
                                 anchor="w")
        self.last.pack(side="left", padx=(18, 8), pady=12, fill="x", expand=True)
        self.last.bind("<Button-1>", lambda e: self.show_log())
        g, f = self.glyph("gear")
        self.gear_anchor = ctk.CTkButton(bottom, text=g, font=f, width=36, height=34, corner_radius=8,
                                         fg_color="transparent", hover_color=CHIP, border_width=1, border_color=LINE,
                                         text_color=FG, command=self.open_menu)
        self.gear_anchor.pack(side="right", padx=(6, 12))
        pair = ctk.CTkFrame(bottom, fg_color="transparent")
        pair.pack(side="right")
        ctk.CTkButton(pair, text="Ghép đôi", font=self.font(13, "bold"), height=34, width=96, corner_radius=8,
                      fg_color="transparent", hover_color=CHIP, border_width=1, border_color=LINE, text_color=FG,
                      command=self.show_pairing).pack()

        self.build_list(r)

        self.toast = ctk.CTkLabel(r, text="", fg_color=TOAST_BG, text_color=TOAST_FG, corner_radius=18, height=36,
                                  font=self.font(13, "bold"))
        self.menu_autostart = tk.BooleanVar(value=autostart_enabled())
        self.render_history()

    def search_soon(self):
        """Chờ gõ xong (180 ms) mới lọc, khỏi vẽ lại sau mỗi phím."""
        if getattr(self, "_search_job", None):
            self.root.after_cancel(self._search_job)
        self._search_job = self.root.after(180, self.render_history)

    def set_filter(self, value):
        self.filter = {"Tất cả": "all", "PC": "pc", "iPhone": "ios"}[value]
        self.render_history()

    def open_menu(self):
        m = tk.Menu(self.root, tearoff=0, font=(self.ui_font or "TkDefaultFont", 10))
        if WIN:
            m.add_checkbutton(label="Tự chạy ngầm khi đăng nhập Windows", variable=self.menu_autostart,
                              command=self.toggle_autostart)
            m.add_command(label="Tạo lối tắt (Desktop + Start Menu)", command=self.make_shortcuts)
            m.add_separator()
        m.add_command(label="Xem nhật ký", command=self.show_log)
        m.add_command(label="Mở thư mục ClipBridge", command=self.open_folder)
        m.add_separator()
        m.add_command(label="Gỡ cài đặt…", command=self.uninstall)
        m.add_command(label="Thoát hẳn", command=self.quit)
        w = self.gear_anchor
        m.update_idletasks()
        m.tk_popup(w.winfo_rootx() + w.winfo_width() - m.winfo_reqwidth(), w.winfo_rooty() - m.winfo_reqheight() - 4)

    # ---------- thông báo ----------
    def show_toast(self, text, ms=1800):
        self.toast.configure(text=f"  {text}  ")
        self.toast.place(relx=0.5, rely=1.0, y=-72, anchor="s")
        self.toast.lift()
        if self.toast_timer:
            self.root.after_cancel(self.toast_timer)
        self.toast_timer = self.root.after(ms, self.toast.place_forget)

    def add_log(self, line):
        self.logs = (self.logs + [line])[-300:]
        msg = line[11:]
        if "➜" in msg or "copy" in msg.lower() or "Lỗi" in msg:
            ev = msg.split(" - ")[0]
            self.last_event = f"{line[1:6]} · {ev[:48] + '…' if len(ev) > 48 else ev}"
            self.last.configure(text=f"{self.last_event}   ·   Xem nhật ký")
        if self.log_win and self.log_win.winfo_exists():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

    def show_log(self):
        if self.log_win and self.log_win.winfo_exists():
            self.log_win.lift()
            return
        w = self.log_win = ctk.CTkToplevel(self.root, fg_color=CARD)
        w.title("Nhật ký ClipBridge")
        w.geometry("560x360")
        self.log_box = ctk.CTkTextbox(w, font=ctk.CTkFont(family=self.mono_font, size=12), fg_color=FIELD,
                                      text_color=FG, corner_radius=8)
        self.log_box.pack(fill="both", expand=True, padx=12, pady=12)
        self.log_box.insert("end", "\n".join(self.logs) + ("\n" if self.logs else ""))
        self.log_box.see("end")
        self.log_box.configure(state="disabled")
        w.after(100, w.lift)

    # ---------- đồng bộ ----------
    def start_sync(self):
        if self.sync:
            self.sync.stop.set()
        self.sync = None
        self.gen += 1
        c, gen = dict(self.cfg), self.gen

        def run():
            sync = cc.start_sync_with_retry(c, background=True)
            sync.on_change = lambda: events.put(("changed", None))
            events.put(("synced", (gen, sync)))

        threading.Thread(target=run, daemon=True).start()

    def tick_status(self):
        s = self.sync
        if s is None:
            dot, title = MUT, "Đang kết nối…"
        elif s.realtime:
            dot, title = OK, "Đã kết nối"
        else:
            dot, title = WARN, "Đã kết nối (chậm hơn)"
        self.dot.configure(text_color=dot)
        self.status.configure(text=title)
        self.sub.configure(text="Copy trên PC sẽ tự gửi sang iPhone" if self.auto.get()
                           else "Đã tắt tự gửi – copy trên PC sẽ không gửi đi")
        if (1 if ctk.get_appearance_mode() == "Dark" else 0) != self.mode:
            self.render_history()  # Windows vừa đổi sáng/tối
        self.root.after(1000, self.tick_status)

    def toggle_pause(self):
        self.switch.configure(border_color=ACC if self.auto.get() else SWITCH_OFF)
        if self.sync:
            self.sync.paused = not self.auto.get()
        app_log("Tiếp tục tự gửi." if self.auto.get() else "Đã TẮT tự gửi.")

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
                    sync.paused = not self.auto.get()
                    self.sync = sync
                    app_log("Đang chạy. Copy trên PC sẽ tự gửi sang iPhone.")
                    self.refresh_history(0)
                elif kind == "changed":
                    self.refresh_history()
                elif kind == "history":
                    self.got_history(*val)
                elif kind == "show":
                    self.show()
                elif kind == "call":
                    val()
        except queue.Empty:
            pass
        self.root.after(150, self.poll_events)

    # ---------- lịch sử ----------
    def refresh_history(self, delay=500):
        if self.hist_timer:
            self.root.after_cancel(self.hist_timer)
        self.hist_timer = self.root.after(delay, self._load_history)

    def _load_history(self):
        self.hist_timer = None
        if not self.sync or self.root.state() == "withdrawn":
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
                            thumbs[it["id"]] = thumbnail_png(it["payload"], 80)
                        except Exception:
                            thumbs[it["id"]] = None
                events.put(("history", (items, thumbs, None)))
            except Exception as e:
                events.put(("history", ([], {}, str(e))))

        threading.Thread(target=work, daemon=True).start()

    def got_history(self, items, thumbs, error):
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
                    ph = tk.PhotoImage(data=base64.b64encode(png).decode())
                    k = max(1, -(-max(ph.width(), ph.height()) // 80))
                    self.thumbs[i] = ph.subsample(k) if k > 1 else ph
                except tk.TclError:
                    pass
        keep = {it["id"] for it in items}
        self.thumbs = {k: v for k, v in self.thumbs.items() if k in keep}
        self.items = items
        self.render_history()

    def visible_items(self):
        q = self.search.get().strip().lower()
        me = self.cfg["sender"]
        out = []
        for it in self.items:
            dev = DEVICES[source_of(it["sender"], me)][1]
            if self.filter != "all" and dev != self.filter:
                continue
            if q:
                hay = it.get("payload", "") if it["kind"] == "text" else "ảnh image"
                if not isinstance(hay, str) or q not in hay.lower():
                    continue
            out.append(it)
        return out

    # ---------- danh sách lịch sử (widget tk nhẹ + cuộn mượt theo pixel) ----------
    # Mỗi widget customtkinter là một Canvas tự vẽ lại -> 20 dòng mất ~0,4 s để dựng và cuộn bị giật.
    # Ở đây dòng dùng tk.Frame/tk.Label thường, ảnh bo góc vẽ sẵn một lần, dòng được giữ lại giữa các lần lọc.
    def c(self, pair):
        return pair[self.mode]

    def px(self, n):
        return max(1, round(n * self.scale))

    def tkfont(self, size, weight="normal", family=None):
        return (family or self.ui_font or "TkDefaultFont", -self.px(size), weight)

    def rounded(self, w, h, r, color):
        """Ảnh nền bo góc (có khử răng cưa), lưu cache theo kích thước + màu."""
        key = (w, h, r, color)
        if key not in self._img_cache:
            rr, gg, bb = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            px = bytearray(w * h * 4)
            for y in range(h):
                for x in range(w):
                    cover = 0
                    for sy in (0.125, 0.375, 0.625, 0.875):
                        for sx in (0.125, 0.375, 0.625, 0.875):
                            fx, fy = x + sx, y + sy
                            dx = max(r - fx, 0, fx - (w - r))
                            dy = max(r - fy, 0, fy - (h - r))
                            cover += dx * dx + dy * dy <= r * r
                    i = (y * w + x) * 4
                    px[i:i + 4] = bytes((bb, gg, rr, cover * 255 // 16))
            png = cb.encode_png(w, h, bytes(px))
            self._img_cache[key] = tk.PhotoImage(data=base64.b64encode(png).decode())
        return self._img_cache[key]

    def build_list(self, parent):
        self.mode = 1 if ctk.get_appearance_mode() == "Dark" else 0
        try:
            self.scale = ctk.ScalingTracker.get_widget_scaling(self.root)
        except Exception:
            self.scale = 1.0
        self._img_cache, self.row_cache, self.rows = {}, {}, []
        self.hover_row, self.no_hover_until, self._scroll_left, self._scroll_job = None, 0.0, 0.0, None
        box = ctk.CTkFrame(parent, fg_color=BG, corner_radius=0)
        box.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(box, highlightthickness=0, bd=0, yscrollincrement=1, bg=self.c(BG))
        sb = ctk.CTkScrollbar(box, command=self.canvas.yview, fg_color=BG)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 2), pady=4)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=self.c(BG))
        win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(win, width=e.width), self._region()))
        self.inner.bind("<Configure>", lambda e: self._region())
        self.root.bind_all("<MouseWheel>", lambda e: self.wheel(-e.delta / 120), add="+")
        self.root.bind_all("<Button-4>", lambda e: self.wheel(1), add="+")
        self.root.bind_all("<Button-5>", lambda e: self.wheel(-1), add="+")
        self.head_lbl = tk.Label(self.inner, anchor="w")
        self.empty_lbl = tk.Label(self.inner, justify="center")
        self.hint_lbl = tk.Label(self.inner, text="Nhấp đúp một dòng để copy · Tự xoá sau 4 giờ", anchor="w")
        self.blank_btn = tk.PhotoImage(width=self.px(72), height=self.px(30))  # trong suốt: giữ chỗ cho nút Copy
        self.style_static()
        self.prewarm_images()

    def prewarm_images(self):
        """Vẽ sẵn ảnh bo góc của chế độ hiện tại, để lần hover/cuộn đầu không bị khựng."""
        for col in (PC_SOFT, IOS_SOFT):
            self.rounded(self.px(32), self.px(32), self.px(8), self.c(col))
        self.rounded(self.px(72), self.px(30), self.px(8), self.c(ACC))

    def style_static(self):
        bg = self.c(BG)
        self.canvas.configure(bg=bg)
        self.inner.configure(bg=bg)
        self.head_lbl.configure(bg=bg, fg=self.c(MUT), font=self.tkfont(11, "bold"))
        self.empty_lbl.configure(bg=bg, fg=self.c(MUT), font=self.tkfont(13), wraplength=self.px(380))
        self.hint_lbl.configure(bg=bg, fg=self.c(MUT), font=self.tkfont(11))

    def _region(self):
        bbox = self.canvas.bbox("all") or (0, 0, 0, 0)
        self.canvas.configure(scrollregion=(0, 0, bbox[2], max(bbox[3], self.canvas.winfo_height())))

    def wheel(self, notches):
        x, y = self.root.winfo_pointerxy()
        under = self.root.winfo_containing(x, y)
        if under is None or not str(under).startswith(str(self.canvas)):
            return
        self.set_hover(None)
        self.no_hover_until = time.monotonic() + 0.25
        self._scroll_left -= notches * self.px(64)
        if not self._scroll_job:
            self._scroll_step()

    def _scroll_step(self):
        """Cuộn dần (ease-out) thay vì nhảy cả nấc -> mượt hơn."""
        left = self._scroll_left
        if abs(left) < 1:
            self._scroll_left, self._scroll_job = 0.0, None
            return
        step = int(left * 0.35) or (1 if left > 0 else -1)
        before = self.canvas.yview()
        self.canvas.yview_scroll(step, "units")
        self._scroll_left = 0.0 if self.canvas.yview() == before else left - step  # chạm đầu/cuối thì dừng
        self._scroll_job = self.root.after(10, self._scroll_step)

    def render_history(self):
        mode = 1 if ctk.get_appearance_mode() == "Dark" else 0
        if mode != self.mode:  # đổi sáng/tối -> vẽ lại toàn bộ
            self.mode = mode
            for row in self.row_cache.values():
                row["frame"].destroy()
            self.row_cache = {}
            self.style_static()
            self.prewarm_images()
        items = self.visible_items()
        me = self.cfg["sender"]
        newest = self.items[0]["id"] if self.items else None
        keep = {it["id"] for it in self.items}
        for i in [i for i in self.row_cache if i not in keep]:
            self.row_cache.pop(i)["frame"].destroy()
        for w in self.inner.pack_slaves():
            w.pack_forget()
        self.hover_row = None
        self.head_lbl.configure(text="4 GIỜ QUA" + (f" · {len(self.items)} MỤC" if self.items else ""))
        self.head_lbl.pack(fill="x", padx=self.px(14), pady=(self.px(10), self.px(4)))
        if not items:
            self.empty_lbl.configure(text="Chưa có gì trong 4 giờ qua. Copy chữ hoặc ảnh trên PC hoặc iPhone, "
                                     "nó sẽ hiện ở đây." if not self.items else "Không có mục nào khớp.")
            self.empty_lbl.pack(fill="x", pady=self.px(40))
        self.rows = []
        for it in items:
            key = (it["id"], it["id"] == newest)
            row = self.row_cache.get(it["id"])
            if row is None or row["key"] != key:
                if row:
                    row["frame"].destroy()
                row = self.row_cache[it["id"]] = self.make_row(it, me, it["id"] == newest)
                row["key"] = key
            row["frame"].pack(fill="x", padx=self.px(8), pady=1)
            self.rows.append(row["frame"])
        if items:
            self.hint_lbl.pack(fill="x", padx=self.px(14), pady=(self.px(6), self.px(12)))
        self.canvas.yview_moveto(0)

    def make_row(self, it, me, newest):
        name, dev = DEVICES[source_of(it["sender"], me)]
        bg = self.c(BG)
        ok = "error" not in it
        f = tk.Frame(self.inner, bg=bg, cursor="arrow")
        g, gf = self.glyph(dev)
        gfont = (gf.cget("family"), -self.px(14)) if self.icon_font else self.tkfont(11, "bold")
        soft, ink = (PC_SOFT, PC_C) if dev == "pc" else (IOS_SOFT, IOS_C)
        widgets = [tk.Label(f, image=self.rounded(self.px(32), self.px(32), self.px(8), self.c(soft)), text=g,
                            compound="center", font=gfont, fg=self.c(ink), bg=bg, bd=0)]
        widgets[0].pack(side="left", padx=(self.px(10), self.px(10)), pady=self.px(9))
        if ok and it["kind"] == "image" and it["id"] in self.thumbs:
            widgets.append(tk.Label(f, image=self.thumbs[it["id"]], bg=bg, bd=0))
            widgets[-1].pack(side="left", padx=(0, self.px(10)), pady=self.px(6))
        btn = None
        if ok:
            cg, cf = self.glyph("copy")
            btn = tk.Label(f, image=self.blank_btn, text=cg, compound="center", bg=bg, fg=self.c(MUT), bd=0,
                           cursor="hand2", font=(cf.cget("family"), -self.px(14)) if self.icon_font
                           else self.tkfont(12, "bold"))
            btn.pack(side="right", padx=self.px(8))
            btn.bind("<Button-1>", lambda e: self.copy(it))
            widgets.append(btn)
        body = tk.Frame(f, bg=bg)
        body.pack(side="left", fill="x", expand=True, pady=self.px(8))
        if not ok:
            text, color = f"Không mở được: {it['error']}", BAD
        elif it["kind"] == "text":
            t = " ".join(it["payload"].split())
            text, color = (t[:70] + "…") if len(t) > 70 else (t or "(trống)"), FG
        else:
            text, color = "Ảnh", FG
        meta = f"{name} · {hhmm(it['at'])}"
        if ok and it["kind"] == "image":
            meta += f" · {max(1, len(it['payload']) // 1024)} KB"
        if newest:
            meta += " · mới nhất"
        widgets += [body, tk.Label(body, text=text, font=self.tkfont(13), fg=self.c(color), bg=bg, anchor="w"),
                    tk.Label(body, text=meta, font=self.tkfont(11), fg=self.c(MUT), bg=bg, anchor="w")]
        widgets[-2].pack(fill="x")
        widgets[-1].pack(fill="x")
        row = {"frame": f, "widgets": widgets, "btn": btn, "badge": widgets[0]}
        for w in [f] + widgets:
            w.bind("<Enter>", lambda e: self.set_hover(row), add="+")
            if ok and w is not btn:
                w.bind("<Double-Button-1>", lambda e: self.copy(it), add="+")
        f.bind("<Leave>", lambda e: self._maybe_leave(row), add="+")
        return row

    def set_hover(self, row):
        if row is not None and time.monotonic() < self.no_hover_until:
            return  # đang cuộn: không đổi màu dòng (tránh vẽ lại liên tục)
        if row is self.hover_row:
            return
        old, self.hover_row = self.hover_row, row
        for r, on in ((old, False), (row, True)):
            if not r or not r["frame"].winfo_exists():
                continue
            bg = self.c(CARD if on else BG)
            r["frame"].configure(bg=bg)
            for w in r["widgets"]:
                w.configure(bg=bg)
            b = r["btn"]
            if b is not None:
                if on:
                    b.configure(image=self.rounded(self.px(72), self.px(30), self.px(8), self.c(ACC)), text="Copy",
                                fg=self.c(ACC_FG), font=self.tkfont(12, "bold"))
                else:
                    cg, cf = self.glyph("copy")
                    b.configure(image=self.blank_btn, text=cg, fg=self.c(MUT),
                                font=(cf.cget("family"), -self.px(14)) if self.icon_font else self.tkfont(12, "bold"))

    def _maybe_leave(self, row):
        x, y = self.root.winfo_pointerxy()
        under = self.root.winfo_containing(x, y)
        if under is None or not str(under).startswith(str(row["frame"])):
            if self.hover_row is row:
                self.set_hover(None)

    def copy(self, it, btn=None):
        try:
            if self.sync:
                self.sync.copy_local(it["kind"], it["payload"])
            elif it["kind"] == "text":
                self.root.clipboard_clear()
                self.root.clipboard_append(it["payload"])
            self.show_toast("✓  Đã copy – Ctrl+V để dán")
            app_log("Đã copy lại từ lịch sử - Ctrl+V để dán.")
        except Exception as e:
            messagebox.showerror("ClipBridge", f"Không copy được: {e}")

    # ---------- ghép đôi ----------
    def show_pairing(self):
        if self.pair_win and self.pair_win.winfo_exists():
            self.pair_win.lift()
            return
        w = self.pair_win = ctk.CTkToplevel(self.root, fg_color=CARD)
        w.title("Ghép đôi iPhone")
        w.resizable(False, False)
        url = f"{self.cfg['server']}/#p={self.cfg['code']}"
        body = ctk.CTkFrame(w, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28, pady=(20, 16))
        ctk.CTkLabel(body, text="Quét bằng Camera iPhone", font=self.font(19, "bold"), text_color=FG).pack()
        ctk.CTkLabel(body, text="Safari sẽ mở ClipBridge và tự kết nối.", font=self.font(13), text_color=MUT).pack()
        qr_box = ctk.CTkFrame(body, fg_color=("#FFFFFF", "#FFFFFF"), corner_radius=16, border_width=1,
                              border_color=LINE)
        qr_box.pack(pady=16)
        cv = tk.Canvas(qr_box, width=200, height=200, bg="white", highlightthickness=0)
        cv.pack(padx=12, pady=12)
        cv.create_text(100, 100, text="Đang tạo mã QR…", fill="#6B7280")

        def draw(m):
            if not cv.winfo_exists():
                return
            cv.delete("all")
            if not m:
                cv.create_text(100, 100, width=180, fill="#6B7280", justify="center",
                               text="Chưa có thư viện qrcode.\nHãy copy mã bên dưới.")
                return
            n = len(m)
            s = 200 // n
            off = (200 - s * n) // 2
            for y, r in enumerate(m):
                for x, v in enumerate(r):
                    if v:
                        cv.create_rectangle(off + x * s, off + y * s, off + (x + 1) * s, off + (y + 1) * s,
                                            fill="black", width=0)

        def work():
            m = qr_matrix(url)
            events.put(("call", lambda: draw(m)))

        threading.Thread(target=work, daemon=True).start()

        ctk.CTkLabel(body, text="Hoặc copy mã rồi dán vào ClipBridge trên iPhone", font=self.font(12),
                     text_color=MUT).pack()
        box = ctk.CTkFrame(body, fg_color=FIELD, corner_radius=12)
        box.pack(fill="x", pady=(6, 4))
        code = self.cfg["code"]
        ctk.CTkLabel(box, text="•••••-•••••-•••••-" + code[-5:], font=ctk.CTkFont(family=self.mono_font, size=15,
                     weight="bold"), text_color=FG).pack(side="left", expand=True, padx=(14, 6), pady=6)
        copy_btn = ctk.CTkButton(box, text="Copy mã", width=104, height=36, corner_radius=8, fg_color=ACC,
                                 hover_color=ACC_HOVER, text_color=ACC_FG, font=self.font(13, "bold"))
        copy_btn.pack(side="right", padx=6, pady=6)
        copy_btn.configure(command=lambda: self.copy_code(copy_btn))
        ctk.CTkLabel(body, text="Mã đầy đủ không hiện trên màn hình. Clipboard tự xoá mã sau 60 giây.",
                     font=self.font(11), text_color=MUT).pack()
        warn = ctk.CTkFrame(body, fg_color=WARN_SOFT, corner_radius=12)
        warn.pack(fill="x", pady=(14, 0))
        ctk.CTkLabel(warn, text="Ai có mã này đều đọc được clipboard của bạn. Đừng chụp màn hình QR gửi người khác.",
                     font=self.font(12), text_color=WARN_INK, wraplength=340, justify="left", anchor="w"
                     ).pack(fill="x", padx=14, pady=10)

        ctk.CTkFrame(w, fg_color=LINE, height=1, corner_radius=0).pack(fill="x")
        bar = ctk.CTkFrame(w, fg_color="transparent")
        bar.pack(fill="x", padx=16, pady=12)
        ctk.CTkButton(bar, text="Tạo mã mới…", width=110, height=36, corner_radius=8, fg_color="transparent",
                      hover_color=CHIP, text_color=BAD, font=self.font(13, "bold"), command=self.new_code).pack(side="left")
        ctk.CTkButton(bar, text="Xong", width=88, height=36, corner_radius=8, fg_color=ACC, hover_color=ACC_HOVER,
                      text_color=ACC_FG, font=self.font(14, "bold"), command=w.destroy).pack(side="right")
        w.after(100, lambda: (w.lift(), w.focus_force()))

    def copy_code(self, btn):
        text = cc.pretty_code(self.cfg["code"])
        try:
            if self.sync:
                self.sync.copy_local("text", text)  # không tự gửi mã sang iPhone
                seq = self.sync.own_seq
            else:
                self.root.clipboard_clear()
                self.root.clipboard_append(text)
                seq = None
        except Exception as e:
            messagebox.showerror("ClipBridge", f"Không copy được: {e}", parent=self.pair_win)
            return
        btn.configure(text="✓ Đã copy", fg_color=OK_SOFT, hover_color=OK_SOFT, text_color=OK)
        self.root.after(1800, lambda: btn.winfo_exists() and btn.configure(
            text="Copy mã", fg_color=ACC, hover_color=ACC_HOVER, text_color=ACC_FG))

        def clear():
            # chỉ xoá nếu clipboard vẫn là mã (người dùng chưa copy gì khác)
            s = self.sync
            if s and seq is not None and s.clip.seq() == seq:
                s.copy_local("text", "")
            elif s is None:
                try:
                    if self.root.clipboard_get() == text:
                        self.root.clipboard_clear()
                except tk.TclError:
                    pass

        self.root.after(60_000, clear)

    def new_code(self):
        if not messagebox.askyesno("ClipBridge", "Tạo mã mới sẽ ngắt kết nối mọi iPhone đang ghép đôi.\n"
                                   "Bạn sẽ phải quét lại mã QR mới. Tiếp tục?", parent=self.pair_win):
            return
        self.cfg["code"] = cc.new_code()
        cc.save_cfg(self.cfg)
        self.pair_win.destroy()
        app_log("Đã tạo mã ghép đôi mới.")
        self.items = []
        self.render_history()
        self.start_sync()
        self.show_pairing()

    # ---------- cài đặt ----------
    def toggle_autostart(self):
        try:
            set_autostart(self.menu_autostart.get())
            self.show_toast("✓  Đã bật tự chạy khi đăng nhập" if self.menu_autostart.get() else "Đã tắt tự khởi động")
        except Exception as e:
            self.menu_autostart.set(not self.menu_autostart.get())
            messagebox.showerror("ClipBridge", f"Không đổi được tự khởi động: {e}")

    def make_shortcuts(self):
        try:
            create_shortcuts()
            self.show_toast("✓  Đã tạo lối tắt trên Desktop và Start Menu", 2600)
        except Exception as e:
            messagebox.showerror("ClipBridge", f"Không tạo được lối tắt: {e}")

    def open_folder(self):
        if WIN:
            os.startfile(str(cc.APP_DIR))

    def uninstall(self):
        if not messagebox.askyesno(
                "Gỡ cài đặt ClipBridge",
                "ClipBridge sẽ:\n• tắt tự khởi động\n• xoá lối tắt trên Desktop và Start Menu\n"
                "• xoá mã ghép đôi và nhật ký trên máy này\nrồi thoát.\n\nTiếp tục?"):
            return
        errors = uninstall_files()
        msg = ("Đã gỡ xong. Giờ bạn chỉ cần xoá thư mục:\n" + str(cc.APP_DIR) +
               ("\n\nMột số mục không xoá được:\n" + "\n".join(errors) if errors else ""))
        messagebox.showinfo("ClipBridge", msg)
        self.open_folder()
        self.quit()

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
                                "Muốn tắt hẳn: nút Cài đặt (bánh răng) → Thoát hẳn.")

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
    root = ctk.CTk()
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
