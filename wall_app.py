#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""壁纸提取器 App — Wallpaper Engine 壁纸图片提取工具的图形界面。

把 scene.pkg / .mpkg / .mkg / .tex / 壁纸文件夹 拖进窗口任意位置即可提取；
提取结果按包分组（一个包文件夹 = 一段，互不混排），双击缩略图全屏翻看。
完全复用同目录下的 extract-wallpaper.ps1 + repkg/RePKG.exe 引擎。
"""

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


def _ensure_pyside():
    try:
        import PySide6  # noqa: F401
        return True
    except ImportError:
        msg = ("缺少界面库，无法启动壁纸提取器。\n\n"
               "请在命令行运行一次：\npython -m pip install PySide6-Essentials")
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, "壁纸提取器", 0x10)
        except Exception:
            print(msg)
        return False


if not _ensure_pyside():
    sys.exit(1)

from collections import OrderedDict, deque

from PySide6.QtCore import (Qt, QProcess, QObject, QPoint, QRunnable, QSize,
                            QThreadPool, QTimer, QUrl, QMimeData, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QImage,
                           QImageReader, QKeySequence, QLinearGradient,
                           QPainter, QPainterPath, QPalette, QPen, QPixmap,
                           QShortcut)
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QFrame,
                               QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QScrollArea,
                               QSizePolicy, QTextEdit, QToolButton,
                               QVBoxLayout, QWidget)

if getattr(sys, "frozen", False):  # PyInstaller 打包后：exe 所在目录
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
ENGINE_PS1 = APP_DIR / "extract-wallpaper.ps1"
DEFAULT_OUT = APP_DIR / "已提取图片"
CFG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "WallExtractor"
CFG_FILE = CFG_DIR / "config.json"
SINGLE_KEY = "WallExtractor.SingleInstance.v1"  # 防多开的命名管道
IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
VID_EXTS = {".mp4", ".webm", ".mkv", ".avi", ".mov", ".m4v"}
MEDIA_EXTS = IMG_EXTS | VID_EXTS
PKG_EXTS = {".pkg", ".mpkg", ".mkg", ".tex"}
THUMB_W, THUMB_H, GAP = 210, 131, 12
THUMB_MIN_H, THUMB_MAX_H = 90, 400
CAPTION_H = 26         # 缩略图下方文件名条高度
THUMB_RADIUS = 11      # 缩略图圆角半径
THUMB_THREADS = 4  # 缩略图解码线程上限（提取时临时降到 1–2，把核让给 RePKG）
MAX_PARALLEL = 8   # 并行提取进程硬上限（更高收益递减，且每个进程要占几百 MB 内存）
PRUNE_MARK = ".wall_extractor_out"  # 输出根目录标记：有它才自动清理空文件夹
THUMB_CACHE = CFG_DIR / "thumbs"    # 缩略图磁盘缓存（重启后免重新解码）


def _physical_cores() -> int:
    """物理核数（GetLogicalProcessorInformationEx）；失败返回 0。"""
    try:
        k32 = ctypes.windll.kernel32
        length = ctypes.c_ulong(0)
        k32.GetLogicalProcessorInformationEx(0, None, ctypes.byref(length))
        if length.value <= 0:
            return 0
        buf = ctypes.create_string_buffer(length.value)
        if not k32.GetLogicalProcessorInformationEx(0, buf, ctypes.byref(length)):
            return 0
        raw, n, off = buf.raw, 0, 0
        while off + 8 <= length.value:
            rel = int.from_bytes(raw[off:off + 4], "little")
            size = int.from_bytes(raw[off + 4:off + 8], "little")
            if size <= 0:
                break
            if rel == 0:  # RelationProcessorCore
                n += 1
            off += size
        return n
    except Exception:
        return 0


def _total_ram_gb() -> float:
    """物理内存总量（GB）；失败返回 0。"""
    class _MS(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    try:
        ms = _MS()
        ms.dwLength = ctypes.sizeof(ms)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms)):
            return ms.ullTotalPhys / (1 << 30)
    except Exception:
        pass
    return 0.0


def auto_parallel() -> int:
    """自动并行度：物理核的一半（1–8），再按内存总量收口。

    RePKG 单线程，只有多进程才能吃满多核；但每进程峰值内存可达数百 MB，
    内存小的机器必须减半，否则提取时容易把系统拖进交换区。
    """
    logical = os.cpu_count() or 4
    phys = _physical_cores() or max(1, logical // 2)
    n = max(1, min(MAX_PARALLEL, phys // 2))
    ram = _total_ram_gb()
    if ram:
        cap = 2 if ram < 8 else 4 if ram < 12 else 6 if ram < 16 else MAX_PARALLEL
        n = min(n, cap)
    return max(1, n)


def resolve_parallel(cfg) -> int:
    """配置里的 parallel：0/缺省 = 自动，1..MAX_PARALLEL = 指定。"""
    try:
        v = int(cfg.get("parallel", 0) or 0)
    except (TypeError, ValueError):
        v = 0
    if v:
        return max(1, min(MAX_PARALLEL, v))
    return auto_parallel()


def _norm_key(path) -> str:
    """路径规范化（大小写/斜杠统一），用于队列去重与包含关系判断。"""
    return os.path.normcase(str(path).replace("/", "\\"))


def _out_dir_name(path) -> str:
    """推导引擎会用的输出子目录名（与引擎规则一致），用于并发写冲突判断。"""
    p = Path(path)
    if p.is_dir():
        return p.name.lower()
    suf = p.suffix.lower()
    if suf == ".tex":
        return p.stem.lower()
    if suf in PKG_EXTS:
        return p.parent.name.lower()
    return p.name.lower()


def _wall_name(path) -> str:
    """展示用的壁纸名（保留原始大小写）：包取所在文件夹名，纹理取文件名。"""
    p = Path(path)
    if p.is_dir():
        return p.name
    suf = p.suffix.lower()
    if suf == ".tex":
        return p.stem
    if suf in PKG_EXTS:
        return p.parent.name
    return p.name


def _items_conflict(a, b) -> bool:
    """两个队列条目是否可能写同一个输出目录（必须串行，不能并行）。"""
    if _out_dir_name(a["path"]) == _out_dir_name(b["path"]):
        return True
    pa, pb = Path(a["path"]), Path(b["path"])
    ka, kb = _norm_key(a["path"]), _norm_key(b["path"])
    if pa.is_dir() and kb.startswith(ka.rstrip("\\") + "\\"):
        return True
    if pb.is_dir() and ka.startswith(kb.rstrip("\\") + "\\"):
        return True
    return False


def _item_cost(pp: Path) -> int:
    """粗估条目成本（字节）：文件取大小，文件夹取其内全部包文件大小之和。"""
    try:
        if pp.is_dir():
            total = 0
            for f in pp.rglob("*"):
                if f.is_file() and f.name.lower().endswith((".pkg", ".mpkg", ".mkg")):
                    try:
                        total += f.stat().st_size
                    except OSError:
                        pass
            return total
        return pp.stat().st_size
    except OSError:
        return 0


def load_config():
    try:
        return json.loads(CFG_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


@dataclass
class FileInfo:
    path: str
    name: str
    size: int
    mtime: float
    is_video: bool
    w: int = 0
    h: int = 0


@dataclass
class GroupData:
    name: str
    path: str
    files: list
    mtime: float

    @property
    def images(self):
        return [f for f in self.files if not f.is_video]

    @property
    def videos(self):
        return [f for f in self.files if f.is_video]


def human_size(n):
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


def fmt_rel_time(epoch):
    lt = time.localtime(epoch)
    today = time.localtime()
    if lt[:3] == today[:3]:
        return "今天 " + time.strftime("%H:%M", lt)
    if lt.tm_yday == today.tm_yday - 1 and lt.tm_year == today.tm_year:
        return "昨天 " + time.strftime("%H:%M", lt)
    return time.strftime("%m-%d %H:%M", lt)


_DIM_CACHE = {}  # (路径, mtime) -> (宽, 高)：图头只读一次，重扫零开销


def _image_dims(path: str, mtime: float):
    key = (path, mtime)
    got = _DIM_CACHE.get(key)
    if got is None:
        s = QImageReader(path).size()  # 只读图头，不解码像素
        got = (s.width(), s.height()) if s.isValid() else (0, 0)
        _DIM_CACHE[key] = got
    return got


def scan_groups(outdir: Path):
    """扫描输出根目录：一个子文件夹 = 一个壁纸分组（一个包）。"""
    groups = []
    if not outdir.is_dir():
        return groups
    for d in outdir.iterdir():
        if not d.is_dir() or d.name.startswith("."):
            continue  # 跳过隐藏目录（引擎并行提取时的 .wall_tmp_* 临时目录等）
        files, newest = [], d.stat().st_mtime
        for f in d.rglob("*"):
            if not f.is_file():
                continue
            ext = f.suffix.lower()
            if ext not in IMG_EXTS and ext not in VID_EXTS:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            is_vid = ext in VID_EXTS
            w, h = (0, 0) if is_vid else _image_dims(str(f), st.st_mtime)
            files.append(FileInfo(str(f), f.name, st.st_size, st.st_mtime,
                                  is_vid, w, h))
        if not files:
            continue
        files.sort(key=lambda fi: fi.name.lower())
        groups.append(GroupData(d.name, str(d), files, newest))
    groups.sort(key=lambda g: g.mtime, reverse=True)
    return groups


# ---------------------------------------------------------------- 系统操作
def set_as_wallpaper(path: str) -> bool:
    """设为桌面壁纸；webp/gif 先转 png（Windows 壁纸接口不认这两种）。"""
    p = Path(path)
    if p.suffix.lower() in (".webp", ".gif"):
        tmp = Path(os.environ.get("TEMP", str(p.parent))) / f"wall_tmp_{p.stem}.png"
        img = QImage(str(p))
        if img.isNull() or not img.save(str(tmp), "PNG"):
            return False
        p = tmp
    SPI_SETDESKWALLPAPER = 0x0014
    SPIF_UPDATEINIFILE, SPIF_SENDCHANGE = 0x01, 0x02
    return bool(ctypes.windll.user32.SystemParametersInfoW(
        SPI_SETDESKWALLPAPER, 0, str(p), SPIF_UPDATEINIFILE | SPIF_SENDCHANGE))


def recycle_file(path: str) -> bool:
    """删除到回收站（可撤销）。"""

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", ctypes.c_uint),
                    ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]

    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = 3  # FO_DELETE
    op.pFrom = str(path) + "\0"
    op.pTo = None
    op.fFlags = 0x40 | 0x10 | 0x04  # FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


def _dir_has_media(path) -> bool:
    """文件夹下（递归）是否存在任何图片/视频文件。"""
    try:
        for f in Path(path).rglob("*"):
            if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
                return True
    except OSError:
        return True  # 读不了的不动它，宁可多留不可误删
    return False


def prune_empty_folder(path) -> bool:
    """文件夹下没有任何图片/视频时，整个文件夹移入回收站。删了返回 True。"""
    if _dir_has_media(path):
        return False
    return recycle_file(str(path))


def prune_empty_folders(outdir) -> list:
    """清扫输出根目录：子文件夹里没图片/视频的都清掉，返回被清的名字列表。"""
    gone = []
    root = Path(outdir)
    if not root.is_dir():
        return gone
    for d in list(root.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and prune_empty_folder(str(d)):
            gone.append(d.name)
    return gone


def outdir_owned(outdir) -> bool:
    """输出目录是否确认属于提取器：默认目录，或带有标记文件。

    自定义输出目录如果从未被提取器写过，就不做"清空文件夹"操作，
    避免用户把输出目录指到 D:\\ 之类的地方时误清无关的空目录。
    """
    root = Path(outdir)
    if root == DEFAULT_OUT:
        return True
    return (root / PRUNE_MARK).is_file()


def mark_outdir(outdir):
    """有产物写进输出目录后打标记（之后才允许自动清空文件夹）。"""
    try:
        root = Path(outdir)
        root.mkdir(parents=True, exist_ok=True)
        p = root / PRUNE_MARK
        if not p.exists():
            p.write_text(
                "壁纸提取器输出目录标记：删除后 App 不再自动清理空文件夹。\n",
                "utf-8")
    except OSError:
        pass


def open_in_explorer(path: str):
    """用系统默认方式打开文件所在的文件夹。

    不用 explorer /select 定位（那种调用经常留下无窗口的 explorer.exe
    空壳进程，每个 ~55MB，攒多了吃掉好几个 G 内存）。
    """
    p = Path(path)
    folder = p.parent if p.is_file() else p
    os.startfile(str(folder))


FACTORY_ID = "75dff2b7-6936-4c06-a8bb-676a7b00b24b"  # Win11 文件夹窗口的 DCOM 宿主
KEEP_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
                "CabinetWClass"}


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


class _PBI(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p), ("Reserved3", ctypes.c_void_p)]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p)]


def _process_cmdline(pid) -> str:
    """跨进程读取进程命令行（经 PEB），失败返回空串。"""
    k32, ntdll = ctypes.windll.kernel32, ctypes.windll.ntdll
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFORMATION | VM_READ
    if not h:
        return ""
    try:
        pbi, got = _PBI(), ctypes.c_ulong()
        if ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi),
                                           ctypes.sizeof(pbi), ctypes.byref(got)):
            return ""
        ptr = ctypes.c_void_p()
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(pbi.PebBaseAddress + 0x20),
                                     ctypes.byref(ptr), 8, ctypes.byref(n)):
            return ""
        us = _UNICODE_STRING()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(ptr.value + 0x70),
                                     ctypes.byref(us), ctypes.sizeof(us),
                                     ctypes.byref(n)):
            return ""
        if us.Length == 0:
            return ""
        s = ctypes.create_unicode_buffer(us.Length)
        if not k32.ReadProcessMemory(h, us.Buffer, s, us.Length, ctypes.byref(n)):
            return ""
        return s.value
    finally:
        k32.CloseHandle(h)


def explorer_ghost_pids():
    """找出 /factory 空壳：命令行带 /factory 宿主 ID、且没有任何可见保留窗口
    （任务栏/桌面/打开着的文件夹）的 explorer.exe。"""
    user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    entry, pids = _PROCESSENTRY32W(), []
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == "explorer.exe":
            pids.append(entry.th32ProcessID)
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)

    keepwin = set()
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    buf = ctypes.create_unicode_buffer(256)
    cur = wintypes.DWORD()

    def on_window(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(cur))
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value in KEEP_CLASSES:
                keepwin.add(cur.value)
        return True

    user32.EnumWindows(CB(on_window), 0)
    return [p for p in pids
            if p not in keepwin and FACTORY_ID in _process_cmdline(p).lower()]


def cleanup_explorer_zombies():
    """结束 /factory 空壳，返回 (已结束数, 发现数)。"""
    pids = explorer_ghost_pids()
    killed = 0
    for p in pids:
        h = ctypes.windll.kernel32.OpenProcess(0x0001, False, p)
        if h:
            if ctypes.windll.kernel32.TerminateProcess(h, 1):
                killed += 1
            ctypes.windll.kernel32.CloseHandle(h)
    return killed, len(pids)


# ---------------------------------------------------------------- 定时清理空壳（计划任务）
# Win11 会给每个文件夹窗口配一个 /factory DCOM 宿主进程，窗口关闭后宿主常不退出
# （系统 bug，任何开文件夹窗口的程序都会触发）。这里注册一个每 30 分钟跑一次的
# 计划任务，用与 🧹 按钮相同的 v2 规则把空壳收走，让它短命到不可见。
CLEAN_TASK_NAME = "WallExtractor_CleanExplorerGhosts"
CLEAN_SCRIPT_PATH = CFG_DIR / "clean_ghosts.pyw"
CREATE_NO_WINDOW = 0x08000000

# 独立清理脚本：只依赖 ctypes，与上方 v2 规则保持同一套判定，pythonw 静默运行
CLEAN_GHOSTS_PYW = r'''
import ctypes, os, time
from ctypes import wintypes

FACTORY_ID = "75dff2b7-6936-4c06-a8bb-676a7b00b24b"
KEEP_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW",
                "CabinetWClass"}
LOG = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                   "WallExtractor", "clean_ghosts.log")


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


class _PBI(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p), ("Reserved3", ctypes.c_void_p)]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort),
                ("Buffer", ctypes.c_void_p)]


def _process_cmdline(pid):
    k32, ntdll = ctypes.windll.kernel32, ctypes.windll.ntdll
    h = k32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        return ""
    try:
        pbi, got = _PBI(), ctypes.c_ulong()
        if ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi),
                                           ctypes.sizeof(pbi), ctypes.byref(got)):
            return ""
        ptr = ctypes.c_void_p()
        n = ctypes.c_size_t()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(pbi.PebBaseAddress + 0x20),
                                     ctypes.byref(ptr), 8, ctypes.byref(n)):
            return ""
        us = _UNICODE_STRING()
        if not k32.ReadProcessMemory(h, ctypes.c_void_p(ptr.value + 0x70),
                                     ctypes.byref(us), ctypes.sizeof(us),
                                     ctypes.byref(n)):
            return ""
        if us.Length == 0:
            return ""
        s = ctypes.create_unicode_buffer(us.Length)
        if not k32.ReadProcessMemory(h, us.Buffer, s, us.Length, ctypes.byref(n)):
            return ""
        return s.value
    finally:
        k32.CloseHandle(h)


def explorer_ghost_pids():
    user32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    entry, pids = _PROCESSENTRY32W(), []
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == "explorer.exe":
            pids.append(entry.th32ProcessID)
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)

    keepwin = set()
    CB = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    buf = ctypes.create_unicode_buffer(256)
    cur = wintypes.DWORD()

    def on_window(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(cur))
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value in KEEP_CLASSES:
                keepwin.add(cur.value)
        return True

    user32.EnumWindows(CB(on_window), 0)
    return [p for p in pids
            if p not in keepwin and FACTORY_ID in _process_cmdline(p).lower()]


def _log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 100000:
            os.remove(LOG)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def main():
    try:
        pids = explorer_ghost_pids()
    except Exception as e:
        _log("枚举失败: " + repr(e))
        return
    killed = 0
    k32 = ctypes.windll.kernel32
    for p in pids:
        h = k32.OpenProcess(0x0001, False, p)
        if h:
            if k32.TerminateProcess(h, 1):
                killed += 1
            k32.CloseHandle(h)
    if pids:
        _log(f"发现 {len(pids)} 个空壳，结束 {killed} 个")


if __name__ == "__main__":
    main()
'''


def _find_pythonw():
    """优先用系统的 pythonw 静默跑独立脚本；找不到则回退到本程序 --cleanghosts。"""
    cands = []
    if not getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).with_name("pythonw.exe"))
        cands.append(Path(getattr(sys, "base_prefix", sys.executable)) / "pythonw.exe")
    la = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    if la.is_dir():
        for d in sorted(la.iterdir(), reverse=True):
            cands.append(d / "pythonw.exe")
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def _clean_task_cmd() -> str:
    pw = _find_pythonw()
    if pw:
        return f'"{pw}" "{CLEAN_SCRIPT_PATH}"'
    return f'"{sys.executable}" --cleanghosts'


def sched_clean_enabled() -> bool:
    r = subprocess.run(["schtasks", "/Query", "/TN", CLEAN_TASK_NAME],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    return r.returncode == 0


def sched_clean_install():
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    CLEAN_SCRIPT_PATH.write_text(CLEAN_GHOSTS_PYW, encoding="utf-8")
    r = subprocess.run(["schtasks", "/Create", "/F", "/TN", CLEAN_TASK_NAME,
                        "/TR", _clean_task_cmd(), "/SC", "MINUTE", "/MO", "30"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or b"").decode("gbk", "replace").strip()
        raise RuntimeError(msg or "schtasks 注册失败")


def sched_clean_remove():
    subprocess.run(["schtasks", "/Delete", "/F", "/TN", CLEAN_TASK_NAME],
                   capture_output=True, creationflags=CREATE_NO_WINDOW)


def _clean_ghosts_cli():
    killed, found = cleanup_explorer_zombies()
    print(f"清理完成: 发现 {found} 个空壳，结束 {killed} 个")
    return 0


def _win_title_bar(hwnd: int, dark: bool, bg_hex: str, txt_hex: str):
    """把 Windows 原生标题栏染成主题色：Win10 用沉浸式深色模式，Win11 精确配色。

    返回 (深色模式, 标题栏颜色, 标题文字颜色) 三个调用的 HRESULT，供自检。
    """
    hrs = (-1, -1, -1)
    if not hwnd:
        return hrs
    try:
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1 if dark else 0)
        hr_dark = -1
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（19 兼容旧版）
            hr_dark = dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), ctypes.c_int(attr),
                                                ctypes.byref(on), ctypes.sizeof(on))
            if hr_dark == 0:
                break

        def colorref(h: str) -> ctypes.c_uint:
            r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
            return ctypes.c_uint((b << 16) | (g << 8) | r)  # COLORREF 0x00BBGGRR

        cap = colorref(bg_hex)  # 35 = DWMWA_CAPTION_COLOR
        hr_cap = dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), ctypes.c_int(35),
                                           ctypes.byref(cap), ctypes.sizeof(cap))
        txt = colorref(txt_hex)  # 36 = DWMWA_TEXT_COLOR
        hr_txt = dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), ctypes.c_int(36),
                                           ctypes.byref(txt), ctypes.sizeof(txt))
        return (hr_dark, hr_cap, hr_txt)
    except Exception:
        return hrs


# ---------------------------------------------------------------- 引擎输出解码
def decode_line(b: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc).rstrip("\r")
        except UnicodeDecodeError:
            pass
    return b.decode("utf-8", "replace").rstrip("\r")


def _html_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def html_line(t: str) -> str:
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for tag, color in (("[图片]", "#3ecf8e"), ("[视频]", "#6fb7ff"), ("[音频]", "#a78bfa"),
                       ("[跳过]", "#f0b44c"), ("[警告]", "#f0b44c"), ("[错误]", "#ff6b6b"),
                       ("[引擎]", "#8b95a7"), ("[提示]", "#8b95a7"), ("[加速]", "#8b95a7")):
        t = t.replace(tag, f'<span style="color:{color}">{tag}</span>')
    if t.startswith("处理:") or t.startswith("============"):
        t = f'<span style="color:#69b7ff">{t}</span>'
    return t


# ---------------------------------------------------------------- 主题样式
def build_qss(dark: bool) -> str:
    """生成整套 QSS。设计基调：低饱和深色/浅色底 + 蓝紫渐变强调色。"""
    if dark:
        bg, surface, surface2, surface3 = "#0f1218", "#161b23", "#1d2430", "#242c3a"
        line, line_soft = "#252d3a", "#1b2230"
        txt, sub, muted = "#e6eaf2", "#98a3b8", "#6f7a8e"
        acc, acc2, acc_hover, acc_press = "#6f8cff", "#8b6cff", "#8099ff", "#5b76e8"
        sel_bg, sel_fg = "#22304f", "#9db4ff"
        tag_bg, tag_fg = "#3a2e14", "#f0b44c"
        warn, danger = "#f0b44c", "#ff6b6b"
        logbg, fs1, fs2 = "#0c1015", "#151b26", "#0a0c11"
    else:
        bg, surface, surface2, surface3 = "#f3f5fa", "#ffffff", "#eef1f7", "#e4e9f2"
        line, line_soft = "#dfe5f0", "#e9edf5"
        txt, sub, muted = "#151b26", "#5d697c", "#8b95a7"
        acc, acc2, acc_hover, acc_press = "#3d6bf5", "#7b5cf0", "#4f7bff", "#2f57d6"
        sel_bg, sel_fg = "#e6edff", "#2f57d6"
        tag_bg, tag_fg = "#fff2d8", "#b45309"
        warn, danger = "#d97706", "#dc2626"
        logbg, fs1, fs2 = "#eef1f7", "#f6f8fc", "#e3e8f2"
    return f"""
    QWidget#root {{ background:{bg}; }}
    QWidget#Header {{ background:{surface}; border-bottom:1px solid {line}; }}
    QWidget#FilterBar {{ background:{bg}; border-bottom:1px solid {line_soft}; }}
    QWidget#BottomBar {{ background:{surface}; border-top:1px solid {line}; }}
    QWidget#QueueRow {{ background:{surface}; border-top:1px solid {line}; }}
    QWidget#SelBar {{ background:{sel_bg}; border-top:1px solid {line}; }}

    QLabel {{ color:{txt}; background:transparent; }}
    QLabel#logo {{ color:#ffffff; font-size:16px; font-weight:700;
                   border-radius:9px;
                   background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 {acc},stop:1 {acc2}); }}
    QLabel#brandTitle {{ font-size:15px; font-weight:700; }}
    QLabel#brandSub {{ color:{muted}; font-size:11px; }}
    QLabel#hint {{ color:{muted}; font-size:12px; }}
    QLabel#hintSmall {{ color:{muted}; font-size:11px; }}
    QLabel#sectionHead {{ font-size:14px; font-weight:700; }}
    QLabel#sectionCount {{ color:{sub}; font-size:12px; }}
    QLabel#sectionIcon {{ background:{surface3}; border-radius:7px; font-size:13px; }}
    QLabel#newTag {{ color:{tag_fg}; background:{tag_bg}; border-radius:9px;
                     padding:2px 8px; font-size:11px; font-weight:600; }}
    QLabel#cap {{ color:{sub}; font-size:11px; }}
    QLabel#dims {{ background:rgba(8,10,16,175); color:#e8edf7; border-radius:7px;
                   padding:1px 7px; font-size:10px; }}
    QLabel#emptyIcon {{ background:{sel_bg}; color:{acc}; border-radius:38px;
                        font-size:34px; }}
    QLabel#emptyTitle {{ font-size:19px; font-weight:700; }}
    QLabel#emptyText {{ color:{sub}; font-size:12px; }}
    QLabel#emptyHint {{ color:{muted}; font-size:11px; }}
    QLabel#noMatch {{ color:{muted}; font-size:13px; padding:30px 12px; }}
    QLabel#queueLabel {{ color:{sub}; font-size:12px; font-weight:600; }}
    QLabel#pct {{ color:{sub}; font-size:11px; }}
    QLabel#selInfo {{ color:{sel_fg}; font-size:12px; font-weight:600; }}

    QPushButton {{ background:transparent; color:{sub}; border:1px solid transparent;
                   border-radius:9px; padding:6px 12px; font-size:12px; }}
    QPushButton:hover {{ background:{surface2}; color:{txt}; }}
    QPushButton:pressed {{ background:{surface3}; }}
    QPushButton:disabled {{ color:{muted}; }}
    QPushButton#primary {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,
                           stop:0 {acc},stop:1 {acc2}); color:#ffffff; border:none;
                           font-weight:600; padding:7px 18px; }}
    QPushButton#primary:hover {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:1,
                                stop:0 {acc_hover},stop:1 {acc2}); }}
    QPushButton#primary:pressed {{ background:{acc_press}; }}
    QPushButton#primary:disabled {{ background:{surface2}; color:{muted}; }}
    QPushButton#danger {{ color:{danger}; }}
    QPushButton#danger:hover {{ background:rgba(255,107,107,34); color:{danger}; }}
    QPushButton[chip="true"] {{ background:{surface2}; color:{sub}; border:1px solid transparent;
                                border-radius:14px; padding:4px 13px; font-size:12px; }}
    QPushButton[chip="true"]:hover {{ color:{txt}; border-color:{line}; }}
    QPushButton[chip="true"]:checked {{ background:{sel_bg}; color:{sel_fg};
                                        border-color:transparent; font-weight:600; }}
    QPushButton#multiBtn {{ border:1px solid {line}; }}
    QPushButton#multiBtn:checked {{ background:{sel_bg}; color:{sel_fg};
                                    border-color:transparent; font-weight:600; }}
    QPushButton#selAll {{ padding:2px 9px; font-size:11px; color:{muted};
                          border:1px solid {line}; border-radius:7px; }}
    QPushButton#selAll:hover {{ color:{txt}; border-color:{acc}; }}

    QToolButton {{ background:transparent; color:{sub}; border:1px solid transparent;
                   border-radius:9px; padding:5px 7px; font-size:13px; }}
    QToolButton:hover {{ background:{surface2}; color:{txt}; }}
    QToolButton:pressed {{ background:{surface3}; }}
    QToolButton:checked {{ background:{sel_bg}; color:{sel_fg}; }}
    QToolButton::menu-indicator {{ image:none; width:0; }}
    QToolButton#iconBtn {{ padding:5px; font-size:15px; }}
    QToolButton#hoverBtn {{ background:rgba(10,13,20,180); color:#ffffff; border:none;
                            border-radius:8px; padding:4px 6px; font-size:12px; }}
    QToolButton#hoverBtn:hover {{ background:{acc}; }}

    QLineEdit#search {{ background:{surface2}; border:1px solid {line}; border-radius:9px;
                        padding:5px 10px; color:{txt}; font-size:12px;
                        selection-background-color:{acc}; }}
    QLineEdit#search:focus {{ border-color:{acc}; background:{surface}; }}

    QFrame#section {{ background:{surface}; border:1px solid {line}; border-radius:16px; }}
    QFrame#emptyCard {{ background:{surface}; border:1px solid {line}; border-radius:18px; }}
    QFrame#thumb {{ background:{surface2}; border:1px solid transparent; border-radius:12px; }}
    QFrame#thumb:hover {{ background:{surface3}; border-color:{line}; }}
    QFrame#thumb[selected="true"] {{ border:2px solid {acc}; background:{surface3}; }}
    QLabel#selBadge {{ background:{acc}; color:#ffffff; border-radius:11px;
                        font-size:11px; font-weight:700; }}

    QScrollArea {{ background:transparent; border:none; }}
    QScrollBar:vertical {{ background:transparent; width:9px; margin:2px; }}
    QScrollBar::handle:vertical {{ background:{line}; border-radius:4px; min-height:28px; }}
    QScrollBar::handle:vertical:hover {{ background:{sub}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
    QScrollBar:horizontal {{ background:transparent; height:9px; margin:2px; }}
    QScrollBar::handle:horizontal {{ background:{line}; border-radius:4px; min-width:28px; }}
    QScrollBar::handle:horizontal:hover {{ background:{sub}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}

    QTextEdit#log {{ background:{logbg}; color:{sub}; border:none; border-top:1px solid {line};
                     font-family:Consolas,'Courier New',monospace; font-size:12px;
                     padding:8px 12px; }}
    QProgressBar {{ background:{surface2}; border:none; border-radius:3px; }}
    QProgressBar::chunk {{ background:qlineargradient(x1:0,y1:0,x2:1,y2:0,
                          stop:0 {acc},stop:1 {acc2}); border-radius:3px; }}

    QMenu {{ background:{surface}; color:{txt}; border:1px solid {line}; border-radius:10px;
             padding:5px; }}
    QMenu::item {{ padding:6px 26px 6px 12px; border-radius:7px; }}
    QMenu::item:selected {{ background:{surface2}; }}
    QMenu::item:disabled {{ color:{muted}; }}
    QMenu::separator {{ height:1px; background:{line}; margin:4px 8px; }}
    QMenu::indicator {{ width:14px; height:14px; }}
    QToolTip {{ background:{surface3}; color:{txt}; border:1px solid {line};
                border-radius:6px; padding:4px 8px; }}

    QDialog#fsbg {{ background:qradialgradient(cx:0.5,cy:0.42,radius:0.9,
                    stop:0 {fs1},stop:1 {fs2}); }}
    QFrame#fsTop, QFrame#fsBar {{ background:{surface}; border:1px solid {line};
                                   border-radius:14px; }}
    QLabel#fsIndex {{ background:{surface3}; color:{sub}; border-radius:8px;
                      padding:3px 9px; font-size:11px; }}
    QLabel#fsName {{ font-size:13px; font-weight:600; }}
    QLabel#fsMeta {{ color:{muted}; font-size:11px; }}
    QLabel#fsHint {{ color:{muted}; font-size:11px; }}
    QToolButton#fsNav {{ background:{surface3}; border-radius:10px; padding:4px 10px;
                         font-size:16px; }}
    QToolButton#fsNav:hover {{ background:{acc}; color:#ffffff; }}
    QToolButton#fsClose {{ font-size:15px; padding:5px 8px; }}

    QDialog#skipdlg {{ background:{surface}; }}
    QLabel#skipdlgIcon {{ color:{warn}; font-size:22px; background:transparent; }}
    QLabel#skipdlgTitle {{ font-size:16px; font-weight:700; }}
    QFrame#skipdlgList {{ background:{surface2}; border:1px solid {line}; border-radius:10px; }}
    QLabel#skipdlgItem {{ color:{txt}; font-size:13px; }}
    """


def apply_palette(dark: bool):
    app = QApplication.instance()
    pal = app.palette()
    if dark:
        pal.setColor(QPalette.Window, QColor("#0f1218"))
        pal.setColor(QPalette.WindowText, QColor("#e6eaf2"))
        pal.setColor(QPalette.Base, QColor("#161b23"))
        pal.setColor(QPalette.Text, QColor("#e6eaf2"))
        pal.setColor(QPalette.Button, QColor("#1d2430"))
        pal.setColor(QPalette.ButtonText, QColor("#e6eaf2"))
        pal.setColor(QPalette.Highlight, QColor("#6f8cff"))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    else:
        pal.setColor(QPalette.Window, QColor("#f3f5fa"))
        pal.setColor(QPalette.WindowText, QColor("#151b26"))
        pal.setColor(QPalette.Base, QColor("#ffffff"))
        pal.setColor(QPalette.Text, QColor("#151b26"))
        pal.setColor(QPalette.Button, QColor("#eef1f7"))
        pal.setColor(QPalette.ButtonText, QColor("#151b26"))
        pal.setColor(QPalette.Highlight, QColor("#3d6bf5"))
        pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    app.setPalette(pal)


def _rounded_pixmap(pm: QPixmap, radius: int = THUMB_RADIUS) -> QPixmap:
    """把图片裁成圆角，避免卡片里出现生硬直角。"""
    if pm.isNull():
        return pm
    out = QPixmap(pm.size())
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    r = min(radius, pm.width() // 2, pm.height() // 2)
    path.addRoundedRect(0, 0, pm.width(), pm.height(), r, r)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pm)
    p.end()
    return out


# ---------------------------------------------------------------- 缩略图加载
def _thumb_cache_paths(path: str, mtime: float, img_h: int):
    """缩略图磁盘缓存文件：<sha1>.png + 同名 .dims（记录原图宽高）。"""
    key = f"{THUMB_W}x{img_h}|{mtime:.3f}|{path}".encode("utf-8", "surrogatepass")
    base = THUMB_CACHE / hashlib.sha1(key).hexdigest()
    return base.with_suffix(".png"), base.with_suffix(".dims")


def _prune_thumb_cache(limit_mb=300, limit_files=4000):
    """限制缩略图缓存体积（启动后/每批提取后调用，按最旧先删）。"""
    try:
        files = sorted(THUMB_CACHE.glob("*.png"), key=lambda p: p.stat().st_mtime)
        total = sum(f.stat().st_size for f in files)
    except OSError:
        return
    while files and (len(files) > limit_files or total > (limit_mb << 20)):
        f = files.pop(0)
        try:
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
            f.with_suffix(".dims").unlink(missing_ok=True)
        except OSError:
            break


class _ThumbJob(QRunnable):
    class Sig(QObject):
        done = Signal(str, QImage, int, int)

    def __init__(self, path, key, tw_, th_, mtime):
        super().__init__()
        self.path, self.key = path, key
        self.tw_, self.th_ = tw_, th_  # 目标显示尺寸（等宽变高，随原图比例）
        self.mtime = mtime
        self.sig = self.Sig()

    def _from_cache(self):
        """命中磁盘缓存则直接返回缩放好的缩略图，省掉解码。"""
        cf, mf = _thumb_cache_paths(self.path, self.mtime, self.th_)
        try:
            if not cf.is_file():
                return None
            img = QImage(str(cf))
            if img.isNull():
                return None
            w = h = 0
            try:
                w, h = (int(x) for x in mf.read_text("utf-8").split())
            except (OSError, ValueError):
                pass
            self.sig.done.emit(self.key, img, w, h)
            return True
        except OSError:
            return None

    def _save_cache(self, img: QImage, w: int, h: int):
        try:
            cf, mf = _thumb_cache_paths(self.path, self.mtime, self.th_)
            THUMB_CACHE.mkdir(parents=True, exist_ok=True)
            if img.save(str(cf), "PNG"):
                mf.write_text(f"{w} {h}", "utf-8")
        except OSError:
            pass

    def run(self):
        if self._from_cache():
            return
        r = QImageReader(self.path)
        r.setAutoTransform(True)
        size = r.size()
        w, h = size.width(), size.height()
        tw_, th_ = self.tw_, self.th_
        fmt = bytes(r.format()).lower()
        if fmt in (b"jpeg", b"jpg") and w > 0 and h > 0:
            # JPG 支持解码中途缩小（setScaledSize），大图省约 7 成耗时、
            # 九成峰值内存；PNG 等格式不支持缩放解码，走了反而更慢，维持原路径
            scale = min(tw_ / w, th_ / h)  # 完整显示（KeepAspectRatio），不裁剪
            cw, ch = round(w * scale), round(h * scale)
            dw, dh = min(w, cw * 2), min(h, ch * 2)
            if dw < w or dh < h:
                r.setScaledSize(QSize(dw, dh))
            img = r.read()
            if not img.isNull():
                img = img.scaled(cw, ch, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
        else:
            img = r.read()
            if not img.isNull():
                img = img.scaled(tw_, th_, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
        if not img.isNull():
            self._save_cache(img, w, h)
        self.sig.done.emit(self.key, img, w, h)


def _video_pixmap(w: int, h: int) -> QPixmap:
    """视频缩略图占位图：渐变底 + 播放三角，比一个 emoji 更清楚。"""
    pm = QPixmap(max(1, w), max(1, h))
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, w, h)
    g.setColorAt(0, QColor("#25365e"))
    g.setColorAt(1, QColor("#5b3a8c"))
    p.fillRect(pm.rect(), g)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 255, 255, 225))
    s = min(w, h) * 0.16
    cx, cy = w / 2, h / 2
    path = QPainterPath()
    path.moveTo(cx - s * 0.6, cy - s)
    path.lineTo(cx + s * 0.9, cy)
    path.lineTo(cx - s * 0.6, cy + s)
    path.closeSubpath()
    p.drawPath(path)
    p.end()
    return pm


# ---------------------------------------------------------------- 缩略图控件
class ThumbWidget(QFrame):
    def __init__(self, fi: FileInfo, gal: "GalleryView"):
        super().__init__()
        self.setObjectName("thumb")
        self.fi, self.gal = fi, gal
        # 等宽变高：宽度固定，高度按原图比例；极端比例钳到范围内（整体缩进保持比例）
        self.img_h = THUMB_H  # 视频 / 未知尺寸用默认高度
        if not fi.is_video and fi.w > 0 and fi.h > 0:
            self.img_h = max(THUMB_MIN_H, min(THUMB_MAX_H,
                                              round(THUMB_W * fi.h / fi.w)))
        self.setFixedSize(THUMB_W, self.img_h + CAPTION_H)
        self.key = f"{fi.path}|{fi.mtime:.3f}"
        self._res = ""
        self._hovering = False

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self.img = QLabel()
        self.img.setFixedSize(THUMB_W, self.img_h)
        self.img.setAlignment(Qt.AlignCenter)
        self.img.setStyleSheet("background:transparent;")
        self.cap = QLabel()
        self.cap.setObjectName("cap")
        self.cap.setFixedHeight(CAPTION_H)
        self.cap.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.cap.setContentsMargins(10, 0, 10, 0)
        v.addWidget(self.img)
        v.addWidget(self.cap)

        # 尺寸/类型角标（图片显示分辨率，视频显示“视频”）
        self.dims = QLabel(self.img)
        self.dims.setObjectName("dims")
        self.dims.setAlignment(Qt.AlignCenter)
        self.dims.hide()

        if fi.is_video:
            self.img.setPixmap(
                _rounded_pixmap(_video_pixmap(THUMB_W, self.img_h), THUMB_RADIUS))
            self._base = fi.name
            self._set_dims("视频")
            self.img.setToolTip("视频 · 双击用系统播放器打开")
        else:
            self.img.setText("…")
            self._base = fi.name
            self.img.setToolTip("双击用系统看图打开，回车全屏翻看")
            # 缩略图懒加载：不再在此处排队，等滚动到可见区域时由 GalleryView 触发
        self._update_cap()

        # 悬停快捷操作：懒创建，避免每张缩略图都常驻几个按钮控件
        self._hover = None
        if fi.is_video:
            self._hover_acts = [
                ("播放", "用默认播放器打开", lambda: os.startfile(fi.path)),
                ("位置", "打开所在位置", lambda: open_in_explorer(fi.path))]
        else:
            self._hover_acts = [
                ("壁纸", "设为桌面壁纸", lambda: self.gal.win.do_set_wallpaper(fi)),
                ("全屏", "全屏翻看", lambda: self.gal.request_preview(fi)),
                ("删除", "删除（移入回收站）", lambda: self.gal.win.do_delete(fi))]

        # 多选勾选角标（仅多选模式显示）
        self.badge = QLabel("✓", self)
        self.badge.setObjectName("selBadge")
        self.badge.setAlignment(Qt.AlignCenter)
        self.badge.setFixedSize(22, 22)
        self.badge.move(THUMB_W - 30, 8)
        self.badge.hide()

    def _update_cap(self):
        fm = QFontMetrics(self.cap.font())
        self.cap.setText(fm.elidedText(self._base, Qt.ElideRight, THUMB_W - 20))

    def _set_dims(self, text: str):
        self._res = text
        self.dims.setText(text)
        self.dims.adjustSize()
        self.dims.move(8, 8)
        if text:
            self.dims.show()
            self.dims.raise_()
        else:
            self.dims.hide()

    def set_pixmap(self, pm: QPixmap, w: int, h: int):
        self.img.setPixmap(_rounded_pixmap(pm, THUMB_RADIUS))
        if w and h:
            self._set_dims(f"{w}×{h}")
        self._update_cap()

    def set_failed(self):
        self.img.setText("⚠")
        self.img.setStyleSheet("color:#f0b44c;font-size:22px;background:transparent;")
        self.cap.setText("无法预览")

    # ---- 悬停快捷操作
    def _ensure_hover(self):
        """首次悬停时再创建快捷按钮，几千张图时省下大量控件。"""
        if self._hover is not None:
            return
        self._hover = QWidget(self.img)
        hl = QHBoxLayout(self._hover)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)
        for text, tip, fn in self._hover_acts:
            b = QToolButton()
            b.setObjectName("hoverBtn")
            b.setText(text)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(
                "QToolButton{background:rgba(10,13,20,195);color:#ffffff;"
                "border:none;border-radius:7px;padding:4px 8px;font-size:11px;}"
                "QToolButton:hover{background:#6f8cff;}")
            b.clicked.connect(fn)
            hl.addWidget(b)
        self._hover.hide()
        self._layout_hover()

    def _layout_hover(self):
        if self._hover is None:
            return
        self._hover.adjustSize()
        x = THUMB_W - self._hover.width() - 8
        y = self.img_h - self._hover.height() - 8
        self._hover.move(max(6, x), max(6, y))

    def _set_hover(self, on: bool):
        self._hovering = on
        show = on and not self.gal.multi_mode and bool(self.gal.win.groups)
        if show:
            self._ensure_hover()
            self._hover.setVisible(True)
            self._layout_hover()
            self._hover.raise_()
        elif self._hover is not None:
            self._hover.hide()

    def enterEvent(self, e):
        self._set_hover(True)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._set_hover(False)
        super().leaveEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._layout_hover()

    def set_selected(self, on: bool):
        self.setProperty("selected", "true" if on else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def set_checked(self, on: bool):
        self.badge.setVisible(on)

    def mousePressEvent(self, e):
        if self.gal.multi_mode:
            self.gal.toggle_multi(self)
        else:
            self.gal.select(self)
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, e):
        # 双击 = 用系统默认程序打开（图片→看图器，视频→播放器）
        if self.gal.multi_mode:
            return  # 多选模式下双击不再打开，避免误触
        if Path(self.fi.path).exists():
            os.startfile(self.fi.path)

    def contextMenuEvent(self, e):
        if self.gal.multi_mode:
            return  # 多选模式走底部操作条，右键菜单禁用
        self.gal.show_file_menu(self.fi, e.globalPos())


# ---------------------------------------------------------------- 分组区块
class SectionWidget(QFrame):
    def __init__(self, g: GroupData, is_new: bool, gal: "GalleryView"):
        super().__init__()
        self.setObjectName("section")
        self.group, self.gal = g, gal
        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 16)
        v.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        icon = QLabel("📁")
        icon.setObjectName("sectionIcon")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(28, 28)
        title = QLabel(g.name)
        title.setObjectName("sectionHead")
        n_img, n_vid = len(g.images), len(g.videos)
        self.cnt = QLabel(f"{n_img} 张图片" + (f" · {n_vid} 个视频" if n_vid else ""))
        self.cnt.setObjectName("sectionCount")
        newt = QLabel("新提取")
        newt.setObjectName("newTag")
        newt.setVisible(is_new)
        self.b_all = QPushButton("全选")
        self.b_all.setObjectName("selAll")
        self.b_all.setToolTip("全选 / 取消全选本文件夹的文件")
        self.b_all.clicked.connect(lambda: gal.select_all_section(self))
        self.b_all.setVisible(False)  # 仅多选模式显示
        b_open = QToolButton()
        b_open.setText("📂")
        b_open.setObjectName("iconBtn")
        b_open.setToolTip("在资源管理器中打开这个分组文件夹")
        b_open.setCursor(Qt.PointingHandCursor)
        b_open.clicked.connect(lambda: open_in_explorer(g.path))
        head.addWidget(icon)
        head.addWidget(title)
        head.addWidget(newt)
        head.addWidget(self.b_all)
        head.addWidget(self.cnt)
        head.addStretch(1)
        head.addWidget(b_open)
        v.addLayout(head)

        self.cols_host = QWidget()
        self.cols_l = QHBoxLayout(self.cols_host)
        self.cols_l.setContentsMargins(0, 0, 0, 0)
        self.cols_l.setSpacing(GAP)
        self.cols_l.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        v.addWidget(self.cols_host)
        self._col_layouts = []

        self.thumbs = [ThumbWidget(fi, gal) for fi in g.files]
        self.set_cols(4)

    def set_cols(self, cols: int):
        """瀑布流布局：按当前列数把缩略图分到最短的一列，避免等宽变高留大洞。"""
        cols = max(1, int(cols))
        self.cols = cols
        # 清空旧列布局（保留控件，仅从布局里摘掉）
        while self.cols_l.count():
            item = self.cols_l.takeAt(0)
            lay = item.layout()
            if lay is not None:
                while lay.count():
                    lay.takeAt(0)
                lay.deleteLater()
        self._col_layouts = []
        heights = [0] * cols
        for _ in range(cols):
            lay = QVBoxLayout()
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(GAP)
            lay.setAlignment(Qt.AlignTop)
            self._col_layouts.append(lay)
            self.cols_l.addLayout(lay)
        for t in self.thumbs:
            k = min(range(cols), key=lambda i: heights[i])
            self._col_layouts[k].addWidget(t, 0, Qt.AlignTop | Qt.AlignHCenter)
            heights[k] += t.height() + GAP
        for lay in self._col_layouts:
            lay.addStretch(1)
        self.cols_l.addStretch(1)

    def update_count(self):
        n_img, n_vid = len(self.group.images), len(self.group.videos)
        self.cnt.setText(f"{n_img} 张图片" + (f" · {n_vid} 个视频" if n_vid else ""))

    def sync_all_btn(self):
        """全选按钮文字跟随本组勾选状态（全选 ⇄ 取消全选）。"""
        ids = [t for t in self.thumbs if not t.isHidden()]
        all_on = bool(ids) and all(t in self.gal._sel_multi for t in ids)
        self.b_all.setText("取消全选" if all_on else "全选")


# ---------------------------------------------------------------- 画廊
class GalleryView(QScrollArea):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cont = QWidget()
        self.v = QVBoxLayout(self.cont)
        self.v.setContentsMargins(16, 14, 16, 22)
        self.v.setSpacing(16)
        self.setWidget(self.cont)

        # 空状态：图标 + 标题 + 说明 + 快捷按钮
        self.empty = QWidget()
        ev = QVBoxLayout(self.empty)
        ev.setContentsMargins(0, 0, 0, 0)
        ev.setAlignment(Qt.AlignCenter)
        card = QFrame()
        card.setObjectName("emptyCard")
        c = QVBoxLayout(card)
        c.setContentsMargins(44, 38, 44, 36)
        c.setSpacing(10)
        c.setAlignment(Qt.AlignCenter)
        icon = QLabel("🖼")
        icon.setObjectName("emptyIcon")
        icon.setAlignment(Qt.AlignCenter)
        icon.setFixedSize(76, 76)
        title = QLabel("开始提取壁纸图片")
        title.setObjectName("emptyTitle")
        title.setAlignment(Qt.AlignCenter)
        text = QLabel("把 scene.pkg / .mpkg / .mkg / .tex 或壁纸文件夹拖进窗口\n"
                      "也可以点击下面按钮选择文件")
        text.setObjectName("emptyText")
        text.setAlignment(Qt.AlignCenter)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.setAlignment(Qt.AlignCenter)
        b_import = QPushButton("＋ 导入壁纸包")
        b_import.setObjectName("primary")
        b_import.setCursor(Qt.PointingHandCursor)
        b_import.clicked.connect(win.pick_files)
        b_open = QPushButton("📂 打开输出目录")
        b_open.setCursor(Qt.PointingHandCursor)
        b_open.clicked.connect(lambda: open_in_explorer(win.outdir))
        btns.addWidget(b_import)
        btns.addWidget(b_open)
        self.empty_out = QLabel()
        self.empty_out.setObjectName("emptyHint")
        self.empty_out.setAlignment(Qt.AlignCenter)
        self._set_empty_out(win.outdir)
        hint = QLabel("支持一次拖入多个，自动排队 · 已提取过的会自动跳过 · GIF / WebP 动图保留动画")
        hint.setObjectName("emptyHint")
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        hint.setMaximumWidth(560)
        c.addWidget(icon, 0, Qt.AlignHCenter)
        c.addWidget(title)
        c.addWidget(text)
        c.addLayout(btns)
        c.addWidget(self.empty_out)
        c.addWidget(hint)
        ev.addWidget(card)
        self.v.addWidget(self.empty, 1, Qt.AlignCenter)
        self.empty.setVisible(False)

        self.no_match = QLabel("没有找到匹配的文件 · 换个关键词，或点「全部」")
        self.no_match.setObjectName("noMatch")
        self.no_match.setAlignment(Qt.AlignCenter)
        self.no_match.setVisible(False)
        self.v.addWidget(self.no_match)

        self.sections, self.groups = [], []
        self.cache = OrderedDict()  # LRU：最近看过的优先保留，超出上限丢弃最旧的
        self._by_key = {}
        self._pending = set()
        self._jobs = {}  # 持有运行中的任务引用，防止被 GC（否则信号可能永远不触发）
        self._sel = None
        self.multi_mode = False
        self._sel_multi = set()  # 多选模式下勾选的缩略图
        # 解码线程限流：快速滚动时不把 CPU 核数全部占满
        tp = QThreadPool.globalInstance()
        tp.setMaxThreadCount(min(THUMB_THREADS, tp.maxThreadCount()))
        self.verticalScrollBar().valueChanged.connect(lambda _: self._load_visible())

    def set_thumb_threads(self, n: int):
        """调整缩略图解码线程数（提取时压低给 RePKG 让路，结束后恢复）。"""
        tp = QThreadPool.globalInstance()
        tp.setMaxThreadCount(max(1, min(int(n), os.cpu_count() or 4)))

    def set_outdir(self, path) -> None:
        self._set_empty_out(path)

    def _set_empty_out(self, path) -> None:
        fm = QFontMetrics(self.empty_out.font())
        self.empty_out.setText(fm.elidedText(f"输出目录：{path}", Qt.ElideMiddle, 520))
        self.empty_out.setToolTip(str(path))

    def set_groups(self, groups, new_names=frozenset(), keep_scroll=False):
        self.groups = groups
        pos = self.verticalScrollBar().value()
        for s in self.sections:
            s.setParent(None)
            s.deleteLater()
        self.sections = []
        self._by_key = {}
        self._sel_multi.clear()  # 旧缩略图控件已销毁，勾选状态清空
        for g in groups:
            s = SectionWidget(g, g.name in new_names, self)
            self.v.insertWidget(self.v.count() - 1, s)
            self.sections.append(s)
            for t in s.thumbs:
                self._by_key[t.key] = t
        for s in self.sections:
            s.b_all.setVisible(self.multi_mode)
        self.win.on_multi_changed()
        self.empty.setVisible(not groups)
        self._sync_visibility()
        if keep_scroll:
            self.verticalScrollBar().setValue(pos)
        QTimer.singleShot(0, self._relayout)
        QTimer.singleShot(150, self._load_visible)

    def _sync_visibility(self):
        """分组筛选 + 文件名搜索的组合可见性；顺便维护空/无匹配提示。"""
        f = self.win.current_filter
        q = getattr(self.win, "search_text", "")
        any_visible = False
        for s, g in zip(self.sections, self.groups):
            group_ok = f is None or g.name == f
            any_thumb = False
            for t in s.thumbs:
                match = (not q) or (q in t.fi.name.lower())
                t.setVisible(group_ok and match)
                any_thumb = any_thumb or match
            vis = group_ok and (any_thumb or not q)
            s.setVisible(vis)
            any_visible = any_visible or vis
        self.no_match.setVisible(bool(self.groups) and not any_visible)
        if f is not None:
            self.verticalScrollBar().setValue(0)
        QTimer.singleShot(0, self._load_visible)

    def apply_filter(self, name):
        self._sync_visibility()

    def apply_search(self, query):
        self._sync_visibility()

    def scroll_to_group(self, name):
        for s, g in zip(self.sections, self.groups):
            if g.name == name:
                self.verticalScrollBar().setValue(max(0, s.y() - 8))
                return

    def remove_file(self, fi: FileInfo) -> bool:
        """删除后原位移除该缩略图，不整页重建（避免界面跳动）。

        返回 True 表示该分组已清空并被移除。
        """
        for s, g in zip(list(self.sections), self.groups):
            if not any(f.path == fi.path for f in g.files):
                continue
            for t in s.thumbs:
                if t.fi.path == fi.path:
                    if self._sel is t:
                        self._sel = None
                    s.thumbs.remove(t)
                    self._by_key.pop(t.key, None)
                    t.setParent(None)
                    t.deleteLater()
                    break
            try:
                g.files.remove(fi)
            except ValueError:
                pass
            if not g.files:
                if self.win.current_filter == g.name:
                    self.win.current_filter = None
                self.sections.remove(s)
                self.groups.remove(g)
                self.win.new_names.discard(g.name)
                s.setParent(None)
                s.deleteLater()
                self.empty.setVisible(not self.groups)
                self._sync_visibility()
                # 组删空的同时把磁盘上的空文件夹也清掉（回收站可反悔）；
                # prune 内部会重查磁盘，提取并发写入新图时不会误删
                if self.win._can_prune() and prune_empty_folder(g.path):
                    self.win._pruned_n += 1
                return True
            s.update_count()
            s.set_cols(s.cols)
            return False
        return False

    def _relayout(self):
        w = self.viewport().width()
        # 页面左右边距 16*2 + 分组卡片内边距 16*2 + 卡片边框/滚动条余量
        avail = max(THUMB_W * 2 + GAP, w - 66)
        cols = max(2, (avail + GAP) // (THUMB_W + GAP))
        for s in self.sections:
            s.set_cols(cols)
        self._load_visible()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(60, self._load_visible)

    def _load_visible(self):
        """懒加载：只渲染视口附近（上下各留约 0.6 屏缓冲）的缩略图，
        滚动到哪个文件夹才加载哪个，未显示的不渲染。"""
        if not self.isVisible() or not self.sections:
            return
        lay = self.cont.layout()
        if lay is not None:
            lay.activate()  # 强制完成布局，保证取到的是真实坐标
        sb = self.verticalScrollBar().value()
        vh = self.viewport().height()
        top, bottom = sb - vh * 0.6, sb + vh * 1.6
        for s in self.sections:
            if not s.isVisible():
                continue
            sy = s.mapTo(self.cont, QPoint(0, 0)).y()
            if sy > bottom or sy + s.height() < top:
                continue  # 整组都在视口外，跳过
            for t in s.thumbs:
                if t.fi.is_video or not t.isVisible():
                    continue  # 搜索过滤掉的缩略图不排队解码
                y = t.mapTo(self.cont, QPoint(0, 0)).y()
                if top <= y <= bottom:
                    self.load_thumb(t)

    # ---- 缩略图
    def load_thumb(self, tw: ThumbWidget):
        if tw.key in self.cache:
            self.cache.move_to_end(tw.key)
            pm, w, h = self.cache[tw.key]
            tw.set_pixmap(pm, w, h)
            return
        if tw.key in self._pending:
            return
        self._pending.add(tw.key)
        job = _ThumbJob(tw.fi.path, tw.key, THUMB_W, tw.img_h, tw.fi.mtime)
        job.sig.done.connect(self.on_thumb)
        self._jobs[tw.key] = job
        QThreadPool.globalInstance().start(job)

    def on_thumb(self, key, img: QImage, w, h):
        self._pending.discard(key)
        self._jobs.pop(key, None)
        if img.isNull():
            tw = self._by_key.get(key)
            if tw:
                tw.set_failed()
            return
        pm = QPixmap.fromImage(img)
        self.cache[key] = (pm, w, h)
        self.cache.move_to_end(key)
        while len(self.cache) > 400:  # 只留最近看过的，内存稳定在几十 MB
            self.cache.popitem(last=False)
        tw = self._by_key.get(key)
        if tw:
            tw.set_pixmap(pm, w, h)

    # ---- 选择 / 菜单 / 预览
    def select(self, tw: ThumbWidget):
        if self._sel:
            self._sel.set_selected(False)
        self._sel = tw
        tw.set_selected(True)
        self.win.set_status_file(tw.fi)

    def selected_file(self):
        return self._sel.fi if self._sel is not None else None

    # ---- 多选模式
    def set_multi(self, on: bool):
        if on and self._sel is not None:
            self._sel.set_selected(False)
            self._sel = None
        self.multi_mode = on
        if not on:
            for t in self._sel_multi:
                t.set_selected(False)
                t.set_checked(False)
            self._sel_multi.clear()
        for s in self.sections:
            s.b_all.setVisible(on)
            s.sync_all_btn()
            if on:
                for t in s.thumbs:  # 多选时收起悬停快捷操作，避免误触
                    t._set_hover(False)
        self.win.on_multi_changed()

    def toggle_multi(self, tw: ThumbWidget):
        on = tw not in self._sel_multi
        if on:
            self._sel_multi.add(tw)
        else:
            self._sel_multi.discard(tw)
        tw.set_selected(on)
        tw.set_checked(on)
        for s in self.sections:
            if tw in s.thumbs:
                s.sync_all_btn()
                break
        self.win.on_multi_changed()

    def select_all_section(self, s: SectionWidget):
        ids = [t for t in s.thumbs if not t.isHidden()]
        all_on = bool(ids) and all(t in self._sel_multi for t in ids)
        for t in ids:
            if all_on:
                self._sel_multi.discard(t)
                t.set_selected(False)
                t.set_checked(False)
            else:
                self._sel_multi.add(t)
                t.set_selected(True)
                t.set_checked(True)
        s.sync_all_btn()
        self.win.on_multi_changed()

    def select_all_visible(self):
        """Ctrl+A：全选当前可见（筛选后）分组的全部文件。"""
        for s in self.sections:
            if not s.isVisible():
                continue
            for t in s.thumbs:
                if t.isHidden():
                    continue
                self._sel_multi.add(t)
                t.set_selected(True)
                t.set_checked(True)
        for s in self.sections:
            s.sync_all_btn()
        self.win.on_multi_changed()

    def sel_files(self):
        return [t.fi for t in self._sel_multi]

    def remove_files(self, files) -> bool:
        """批量原位移除（只重排受影响的分组，不整页重建，避免跳动）。

        返回 True 表示有分组被删空并移除。
        """
        dead = {fi.path for fi in files}
        group_gone = False
        for s, g in zip(list(self.sections), list(self.groups)):
            if not any(f.path in dead for f in g.files):
                continue
            for t in [x for x in s.thumbs if x.fi.path in dead]:
                if self._sel is t:
                    self._sel = None
                self._sel_multi.discard(t)
                s.thumbs.remove(t)
                self._by_key.pop(t.key, None)
                t.setParent(None)
                t.deleteLater()
            g.files = [f for f in g.files if f.path not in dead]
            if not g.files:
                group_gone = True
                if self.win.current_filter == g.name:
                    self.win.current_filter = None
                self.sections.remove(s)
                self.groups.remove(g)
                self.win.new_names.discard(g.name)
                s.setParent(None)
                s.deleteLater()
                if self.win._can_prune() and prune_empty_folder(g.path):
                    self.win._pruned_n += 1
            else:
                s.update_count()
                s.set_cols(s.cols)
                s.sync_all_btn()
        if group_gone:
            self.empty.setVisible(not self.groups)
            self._sync_visibility()
        self.win.on_multi_changed()  # 勾选集合已变化，同步底部操作条
        return group_gone

    def show_file_menu(self, fi: FileInfo, pos):
        m = QMenu(self)
        if fi.is_video:
            m.addAction("▶ 用默认播放器打开", lambda: os.startfile(fi.path))
            m.addAction("📂 打开所在位置", lambda: open_in_explorer(fi.path))
        else:
            m.addAction("⛶ 全屏翻看", lambda: self.request_preview(fi))
            m.addAction("🖥 设为桌面壁纸", lambda: self.win.do_set_wallpaper(fi))
            m.addAction("🖼 用系统看图打开", lambda: os.startfile(fi.path))
            m.addAction("💾 另存为…", lambda: self.win.do_save_as(fi))
            m.addAction("📋 复制文件", lambda: self.win.do_copy(fi))
            m.addAction("📂 打开所在位置", lambda: open_in_explorer(fi.path))
            m.addSeparator()
            m.addAction("🗑 删除（移入回收站）", lambda: self.win.do_delete(fi))
        m.exec(pos)

    def request_preview(self, fi: FileInfo):
        items = []
        for s in self.sections:
            if not s.isVisible():
                continue
            items += [t.fi for t in s.thumbs if not t.fi.is_video]
        try:
            idx = [x.path for x in items].index(fi.path)
        except ValueError:
            idx = 0
        dlg = PreviewDialog(self.win, items, idx)
        dlg.showFullScreen()
        dlg.exec()


# ---------------------------------------------------------------- 拖放覆盖层
class DragOverlay(QWidget):
    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAcceptDrops(False)
        self.hide()
        self.n, self.sample = 0, ""

    def set_info(self, n, sample):
        self.n, self.sample = n, sample
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.fillRect(self.rect(), QColor(6, 8, 12, 205))
        r = self.rect().adjusted(16, 16, -16, -16)
        p.setPen(QPen(QColor("#6f8cff"), 2, Qt.DashLine))
        p.setBrush(QColor(18, 22, 30, 245))
        p.drawRoundedRect(r, 18, 18)
        p.setPen(QColor("#ffffff"))
        f = p.font()
        f.setPixelSize(42)
        p.setFont(f)
        p.drawText(self.rect().adjusted(0, -118, 0, -118), Qt.AlignCenter, "⬇")
        f.setPixelSize(22)
        f.setBold(True)
        p.setFont(f)
        p.drawText(self.rect().adjusted(0, -58, 0, -58), Qt.AlignCenter,
                   f"松手开始提取（{self.n} 项）")
        f.setPixelSize(13)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor("#98a3b8"))
        p.drawText(self.rect().adjusted(0, -16, 0, -16), Qt.AlignCenter, self.sample)
        p.drawText(self.rect().adjusted(0, 32, 0, 32), Qt.AlignCenter,
                   "支持一次拖入多个，自动排队 · 已提取过的自动跳过")


# ---------------------------------------------------------------- 全屏预览
class PreviewDialog(QDialog):
    def __init__(self, win: "MainWindow", items, idx):
        super().__init__(None)
        self.setObjectName("fsbg")
        self.win, self.items, self.idx = win, items, idx

        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(12)

        top = QFrame()
        top.setObjectName("fsTop")
        th = QHBoxLayout(top)
        th.setContentsMargins(14, 9, 10, 9)
        th.setSpacing(10)
        self.idxlab = QLabel("1 / 1")
        self.idxlab.setObjectName("fsIndex")
        self.namlab = QLabel("")
        self.namlab.setObjectName("fsName")
        self.metalab = QLabel("")
        self.metalab.setObjectName("fsMeta")
        b_close = QToolButton()
        b_close.setText("✕")
        b_close.setObjectName("fsClose")
        b_close.setToolTip("关闭（Esc）")
        b_close.setCursor(Qt.PointingHandCursor)
        b_close.clicked.connect(self.close)
        th.addWidget(self.idxlab)
        th.addWidget(self.namlab)
        th.addStretch(1)
        th.addWidget(self.metalab)
        th.addSpacing(4)
        th.addWidget(b_close)
        v.addWidget(top)

        self.imgl = QLabel()
        self.imgl.setAlignment(Qt.AlignCenter)
        self.imgl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.imgl.setMinimumSize(240, 160)
        shadow = QGraphicsDropShadowEffect(self.imgl)
        shadow.setBlurRadius(46)
        shadow.setOffset(0, 12)
        shadow.setColor(QColor(0, 0, 0, 185))
        self.imgl.setGraphicsEffect(shadow)
        v.addWidget(self.imgl, 1)

        bar = QFrame()
        bar.setObjectName("fsBar")
        bh = QHBoxLayout(bar)
        bh.setContentsMargins(10, 8, 14, 8)
        bh.setSpacing(6)
        b_prev = QToolButton()
        b_prev.setText("‹")
        b_prev.setObjectName("fsNav")
        b_prev.setToolTip("上一张（←）")
        b_prev.setCursor(Qt.PointingHandCursor)
        b_prev.clicked.connect(self.prev)
        b_next = QToolButton()
        b_next.setText("›")
        b_next.setObjectName("fsNav")
        b_next.setToolTip("下一张（→）")
        b_next.setCursor(Qt.PointingHandCursor)
        b_next.clicked.connect(self.next)
        b_wp = QPushButton("🖥 设为桌面壁纸")
        b_wp.setObjectName("primary")
        b_wp.setCursor(Qt.PointingHandCursor)
        b_wp.clicked.connect(self.set_wp)
        b_sys = QPushButton("🖼 系统看图打开")
        b_sys.setCursor(Qt.PointingHandCursor)
        b_sys.clicked.connect(self.open_sys)
        b_save = QPushButton("💾 另存为…")
        b_save.setCursor(Qt.PointingHandCursor)
        b_save.clicked.connect(self.save_as)
        b_loc = QPushButton("📂 打开所在位置")
        b_loc.setCursor(Qt.PointingHandCursor)
        b_loc.clicked.connect(self.open_loc)
        hint = QLabel("← → 切换 · Esc 关闭 · 右键更多操作")
        hint.setObjectName("fsHint")
        bh.addWidget(b_prev)
        bh.addWidget(b_next)
        bh.addSpacing(6)
        bh.addWidget(b_wp)
        bh.addWidget(b_sys)
        bh.addWidget(b_save)
        bh.addWidget(b_loc)
        bh.addStretch(1)
        bh.addWidget(hint)
        v.addWidget(bar)

        for key, fn in ((Qt.Key_Left, self.prev), (Qt.Key_Right, self.next),
                        (Qt.Key_Escape, self.close)):
            sc = QShortcut(QKeySequence(key), self)
            sc.activated.connect(fn)
        self._refresh()

    def current(self):
        return self.items[self.idx] if self.items else None

    def prev(self):
        self.idx = (self.idx - 1) % len(self.items)
        self._refresh()

    def next(self):
        self.idx = (self.idx + 1) % len(self.items)
        self._refresh()

    def _refresh(self):
        fi = self.current()
        if fi is None:
            return
        avail = self.imgl.size()
        if avail.width() < 50:
            avail = QSize(int(self.width() * 0.9), int(self.height() * 0.8))
        r = QImageReader(fi.path)
        r.setAutoTransform(True)
        size = r.size()
        w, h = size.width(), size.height()
        fmt = bytes(r.format()).lower()
        jpeg_scaled = False
        if fmt in (b"jpeg", b"jpg") and w > 0 and h > 0:
            # JPEG 支持解码中途缩小，直接按屏幕尺寸解码，超大图翻页不再卡顿
            scale = min(avail.width() / w, avail.height() / h, 1.0)
            if scale < 1.0:
                r.setScaledSize(QSize(max(1, round(w * scale)),
                                      max(1, round(h * scale))))
                jpeg_scaled = True
        img = r.read()
        if not img.isNull():
            if not jpeg_scaled:
                img = img.scaled(avail, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
            self.imgl.setPixmap(QPixmap.fromImage(img))
            fm = QFontMetrics(self.namlab.font())
            self.namlab.setText(fm.elidedText(fi.name, Qt.ElideMiddle, 520))
            self.namlab.setToolTip(fi.name)
            self.metalab.setText(f"{w}×{h} · {human_size(fi.size)}")
            self.idxlab.setText(f"{self.idx + 1} / {len(self.items)}")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh()

    def set_wp(self):
        fi = self.current()
        if fi:
            self.win.do_set_wallpaper(fi)

    def save_as(self):
        fi = self.current()
        if fi:
            self.win.do_save_as(fi)

    def open_loc(self):
        fi = self.current()
        if fi:
            open_in_explorer(fi.path)

    def open_sys(self):
        fi = self.current()
        if fi and Path(fi.path).exists():
            os.startfile(fi.path)

    def contextMenuEvent(self, e):
        fi = self.current()
        if not fi:
            return
        self.win.gal.show_file_menu(fi, e.globalPos())
        alive = [x for x in self.items if Path(x.path).exists()]
        if len(alive) != len(self.items):
            self.items = alive
            if not self.items:
                self.close()
                return
            self.idx = min(self.idx, len(self.items) - 1)
            self._refresh()


class SkipDialog(QDialog):
    """「已提取过」汇总弹窗：整批提取结束后弹出一次，与主界面同款主题。"""

    def __init__(self, win: "MainWindow", names, folders):
        super().__init__(win)
        self.setObjectName("skipdlg")
        self.win = win
        self.folders = folders
        self.setWindowTitle("已提取过")

        v = QVBoxLayout(self)
        v.setContentsMargins(20, 18, 20, 16)
        v.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(8)
        icon = QLabel("⊘")
        icon.setObjectName("skipdlgIcon")
        title = QLabel("已提取过")
        title.setObjectName("skipdlgTitle")
        head.addWidget(icon)
        head.addWidget(title)
        head.addStretch(1)
        v.addLayout(head)

        msg = QLabel(f"本次有 {len(names)} 个包之前已经提取过，已自动跳过：")
        msg.setTextFormat(Qt.PlainText)
        msg.setWordWrap(True)
        v.addWidget(msg)

        panel = QFrame()
        panel.setObjectName("skipdlgList")
        lv = QVBoxLayout(panel)
        lv.setContentsMargins(14, 12, 14, 12)
        lv.setSpacing(6)
        for n in names:
            row = QLabel("⊘  " + n)
            row.setTextFormat(Qt.PlainText)
            row.setObjectName("skipdlgItem")
            lv.addWidget(row)
        lv.addStretch(1)
        if len(names) > 6:
            holder = QScrollArea()
            holder.setWidgetResizable(True)
            holder.setWidget(panel)
            holder.setFixedHeight(232)
            v.addWidget(holder, 1)
        else:
            v.addWidget(panel)

        bar = QHBoxLayout()
        bar.setSpacing(10)
        bar.addStretch(1)
        if folders:
            b_open = QPushButton("📂 打开所在文件夹" if len(folders) == 1
                                 else "📂 打开输出目录")
            b_open.setObjectName("primary")
            b_open.clicked.connect(self._open)
            bar.addWidget(b_open)
        b_ok = QPushButton("知道了")
        b_ok.setDefault(True)
        b_ok.clicked.connect(self.accept)
        bar.addWidget(b_ok)
        v.addLayout(bar)

        self.setFixedWidth(440)

    def showEvent(self, e):
        super().showEvent(e)
        # 与主窗口一致的原生标题栏配色
        _win_title_bar(int(self.winId()), self.win.dark,
                       "#0f1218" if self.win.dark else "#f3f5fa",
                       "#e6eaf2" if self.win.dark else "#151b26")

    def _open(self):
        d = self.folders[0] if len(self.folders) == 1 else str(self.win.outdir)
        try:
            os.startfile(d)
        except Exception:
            pass
        self.accept()


# ---------------------------------------------------------------- 提取引擎调度
class _EngineWorker:
    """一条并行提取通道 = 一个 PowerShell 批进程及其归属状态。
    多个 worker 同时跑，各占一个核解压不同的包（RePKG 单线程，多包并行才能吃满多核）；
    调度规则保证并行进程之间不会写同一个输出目录。"""
    __slots__ = ("proc", "batch", "buf", "cur", "names", "folders", "paths")

    def __init__(self):
        self.proc = None
        self.batch = []   # 本 worker 批内的条目下标
        self.buf = b""
        self.cur = -1     # 当前正在出日志的条目下标
        self.names = set()    # 本批会写的输出子目录名（小写）
        self.folders = set()  # 本批文件夹条目的规范化前缀
        self.paths = set()    # 本批全部条目的规范化路径


class ExtractionManager(QObject):
    log_line = Signal(str)
    queue_changed = Signal()
    item_done = Signal(object)
    all_done = Signal(bool)

    # 一个 PowerShell 进程处理一批包，省去每个包一次的进程启动开销。
    # 上限防止命令行超过 Windows 32767 字符限制（长路径全中文时一个约 200+ 字符）。
    BATCH_MAX_ITEMS = 40
    BATCH_MAX_CHARS = 20000
    # FAST=True 时给引擎传 -Fast：RePKG 不降优先级，且引擎退出时不清扫全局临时目录
    # （并行 worker 在跑时全局清扫会删掉别的 worker 正在用的目录）。
    FAST = True

    def __init__(self, cfg=None):
        super().__init__()
        self.cfg = cfg if cfg is not None else {}
        self.parallel = resolve_parallel(self.cfg)  # 自适应/可配置并行度
        self.workers = []   # 在跑的 _EngineWorker
        self.items = []
        self.running = False
        self.cancelled = False
        self.outdir_arg = ""
        self._seen = set()  # 队列内已出现过的规范化路径（拖重复包时直接跳过）

    # ---- 并行度
    def set_parallel(self, value):
        """0=自动，1..MAX_PARALLEL=固定；返回当前生效值。

        调大后立即补开 worker；调小只影响后续补位（在跑的批次先跑完，
        避免中途 kill 造成半成品输出）。
        """
        try:
            v = max(0, min(MAX_PARALLEL, int(value)))
        except (TypeError, ValueError):
            v = 0
        self.cfg["parallel"] = v
        self.parallel = resolve_parallel(self.cfg)
        if self.running:
            self._pump()
        return self.parallel

    def enqueue(self, paths):
        self.cancelled = False
        for p in paths:
            pp = Path(p)
            key = _norm_key(str(pp))
            it = {"path": str(pp), "name": pp.name or str(pp),
                  "state": "wait", "imgs": 0, "vids": 0,
                  "skipped": False, "fail": False, "hdr": False,
                  "dup": False, "retry": 0, "cost": _item_cost(pp)}
            if key in self._seen:
                # 队列里重复拖入的同一个包：直接标跳过，不再传给引擎
                it["state"], it["dup"] = "skip", True
                self.log_line.emit(
                    f'<span style="color:#f0b44c">[跳过]</span> '
                    f"队列里重复的包，跳过: {_html_escape(it['name'])}")
            else:
                self._seen.add(key)
            self.items.append(it)
        self.queue_changed.emit()
        # 提取进行中也可以继续喂新包：有空闲 worker 就立刻开工，
        # 但没有空闲通道时只排队，避免多个进程抢同一个输出目录
        self._pump()

    def reset_queue(self):
        """整批处理结束后清空队列（由界面在 all_done 后调用）。"""
        self.items = []
        self._seen.clear()

    def _out_name(self, it):
        """推导引擎会用的输出子目录名（与引擎规则一致），用于判断写冲突。"""
        return _out_dir_name(it["path"])

    def _wait_idxs(self):
        return [i for i, it in enumerate(self.items) if it["state"] == "wait"]

    def _pump(self):
        """调度入口：有空闲通道就找下一批可安全并行的条目，直到排满或没得排。"""
        if self.cancelled:
            if not self.workers:
                self.running = False
                self.all_done.emit(True)
            return
        if not self.items:
            return
        if not ENGINE_PS1.exists():
            for it in self.items:
                if it["state"] == "wait":
                    it["state"] = "fail"
            self.log_line.emit('<span style="color:#ff6b6b">[错误]</span> '
                               f"未找到提取引擎：{ENGINE_PS1}（请把本程序放在 wall 文件夹里）")
            self.queue_changed.emit()
            self.running = False
            self.all_done.emit(False)
            return
        while len(self.workers) < self.parallel:
            batch = self._take_batch()
            if not batch:
                break
            self._spawn(batch)
        if self.workers:
            self.running = True
            return
        if self._wait_idxs():
            # 兜底：有等待项却排不出任何批次（理论上不会发生），标失败避免卡死
            for i in self._wait_idxs():
                self.items[i]["state"] = "fail"
            self.queue_changed.emit()
        self.running = False
        self.all_done.emit(False)

    def _take_batch(self):
        """从等待项里挑一批互不冲突的条目（同输出目录/嵌套路径的放同批串行）。

        返回条目下标列表；当前剩余条目都和活跃 worker 冲突时返回 []，
        等某个 worker 收工后再次调度。"""
        wait = self._wait_idxs()
        if not wait:
            return []
        # 预取每条目的（规范化路径, 是否文件夹, 输出子目录名），避免 O(n²) 里反复 stat
        specs = {}
        for i in wait:
            it = self.items[i]
            specs[i] = (_norm_key(it["path"]), Path(it["path"]).is_dir(),
                        self._out_name(it))
        # 冲突关系做连通分量：同分量必须同一批（由同一个引擎进程串行处理）
        n = len(wait)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def conflict(i, j):
            (ka, da, na), (kb, db, nb) = specs[i], specs[j]
            if na == nb:
                return True
            if da and kb.startswith(ka.rstrip("\\") + "\\"):
                return True
            if db and ka.startswith(kb.rstrip("\\") + "\\"):
                return True
            return False

        for a in range(n):
            for b in range(a + 1, n):
                if conflict(wait[a], wait[b]):
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[rb] = ra
        comps = {}
        for k, idx in enumerate(wait):
            comps.setdefault(find(k), []).append(idx)
        # 大包优先（LPT）：同批容量有限，先装大件能压缩长尾
        groups = sorted(comps.values(),
                        key=lambda g: -max(self.items[i]["cost"] for i in g))
        for g in groups:
            g = sorted(g)  # 批内保持拖入顺序
            batch, chars = [], 0
            for i in g:
                add = len(self.items[i]["path"]) + 4
                if batch and (len(batch) >= self.BATCH_MAX_ITEMS
                              or chars + add > self.BATCH_MAX_CHARS):
                    break
                batch.append(i)
                chars += add
            if batch and not self._conflicts_active(batch):
                return batch
        return []

    def _conflicts_active(self, batch):
        """这批条目是否会和一个正在跑的 worker 写同一个输出目录。"""
        for w in self.workers:
            for i in batch:
                it = self.items[i]
                key = _norm_key(it["path"])
                if self._out_name(it) in w.names:
                    return True
                for f in w.folders:  # 候选位于活跃文件夹条目之内
                    if key.startswith(f):
                        return True
                if Path(it["path"]).is_dir():  # 候选文件夹包含活跃条目的路径
                    prefix = key.rstrip("\\") + "\\"
                    if any(p.startswith(prefix) for p in w.paths):
                        return True
        return False

    def _spawn(self, batch):
        w = _EngineWorker()
        w.batch = list(batch)
        w.cur = batch[0]
        for j in batch:
            it = self.items[j]
            it["state"] = "run"
            key = _norm_key(it["path"])
            w.paths.add(key)
            w.names.add(self._out_name(it))
            if Path(it["path"]).is_dir():
                w.folders.add(key.rstrip("\\") + "\\")
        # 用 -Command 包一层并强制引擎输出 UTF-8：默认 GBK 下 [图片] 等标记
        # 可能恰好是合法 UTF-8 而被解成乱码，导致新增计数丢失、界面不刷新。
        # （主机与 RePKG 的优先级由引擎 -Fast 控制，见类头注释）
        q = lambda s: s.replace("'", "''")
        cmd = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
               f"& '{q(str(ENGINE_PS1))}' -NoOpen")
        if self.FAST:
            cmd += " -Fast"
        if self.outdir_arg:
            cmd += f" -OutDir '{q(self.outdir_arg)}'"
        for j2 in batch:
            cmd += f" '{q(self.items[j2]['path'])}'"
        w.proc = QProcess(self)
        w.proc.setWorkingDirectory(str(APP_DIR))
        w.proc.readyReadStandardOutput.connect(lambda *a, wk=w: self._read(wk))
        # finished 信号带 (exitCode, exitStatus) 参数，用 *a 吞掉，worker 走关键字默认值
        w.proc.finished.connect(lambda *a, wk=w: self._done(wk))
        w.proc.errorOccurred.connect(lambda err, wk=w: self._err(err, wk))
        w.proc.start("powershell.exe", ["-NoProfile", "-NonInteractive",
                                        "-ExecutionPolicy", "Bypass",
                                        "-Command", cmd])
        self.workers.append(w)

    def _read(self, w):
        w.buf += bytes(w.proc.readAllStandardOutput())
        while b"\n" in w.buf:
            raw, w.buf = w.buf.split(b"\n", 1)
            self._emit(w, decode_line(raw))

    def _emit(self, w, text):
        if not text.strip():
            return
        # 批处理模式下引擎每开始处理一个包/文件夹/纹理都会打印 "处理: 路径"，
        # 以此把后续日志归属到对应条目（路径可能是队列里文件夹的子路径）
        if text.startswith("处理: "):
            self._switch_to(w, text[len("处理: "):].strip(), mark=True)
        elif text.startswith("[错误] 找不到路径: "):
            self._switch_to(w, text[len("[错误] 找不到路径: "):].strip(), mark=False)
        it = self.items[w.cur] if 0 <= w.cur < len(self.items) else None
        if it is not None:
            if "[图片]" in text:
                it["imgs"] += 1
            if "[视频]" in text:
                it["vids"] += 1
            if "[跳过]" in text:
                it["skipped"] = True
                # 引擎对"已提取过"有专门的提示文案，与"纹理里没图片"区分开，
                # 只有前者才算重复提取，供完成后的汇总弹窗使用
                if "已提取过了" in text:
                    it["dup"] = True
            if "[错误]" in text:
                it["fail"] = True
        self.log_line.emit(html_line(text))

    def _finish_item(self, j):
        it = self.items[j]
        if it["state"] in ("done", "skip", "fail", "cancel"):
            return
        if self.cancelled:
            it["state"] = "cancel"
        elif it["fail"] and not (it["imgs"] or it["vids"]):
            it["state"] = "fail"
        elif it["skipped"]:
            it["state"] = "skip"
        else:
            it["state"] = "done"
        self.queue_changed.emit()
        self.item_done.emit(it)

    def _match_item(self, w, eng_path):
        """把引擎打印的路径对应到本批条目：精确匹配文件，或前缀匹配文件夹（取最长）。"""
        p = os.path.normcase(eng_path.replace("/", "\\"))
        best, best_len = None, -1
        for j in w.batch:
            ip = os.path.normcase(self.items[j]["path"].replace("/", "\\"))
            if p == ip:
                return j
            if p.startswith(ip.rstrip("\\") + "\\") and len(ip) > best_len:
                best, best_len = j, len(ip)
        return best

    def _switch_to(self, w, eng_path, mark):
        j = self._match_item(w, eng_path)
        if j is None:
            return
        # 只有确实轮到它（已输出过自己的 "处理:" 头）的条目才在切换时收尾；
        # 还没轮到的条目（比如包在引擎里排在散图文件夹之后）先让位，
        # 等引擎处理到它时再归属，避免提前按 0 张定稿
        if j != w.cur:
            if 0 <= w.cur < len(self.items) and self.items[w.cur]["hdr"]:
                self._finish_item(w.cur)
            w.cur = j
            if self.items[j]["state"] == "wait":
                self.items[j]["state"] = "run"
            self.queue_changed.emit()
        if mark:
            self.items[j]["hdr"] = True

    def _done(self, w):
        crashed = False
        if w.proc:
            w.buf += bytes(w.proc.readAllStandardOutput())
            if w.buf.strip():
                self._emit(w, decode_line(w.buf))
            w.buf = b""
            # 进程异常退出（崩溃/被杀）时，本批没跑完的包不能谎报成完成；
            # 先自动重试一次，第二次仍异常才标失败让用户重新拖入
            crashed = w.proc.exitStatus() != QProcess.NormalExit
            w.proc.deleteLater()
            w.proc = None
        if crashed and not self.cancelled:
            self.log_line.emit('<span style="color:#ff6b6b">[错误]</span> '
                               '提取进程异常退出，本批未完成的包将重试一次')
        retried = set()
        if 0 <= w.cur < len(self.items):
            j = w.cur
            it = self.items[j]
            if self.cancelled:
                it["state"] = "cancel"
            elif crashed and it["state"] == "run":
                self._retry_item(j, retried)
            else:
                self._finish_item(j)
        w.cur = -1
        # 批内从未被引擎输出过的残留条目（如文件夹里的包已作为单独条目提取过）
        # 也按同一套规则收尾；异常退出时先重试一次，重试过再标失败
        for j in w.batch:
            if j in retried:
                continue
            it = self.items[j]
            if it["state"] not in ("wait", "run"):
                continue
            if self.cancelled:
                it["state"] = "cancel"
            elif crashed:
                self._retry_item(j, retried)
            else:
                self._finish_item(j)
        if retried:
            names = "、".join(self.items[j]["name"] for j in sorted(retried)[:3])
            more = f" 等 {len(retried)} 项" if len(retried) > 3 else ""
            self.log_line.emit('<span style="color:#f0b44c">[提示]</span> '
                               f"正在重试失败批次：{names}{more}")
        if w in self.workers:
            self.workers.remove(w)
        self._pump()

    def _retry_item(self, j, retried):
        """异常退出后把条目重置回等待队列；每个条目最多重试一次。"""
        it = self.items[j]
        if it["retry"] < 1:
            it["retry"] += 1
            it["state"] = "wait"
            it["hdr"] = False
            it["imgs"] = it["vids"] = 0
            it["skipped"] = it["fail"] = False
            retried.add(j)
            self.queue_changed.emit()
        else:
            it["state"] = "fail"
            self.queue_changed.emit()
            self.item_done.emit(it)

    def _err(self, err, w):
        if err != QProcess.FailedToStart:
            return  # 运行中崩溃等已触发 finished，由 _done 统一收尾
        self.log_line.emit(f'<span style="color:#ff6b6b">[错误]</span> '
                           f"无法启动 powershell.exe：{err}")
        for j in w.batch:
            it = self.items[j]
            if it["state"] in ("wait", "run"):
                it["state"] = "fail"
        if w in self.workers:
            self.workers.remove(w)
        self.queue_changed.emit()
        self._pump()

    def cancel(self):
        self.cancelled = True
        for w in self.workers:
            if w.proc and w.proc.state() != QProcess.NotRunning:
                w.proc.kill()
        for it in self.items:
            if it["state"] in ("wait", "run"):
                it["state"] = "cancel"
        self.queue_changed.emit()
        if not self.workers:
            self.running = False
            self.all_done.emit(True)


# ---------------------------------------------------------------- 主窗口
class MainWindow(QMainWindow):
    def __init__(self, cfg, demo_groups=None):
        super().__init__()
        self.cfg = cfg
        self.dark = cfg.get("theme", "dark") == "dark"
        self.outdir = Path(cfg.get("outdir") or DEFAULT_OUT)
        self.current_filter = None
        self.groups = []
        self.new_names = set()
        self.recents = cfg.get("recents", [])
        self._pending_focus = None
        self._prev_map = {}
        self._pruned_n = 0  # 本轮删除顺手清掉的空文件夹数，由状态栏统一汇报
        self._drag_paths, self._drag_ignored = [], 0
        self._chips = []
        self.search_text = ""  # 文件名搜索关键词（小写）

        self.setWindowTitle("壁纸提取器")
        icon = self._load_app_icon()
        self.setWindowIcon(icon if icon is not None else self._make_icon())
        self.setAcceptDrops(True)
        self.setMinimumSize(880, 560)
        geo = cfg.get("geometry")
        if geo:
            try:
                self.restoreGeometry(bytes.fromhex(geo))
            except Exception:
                self.resize(1180, 720)
        else:
            self.resize(1180, 720)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # ── 顶部：品牌 + 主操作 + 主题/设置
        tb = QWidget()
        tb.setObjectName("Header")
        h = QHBoxLayout(tb)
        h.setContentsMargins(18, 11, 18, 11)
        h.setSpacing(8)
        logo = QLabel("W")
        logo.setObjectName("logo")
        logo.setAlignment(Qt.AlignCenter)
        logo.setFixedSize(34, 34)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        title = QLabel("壁纸提取器")
        title.setObjectName("brandTitle")
        sub = QLabel("Wallpaper Engine 图片提取")
        sub.setObjectName("brandSub")
        brand.addWidget(title)
        brand.addWidget(sub)
        self.brand_sub = sub
        h.addWidget(logo)
        h.addSpacing(2)
        h.addLayout(brand)
        h.addStretch(1)

        self.b_recent = QToolButton()
        self.b_recent.setText("🕘 最近提取")
        self.b_recent.setToolTip("最近提取过的壁纸")
        self.b_recent.setPopupMode(QToolButton.InstantPopup)
        b_out = QPushButton("📂 输出目录")
        b_out.setCursor(Qt.PointingHandCursor)
        b_out.setToolTip("打开输出目录")
        b_out.clicked.connect(lambda: open_in_explorer(self.outdir))
        self.b_multi = QPushButton("☑ 多选")
        self.b_multi.setObjectName("multiBtn")
        self.b_multi.setCheckable(True)
        self.b_multi.setCursor(Qt.PointingHandCursor)
        self.b_multi.setToolTip("多选模式：单击缩略图勾选，可批量删除；Esc 或「完成」退出")
        self.b_multi.toggled.connect(self.set_multi_mode)
        b_add = QPushButton("＋ 导入壁纸包")
        b_add.setObjectName("primary")
        b_add.setCursor(Qt.PointingHandCursor)
        b_add.setToolTip("选择 scene.pkg / .mpkg / .mkg / .tex 文件（Ctrl+O）")
        b_add.clicked.connect(self.pick_files)
        self.b_theme = QToolButton()
        self.b_theme.setObjectName("iconBtn")
        self.b_theme.setToolTip("切换 深色 / 浅色 主题")
        self.b_theme.setCursor(Qt.PointingHandCursor)
        self.b_theme.clicked.connect(lambda: self.set_theme(not self.dark))
        b_cfg = QToolButton()
        b_cfg.setObjectName("iconBtn")
        b_cfg.setText("⚙")
        b_cfg.setToolTip("设置")
        b_cfg.setCursor(Qt.PointingHandCursor)
        b_cfg.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(b_cfg)
        self.act_dark = m.addAction("深色主题")
        self.act_dark.setCheckable(True)
        self.act_light = m.addAction("浅色主题")
        self.act_light.setCheckable(True)
        m.addSeparator()
        m.addAction("更改输出目录…", self.change_outdir)
        m.addAction("恢复默认输出目录", self.reset_outdir)
        m.addSeparator()
        pm = m.addMenu("并行提取数")
        self._par_actions = []
        for label, val in ([("自动（按核数/内存）", 0)] +
                           [(str(i), i) for i in range(1, MAX_PARALLEL + 1)]):
            a = pm.addAction(label)
            a.setCheckable(True)
            a.triggered.connect(lambda _=False, v=val: self.set_parallel(v))
            self._par_actions.append((val, a))
        m.addSeparator()
        m.addAction("🧹 清理多余的文件夹进程", self.cleanup_explorer)
        self.act_sched = m.addAction("🕒 定时清理空壳（每 30 分钟计划任务）")
        self.act_sched.setCheckable(True)
        self.act_sched.setChecked(bool(self.cfg.get("sched_clean")))
        self.act_sched.toggled.connect(self.toggle_sched_clean)
        m.addSeparator()
        self.act_log = m.addAction("显示提取日志")
        self.act_log.setCheckable(True)
        self.act_dark.triggered.connect(lambda: self.set_theme(True))
        self.act_light.triggered.connect(lambda: self.set_theme(False))
        self.act_log.toggled.connect(lambda on: self.toggle_log(on))
        b_cfg.setMenu(m)

        for w in (self.b_recent, b_out, self.b_multi, b_add):
            h.addWidget(w)
        h.addSpacing(2)
        h.addWidget(self.b_theme)
        h.addWidget(b_cfg)
        v.addWidget(tb)

        # ── 筛选栏：分组标签 + 文件名搜索 + 拖放提示
        chips_row = QWidget()
        chips_row.setObjectName("FilterBar")
        crow = QHBoxLayout(chips_row)
        crow.setContentsMargins(18, 8, 18, 8)
        crow.setSpacing(10)
        self.chips_scroll = QScrollArea()
        self.chips_scroll.setObjectName("ChipsScroll")
        self.chips_scroll.setWidgetResizable(True)
        self.chips_scroll.setFixedHeight(34)
        self.chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chips_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        chips_inner = QWidget()
        self.chips_l = QHBoxLayout(chips_inner)
        self.chips_l.setContentsMargins(0, 0, 0, 0)
        self.chips_l.setSpacing(8)
        self.chips_l.addStretch(1)
        self.chips_scroll.setWidget(chips_inner)
        crow.addWidget(self.chips_scroll, 1)
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("🔍 搜索文件名")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(190)
        self.search.setToolTip("按文件名筛选（Ctrl+F）")
        self.search.textChanged.connect(self._on_search_changed)
        crow.addWidget(self.search)
        self.hint = QLabel("拖入 pkg / 文件夹即可提取")
        self.hint.setObjectName("hint")
        crow.addWidget(self.hint)
        v.addWidget(chips_row)

        # ── 画廊
        self.gal = GalleryView(self)
        v.addWidget(self.gal, 1)

        # ── 提取队列行
        self.queue_row = QWidget()
        self.queue_row.setObjectName("QueueRow")
        qh = QHBoxLayout(self.queue_row)
        qh.setContentsMargins(18, 8, 18, 8)
        qh.setSpacing(12)
        qlab = QLabel("队列")
        qlab.setObjectName("queueLabel")
        self.qchips = QLabel()
        self.qchips.setTextFormat(Qt.RichText)
        self.pbar = QProgressBar()
        self.pbar.setFixedHeight(6)
        self.pbar.setFixedWidth(220)
        self.pct = QLabel()
        self.pct.setObjectName("pct")
        b_cancel = QPushButton("取消")
        b_cancel.setObjectName("danger")
        b_cancel.clicked.connect(self.manager_cancel)
        b_log = QPushButton("日志 ▾")
        b_log.clicked.connect(lambda: self.toggle_log(not self.act_log.isChecked()))
        qh.addWidget(qlab)
        qh.addWidget(self.qchips, 1)
        qh.addWidget(self.pbar)
        qh.addWidget(self.pct)
        qh.addWidget(b_cancel)
        qh.addWidget(b_log)
        self.queue_row.setVisible(False)
        v.addWidget(self.queue_row)

        # ── 多选操作条（仅多选模式显示）
        self.selbar = QWidget()
        self.selbar.setObjectName("SelBar")
        mb = QHBoxLayout(self.selbar)
        mb.setContentsMargins(18, 8, 18, 8)
        mb.setSpacing(10)
        self.sel_info = QLabel("已选 0 个文件")
        self.sel_info.setObjectName("selInfo")
        self.b_del_sel = QPushButton("🗑 删除所选")
        self.b_del_sel.setObjectName("danger")
        self.b_del_sel.setEnabled(False)
        self.b_del_sel.clicked.connect(self.do_delete_selected)
        b_done = QPushButton("完成")
        b_done.setToolTip("退出多选模式（Esc）")
        b_done.clicked.connect(lambda: self.b_multi.setChecked(False))
        mb.addWidget(self.sel_info)
        mb.addStretch(1)
        mb.addWidget(self.b_del_sel)
        mb.addWidget(b_done)
        self.selbar.setVisible(False)
        v.addWidget(self.selbar)

        # ── 状态栏
        st = QWidget()
        st.setObjectName("BottomBar")
        sh = QHBoxLayout(st)
        sh.setContentsMargins(18, 8, 18, 8)
        sh.setSpacing(10)
        self.status = QLabel()
        self.status.setTextFormat(Qt.RichText)
        sh.addWidget(self.status)
        sh.addStretch(1)
        self.hint2 = QLabel("双击系统看图 · 回车全屏 · ☑ 多选 · F5 刷新")
        self.hint2.setObjectName("hintSmall")
        sh.addWidget(self.hint2)
        v.addWidget(st)

        # ── 日志
        self.log = QTextEdit()
        self.log.setObjectName("log")
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(150)
        v.addWidget(self.log)
        # 状态栏始终贴底：日志展开时状态栏也保持在最下方
        v.removeWidget(st)
        v.addWidget(st)

        # ── 拖放覆盖层
        self.overlay = DragOverlay(root)

        # ── 日志批处理：每张图一条日志，逐条 append 会被富文本排版拖慢；
        #    先放环形缓冲，80ms 合并一次；日志隐藏时只留缓冲不排版。
        self._log_buf = deque(maxlen=4000)
        self._log_timer = QTimer(self)
        self._log_timer.setSingleShot(True)
        self._log_timer.setInterval(80)
        self._log_timer.timeout.connect(self._flush_log)

        # ── 合并刷新：提取中每完成一个包不整页重建，350ms 内的变化只刷一次
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(350)
        self._refresh_timer.timeout.connect(self._refresh_from_disk)

        # ── 提取引擎
        self.manager = ExtractionManager(self.cfg)
        self.manager.outdir_arg = "" if self.outdir == DEFAULT_OUT else str(self.outdir)
        self.manager.log_line.connect(self._queue_log)
        self.manager.queue_changed.connect(self.refresh_queue)
        self.manager.item_done.connect(self.on_item_done)
        self.manager.all_done.connect(self.on_all_done)
        self._sync_parallel_actions()

        # ── 快捷键
        sc = QShortcut(QKeySequence("F5"), self)
        sc.activated.connect(self.rescan)
        sc = QShortcut(QKeySequence("Ctrl+O"), self)
        sc.activated.connect(self.pick_files)
        sc = QShortcut(QKeySequence(Qt.Key_Return), self)
        sc.activated.connect(self.open_selected_preview)
        sc = QShortcut(QKeySequence(Qt.Key_Enter), self)
        sc.activated.connect(self.open_selected_preview)
        sc = QShortcut(QKeySequence("Ctrl+F"), self)
        sc.activated.connect(self._focus_search)
        sc = QShortcut(QKeySequence(Qt.Key_Escape), self)
        sc.activated.connect(self._on_escape)
        sc = QShortcut(QKeySequence("Ctrl+A"), self)
        sc.activated.connect(self._select_all_visible)

        self._build_recent_menu()
        self.toggle_log(bool(cfg.get("log")))
        self.set_theme(self.dark, save=False)
        if demo_groups is not None:
            self._set_groups(demo_groups)
            self._update_status()
        else:
            self.rescan(first=True)
            # 启动清一遍空壳文件夹（删空的分组磁盘上只剩空文件夹），不挡启动
            QTimer.singleShot(0, self._startup_sweep)
        # 启动后延迟补注册丢失的定时清理任务（不挡启动）
        QTimer.singleShot(3000, self.ensure_sched_clean)
        # 启动后限制缩略图磁盘缓存体积（不挡启动）
        QTimer.singleShot(2500, _prune_thumb_cache)

    # ---- 图标
    @staticmethod
    def _load_app_icon():
        bases = [APP_DIR]
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            bases.append(Path(meipass))
        for base in bases:
            p = base / "icon.ico"
            if p.exists():
                return QIcon(str(p))
        return None

    def _make_icon(self):
        pm = QPixmap(64, 64)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        g = QLinearGradient(0, 0, 64, 64)
        g.setColorAt(0, QColor("#6f8cff"))
        g.setColorAt(1, QColor("#7a3cff"))
        p.fillRect(pm.rect(), g)
        p.setPen(QColor("#ffffff"))
        f = p.font()
        f.setPixelSize(36)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, "W")
        p.end()
        return QIcon(pm)

    # ---- 主题
    def set_theme(self, dark, save=True):
        self.dark = dark
        apply_palette(dark)
        QApplication.instance().setStyleSheet(build_qss(dark))
        _win_title_bar(int(self.winId()), dark,
                       "#0f1218" if dark else "#f3f5fa",
                       "#e6eaf2" if dark else "#151b26")
        self.act_dark.setChecked(dark)
        self.act_light.setChecked(not dark)
        if hasattr(self, "b_theme"):
            self.b_theme.setText("☀" if dark else "🌙")
            self.b_theme.setToolTip("切换到浅色主题" if dark else "切换到深色主题")
        if save:
            self.cfg["theme"] = "dark" if dark else "light"
            save_config(self.cfg)

    def toggle_log(self, on):
        self.act_log.blockSignals(True)
        self.act_log.setChecked(on)
        self.act_log.blockSignals(False)
        self.log.setVisible(on)
        if on:
            self._flush_log()  # 把隐藏期间积攒的日志补上
        self.act_log.setText("隐藏提取日志" if on else "显示提取日志")
        self.cfg["log"] = on
        save_config(self.cfg)

    def _queue_log(self, html):
        """日志先进缓冲；可见时每 80ms 合并一次，避免逐条富文本 append 卡界面。"""
        self._log_buf.append(html)
        if self.log.isVisible() and not self._log_timer.isActive():
            self._log_timer.start()

    def _flush_log(self):
        if not self.log.isVisible():
            return
        lines = list(self._log_buf)
        self._log_buf.clear()
        if not lines:
            return
        if self.log.document().blockCount() > 4000:
            self.log.clear()
            self.log.append('<span style="color:#8b95a7">[提示] 日志过长，已清空较早内容</span>')
        self.log.append("<br>".join(lines))
        sb = self.log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def set_parallel(self, value):
        """⚙ 并行提取数：0=自动，1..MAX_PARALLEL=固定。"""
        self.cfg["parallel"] = int(value or 0)
        save_config(self.cfg)
        n = self.manager.set_parallel(value)
        self._sync_parallel_actions()
        self.status.setText(f"● 并行提取进程数：{n}"
                            + ("" if value else "（自动，按核数/内存）"))

    def _sync_parallel_actions(self):
        cur = int(self.cfg.get("parallel", 0) or 0)
        for val, a in self._par_actions:
            a.setChecked(val == cur)

    # ---- 输出目录 / 扫描
    def change_outdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出根目录", str(self.outdir))
        if d:
            self.outdir = Path(d)
            self.cfg["outdir"] = str(d)
            save_config(self.cfg)
            self.manager.outdir_arg = ("" if self.outdir == DEFAULT_OUT
                                       else str(self.outdir))
            self.gal.set_outdir(self.outdir)
            self.rescan()

    def reset_outdir(self):
        self.outdir = DEFAULT_OUT
        self.cfg.pop("outdir", None)
        save_config(self.cfg)
        self.manager.outdir_arg = ""
        self.gal.set_outdir(self.outdir)
        self.rescan()

    def cleanup_explorer(self):
        killed, found = cleanup_explorer_zombies()
        if found == 0:
            self.status.setText("● 没有多余的文件夹进程，很干净 ✓")
        else:
            self.status.setText(f"● 已清理 {killed}/{found} 个空壳文件夹进程，"
                                f"释放约 {killed * 0.055:.1f} GB 内存")

    def toggle_sched_clean(self, on):
        """⚙ 里的定时清理开关：注册/删除每 30 分钟跑一次的计划任务。"""
        try:
            if on:
                sched_clean_install()
                if not sched_clean_enabled():
                    raise RuntimeError("注册后查询不到计划任务")
            else:
                sched_clean_remove()
        except Exception as ex:
            QMessageBox.warning(self, "定时清理空壳", f"设置失败：{ex}")
            self.act_sched.blockSignals(True)
            self.act_sched.setChecked(not on)
            self.act_sched.blockSignals(False)
            return
        self.cfg["sched_clean"] = bool(on)
        save_config(self.cfg)
        self.status.setText("● 已开启定时清理空壳（每 30 分钟）" if on
                            else "● 已关闭定时清理空壳")

    def ensure_sched_clean(self):
        """配置里开着但计划任务丢了（重装系统/删任务/路径变动）时自动补注册。"""
        if not self.cfg.get("sched_clean"):
            return
        try:
            if not sched_clean_enabled():
                sched_clean_install()
        except Exception:
            pass

    def rescan(self, first=False):
        self._refresh_timer.stop()
        self._prev_map = {g.name: (g.mtime, len(g.files)) for g in self.groups}
        self.groups = scan_groups(self.outdir)
        self._set_groups(self.groups)
        if first:
            self._update_status()

    def _set_groups(self, groups, demo=False):
        self.groups = groups
        self.gal.set_groups(groups, self.new_names)
        self._build_chips()
        if not demo:
            self._update_status()
        if self._pending_focus:
            name = self._pending_focus
            self._pending_focus = None
            if self.current_filter is None:
                QTimer.singleShot(60, lambda: self.gal.scroll_to_group(name))
            else:
                self._set_filter(name)

    def _n_img(self):
        return sum(len(g.images) for g in self.groups)

    def _n_vid(self):
        return sum(len(g.videos) for g in self.groups)

    def _update_status(self):
        if self.manager.running:
            return
        self.status.setText(f'<span style="color:#3ecf8e">●</span> 就绪 · '
                            f"共 {self._n_img()} 张图片 · {self._n_vid()} 个视频")

    def _can_prune(self) -> bool:
        """只有确认属于自己的输出目录才允许自动清理空文件夹。"""
        return outdir_owned(self.outdir)

    def _sweep_empty_folders(self):
        """输出目录里没图片/视频的文件夹统一清进回收站。

        只累加到 _pruned_n 并返回数量，不直接写状态栏——
        汇报方式由调用方定（启动/提取完成/删除后文案各不同）。
        """
        if not self._can_prune():
            return 0
        gone = prune_empty_folders(self.outdir)
        self._pruned_n += len(gone)
        return len(gone)

    def _startup_sweep(self):
        self._sweep_empty_folders()
        self._flush_pruned_note("启动时")

    def _flush_pruned_note(self, prefix="顺手"):
        """把本轮清掉的空文件夹数汇报到状态栏（覆盖当前文案，调用方最后调）。"""
        n, self._pruned_n = self._pruned_n, 0
        if n:
            self.status.setText(
                f'<span style="color:#3ecf8e">●</span> {prefix}清掉 {n} 个空文件夹'
                "（已移入回收站）")

    def set_status_file(self, fi: FileInfo):
        self.status.setText(f'<span style="color:#6f8cff">●</span> {fi.name} · '
                            f"{human_size(fi.size)} · {fmt_rel_time(fi.mtime)}")

    # ---- 分组标签
    def _build_chips(self):
        while self.chips_l.count() > 1:
            it = self.chips_l.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self._chips = []
        chip_all = QPushButton(f"全部 · {self._n_img()}")
        chip_all.setCheckable(True)
        chip_all.setProperty("chip", True)
        chip_all.setCursor(Qt.PointingHandCursor)
        chip_all.clicked.connect(lambda: self._set_filter(None))
        self._chips.append((None, chip_all))
        for g in self.groups:
            text = f"{g.name} · {len(g.images)}"
            if g.videos:
                text += " 🎬"
            if g.name in self.new_names:
                text += " ✨"
            b = QPushButton(text)
            b.setCheckable(True)
            b.setProperty("chip", True)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(g.name)
            b.clicked.connect(lambda _, name=g.name: self._set_filter(name))
            self._chips.append((g.name, b))
        for _, b in self._chips:
            self.chips_l.insertWidget(self.chips_l.count() - 1, b)
        self._sync_chips()

    def _sync_chips(self):
        for name, b in self._chips:
            b.setChecked(name == self.current_filter)

    def _set_filter(self, name):
        self.current_filter = name
        self.gal.apply_filter(name)
        self._sync_chips()

    # ---- 搜索
    def _focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _on_search_changed(self, text):
        self.search_text = (text or "").strip().lower()
        self.gal.apply_search(self.search_text)
        if self.search_text:
            n = sum(1 for g in self.groups for t in g.files
                    if self.search_text in t.name.lower())
            self.status.setText('<span style="color:#6f8cff">●</span> '
                                f"搜索「{text.strip()}」· 匹配 {n} 个文件")
        else:
            self._update_status()

    def _on_escape(self):
        if self.search.hasFocus() and self.search.text():
            self.search.clear()
            return
        if self.b_multi.isChecked():
            self.b_multi.setChecked(False)

    # ---- 文件操作
    def do_set_wallpaper(self, fi: FileInfo):
        ok = set_as_wallpaper(fi.path)
        self.status.setText('<span style="color:#3ecf8e">●</span> 已设为桌面壁纸 ✓'
                            if ok else
                            '<span style="color:#ff6b6b">●</span> 设置壁纸失败')

    def do_save_as(self, fi: FileInfo):
        suffix = Path(fi.path).suffix or ".jpg"
        dst, _ = QFileDialog.getSaveFileName(self, "另存为", fi.name,
                                             f"文件 (*{suffix});;所有文件 (*.*)")
        if dst:
            try:
                shutil.copy2(fi.path, dst)
                self.status.setText(f"● 已另存到 {dst}")
            except OSError as e:
                QMessageBox.warning(self, "另存失败", str(e))

    def do_copy(self, fi: FileInfo):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(fi.path)])
        QApplication.clipboard().setMimeData(mime)
        self.status.setText("● 已复制文件，可在资源管理器中粘贴")

    def do_delete(self, fi: FileInfo):
        r = QMessageBox.question(self, "删除", f"把 {fi.name} 移入回收站？")
        if r == QMessageBox.Yes:
            if recycle_file(fi.path):
                self.status.setText(f"● 已移入回收站：{fi.name}")
                if self.gal.remove_file(fi):
                    self._build_chips()  # 整组被删空，标签需要重建
                else:
                    self._sync_chip_texts()
                self._update_status()
                if self._pruned_n:
                    self._flush_pruned_note("分组已删空，")
            else:
                QMessageBox.warning(self, "删除失败", "移入回收站失败，文件未删除。")

    def _select_all_visible(self):
        if self.gal.multi_mode:
            self.gal.select_all_visible()

    # ---- 多选模式
    def set_multi_mode(self, on):
        self.gal.set_multi(on)

    def on_multi_changed(self):
        n = len(self.gal._sel_multi)
        self.sel_info.setText(f"已选 {n} 个文件")
        self.b_del_sel.setText(f"🗑 删除所选（{n}）" if n else "🗑 删除所选")
        self.b_del_sel.setEnabled(n > 0)
        self.selbar.setVisible(self.gal.multi_mode)

    def do_delete_selected(self):
        files = self.gal.sel_files()
        if not files:
            return
        r = QMessageBox.question(self, "删除所选",
                                 f"把选中的 {len(files)} 个文件移入回收站？")
        if r != QMessageBox.Yes:
            return
        ok = [fi for fi in files if recycle_file(fi.path)]
        if not ok:
            QMessageBox.warning(self, "删除失败", "移入回收站失败，文件未删除。")
            return
        if self.gal.remove_files(ok):
            self._build_chips()  # 有分组被删空，标签需要重建
        else:
            self._sync_chip_texts()
        self._update_status()
        fail = len(files) - len(ok)
        pruned = f"，顺手清掉 {self._pruned_n} 个空文件夹" if self._pruned_n else ""
        self._pruned_n = 0
        self.status.setText(
            f'<span style="color:#3ecf8e">●</span> 已移入回收站 {len(ok)} 个文件'
            + (f"（{fail} 个失败）" if fail else "") + pruned)

    def _sync_chip_texts(self):
        """原位刷新标签上的计数（不重建，避免闪烁）。"""
        for name, b in self._chips:
            if name is None:
                b.setText(f"全部 · {self._n_img()}")
                continue
            g = next((x for x in self.groups if x.name == name), None)
            if g is None:
                continue
            text = f"{g.name} · {len(g.images)}"
            if g.videos:
                text += " 🎬"
            if g.name in self.new_names:
                text += " ✨"
            b.setText(text)

    # ---- 选择文件 / 拖放
    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择要提取的壁纸包", str(APP_DIR),
            "壁纸包 (*.pkg *.mpkg *.mkg *.tex);;所有文件 (*.*)")
        if files:
            self._start_extraction(files)

    def _start_extraction(self, paths):
        """提取期间把缩略图解码线程压低，把核让给 RePKG（结束后恢复）。"""
        self.gal.set_thumb_threads(2 if (os.cpu_count() or 4) > 4 else 1)
        self.manager.enqueue(paths)

    def _paths_from_mime(self, mime):
        paths, ignored = [], 0
        for u in mime.urls():
            if not u.isLocalFile():  # 拖进来的网页/地址不是本地文件，直接忽略
                ignored += 1
                continue
            p = Path(u.toLocalFile())
            if p.is_dir() or p.suffix.lower() in PKG_EXTS:
                paths.append(str(p))
            else:
                ignored += 1
        return paths, ignored

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            paths, ignored = self._paths_from_mime(e.mimeData())
            if paths:
                self._drag_paths, self._drag_ignored = paths, ignored
                names = [Path(p).name for p in paths[:2]]
                sample = "、".join(names) + (f" 等 {len(paths)} 项" if len(paths) > 2 else "")
                self.overlay.set_info(len(paths), sample)
                self.overlay.setGeometry(self.centralWidget().rect())
                self.overlay.show()
                self.overlay.raise_()
                e.acceptProposedAction()
                return
        e.ignore()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.overlay.hide()

    def dropEvent(self, e):
        self.overlay.hide()
        paths = self._drag_paths
        self._drag_paths = []
        if paths:
            if self._drag_ignored:
                self._queue_log(f'<span style="color:#8b95a7">[提示]</span> '
                                f"已忽略 {self._drag_ignored} 个非壁纸包文件")
            self._start_extraction(paths)
        e.acceptProposedAction()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.overlay.isVisible():
            self.overlay.setGeometry(self.centralWidget().rect())
        # 窄窗口隐藏次要文字，避免顶栏/底栏拥挤
        w = self.width()
        if hasattr(self, "hint"):
            self.hint.setVisible(w >= 1020)
        if hasattr(self, "brand_sub"):
            self.brand_sub.setVisible(w >= 960)
        if hasattr(self, "hint2"):
            self.hint2.setVisible(w >= 900)

    # ---- 队列 / 提取过程
    def refresh_queue(self):
        items = self.manager.items
        active = bool(items) and (self.manager.running or
                                  any(it["state"] in ("wait", "run") for it in items))
        self.queue_row.setVisible(active)
        if not items:
            return
        style = {"done": ("#3ecf8e", "✓"), "skip": ("#f0b44c", "⊘"),
                 "fail": ("#ff6b6b", "✗"), "cancel": ("#6f7a8e", "⊘"),
                 "run": ("#69b7ff", "⟳"), "wait": ("#6f7a8e", "⋯")}
        parts = []
        for it in items[-8:]:
            c, mark = style.get(it["state"], ("#6f7a8e", ""))
            if it["state"] == "done":
                tail = f"{it['imgs']} 张" + (f" / {it['vids']} 视频" if it["vids"] else "")
            elif it["state"] == "skip":
                tail = "已提取过，跳过"
            elif it["state"] == "run":
                tail = f"提取中 {it['imgs']} 张"
            else:
                tail = ""
            parts.append(f'<span style="color:{c}">{mark} {_html_escape(_wall_name(it["path"]))}'
                         + (f" · {tail}" if tail else "") + "</span>")
        self.qchips.setText(" &nbsp; ".join(parts))
        done_n = sum(1 for it in items if it["state"] in ("done", "skip", "fail", "cancel"))
        self.pbar.setMaximum(len(items))
        self.pbar.setValue(done_n)
        self.pct.setText(f"{done_n}/{len(items)}")
        self.status.setText('<span style="color:#69b7ff">●</span> 提取中…')

    def on_item_done(self, it):
        if it["state"] == "cancel":
            return
        if it["state"] == "done" and (it["imgs"] or it["vids"]):
            self.recents.insert(0, {"src": it["path"], "group": _wall_name(it["path"]),
                                    "imgs": it["imgs"], "t": time.time()})
            self.recents = self.recents[:20]
            self.cfg["recents"] = self.recents
            save_config(self.cfg)
            self._build_recent_menu()
        # 合并刷新：350ms 内完成的多个包只扫盘/重建一次，提取中界面不再每包一跳
        self._refresh_timer.start()

    def _refresh_from_disk(self):
        """扫描输出目录对比差异，有实际变化才重建界面（跳过/失败时界面不动）。"""
        old = {g.name: (g.mtime, len(g.files)) for g in self.groups}
        self.groups = scan_groups(self.outdir)
        new = {g.name: (g.mtime, len(g.files)) for g in self.groups}
        changed = [n for n in new if old.get(n) != new[n]]
        removed = [n for n in old if n not in new]
        if not changed and not removed:
            self._update_status()
            return
        self._prev_map = old
        for n in changed:
            self.new_names.add(n)
        if changed:
            self._pending_focus = changed[0]
        self._set_groups(self.groups)
        if removed:
            self._build_chips()

    def on_all_done(self, cancelled):
        # 本批结束后顺手收掉提取期间产生的 /factory 空壳（若有）
        cleanup_explorer_zombies()
        # 把还没落地的合并刷新立刻做掉，保证"提取完成"看到的是最终结果
        if self._refresh_timer.isActive():
            self._refresh_timer.stop()
        self._refresh_from_disk()
        # 整批完成后清一遍空文件夹（视频包/被删光的组留下的空壳）
        self._sweep_empty_folders()
        # 有产物说明输出目录归本工具使用，打标记后才允许后续自动清空文件夹
        if not cancelled and any(it["imgs"] or it["vids"] for it in self.manager.items):
            mark_outdir(self.outdir)
        imgs = sum(it["imgs"] for it in self.manager.items)
        vids = sum(it["vids"] for it in self.manager.items)
        if cancelled:
            self.status.setText('<span style="color:#f0b44c">●</span> 已取消')
            self._flush_pruned_note()
            QTimer.singleShot(3000, self._update_status)
        else:
            extra = f"，清掉 {self._pruned_n} 个空文件夹" if self._pruned_n else ""
            self._pruned_n = 0
            self.status.setText(f'<span style="color:#3ecf8e">●</span> 提取完成：'
                                f"新增 {imgs} 张图片"
                                + (f" / {vids} 个视频" if vids else "") + extra)
            QTimer.singleShot(4000, self._update_status)
        if not cancelled:
            self._show_skip_popup()
        self.manager.reset_queue()
        self.refresh_queue()
        self.gal.set_thumb_threads(THUMB_THREADS)  # 恢复缩略图解码线程
        QTimer.singleShot(5000, _prune_thumb_cache)

    def _show_skip_popup(self):
        """本次队列里有"已提取过"的重复包时，弹汇总弹窗提醒。"""
        names, folders, seen = [], [], set()
        outroot = Path(self.outdir)
        for it in self.manager.items:
            if it["state"] != "skip" or not it.get("dup"):
                continue
            # 同名 scene.pkg 常见，用输出文件夹名区分不同壁纸
            wall = _wall_name(it["path"])
            if wall in seen:
                continue
            seen.add(wall)
            names.append(wall)
            d = outroot / wall
            if d.is_dir():
                folders.append(str(d))
        if names:
            SkipDialog(self, names, folders).exec()

    def manager_cancel(self):
        self.manager.cancel()

    def open_selected_preview(self):
        fi = self.gal.selected_file()
        if fi is not None and not fi.is_video:
            self.gal.request_preview(fi)

    # ---- 最近提取
    def _build_recent_menu(self):
        m = QMenu(self)
        m.setToolTipsVisible(True)
        if not self.recents:
            a = m.addAction("（暂无记录）")
            a.setEnabled(False)
        for r in self.recents[:10]:
            a = m.addAction(f"📦 {r['group']} · {r['imgs']} 张 · {fmt_rel_time(r['t'])}")
            a.setToolTip(r["src"])
            a.triggered.connect(lambda _, g=r["group"]: self._goto_group(g))
        if self.recents:
            m.addSeparator()
            clr = m.addAction("清空最近记录")
            clr.triggered.connect(self._clear_recents)
        self.b_recent.setMenu(m)

    def _goto_group(self, name):
        if any(g.name == name for g in self.groups):
            self._set_filter(name)

    def _clear_recents(self):
        self.recents = []
        self.cfg["recents"] = []
        save_config(self.cfg)
        self._build_recent_menu()

    # ---- 其他
    def closeEvent(self, e):
        if self.manager.running:
            r = QMessageBox.question(self, "正在提取", "仍有任务在提取，确定退出？")
            if r != QMessageBox.Yes:
                e.ignore()
                return
            self.manager.cancel()
        self.cfg["geometry"] = self.saveGeometry().toHex().data().decode()
        save_config(self.cfg)
        cleanup_explorer_zombies()  # 退出时收尾，不留空壳过夜
        super().closeEvent(e)


# ---------------------------------------------------------------- 自测 / 演示
def _demo_groups():
    d = CFG_DIR / "demo_pics"
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    pairs = [("#1a2a6c", "#fdbb2d"), ("#0f2027", "#2c5364"), ("#3a1c71", "#ffaf7b"),
             ("#134e5e", "#71b280"), ("#aa076b", "#61045f"), ("#1d4350", "#a43931"),
             ("#16222a", "#3a6073"), ("#2b5876", "#4e4376"), ("#41295a", "#2f0743"),
             ("#000428", "#004e92"), ("#136a8a", "#267871"), ("#5f2c82", "#49a09d"),
             ("#232526", "#414345"), ("#603813", "#b29f94")]
    groups_def = [("赛博朋克夜城", 6, 1), ("山间晨雾", 4, 0), ("像素小镇", 40, 0)]
    out, k = [], 0
    for name, n_img, n_vid in groups_def:
        files = []
        for i in range(n_img):
            c1, c2 = pairs[k % len(pairs)]
            k += 1
            # 混入竖图（每 4 张第 3 张），演示等宽变高布局
            iw, ih = (262, 420) if i % 4 == 2 else (420, 262)
            fp = d / f"{name}_{i:02d}.png"
            img = QImage(iw, ih, QImage.Format_RGB32)
            p = QPainter(img)
            g = QLinearGradient(0, 0, iw, ih)
            g.setColorAt(0, QColor(c1))
            g.setColorAt(1, QColor(c2))
            p.fillRect(img.rect(), g)
            p.end()
            img.save(str(fp), "PNG")
            files.append(FileInfo(str(fp), fp.name, fp.stat().st_size,
                                  fp.stat().st_mtime, False, iw, ih))
        for i in range(n_vid):
            files.append(FileInfo(str(d / f"{name}_video_{i}.mp4"),
                                  f"video_0{i + 1}.mp4", 13212000, time.time(), True))
        out.append(GroupData(name, str(d), files, time.time()))
    return out


def _selftest():
    app = QApplication.instance()
    out_dir = APP_DIR / "ui-preview"
    out_dir.mkdir(exist_ok=True)
    # 自测不要碰真实用户配置/缩略图缓存：重定向到 ui-preview 下的临时目录
    global CFG_DIR, CFG_FILE, THUMB_CACHE
    CFG_DIR = out_dir / "_selftest_cfg"
    CFG_FILE = CFG_DIR / "config.json"
    THUMB_CACHE = CFG_DIR / "thumbs"
    win = MainWindow({"theme": "dark"}, demo_groups=_demo_groups())
    win.resize(1180, 720)
    win.show()
    hrs = _win_title_bar(int(win.winId()), True, "#171a20", "#d8dde5")
    print("titlebar hrs (dark/caption/text):",
          [hex(h & 0xffffffff) for h in hrs])

    def grab1():
        win.grab().save(str(out_dir / "main-dark.png"), "PNG")
        imgs = [fi for fi in win.groups[0].files if not fi.is_video]
        dlg = PreviewDialog(win, imgs, 0)

        def grab2():
            dlg.grab().save(str(out_dir / "preview-dark.png"), "PNG")
            dlg.close()
            win.gal.remove_file(win.groups[0].images[0])
            win._sync_chip_texts()
            win._update_status()

            def grab3():
                win.grab().save(str(out_dir / "after-delete.png"), "PNG")
                # 多选模式：勾选第一组前两张 + 全选第二组，截多选效果图
                win.b_multi.setChecked(True)
                for t in win.gal.sections[0].thumbs[:2]:
                    win.gal.toggle_multi(t)
                win.gal.select_all_section(win.gal.sections[1])

                def grab35():
                    win.grab().save(str(out_dir / "multi-select.png"), "PNG")
                    win.b_multi.setChecked(False)
                    win.set_theme(False, save=False)

                    def grab4():
                        win.grab().save(str(out_dir / "main-light.png"), "PNG")
                        rendered = sum(1 for t in win.gal._by_key.values()
                                       if not t.fi.is_video and not t.img.pixmap().isNull())
                        print(f"lazy check: 已渲染 {rendered} / 总数 {win._n_img()}")
                        win.close()
                        app.quit()

                    QTimer.singleShot(500, grab4)

                QTimer.singleShot(500, grab35)

            QTimer.singleShot(500, grab3)

        QTimer.singleShot(600, grab2)
        dlg.showFullScreen()

    QTimer.singleShot(1800, grab1)
    app.exec()
    print("saved:", out_dir)


def _smoke():
    """验证 powershell 子进程与中文输出解码链路。"""
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication([])
    p = QProcess()

    def done(code, status):
        raw = bytes(p.readAllStandardOutput())
        print(f"exit={code} decoded={decode_line(raw).strip()!r}")
        app.quit()

    p.finished.connect(done)
    p.start("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command",
                               "Write-Host '中文输出测试 [图片] OK'; "
                               "Write-Host '第二行正常'"])
    QTimer.singleShot(20000, app.quit)
    app.exec()


def _test_empty_prune():
    """空文件夹清扫的无头自测：临时目录 + 假回收站，不碰真实输出目录。"""
    import tempfile
    root = Path(tempfile.mkdtemp(prefix="wall_prune_test_"))
    (root / "empty1").mkdir()
    (root / "hasimg").mkdir()
    (root / "hasimg" / "a.png").write_bytes(b"x")
    (root / "hasimg" / "sub").mkdir()
    (root / "hasimg" / "sub" / "b.jpg").write_bytes(b"x")
    (root / "jsononly" / "sub").mkdir(parents=True)      # 没图但有杂文件
    (root / "jsononly" / "sub" / "n.txt").write_text("x", "utf-8")
    (root / "maskkeep" / "masks").mkdir(parents=True)    # 只剩 masks 也算有图
    (root / "maskkeep" / "masks" / "m.png").write_bytes(b"x")
    real_recycle = recycle_file
    recycled = []

    def fake_recycle(p):
        p = Path(p)
        if not p.exists():
            return False  # 和真实 recycle_file 一致：路径不存在时失败
        recycled.append(p.name)
        shutil.rmtree(p, ignore_errors=True)
        return True

    globals()["recycle_file"] = fake_recycle
    try:
        gone = prune_empty_folders(root)
        assert sorted(gone) == ["empty1", "jsononly"], gone
        assert (root / "hasimg" / "a.png").exists()
        assert (root / "hasimg" / "sub" / "b.jpg").exists()
        assert (root / "maskkeep" / "masks" / "m.png").exists()
        assert not (root / "empty1").exists() and not (root / "jsononly").exists()
        assert prune_empty_folder(str(root / "hasimg")) is False  # 有图的不动
        assert prune_empty_folder(str(root / "NoSuchDir")) is False
        print("空文件夹清扫自测通过：清", gone, "；保留 hasimg / maskkeep")
    finally:
        globals()["recycle_file"] = real_recycle
        shutil.rmtree(root, ignore_errors=True)
    return 0


def _test_sched():
    """无头自测：并行度解析、写冲突分批、队列去重、缩略图缓存键（不启动引擎）。"""
    import tempfile
    from PySide6.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    assert app is not None  # 保活：测试期间 QCoreApplication 实例必须存在

    # 并行度：自动值有界、显式值可覆盖
    assert 1 <= auto_parallel() <= MAX_PARALLEL, auto_parallel()
    assert resolve_parallel({}) == auto_parallel()
    assert resolve_parallel({"parallel": 0}) == auto_parallel()
    assert resolve_parallel({"parallel": 3}) == 3
    assert resolve_parallel({"parallel": 99}) == MAX_PARALLEL
    assert resolve_parallel({"parallel": "x"}) == auto_parallel()

    # 输出子目录名与写冲突判定（同输出目录的条目必须串行）
    assert _out_dir_name(r"C:\w\One\scene.pkg") == "one"
    assert _out_dir_name(r"C:\w\One\scene.mpkg") == "one"
    assert _out_dir_name(r"D:\tex\pic.tex") == "pic"
    assert _out_dir_name(r"C:\w\My Wall") == "my wall"
    assert _items_conflict({"path": r"C:\w\One\scene.pkg"},
                           {"path": r"C:\w\One\scene2.pkg"})
    assert _items_conflict({"path": r"C:\w\One"},
                           {"path": r"C:\w\One\scene.pkg"})
    assert not _items_conflict({"path": r"C:\w\One\scene.pkg"},
                               {"path": r"C:\w\Two\scene.pkg"})

    # 队列去重 + 冲突条目必须同批
    root = Path(tempfile.mkdtemp(prefix="wall_sched_test_"))
    try:
        paths = []
        for k, name in enumerate(("A", "B", "C", "D")):
            (root / name).mkdir()
            p = root / name / "scene.pkg"
            p.write_bytes(b"x" * (100 * (k + 1)))
            paths.append(str(p))
        extra = root / "A" / "scene2.pkg"
        extra.write_bytes(b"x" * 10)
        paths.append(str(extra))

        mgr = ExtractionManager({"parallel": 3})
        mgr._pump = lambda: None  # 只测规划，不真启动 QProcess
        mgr.enqueue(paths + [paths[0]])  # 最后一个是重复包
        assert mgr.items[-1]["state"] == "skip" and mgr.items[-1]["dup"]

        batch = mgr._take_batch()
        assert batch, "应能规划出批次"
        names = [mgr._out_name(mgr.items[i]) for i in batch]
        assert len(names) == len(set(names)), names  # 同批不能有两个相同输出目录
        assert len(batch) <= ExtractionManager.BATCH_MAX_ITEMS
        idx_a = [i for i, it in enumerate(mgr.items)
                 if it["path"].lower().endswith(r"\a\scene.pkg")
                 or it["path"].lower().endswith(r"\a\scene2.pkg")]
        if names[0] == "a":
            assert set(idx_a) <= set(batch), "同输出目录的条目必须同批"

        # 活跃 worker 占住 A 后，后续批次不能再包含 A
        w = _EngineWorker()
        w.names.add("a")
        w.paths.add(_norm_key(str(root / "A" / "scene.pkg")))
        mgr.workers = [w]
        batch2 = mgr._take_batch()
        assert all(mgr._out_name(mgr.items[i]) != "a" for i in batch2)
        mgr.workers = []

        # 大批同目录条目受 BATCH_MAX_ITEMS 上限约束
        mgr.items = [{"path": str(root / "A" / f"p{k}.pkg"), "name": f"p{k}.pkg",
                      "state": "wait", "imgs": 0, "vids": 0,
                      "skipped": False, "fail": False, "hdr": False,
                      "dup": False, "retry": 0, "cost": 0}
                     for k in range(ExtractionManager.BATCH_MAX_ITEMS + 10)]
        big = mgr._take_batch()
        assert len(big) == ExtractionManager.BATCH_MAX_ITEMS, len(big)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # 缩略图缓存键：同图同参数命中同一个文件，mtime 变化则换文件
    c1 = _thumb_cache_paths(r"C:\w\a.png", 1.0, 131)[0]
    c2 = _thumb_cache_paths(r"C:\w\a.png", 1.0, 131)[0]
    c3 = _thumb_cache_paths(r"C:\w\a.png", 2.0, 131)[0]
    assert c1 == c2 and c1 != c3 and c1.suffix == ".png"

    print("调度/并行自测通过：auto_parallel =", auto_parallel(),
          "；批次上限 =", ExtractionManager.BATCH_MAX_ITEMS)
    return 0


def main():
    if "--test-empty" in sys.argv:
        return _test_empty_prune()  # 无头，不需要 QApplication
    if "--test-sched" in sys.argv:
        return _test_sched() or 0  # 无头，只创建 QCoreApplication
    if "--smoke" in sys.argv:
        return _smoke() or 0
    if "--cleanghosts" in sys.argv:
        return _clean_ghosts_cli()  # 计划任务回退入口，无需 GUI
    app = QApplication(sys.argv)
    app.setApplicationName("壁纸提取器")
    app.setFont(QFont("Microsoft YaHei UI", 9))
    if "--selftest" in sys.argv:
        return _selftest() or 0

    # ── 防多开：已有实例在运行就把它的窗口调到前台，本次直接退出，不再占用资源
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    probe = QLocalSocket()
    probe.connectToServer(SINGLE_KEY)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return 0
    QLocalServer.removeServer(SINGLE_KEY)  # 清掉上次异常退出残留的管道
    single_server = QLocalServer()
    single_server.listen(SINGLE_KEY)
    app._single_server = single_server  # 保住引用防止被回收

    cfg = load_config()
    win = MainWindow(cfg)

    def _activate_existing():
        while single_server.hasPendingConnections():
            single_server.nextPendingConnection().deleteLater()
        win.showNormal()
        win.raise_()
        win.activateWindow()
        win.status.setText('<span style="color:#f0b44c">●</span> '
                           "检测到重复启动，已调出正在运行的窗口")
        QTimer.singleShot(4000, win._update_status)

    single_server.newConnection.connect(_activate_existing)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
