"""
Auto Screenshot - 선택한 창을 페이지 단위로 스크롤하며 자동 캡처하는 Windows 앱

동작 방식 (마우스 클릭 전환 모드, 기본)
  매 페이지마다
    1) 마우스가 대상 창의 클릭 위치로 이동해 클릭 → 대상 창 활성화
    2) Page Down 등 페이지 이동 키 전송
    3) 마우스가 이 앱의 캡처 패널로 돌아와 클릭 → 이 앱 활성화
    4) 이 앱이 활성화된 상태에서 캡처 (패널은 캡처 순간 투명 처리)
  → 대상 창이 포커스를 잃은 상태에서 찍으므로 글자 커서(캐럿)와 마우스 포인터가
    캡처에 포함되지 않고, 대상 앱이 활성 상태일 때만 동작하는 캡처 방지도 피할 수 있습니다.

의존성: Python 3.9+, Pillow (tkinter 는 표준 포함)
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

if sys.platform != "win32":
    sys.exit("이 앱은 Windows 전용입니다.")

# 고해상도(DPI 배율) 환경에서 좌표가 어긋나지 않도록 tkinter 생성 전에 설정
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageChops, ImageGrab

APP_VERSION = "1.1.0"

# ──────────────────────────────────────────────────────────────
# Win32 헬퍼
# ──────────────────────────────────────────────────────────────
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

user32.GetForegroundWindow.restype = wt.HWND
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.MonitorFromWindow.restype = wt.HANDLE
user32.MonitorFromWindow.argtypes = [wt.HWND, wt.DWORD]
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD, ctypes.c_ubyte, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]

EnumWindowsProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x80
SW_RESTORE = 9
GA_ROOT = 2
LWA_ALPHA = 2

VK_LBUTTON = 0x01
VK_SHIFT, VK_CONTROL = 0x10, 0x11
VK_ESCAPE, VK_SPACE = 0x1B, 0x20
VK_NEXT, VK_HOME, VK_DOWN = 0x22, 0x24, 0x28
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_WHEEL = 0x0800


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def _process_name(hwnd):
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def list_windows(exclude_pid=None):
    """화면에 보이는 최상위 창 목록 [(hwnd, 제목, 프로세스명)]"""
    result = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        cloaked = ctypes.c_int(0)
        dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value:
            return True
        if exclude_pid is not None:
            pid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == exclude_pid:
                return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value in ("Program Manager",):
            return True
        result.append((hwnd, buf.value, _process_name(hwnd)))
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return result


def window_exists(hwnd):
    return bool(user32.IsWindow(hwnd))


def window_rect(hwnd):
    """그림자를 뺀 실제 창 영역 (left, top, right, bottom)"""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _monitor_info(hwnd):
    hmon = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    return mi


def monitor_rect(hwnd):
    r = _monitor_info(hwnd).rcMonitor
    return r.left, r.top, r.right, r.bottom


def monitor_work_rect(hwnd):
    r = _monitor_info(hwnd).rcWork
    return r.left, r.top, r.right, r.bottom


def root_of(hwnd):
    return user32.GetAncestor(hwnd, GA_ROOT) if hwnd else None


def foreground_root():
    return root_of(user32.GetForegroundWindow())


def cursor_pos():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def window_at(x, y):
    return root_of(user32.WindowFromPoint(wt.POINT(int(x), int(y))))


def activate_window(hwnd):
    """대상 창을 맨 앞으로 가져오고 성공 여부 반환 (키 입력 모드용)"""
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tgt_tid = user32.GetWindowThreadProcessId(hwnd, None)
    attached = []
    for tid in {fg_tid, tgt_tid}:
        if tid and tid != cur_tid and user32.AttachThreadInput(cur_tid, tid, True):
            attached.append(tid)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for tid in attached:
            user32.AttachThreadInput(cur_tid, tid, False)
    time.sleep(0.15)
    return user32.GetForegroundWindow() == hwnd


def click_at(x, y):
    """마우스를 (x, y)로 옮겨 왼쪽 클릭"""
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.06)
    user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.03)
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def set_alpha(hwnd, alpha):
    """레이어드 창의 투명도 (0.0~1.0). 다른 스레드에서도 호출 가능"""
    user32.SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, int(alpha * 255))), LWA_ALPHA)


def _key(vk, up=False):
    flags = (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0) | (KEYEVENTF_KEYUP if up else 0)
    user32.keybd_event(vk, user32.MapVirtualKeyW(vk, 0), flags, 0)


def press_key(vk, mods=()):
    for m in mods:
        _key(m)
    _key(vk)
    _key(vk, up=True)
    for m in reversed(mods):
        _key(m, up=True)


def mouse_wheel(notches, at=None):
    if at:
        user32.SetCursorPos(int(at[0]), int(at[1]))
        time.sleep(0.05)
    user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, ctypes.c_uint32(-120 * notches).value, 0)


def esc_pressed():
    return bool(user32.GetAsyncKeyState(VK_ESCAPE) & 0x8001)


def rects_overlap(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def point_in(rect, x, y):
    return rect[0] <= x < rect[2] and rect[1] <= y < rect[3]


def click_point(hwnd, ratio):
    """창 영역 기준 비율 (fx, fy) → 화면 좌표. ratio 가 None 이면 창 중앙"""
    l, t, r, b = window_rect(hwnd)
    fx, fy = ratio or (0.5, 0.5)
    return l + (r - l) * fx, t + (b - t) * fy


# ──────────────────────────────────────────────────────────────
# 캡처 로직
# ──────────────────────────────────────────────────────────────
MODE_CLICK = "마우스 클릭 전환 (권장)"
MODE_KEY = "키 입력만 (빠름)"
MODES = [MODE_CLICK, MODE_KEY]

AREA_WINDOW = "선택한 창 영역"
AREA_MONITOR = "창이 있는 모니터 전체"
AREA_ALL = "모든 모니터"
AREAS = [AREA_WINDOW, AREA_MONITOR, AREA_ALL]

START_OPTIONS = {
    "현재 위치에서 시작": None,
    "Ctrl+Home (문서 맨 위로)": (VK_HOME, (VK_CONTROL,)),
    "Ctrl+Shift+Home (터미널 맨 위로)": (VK_HOME, (VK_CONTROL, VK_SHIFT)),
}

SCROLL_OPTIONS = {
    "Page Down": ("key", VK_NEXT, ()),
    "Shift+Page Down (터미널 스크롤)": ("key", VK_NEXT, (VK_SHIFT,)),
    "Space": ("key", VK_SPACE, ()),
    "↓ 방향키": ("key", VK_DOWN, ()),
    "마우스 휠": ("wheel", 3, ()),
}


def capture_bbox(hwnd, area):
    if area == AREA_WINDOW:
        return window_rect(hwnd)
    if area == AREA_MONITOR:
        return monitor_rect(hwnd)
    return None  # 모든 모니터


def grab(bbox):
    return ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")


def images_same(a, b, threshold=0.002):
    """두 캡처가 사실상 같은지 (깜빡이는 커서 등 미세 변화는 무시)"""
    if a is None or b is None or a.size != b.size:
        return False
    w, h = a.size
    small = (max(1, w // 2), max(1, h // 2))
    diff = ImageChops.difference(a.resize(small), b.resize(small)).convert("L")
    changed = diff.point(lambda p: 255 if p > 24 else 0).histogram()[255]
    return changed / (small[0] * small[1]) < threshold


def safe_name(text, limit=40):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", text).strip(" ._")
    return (name[:limit].strip(" ._") or "capture")


class CaptureJob(threading.Thread):
    """백그라운드에서 (대상 클릭 → 페이지 이동 → 앱 복귀 → 캡처)를 반복"""

    def __init__(self, hwnd, cfg, out_dir, events, panel_hwnd, click_ratio=None):
        super().__init__(daemon=True)
        self.hwnd, self.cfg, self.out_dir, self.events = hwnd, cfg, out_dir, events
        self.panel_hwnd = panel_hwnd
        self.click_ratio = click_ratio
        self.click_mode = cfg["mode"] == MODE_CLICK
        self.stop_flag = threading.Event()
        self.saved = []

    def emit(self, kind, **data):
        self.events.put((kind, data))

    def wait(self, seconds):
        """대기하면서 Esc / 중지 버튼 감시. 중지되면 True"""
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_flag.is_set() or esc_pressed():
                self.stop_flag.set()
                return True
            time.sleep(0.03)
        return self.stop_flag.is_set()

    def check_stop(self):
        if self.stop_flag.is_set() or esc_pressed():
            raise InterruptedError

    # ── 포커스 전환 ──
    def focus_target(self):
        """대상 창을 활성화 (클릭 모드: 마우스로 클릭)"""
        if not self.click_mode:
            if foreground_root() == self.hwnd or activate_window(self.hwnd):
                return
            raise RuntimeError("대상 창이 앞에 있지 않아 중지했습니다(다른 창이 활성화됨).")
        if user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            time.sleep(0.3)
        x, y = click_point(self.hwnd, self.click_ratio)
        if window_at(x, y) != self.hwnd:
            # 다른 창에 가려져 있으면 한 번 앞으로 가져온 뒤 다시 확인
            activate_window(self.hwnd)
            if window_at(x, y) != self.hwnd:
                raise RuntimeError("대상 창의 클릭 위치가 다른 창에 가려져 있습니다. 창 배치나 클릭 위치를 바꿔 주세요.")
        click_at(x, y)
        time.sleep(0.15)
        if foreground_root() != self.hwnd:
            raise RuntimeError("대상 창을 클릭했지만 활성화되지 않았습니다.")

    def focus_self(self):
        """이 앱(캡처 패널)으로 돌아와 활성화"""
        if not self.click_mode:
            return
        r = wt.RECT()
        user32.GetWindowRect(self.panel_hwnd, ctypes.byref(r))
        click_at(r.left + (r.right - r.left) * 0.3, r.top + (r.bottom - r.top) * 0.62)
        time.sleep(0.12)
        if foreground_root() != self.panel_hwnd:
            raise RuntimeError("캡처 패널로 돌아오지 못했습니다.")

    def grab_without_panel(self, bbox):
        """캡처 순간에만 패널을 투명하게 (포커스는 이 앱에 유지)"""
        set_alpha(self.panel_hwnd, 0)
        time.sleep(0.12)
        try:
            return grab(bbox)
        finally:
            set_alpha(self.panel_hwnd, 0.95)

    def scroll(self):
        kind, code, mods = SCROLL_OPTIONS[self.cfg["scroll"]]
        for _ in range(self.cfg["repeat"]):
            if kind == "key":
                press_key(code, mods)
            else:
                mouse_wheel(code, click_point(self.hwnd, self.click_ratio))
            time.sleep(0.04)

    def run(self):
        reason = "완료"
        try:
            esc_pressed()  # 이전 키 상태 비우기
            for left in range(int(self.cfg["start_delay"]), 0, -1):
                self.emit("status", text=f"{left}초 후 시작 · 마우스를 건드리지 마세요")
                if self.wait(1):
                    raise InterruptedError
            if not window_exists(self.hwnd):
                raise RuntimeError("대상 창이 닫혔습니다.")

            self.out_dir.mkdir(parents=True, exist_ok=True)
            ext = self.cfg["format"]
            max_pages = self.cfg["max_pages"]
            prev = None

            # 시작 위치 이동
            self.focus_target()
            top = START_OPTIONS[self.cfg["start"]]
            if top:
                press_key(*top)
                if self.wait(self.cfg["page_delay"]):
                    raise InterruptedError

            while True:
                self.check_stop()
                if not window_exists(self.hwnd):
                    raise RuntimeError("대상 창이 닫혔습니다.")

                # 이 앱으로 돌아와서 캡처
                self.focus_self()
                time.sleep(0.1)  # 포커스가 빠진 대상 창이 다시 그려질 시간
                img = self.grab_without_panel(capture_bbox(self.hwnd, self.cfg["area"]))

                if self.cfg["detect_end"] and images_same(img, prev):
                    reason = "끝 도달(화면 변화 없음)"
                    break

                path = self.out_dir / f"{len(self.saved) + 1:04d}.{ext}"
                img.save(path, quality=92) if ext == "jpg" else img.save(path)
                self.saved.append(path)
                prev = img
                self.emit("progress", count=len(self.saved), total=max_pages, path=str(path))

                if len(self.saved) >= max_pages:
                    reason = "최대 장수 도달"
                    break

                # 대상 창을 선택하고 페이지 이동
                self.check_stop()
                self.focus_target()
                self.scroll()
                if self.wait(self.cfg["page_delay"]):
                    raise InterruptedError

            if self.cfg["make_pdf"] and self.saved:
                self.emit("status", text="PDF로 합치는 중…")
                pages = [Image.open(p).convert("RGB") for p in self.saved]
                pdf_path = self.out_dir / f"{self.out_dir.name}.pdf"
                pages[0].save(pdf_path, save_all=True, append_images=pages[1:], resolution=96)
        except InterruptedError:
            reason = "사용자 중지"
        except Exception as e:  # noqa: BLE001
            reason = f"오류: {e}"
        self.emit("done", reason=reason, count=len(self.saved), folder=str(self.out_dir))


# ──────────────────────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────────────────────
SETTINGS_FILE = Path.home() / ".auto_screenshot.json"
DEFAULTS = {
    "mode": MODE_CLICK,
    "area": AREA_WINDOW,
    "start": "현재 위치에서 시작",
    "scroll": "Page Down",
    "repeat": 1,
    "max_pages": 50,
    "page_delay": 0.8,
    "start_delay": 3,
    "detect_end": True,
    "format": "png",
    "make_pdf": False,
    "base_dir": str(Path.home() / "Pictures" / "AutoScreenshot"),
}

PANEL_W, PANEL_H = 340, 92


def load_settings():
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception:
        pass
    return data


def save_settings(data):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def panel_position(target_hwnd, ratio):
    """캡처 패널 위치: 대상 창과 겹치지 않는 모니터 모서리 (안 되면 클릭 위치만 피함)"""
    l, t, r, b = monitor_work_rect(target_hwnd)
    m = 12
    cands = [(r - PANEL_W - m, b - PANEL_H - m), (l + m, b - PANEL_H - m),
             (r - PANEL_W - m, t + m), (l + m, t + m)]
    target = window_rect(target_hwnd)
    for x, y in cands:
        if not rects_overlap((x, y, x + PANEL_W, y + PANEL_H), target):
            return x, y
    cx, cy = click_point(target_hwnd, ratio)
    for x, y in cands:
        if not point_in((x - 20, y - 40, x + PANEL_W + 20, y + PANEL_H + 20), cx, cy):
            return x, y
    return cands[0]


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.job = None
        self.panel = None
        self.windows = []
        self.last_folder = None
        self.click_ratio = None
        self.click_ratio_hwnd = None
        s = load_settings()

        root.title(f"Auto Screenshot {APP_VERSION} - 자동 페이지 캡처")
        # DPI 배율(125%, 150% …)에 맞춰 창 크기 조정
        sc = max(1.0, root.winfo_fpixels("1i") / 96)
        w, h = int(800 * sc), int(720 * sc)
        h = min(h, root.winfo_screenheight() - int(80 * sc))
        root.geometry(f"{w}x{h}")
        root.minsize(int(720 * sc), min(int(640 * sc), h))

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("맑은 고딕", 11, "bold"), padding=8)
        style.configure("Treeview", rowheight=int(22 * sc))

        main = ttk.Frame(root, padding=12)
        main.pack(fill="both", expand=True)

        # 1. 대상 창 선택
        f1 = ttk.LabelFrame(main, text=" ① 캡처할 창 선택 ", padding=8)
        f1.pack(fill="both", expand=True)
        top = ttk.Frame(f1)
        top.pack(fill="x")
        ttk.Label(top, text="스크린샷은 여기서 선택한 창에 대해서만 실행됩니다.").pack(side="left")
        ttk.Button(top, text="🔄 목록 새로고침", command=self.refresh_windows).pack(side="right")

        tree_wrap = ttk.Frame(f1)
        tree_wrap.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(tree_wrap, columns=("title", "proc"), show="headings", height=7, selectmode="browse")
        self.tree.heading("title", text="창 제목")
        self.tree.heading("proc", text="프로그램")
        self.tree.column("title", width=520)
        self.tree.column("proc", width=160)
        sb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        # 2. 캡처 설정
        f2 = ttk.LabelFrame(main, text=" ② 캡처 설정 ", padding=8)
        f2.pack(fill="x", pady=(10, 0))
        self.v_mode = tk.StringVar(value=s["mode"] if s["mode"] in MODES else MODE_CLICK)
        self.v_area = tk.StringVar(value=s["area"])
        self.v_start = tk.StringVar(value=s["start"])
        self.v_scroll = tk.StringVar(value=s["scroll"])
        self.v_repeat = tk.IntVar(value=s["repeat"])
        self.v_max = tk.IntVar(value=s["max_pages"])
        self.v_delay = tk.DoubleVar(value=s["page_delay"])
        self.v_start_delay = tk.IntVar(value=s["start_delay"])
        self.v_detect = tk.BooleanVar(value=s["detect_end"])
        self.v_format = tk.StringVar(value=s["format"])
        self.v_pdf = tk.BooleanVar(value=s["make_pdf"])
        self.v_click = tk.StringVar(value="창 중앙 (기본)")

        def row(r, c, label, widget):
            ttk.Label(f2, text=label).grid(row=r, column=c, sticky="w", padx=(0, 6), pady=3)
            widget.grid(row=r, column=c + 1, sticky="we", padx=(0, 16), pady=3)

        row(0, 0, "동작 방식", ttk.Combobox(f2, textvariable=self.v_mode, values=MODES, state="readonly", width=26))
        click_box = ttk.Frame(f2)
        ttk.Label(click_box, textvariable=self.v_click, width=15).pack(side="left")
        ttk.Button(click_box, text="📍 지정", width=7, command=self.pick_click_point).pack(side="left", padx=(4, 0))
        ttk.Button(click_box, text="기본", width=5, command=self.reset_click_point).pack(side="left", padx=(4, 0))
        row(0, 2, "클릭 위치", click_box)
        row(1, 0, "캡처 영역", ttk.Combobox(f2, textvariable=self.v_area, values=AREAS, state="readonly", width=26))
        row(1, 2, "시작 위치", ttk.Combobox(f2, textvariable=self.v_start, values=list(START_OPTIONS), state="readonly", width=28))
        row(2, 0, "페이지 이동 방식", ttk.Combobox(f2, textvariable=self.v_scroll, values=list(SCROLL_OPTIONS), state="readonly", width=26))
        row(2, 2, "1회 이동 시 반복 횟수", ttk.Spinbox(f2, from_=1, to=50, textvariable=self.v_repeat, width=8))
        row(3, 0, "최대 캡처 장수", ttk.Spinbox(f2, from_=1, to=2000, textvariable=self.v_max, width=8))
        row(3, 2, "페이지 간 대기(초)", ttk.Spinbox(f2, from_=0.1, to=10, increment=0.1, textvariable=self.v_delay, width=8))
        row(4, 0, "시작 전 대기(초)", ttk.Spinbox(f2, from_=0, to=30, textvariable=self.v_start_delay, width=8))
        row(4, 2, "이미지 형식", ttk.Combobox(f2, textvariable=self.v_format, values=["png", "jpg"], state="readonly", width=8))
        chk = ttk.Frame(f2)
        chk.grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Checkbutton(chk, text="화면 변화가 없으면 끝으로 보고 자동 정지", variable=self.v_detect).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(chk, text="PDF로도 합치기", variable=self.v_pdf).pack(side="left")
        ttk.Label(f2, foreground="#666", wraplength=int(740 * sc), justify="left",
                  text="클릭 전환 모드: 마우스가 대상 창을 클릭해 페이지를 넘기고, 다시 이 앱의 캡처 패널을 클릭한 뒤 캡처합니다. "
                       "대상 창이 비활성 상태에서 찍히므로 글자 커서·마우스 포인터가 이미지에 남지 않습니다. "
                       "클릭 위치는 링크·버튼이 없는 빈 곳으로 지정하세요.").grid(row=6, column=0, columnspan=4, sticky="w", pady=(6, 0))
        f2.columnconfigure(1, weight=1)
        f2.columnconfigure(3, weight=1)

        # 3. 저장 위치
        f3 = ttk.LabelFrame(main, text=" ③ 저장 위치 ", padding=8)
        f3.pack(fill="x", pady=(10, 0))
        self.v_base = tk.StringVar(value=s["base_dir"])
        self.v_prefix = tk.StringVar(value="")
        ttk.Label(f3, text="상위 폴더").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=3)
        ttk.Entry(f3, textvariable=self.v_base).grid(row=0, column=1, sticky="we", pady=3)
        ttk.Button(f3, text="찾아보기…", command=self.pick_folder).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(f3, text="폴더 이름").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=3)
        ttk.Entry(f3, textvariable=self.v_prefix).grid(row=1, column=1, sticky="we", pady=3)
        ttk.Label(f3, text="_날짜_시간 자동 추가", foreground="#666").grid(row=1, column=2, padx=(6, 0), sticky="w")
        f3.columnconfigure(1, weight=1)

        # 4. 실행
        f4 = ttk.Frame(main)
        f4.pack(fill="x", pady=(12, 0))
        self.btn_start = ttk.Button(f4, text="▶ 캡처 시작", style="Big.TButton", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_open = ttk.Button(f4, text="📂 저장 폴더 열기", command=self.open_folder, state="disabled")
        self.btn_open.pack(side="right")

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.pack(fill="x", pady=(10, 4))
        self.v_status = tk.StringVar(value="캡처할 창을 선택하세요.")
        ttk.Label(main, textvariable=self.v_status, foreground="#0B5394").pack(anchor="w")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.refresh_windows()
        self.root.after(50, self.poll_events)

    # ── 창 선택 ──
    def refresh_windows(self):
        self.windows = list_windows(exclude_pid=os.getpid())
        self.tree.delete(*self.tree.get_children())
        for hwnd, title, proc in self.windows:
            self.tree.insert("", "end", iid=str(hwnd), values=(title, proc))
        self.v_status.set(f"창 {len(self.windows)}개를 찾았습니다. 캡처할 창을 선택하세요.")

    def selected_window(self):
        sel = self.tree.selection()
        if not sel:
            return None
        hwnd = int(sel[0])
        for w in self.windows:
            if w[0] == hwnd:
                return w
        return None

    def on_select(self, _=None):
        w = self.selected_window()
        if w:
            self.v_prefix.set(safe_name(w[1]))
            self.v_status.set(f"선택됨: {w[1]}")
            if self.click_ratio_hwnd != w[0]:
                self.reset_click_point()

    # ── 클릭 위치 지정 ──
    def reset_click_point(self):
        self.click_ratio = None
        self.click_ratio_hwnd = None
        self.v_click.set("창 중앙 (기본)")

    def pick_click_point(self):
        w = self.selected_window()
        if not w:
            messagebox.showwarning("창 선택", "먼저 캡처할 창을 목록에서 선택하세요.")
            return
        if not window_exists(w[0]):
            messagebox.showwarning("창 없음", "선택한 창이 닫혔습니다. 목록을 새로고침하세요.")
            return
        activate_window(w[0])
        self.root.iconify()
        user32.GetAsyncKeyState(VK_LBUTTON)  # 이전 상태 비우기
        self.v_status.set("대상 창에서 클릭할 위치를 마우스로 한 번 클릭하세요 (10초)")
        self.root.after(30, self._poll_pick, w[0], False, time.time() + 10)

    def _poll_pick(self, hwnd, was_down, deadline):
        down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        if was_down and not down:
            x, y = cursor_pos()
            if window_at(x, y) == hwnd:
                l, t, r, b = window_rect(hwnd)
                fx = min(max((x - l) / max(1, r - l), 0.0), 1.0)
                fy = min(max((y - t) / max(1, b - t), 0.0), 1.0)
                self.click_ratio, self.click_ratio_hwnd = (fx, fy), hwnd
                self.v_click.set(f"가로 {fx:.0%} · 세로 {fy:.0%}")
                self.v_status.set("클릭 위치를 지정했습니다.")
            else:
                self.v_status.set("선택한 창 밖을 클릭했습니다. 다시 지정하세요.")
            self.root.deiconify()
            return
        if time.time() > deadline:
            self.v_status.set("시간이 지나 클릭 위치 지정을 취소했습니다.")
            self.root.deiconify()
            return
        self.root.after(30, self._poll_pick, hwnd, down, deadline)

    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.v_base.get() or str(Path.home()))
        if d:
            self.v_base.set(d)

    def collect_config(self):
        cfg = {
            "mode": self.v_mode.get(),
            "area": self.v_area.get(),
            "start": self.v_start.get(),
            "scroll": self.v_scroll.get(),
            "repeat": max(1, int(self.v_repeat.get())),
            "max_pages": max(1, int(self.v_max.get())),
            "page_delay": max(0.1, float(self.v_delay.get())),
            "start_delay": max(0, int(self.v_start_delay.get())),
            "detect_end": self.v_detect.get(),
            "format": self.v_format.get(),
            "make_pdf": self.v_pdf.get(),
            "base_dir": self.v_base.get().strip(),
        }
        if cfg["area"] not in AREAS:
            cfg["area"] = AREA_WINDOW
        if cfg["mode"] not in MODES:
            cfg["mode"] = MODE_CLICK
        return cfg

    # ── 캡처 패널 (캡처 중 표시되는 작은 창 = 마우스가 돌아오는 '이 앱') ──
    def make_panel(self, target_hwnd):
        p = tk.Toplevel(self.root)
        p.title("Auto Screenshot")
        p.resizable(False, False)
        p.attributes("-toolwindow", True)
        p.attributes("-topmost", True)
        p.attributes("-alpha", 0.95)  # 레이어드 창으로 만들어 캡처 순간 투명 처리 가능
        frame = tk.Frame(p, bg="#0B5394")
        frame.pack(fill="both", expand=True)
        tk.Button(frame, text="■ 중지", command=self.stop, font=("맑은 고딕", 10, "bold"),
                  bg="#E8F0FE", relief="flat", padx=10).pack(side="right", padx=10, pady=10)
        self.v_panel = tk.StringVar(value="준비 중…")
        tk.Label(frame, textvariable=self.v_panel, bg="#0B5394", fg="white", justify="left", anchor="w",
                 font=("맑은 고딕", 10, "bold")).pack(side="left", fill="both", expand=True, padx=(12, 0))
        x, y = panel_position(target_hwnd, self.click_ratio)
        p.geometry(f"{PANEL_W}x{PANEL_H}+{x}+{y}")
        p.protocol("WM_DELETE_WINDOW", self.stop)
        p.update()
        self.panel = p
        return int(p.wm_frame(), 16)

    def start(self):
        w = self.selected_window()
        if not w:
            messagebox.showwarning("창 선택", "먼저 캡처할 창을 목록에서 선택하세요.")
            return
        if not window_exists(w[0]):
            messagebox.showwarning("창 없음", "선택한 창이 닫혔습니다. 목록을 새로고침하세요.")
            self.refresh_windows()
            return
        try:
            cfg = self.collect_config()
        except (ValueError, tk.TclError):
            messagebox.showerror("설정 오류", "숫자 입력값을 확인하세요.")
            return
        if not cfg["base_dir"]:
            messagebox.showwarning("저장 위치", "상위 폴더를 지정하세요.")
            return
        save_settings(cfg)

        folder = f"{safe_name(self.v_prefix.get() or w[1])}_{datetime.now():%Y%m%d_%H%M%S}"
        out_dir = Path(cfg["base_dir"]) / folder
        self.last_folder = out_dir

        self.progress.configure(maximum=cfg["max_pages"], value=0)
        self.btn_start.configure(state="disabled")
        self.btn_open.configure(state="disabled")

        # 메인 창은 숨기고, 작은 캡처 패널만 띄운다
        self.root.withdraw()
        if user32.IsIconic(w[0]):
            user32.ShowWindow(w[0], SW_RESTORE)
        activate_window(w[0])
        panel_hwnd = self.make_panel(w[0])

        ratio = self.click_ratio if self.click_ratio_hwnd == w[0] else None
        self.job = CaptureJob(w[0], cfg, out_dir, self.events, panel_hwnd, ratio)
        self.job.start()

    def stop(self):
        if self.job:
            self.job.stop_flag.set()

    def open_folder(self):
        if self.last_folder and self.last_folder.exists():
            os.startfile(self.last_folder)

    def poll_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "status":
                    self.v_status.set(data["text"])
                    if self.panel:
                        self.v_panel.set(f"📸 {data['text']}\nEsc: 중지")
                elif kind == "progress":
                    self.progress.configure(value=data["count"])
                    self.v_status.set(f"{data['count']}장 저장됨 → {os.path.basename(data['path'])}")
                    if self.panel:
                        self.v_panel.set(f"📸 캡처 중 · {data['count']}장 저장\n마우스를 건드리지 마세요 · Esc: 중지")
                elif kind == "done":
                    self.finish(data)
        except queue.Empty:
            pass
        self.root.after(50, self.poll_events)

    def finish(self, data):
        self.job = None
        if self.panel:
            self.panel.destroy()
            self.panel = None
        self.btn_start.configure(state="normal")
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(300, lambda: self.root.attributes("-topmost", False))
        count, reason = data["count"], data["reason"]
        if count:
            self.btn_open.configure(state="normal")
            self.v_status.set(f"✅ {count}장 저장 완료 [{reason}] → {data['folder']}")
        else:
            self.v_status.set(f"저장된 이미지 없음 [{reason}]")
        if reason.startswith("오류"):
            messagebox.showerror("캡처 중지", reason)

    def on_close(self):
        self.stop()
        try:
            save_settings(self.collect_config())
        except Exception:
            pass
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    if "--selftest" in sys.argv:
        root.after(1500, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
