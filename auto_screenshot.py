"""
Auto Screenshot - 화면을 페이지 단위로 넘기며 전체 화면을 자동 캡처하는 Windows 앱

기본 동작 (설정 없이 [캡처 시작]만 누르면)
  매 페이지마다
    1) 마우스가 화면 가운데로 이동해 클릭 → 그 자리의 앱(원격 데스크톱·터미널 등) 활성화
    2) Page Down 전송
    3) 마우스가 이 앱의 캡처 패널로 돌아와 클릭 → 이 앱 활성화
    4) 이 앱이 활성화된 상태에서 전체 화면 캡처 (패널은 캡처 순간 투명 처리)
  → 대상 앱이 비활성 상태에서 찍히므로 글자 커서·마우스 포인터가 남지 않고,
    대상 앱이 활성 상태일 때만 걸리는 캡처 제한을 피할 수 있습니다.
  화면이 더 이상 바뀌지 않으면 자동 정지. Esc 또는 마우스를 직접 움직이면 즉시 중지.

필요할 때만 [⚙ 설정 변경]에서 클릭 위치, 특정 창, 캡처 영역, 이동 방식 등을 바꿉니다.

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

APP_VERSION = "1.2.0"

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
user32.MonitorFromPoint.restype = wt.HANDLE
user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.c_void_p]
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
MONITOR_DEFAULTTONEAREST = 2

VK_LBUTTON = 0x01
VK_SHIFT, VK_CONTROL = 0x10, 0x11
VK_ESCAPE, VK_SPACE = 0x1B, 0x20
VK_NEXT, VK_HOME, VK_DOWN = 0x22, 0x24, 0x28
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E}
KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_WHEEL = 0x0800

# 바탕화면·작업표시줄 (화면 가운데에 앱이 없을 때 감지용)
SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

MOUSE_MOVE_STOP_PX = 40  # 사용자가 마우스를 이만큼 움직이면 중지


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


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def list_windows(exclude_pid=None):
    """화면에 보이는 최상위 창 목록 [(hwnd, 제목, 프로세스명)]"""
    result = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        if user32.GetWindowTextLengthW(hwnd) == 0:
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
        title = window_title(hwnd)
        if title == "Program Manager":
            return True
        result.append((hwnd, title, _process_name(hwnd)))
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


def _rect(r):
    return r.left, r.top, r.right, r.bottom


def _monitor_info(hmon):
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
    return mi


def monitor_rect(hwnd):
    return _rect(_monitor_info(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)).rcMonitor)


def monitor_at(x, y, work=False):
    mi = _monitor_info(user32.MonitorFromPoint(wt.POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST))
    return _rect(mi.rcWork if work else mi.rcMonitor)


def primary_center():
    """주 모니터의 가운데 좌표"""
    return user32.GetSystemMetrics(0) // 2, user32.GetSystemMetrics(1) // 2


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
    """창을 맨 앞으로 가져오고 성공 여부 반환"""
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
    """창 영역 기준 비율 (fx, fy) → 화면 좌표. ratio 가 None 이면 창 가운데"""
    l, t, r, b = window_rect(hwnd)
    fx, fy = ratio or (0.5, 0.5)
    return l + (r - l) * fx, t + (b - t) * fy


# ──────────────────────────────────────────────────────────────
# 옵션
# ──────────────────────────────────────────────────────────────
TARGET_SCREEN = "화면 가운데 앱 (원격·터미널 포함)"
TARGET_WINDOW = "목록에서 창 선택"
TARGETS = [TARGET_SCREEN, TARGET_WINDOW]

MODE_CLICK = "마우스 클릭 전환 (캡처 제한 회피)"
MODE_KEY = "키 입력만 (빠름)"
MODES = [MODE_CLICK, MODE_KEY]

AREA_SCREEN = "전체 화면"
AREA_WINDOW = "대상 창 영역만"
AREA_ALL = "모든 모니터"
AREAS = [AREA_SCREEN, AREA_WINDOW, AREA_ALL]

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


def grab(bbox):
    return ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")


def images_same(a, b, threshold=0.002):
    """두 캡처가 사실상 같은지 (깜빡이는 커서·시계 등 미세 변화는 무시)"""
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


# ──────────────────────────────────────────────────────────────
# 캡처 작업
# ──────────────────────────────────────────────────────────────
class Stopped(Exception):
    pass


class CaptureJob(threading.Thread):
    """백그라운드에서 (대상 클릭 → 페이지 이동 → 앱 복귀 → 캡처)를 반복

    target_hwnd 가 None 이면 '화면 모드': 클릭 위치(기본: 주 모니터 가운데)에 있는 앱이
    첫 클릭 때 대상으로 정해진다.
    """

    def __init__(self, cfg, out_dir, events, panel_hwnd, target_hwnd=None, click_ratio=None, click_abs=None):
        super().__init__(daemon=True)
        self.cfg, self.out_dir, self.events = cfg, out_dir, events
        self.panel_hwnd = panel_hwnd
        self.hwnd = target_hwnd
        self.window_mode = target_hwnd is not None
        self.click_ratio = click_ratio
        self.click_abs = tuple(click_abs) if click_abs else None
        self.click_mode = cfg["mode"] == MODE_CLICK
        self.stop_flag = threading.Event()
        self.stop_reason = "사용자 중지"
        self.last_cursor = None
        self.saved = []

    def emit(self, kind, **data):
        self.events.put((kind, data))

    # ── 중지 감시 (Esc / 중지 버튼 / 마우스 직접 움직임) ──
    def mark_cursor(self):
        self.last_cursor = cursor_pos()

    def should_stop(self):
        if self.stop_flag.is_set():
            return True
        if esc_pressed():
            self.stop_reason = "사용자 중지 (Esc)"
        elif self.last_cursor is not None:
            x, y = cursor_pos()
            lx, ly = self.last_cursor
            if abs(x - lx) > MOUSE_MOVE_STOP_PX or abs(y - ly) > MOUSE_MOVE_STOP_PX:
                self.stop_reason = "마우스 움직임을 감지해 중지"
            else:
                return False
        else:
            return False
        self.stop_flag.set()
        return True

    def check_stop(self):
        if self.should_stop():
            raise Stopped

    def wait(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.check_stop()
            time.sleep(0.03)

    # ── 좌표·영역 ──
    def point(self):
        if self.window_mode:
            return click_point(self.hwnd, self.click_ratio)
        return self.click_abs or primary_center()

    def capture_bbox(self):
        area = self.cfg["area"]
        if area == AREA_WINDOW:
            return window_rect(self.hwnd)
        if area == AREA_ALL:
            return None
        # 전체 화면: 대상 창(또는 클릭 위치)이 있는 모니터
        return monitor_rect(self.hwnd) if self.window_mode else monitor_at(*self.point())

    # ── 포커스 전환 ──
    def focus_target(self):
        """클릭 위치의 앱을 활성화 (클릭 모드: 마우스로 클릭)"""
        if self.window_mode and user32.IsIconic(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            time.sleep(0.3)
        x, y = self.point()
        under = window_at(x, y)

        if self.hwnd is None:  # 화면 모드: 첫 클릭 때 대상 결정
            if not under or under == self.panel_hwnd or class_name(under) in SHELL_CLASSES:
                raise RuntimeError("클릭 위치(화면 가운데)에 앱 창이 없습니다. 캡처할 앱을 화면 가운데에 띄우거나 "
                                   "[⚙ 설정 변경]에서 클릭 위치를 지정하세요.")
            self.hwnd = under
            self.emit("target", title=window_title(under) or class_name(under))
        elif under != self.hwnd:
            if self.window_mode:
                activate_window(self.hwnd)
                under = window_at(x, y)
            if under != self.hwnd:
                raise RuntimeError("클릭 위치를 다른 창이 가리고 있어 중지했습니다.")

        if self.click_mode:
            click_at(x, y)
            self.mark_cursor()
        elif foreground_root() != self.hwnd:
            activate_window(self.hwnd)
        time.sleep(0.15)
        if foreground_root() != self.hwnd:
            raise RuntimeError("대상 앱이 활성화되지 않았습니다.")

    def focus_self(self):
        """이 앱(캡처 패널)으로 돌아와 활성화 → 이 상태에서 캡처"""
        if not self.click_mode:
            return
        r = wt.RECT()
        user32.GetWindowRect(self.panel_hwnd, ctypes.byref(r))
        click_at(r.left + (r.right - r.left) * 0.3, r.top + (r.bottom - r.top) * 0.62)
        self.mark_cursor()
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
                mouse_wheel(code, self.point())
                self.mark_cursor()
            time.sleep(0.04)

    def run(self):
        reason = "완료"
        try:
            esc_pressed()  # 이전 키 상태 비우기
            self.mark_cursor()
            for left in range(int(self.cfg["start_delay"]), 0, -1):
                self.emit("status", text=f"{left}초 후 시작 · 마우스를 건드리지 마세요")
                self.wait(1)
            if self.window_mode and not window_exists(self.hwnd):
                raise RuntimeError("대상 창이 닫혔습니다.")

            self.out_dir.mkdir(parents=True, exist_ok=True)
            ext = self.cfg["format"]
            max_pages = self.cfg["max_pages"]
            prev = None

            self.focus_target()
            top = START_OPTIONS[self.cfg["start"]]
            if top:
                press_key(*top)
                self.wait(self.cfg["page_delay"])

            while True:
                self.check_stop()
                if not window_exists(self.hwnd):
                    raise RuntimeError("대상 창이 닫혔습니다.")

                # 이 앱으로 돌아와서 캡처
                self.focus_self()
                time.sleep(0.1)  # 포커스가 빠진 대상 창이 다시 그려질 시간
                self.check_stop()
                img = self.grab_without_panel(self.capture_bbox())

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

                # 대상 앱을 선택하고 페이지 이동
                self.check_stop()
                self.focus_target()
                self.scroll()
                self.wait(self.cfg["page_delay"])
        except Stopped:
            reason = self.stop_reason
        except Exception as e:  # noqa: BLE001
            reason = f"오류: {e}"

        if self.cfg["make_pdf"] and self.saved:
            try:
                self.emit("status", text="PDF로 합치는 중…")
                pages = [Image.open(p).convert("RGB") for p in self.saved]
                pages[0].save(self.out_dir / f"{self.out_dir.name}.pdf", save_all=True,
                              append_images=pages[1:], resolution=96)
            except Exception as e:  # noqa: BLE001
                reason += f" / PDF 실패: {e}"
        self.emit("done", reason=reason, count=len(self.saved), folder=str(self.out_dir))


# ──────────────────────────────────────────────────────────────
# 설정
# ──────────────────────────────────────────────────────────────
SETTINGS_FILE = Path.home() / ".auto_screenshot.json"
SETTINGS_VERSION = 2
DEFAULTS = {
    "version": SETTINGS_VERSION,
    "target": TARGET_SCREEN,
    "click_abs": None,          # None = 화면 가운데
    "mode": MODE_CLICK,
    "area": AREA_SCREEN,
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
CHOICES = {"target": TARGETS, "mode": MODES, "area": AREAS,
           "start": list(START_OPTIONS), "scroll": list(SCROLL_OPTIONS), "format": ["png", "jpg"]}

PANEL_W, PANEL_H = 360, 92


def load_settings():
    data = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if saved.get("version") == SETTINGS_VERSION:  # 예전 버전 설정은 무시하고 새 기본값 사용
            data.update(saved)
    except Exception:
        pass
    for k, values in CHOICES.items():
        if data.get(k) not in values:
            data[k] = DEFAULTS[k]
    return data


def save_settings(data):
    try:
        SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def panel_position(click_xy, avoid_rect=None):
    """캡처 패널 위치: 클릭 위치가 있는 모니터의 모서리 중 클릭 위치(와 대상 창)를 가리지 않는 곳"""
    cx, cy = click_xy
    l, t, r, b = monitor_at(cx, cy, work=True)
    m = 12
    cands = [(r - PANEL_W - m, b - PANEL_H - m), (l + m, b - PANEL_H - m),
             (r - PANEL_W - m, t + m), (l + m, t + m)]

    def covers_click(x, y):
        return point_in((x - 30, y - 50, x + PANEL_W + 30, y + PANEL_H + 30), cx, cy)

    if avoid_rect:
        for x, y in cands:
            if not rects_overlap((x, y, x + PANEL_W, y + PANEL_H), avoid_rect) and not covers_click(x, y):
                return x, y
    for x, y in cands:
        if not covers_click(x, y):
            return x, y
    return cands[0]


# ──────────────────────────────────────────────────────────────
# GUI
# ──────────────────────────────────────────────────────────────
class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.job = None
        self.panel = None
        self.windows = []
        self.last_folder = None
        self.click_ratio = None       # 창 선택 모드의 클릭 위치 (창 기준 비율)
        self.click_ratio_hwnd = None
        s = load_settings()
        self.click_abs = tuple(s["click_abs"]) if s["click_abs"] else None  # 화면 모드 클릭 위치

        self.sc = sc = max(1.0, root.winfo_fpixels("1i") / 96)
        root.title(f"Auto Screenshot {APP_VERSION}")
        root.minsize(int(640 * sc), 1)
        root.resizable(True, False)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Big.TButton", font=("맑은 고딕", 13, "bold"), padding=(18, 12))
        style.configure("Title.TLabel", font=("맑은 고딕", 15, "bold"))
        style.configure("Treeview", rowheight=int(22 * sc))

        # 변수
        self.v_target = tk.StringVar(value=s["target"])
        self.v_mode = tk.StringVar(value=s["mode"])
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
        self.v_base = tk.StringVar(value=s["base_dir"])
        self.v_click = tk.StringVar()
        self.v_summary = tk.StringVar()
        self.v_status = tk.StringVar(value="캡처할 화면(원격 데스크톱·터미널·문서)을 띄워 두고 [캡처 시작]을 누르세요.")

        main = ttk.Frame(root, padding=14)
        main.pack(fill="both", expand=True)

        # ── 기본 화면 ──
        ttk.Label(main, text="📸 Auto Screenshot", style="Title.TLabel").pack(anchor="w")
        ttk.Label(main, textvariable=self.v_summary, foreground="#444", wraplength=int(600 * sc),
                  justify="left").pack(anchor="w", pady=(4, 10))

        row = ttk.Frame(main)
        row.pack(fill="x")
        self.btn_start = ttk.Button(row, text="▶ 캡처 시작", style="Big.TButton", command=self.start)
        self.btn_start.pack(side="left")
        ttk.Label(row, foreground="#666", justify="left",
                  text="진행 중 중지: Esc 또는\n마우스를 직접 움직이기").pack(side="left", padx=14)

        save = ttk.Frame(main)
        save.pack(fill="x", pady=(12, 0))
        ttk.Label(save, text="저장 폴더").pack(side="left")
        ttk.Entry(save, textvariable=self.v_base).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(save, text="찾아보기…", command=self.pick_folder).pack(side="left")
        self.btn_open = ttk.Button(save, text="📂 열기", command=self.open_folder)
        self.btn_open.pack(side="left", padx=(6, 0))

        self.progress = ttk.Progressbar(main, mode="determinate")
        self.progress.pack(fill="x", pady=(12, 4))
        ttk.Label(main, textvariable=self.v_status, foreground="#0B5394", wraplength=int(600 * sc),
                  justify="left").pack(anchor="w")

        self.btn_adv = ttk.Button(main, text="⚙ 설정 변경 ▸", command=self.toggle_advanced)
        self.btn_adv.pack(anchor="w", pady=(10, 0))

        # ── 상세 설정 (접힘) ──
        self.adv = ttk.LabelFrame(main, text=" 상세 설정 — 필요할 때만 바꾸세요 ", padding=10)
        a = self.adv

        def grid_row(r, label, widget, col=0):
            ttk.Label(a, text=label).grid(row=r, column=col, sticky="w", padx=(0, 6), pady=3)
            widget.grid(row=r, column=col + 1, sticky="we", padx=(0, 14), pady=3)

        grid_row(0, "캡처 대상", ttk.Combobox(a, textvariable=self.v_target, values=TARGETS, state="readonly", width=30))
        click_box = ttk.Frame(a)
        ttk.Label(click_box, textvariable=self.v_click, width=18).pack(side="left")
        ttk.Button(click_box, text="📍 지정", width=7, command=self.pick_click_point).pack(side="left", padx=(4, 0))
        ttk.Button(click_box, text="가운데", width=6, command=self.reset_click_point).pack(side="left", padx=(4, 0))
        grid_row(0, "클릭 위치", click_box, col=2)

        # 창 목록 (캡처 대상 = 목록에서 창 선택 일 때만 표시)
        self.win_frame = ttk.Frame(a)
        bar = ttk.Frame(self.win_frame)
        bar.pack(fill="x")
        ttk.Label(bar, text="캡처할 창을 선택하세요.").pack(side="left")
        ttk.Button(bar, text="🔄 새로고침", command=self.refresh_windows).pack(side="right")
        tw = ttk.Frame(self.win_frame)
        tw.pack(fill="both", expand=True, pady=(4, 0))
        self.tree = ttk.Treeview(tw, columns=("title", "proc"), show="headings", height=6, selectmode="browse")
        self.tree.heading("title", text="창 제목")
        self.tree.heading("proc", text="프로그램")
        self.tree.column("title", width=int(420 * sc))
        self.tree.column("proc", width=int(140 * sc))
        sb = ttk.Scrollbar(tw, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        grid_row(2, "캡처 영역", ttk.Combobox(a, textvariable=self.v_area, values=AREAS, state="readonly", width=30))
        grid_row(2, "페이지 이동", ttk.Combobox(a, textvariable=self.v_scroll, values=list(SCROLL_OPTIONS), state="readonly", width=28), col=2)
        grid_row(3, "시작 위치", ttk.Combobox(a, textvariable=self.v_start, values=list(START_OPTIONS), state="readonly", width=30))
        grid_row(3, "1회 이동 반복", ttk.Spinbox(a, from_=1, to=50, textvariable=self.v_repeat, width=8), col=2)
        grid_row(4, "최대 캡처 장수", ttk.Spinbox(a, from_=1, to=2000, textvariable=self.v_max, width=8))
        grid_row(4, "페이지 간 대기(초)", ttk.Spinbox(a, from_=0.1, to=10, increment=0.1, textvariable=self.v_delay, width=8), col=2)
        grid_row(5, "시작 전 대기(초)", ttk.Spinbox(a, from_=0, to=30, textvariable=self.v_start_delay, width=8))
        grid_row(5, "이미지 형식", ttk.Combobox(a, textvariable=self.v_format, values=["png", "jpg"], state="readonly", width=8), col=2)
        grid_row(6, "동작 방식", ttk.Combobox(a, textvariable=self.v_mode, values=MODES, state="readonly", width=30))
        chk = ttk.Frame(a)
        chk.grid(row=7, column=0, columnspan=4, sticky="w", pady=(4, 0))
        ttk.Checkbutton(chk, text="화면 변화가 없으면 끝으로 보고 자동 정지", variable=self.v_detect).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(chk, text="PDF로도 합치기", variable=self.v_pdf).pack(side="left")
        ttk.Button(a, text="모든 설정 기본값으로", command=self.reset_all).grid(row=8, column=0, columnspan=4, sticky="w", pady=(8, 0))
        a.columnconfigure(1, weight=1)
        a.columnconfigure(3, weight=1)

        for v in (self.v_target, self.v_mode, self.v_area, self.v_scroll, self.v_start,
                  self.v_max, self.v_detect, self.v_pdf):
            v.trace_add("write", lambda *_: self.update_summary())
        self.v_target.trace_add("write", lambda *_: self.on_target_change())

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.on_target_change()
        self.update_summary()
        self.root.after(50, self.poll_events)

    # ── 표시 ──
    def window_mode(self):
        return self.v_target.get() == TARGET_WINDOW

    def click_desc(self):
        if self.window_mode():
            if self.click_ratio and self.selected_window() and self.click_ratio_hwnd == self.selected_window()[0]:
                fx, fy = self.click_ratio
                return f"창 안 (가로 {fx:.0%}, 세로 {fy:.0%})"
            return "창 가운데"
        if self.click_abs:
            return f"지정 위치 ({self.click_abs[0]}, {self.click_abs[1]})"
        return "화면 가운데"

    def update_summary(self):
        self.v_click.set(self.click_desc())
        try:
            mx = int(self.v_max.get())
        except (tk.TclError, ValueError):
            mx = "?"
        area = self.v_area.get()
        who = "선택한 창" if self.window_mode() else self.click_desc()
        scroll = self.v_scroll.get().split(" (")[0]
        parts = [f"현재 설정: {area} 캡처", f"{who} 클릭 → {scroll}"]
        if self.v_mode.get() == MODE_KEY:
            parts[1] = f"{who}에 {scroll} (키 입력만)"
        if self.v_detect.get():
            parts.append("끝에서 자동 정지")
        parts.append(f"최대 {mx}장")
        if self.v_pdf.get():
            parts.append("PDF 합치기")
        self.v_summary.set(" · ".join(parts))

    def toggle_advanced(self):
        if self.adv.winfo_ismapped():
            self.adv.pack_forget()
            self.btn_adv.configure(text="⚙ 설정 변경 ▸")
        else:
            self.adv.pack(fill="x", pady=(8, 0))
            self.btn_adv.configure(text="⚙ 설정 변경 ▾ (닫기)")
        self.root.geometry("")  # 내용에 맞춰 창 높이 자동 조정

    def on_target_change(self):
        if self.window_mode():
            self.win_frame.grid(row=1, column=0, columnspan=4, sticky="we", pady=(4, 8))
            if not self.windows:
                self.refresh_windows()
            if self.v_area.get() == AREA_SCREEN:
                self.v_area.set(AREA_WINDOW)
        else:
            self.win_frame.grid_remove()
        self.update_summary()
        self.root.geometry("")

    # ── 창 선택 (상세 설정) ──
    def refresh_windows(self):
        self.windows = list_windows(exclude_pid=os.getpid())
        self.tree.delete(*self.tree.get_children())
        for hwnd, title, proc in self.windows:
            self.tree.insert("", "end", iid=str(hwnd), values=(title, proc))

    def selected_window(self):
        sel = self.tree.selection()
        if not sel:
            return None
        hwnd = int(sel[0])
        return next((w for w in self.windows if w[0] == hwnd), None)

    def on_select(self, _=None):
        w = self.selected_window()
        if w:
            self.v_status.set(f"선택한 창: {w[1]}")
        self.update_summary()

    # ── 클릭 위치 ──
    def reset_click_point(self):
        self.click_abs = None
        self.click_ratio = None
        self.click_ratio_hwnd = None
        self.update_summary()

    def pick_click_point(self):
        hwnd = None
        if self.window_mode():
            w = self.selected_window()
            if not w or not window_exists(w[0]):
                messagebox.showwarning("창 선택", "먼저 목록에서 캡처할 창을 선택하세요.")
                return
            hwnd = w[0]
            activate_window(hwnd)
        self.root.withdraw()
        user32.GetAsyncKeyState(VK_LBUTTON)  # 이전 상태 비우기
        self.root.after(30, self._poll_pick, hwnd, False, time.time() + 15)

    def _poll_pick(self, hwnd, was_down, deadline):
        down = bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)
        if was_down and not down:
            x, y = cursor_pos()
            if hwnd is None:
                self.click_abs = (x, y)
                self.v_status.set(f"클릭 위치를 ({x}, {y})로 지정했습니다.")
            elif window_at(x, y) == hwnd:
                l, t, r, b = window_rect(hwnd)
                self.click_ratio = (min(max((x - l) / max(1, r - l), 0.0), 1.0),
                                    min(max((y - t) / max(1, b - t), 0.0), 1.0))
                self.click_ratio_hwnd = hwnd
                self.v_status.set("창 안의 클릭 위치를 지정했습니다.")
            else:
                self.v_status.set("선택한 창 밖을 클릭했습니다. 다시 지정하세요.")
            self.update_summary()
            self.root.deiconify()
            return
        if time.time() > deadline:
            self.v_status.set("시간이 지나 클릭 위치 지정을 취소했습니다.")
            self.root.deiconify()
            return
        self.root.after(30, self._poll_pick, hwnd, down, deadline)

    # ── 기타 ──
    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.v_base.get() or str(Path.home()))
        if d:
            self.v_base.set(d)

    def open_folder(self):
        target = self.last_folder if self.last_folder and self.last_folder.exists() else Path(self.v_base.get())
        if target.exists():
            os.startfile(target)
        else:
            messagebox.showinfo("저장 폴더", "아직 저장된 캡처가 없습니다.")

    def reset_all(self):
        d = DEFAULTS
        self.v_target.set(d["target"]); self.v_mode.set(d["mode"]); self.v_area.set(d["area"])
        self.v_start.set(d["start"]); self.v_scroll.set(d["scroll"]); self.v_repeat.set(d["repeat"])
        self.v_max.set(d["max_pages"]); self.v_delay.set(d["page_delay"]); self.v_start_delay.set(d["start_delay"])
        self.v_detect.set(d["detect_end"]); self.v_format.set(d["format"]); self.v_pdf.set(d["make_pdf"])
        self.reset_click_point()
        self.v_status.set("모든 설정을 기본값으로 되돌렸습니다.")

    def collect_config(self):
        cfg = {
            "version": SETTINGS_VERSION,
            "target": self.v_target.get(),
            "click_abs": list(self.click_abs) if self.click_abs else None,
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
        for k, values in CHOICES.items():
            if cfg[k] not in values:
                cfg[k] = DEFAULTS[k]
        return cfg

    # ── 캡처 패널 (캡처 중 표시되는 작은 창 = 마우스가 돌아오는 '이 앱') ──
    def make_panel(self, click_xy, avoid_rect):
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
        x, y = panel_position(click_xy, avoid_rect)
        p.geometry(f"{PANEL_W}x{PANEL_H}+{x}+{y}")
        p.protocol("WM_DELETE_WINDOW", self.stop)
        p.update()
        self.panel = p
        return int(p.wm_frame(), 16)

    def start(self):
        try:
            cfg = self.collect_config()
        except (ValueError, tk.TclError):
            messagebox.showerror("설정 오류", "상세 설정의 숫자 입력값을 확인하세요.")
            return
        if not cfg["base_dir"]:
            messagebox.showwarning("저장 폴더", "저장 폴더를 지정하세요.")
            return

        target_hwnd, ratio = None, None
        if cfg["target"] == TARGET_WINDOW:
            w = self.selected_window()
            if not w or not window_exists(w[0]):
                messagebox.showwarning("창 선택", "[⚙ 설정 변경]의 목록에서 캡처할 창을 선택하세요.\n"
                                                 "(또는 캡처 대상을 '화면 가운데 앱'으로 바꾸세요)")
                self.refresh_windows()
                return
            target_hwnd = w[0]
            ratio = self.click_ratio if self.click_ratio_hwnd == target_hwnd else None
            folder = safe_name(w[1])
        else:
            folder = "Screenshot"
        save_settings(cfg)

        out_dir = Path(cfg["base_dir"]) / f"{folder}_{datetime.now():%Y%m%d_%H%M%S}"
        self.last_folder = out_dir
        self.progress.configure(maximum=cfg["max_pages"], value=0)
        self.btn_start.configure(state="disabled")

        # 메인 창은 숨기고, 작은 캡처 패널만 띄운다
        self.root.withdraw()
        if target_hwnd:
            activate_window(target_hwnd)
            click_xy, avoid = click_point(target_hwnd, ratio), window_rect(target_hwnd)
        else:
            click_xy, avoid = (self.click_abs or primary_center()), None
        panel_hwnd = self.make_panel(click_xy, avoid)

        self.job = CaptureJob(cfg, out_dir, self.events, panel_hwnd,
                              target_hwnd=target_hwnd, click_ratio=ratio, click_abs=self.click_abs)
        self.job.start()

    def stop(self):
        if self.job:
            self.job.stop_flag.set()

    def poll_events(self):
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "status":
                    self.v_status.set(data["text"])
                    if self.panel:
                        self.v_panel.set(f"📸 {data['text']}\nEsc 또는 마우스를 움직이면 중지")
                elif kind == "target":
                    self.v_status.set(f"대상 앱: {data['title']}")
                elif kind == "progress":
                    self.progress.configure(value=data["count"])
                    self.v_status.set(f"{data['count']}장 저장됨 → {os.path.basename(data['path'])}")
                    if self.panel:
                        self.v_panel.set(f"📸 캡처 중 · {data['count']}장 저장\nEsc 또는 마우스를 움직이면 중지")
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
