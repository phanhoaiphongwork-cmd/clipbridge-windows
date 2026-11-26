#!/usr/bin/env python3
"""
Gộp ClipBridge thành MỘT file: ClipBridge.pyw (chứa clipbridge.py, clipbridge_cloud.py,
clipbridge_app.pyw và icon.png).

    py build_single.py              -> tạo dist/ClipBridge.pyw
    py build_single.py -o X.pyw     -> tạo X.pyw

Người dùng chỉ cần cài Python rồi nhấp đúp ClipBridge.pyw: lần đầu app tự cài vào
%LOCALAPPDATA%\\ClipBridge, tạo lối tắt và bật tự khởi động.

Mã nguồn được nhúng nguyên văn (không nén, không mã hoá) để ai cũng đọc được file kết quả.
"""
import argparse
import ast
import base64
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ["clipbridge", "clipbridge_cloud"]  # theo thứ tự import
APP = "clipbridge_app.pyw"


def literal(src):
    """Chuỗi Python (nguyên văn, dễ đọc) có giá trị đúng bằng src."""
    lit = "r'''" + src + "'''"
    if "'''" in src or src.endswith("\\") or ast.literal_eval(lit) != src:
        lit = repr(src)
    assert ast.literal_eval(lit) == src
    return lit


def build(out):
    sources = {name: (ROOT / f"{name}.py").read_text(encoding="utf-8") for name in MODULES}
    app = (ROOT / APP).read_text(encoding="utf-8")
    icon = base64.b64encode((ROOT / "icon.png").read_bytes()).decode()

    parts = [
        '#!/usr/bin/env python3\n'
        '"""ClipBridge - đồng bộ clipboard Windows <-> iPhone, gói trong MỘT file.\n\n'
        'Cài Python 3 (https://www.python.org/downloads/) rồi nhấp đúp file này.\n'
        'Lần đầu chạy, app tự cài vào %LOCALAPPDATA%\\\\ClipBridge, tạo lối tắt và bật tự khởi động.\n\n'
        'File này được tạo tự động bởi build_single.py từ repo clipbridge-windows - đừng sửa tay.\n'
        '"""\n',
        "import linecache as _cb_linecache\n"
        "import os as _cb_os\n"
        "import sys as _cb_sys\n"
        "import types as _cb_types\n\n"
        "__file__ = _cb_os.path.abspath(__file__)\n\n\n"
        "def _cb_run(code, name, namespace):\n"
        "    filename = f\"<ClipBridge.pyw:{name}>\"\n"
        "    lines = code.splitlines(True)\n"
        "    _cb_linecache.cache[filename] = (len(code), None, lines, filename)  # traceback có mã nguồn\n"
        "    exec(compile(code, filename, \"exec\"), namespace)\n\n\n"
        "def _cb_module(name, code):\n"
        "    m = _cb_types.ModuleType(name)\n"
        "    m.__file__ = __file__\n"
        "    _cb_sys.modules[name] = m\n"
        "    _cb_run(code, name, m.__dict__)\n\n\n",
    ]
    for name, src in sources.items():
        parts.append(f"# {'=' * 70} {name}.py\n_cb_module({name!r}, {literal(src)})\n\n\n")
    parts.append(f"# {'=' * 70} icon.png\n_CLIPBRIDGE_BUNDLED_ICON = __import__('base64').b64decode(\n")
    parts.append("".join(f"    {line!r}\n" for line in textwrap.wrap(icon, 96)) + ")\n\n\n")
    parts.append(f"# {'=' * 70} {APP} (chạy như chương trình chính)\n_cb_run({literal(app)}, {APP!r}, globals())\n")

    text = "".join(parts)
    compile(text, str(out), "exec")  # kiểm tra cú pháp
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("-o", "--output", type=Path, default=ROOT / "dist" / "ClipBridge.pyw")
    out = build(ap.parse_args().output)
    print(f"Đã tạo {out} ({out.stat().st_size:,} bytes)")
