"""
بوت تذكير: الوقوف والشرب كل X دقيقة نشاط على Windows، مع احترام أوقات الصلاة حسب المدينة.
Hydration reminder + prayer-quiet windows using Aladhan API (no API key).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

PRAYER_AR: dict[str, str] = {
    "Fajr": "الفجر",
    "Dhuhr": "الظهر",
    "Asr": "العصر",
    "Maghrib": "المغرب",
    "Isha": "العشاء",
}

# عرض العداد الجانبي (واجهة إنجليزية)
PRAYER_EN: dict[str, str] = {
    "Fajr": "Fajr",
    "Dhuhr": "Dhuhr",
    "Asr": "Asr",
    "Maghrib": "Maghrib",
    "Isha": "Isha",
}

try:
    from winotify import Notification as WinNotification
    from winotify import audio
except ImportError:
    WinNotification = None  # type: ignore

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def _idle_seconds() -> float:
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
        return 0.0
    tick = ctypes.windll.kernel32.GetTickCount()
    idle_ms = tick - li.dwTime
    return max(0.0, idle_ms / 1000.0)


def default_config_path() -> Path:
    return Path(__file__).resolve().parent / "config.yaml"


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if yaml:
        data = yaml.safe_load(text)
        return data if isinstance(data, dict) else {}
    # minimal YAML subset: key: value lines
    out: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        elif v.isdigit():
            out[k] = int(v)
        else:
            out[k] = v.strip("\"'")
    return out


PREWARN_SECONDS = 60.0


@dataclass
class UiState:
    """حالة الشريط العلوي (يُحدَّث من خيط البوت، يُقرأ من Tk)."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    line1: str = ""
    line2: str = ""
    alert: str = ""
    urgent: bool = False
    freeze_request_title: str = ""
    freeze_request_minutes: int = 0


@dataclass
class Settings:
    city: str = "Cairo"
    country: str = "Egypt"
    reminder_interval_minutes: int = 50
    idle_threshold_seconds: int = 120
    prayer_quiet_window_minutes: int = 10
    prayer_times_refresh_hours: int = 12
    hydration_break_minutes: int = 10


def merge_settings(cfg: dict[str, Any]) -> Settings:
    s = Settings()
    if "city" in cfg:
        s.city = str(cfg["city"])
    if "country" in cfg:
        s.country = str(cfg["country"])
    if "reminder_interval_minutes" in cfg:
        s.reminder_interval_minutes = int(cfg["reminder_interval_minutes"])
    if "idle_threshold_seconds" in cfg:
        s.idle_threshold_seconds = int(cfg["idle_threshold_seconds"])
    if "prayer_quiet_window_minutes" in cfg:
        s.prayer_quiet_window_minutes = int(cfg["prayer_quiet_window_minutes"])
    if "prayer_times_refresh_hours" in cfg:
        s.prayer_times_refresh_hours = int(cfg["prayer_times_refresh_hours"])
    if "hydration_break_minutes" in cfg:
        s.hydration_break_minutes = int(cfg["hydration_break_minutes"])
    return s


# أسماء الصلوات في استجابة API (توقيت محلي للمدينة)
PRAYER_KEYS = ("Fajr", "Dhuhr", "Asr", "Maghrib", "Isha")


class PrayerTimesService:
    def __init__(self, city: str, country: str) -> None:
        self.city = city
        self.country = country
        self.cache_file = Path(__file__).resolve().parent / "prayer_cache.json"

    def fetch(self) -> tuple[dict[str, datetime], bool]:
        today = date.today()
        url = "https://api.aladhan.com/v1/timingsByCity"
        params = {"city": self.city, "country": self.country}
        
        timings = {}
        from_cache = False
        try:
            r = requests.get(url, params=params, timeout=10)
            r.raise_for_status()
            data = r.json()
            timings = data.get("data", {}).get("timings", {})
            if timings:
                cache_data = {k: str(timings[k]).split()[0] for k in PRAYER_KEYS if k in timings}
                try:
                    self.cache_file.write_text(json.dumps(cache_data), encoding="utf-8")
                except Exception as e:
                    print(f"Failed to write cache: {e}", flush=True)
        except Exception as e:
            print(f"API fetch failed: {e}. Trying cache...", flush=True)
            if self.cache_file.is_file():
                try:
                    timings = json.loads(self.cache_file.read_text(encoding="utf-8"))
                    print("Loaded prayer times from cache.", flush=True)
                    from_cache = True
                except Exception:
                    pass
            if not timings:
                raise ValueError("No API response and no cache available.")

        out: dict[str, datetime] = {}
        for k in PRAYER_KEYS:
            if k not in timings:
                continue
            # "04:30 (EET)" or "04:30"
            raw = str(timings[k]).split()[0]
            h, m = [int(x) for x in raw.split(":")[:2]]
            out[k] = datetime.combine(today, datetime.min.time().replace(hour=h, minute=m))
        return out, from_cache


def fmt_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def next_prayer_eta(
    wall: datetime, prayer_times: dict[str, datetime]
) -> tuple[str, datetime, float] | None:
    """أقرب صلاة قادمة: (الاسم الإنجليزي, الوقت, ثوانٍ متبقية)."""
    if not prayer_times:
        return None
    day = wall.date()
    candidates: list[tuple[str, datetime]] = []
    for name in PRAYER_KEYS:
        t = prayer_times.get(name)
        if t is None:
            continue
        if t > wall:
            candidates.append((name, t))
    fajr = prayer_times.get("Fajr")
    if fajr is not None:
        tomorrow = day + timedelta(days=1)
        fajr_next = datetime.combine(tomorrow, fajr.time())
        candidates.append(("Fajr", fajr_next))
    if not candidates:
        return None
    name, at = min(candidates, key=lambda x: x[1])
    return name, at, (at - wall).total_seconds()


def in_prayer_quiet_window(
    now: datetime,
    prayer_times: dict[str, datetime],
    window_minutes: int,
) -> tuple[bool, str | None]:
    """Returns (is_quiet, reason_prayer_name)."""
    for name, t in prayer_times.items():
        start = t
        end = t + timedelta(minutes=window_minutes)
        if start <= now <= end:
            return True, name
    return False, None


def notify_hydration(title: str, msg: str) -> None:
    if WinNotification:
        toast = WinNotification(app_id="HydrationPrayerBot", title=title, msg=msg)
        toast.set_audio(audio.Default, loop=False)
        toast.show()
        return
    # fallback: PowerShell toast
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, "
        "Windows.UI.Notifications, ContentType = WindowsRuntime]::"
        "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
        f"$t = $xml.GetElementsByTagName('text'); $t[0].AppendChild($xml.CreateTextNode('{title}')); "
        f"$t[1].AppendChild($xml.CreateTextNode('{msg}')); "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('HydrationPrayerBot').Show($toast)"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        print(f"[notify] {title}: {msg}", flush=True)


def _creation_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def run_loop(settings: Settings, stop_event: threading.Event, ui: UiState) -> None:
    prayer_svc = PrayerTimesService(settings.city, settings.country)
    prayer_times: dict[str, datetime] = {}
    last_prayer_fetch = 0.0
    active_accumulator = 0.0
    last_tick = time.monotonic()
    prayer_power_fired: set[str] = set()
    prayer_prewarn_sent: set[str] = set()
    hydration_prewarn_sent_this_cycle = False

    def refresh_prayers() -> None:
        nonlocal prayer_times, last_prayer_fetch
        try:
            prayer_times, from_cache = prayer_svc.fetch()
            if from_cache:
                # Retry in 1 hour if loaded from cache
                last_prayer_fetch = time.time() - (settings.prayer_times_refresh_hours * 3600) + 3600
                print(f"Using cached prayer times. Will retry API in 1 hour.", flush=True)
            else:
                last_prayer_fetch = time.time()
                print(f"Prayer times for {settings.city}, {settings.country}:", prayer_times, flush=True)
        except Exception as e:
            print(f"Prayer fetch error: {e}", flush=True)
            # Try again in 5 minutes if it completely failed
            last_prayer_fetch = time.time() - (settings.prayer_times_refresh_hours * 3600) + 300

    refresh_prayers()
    last_calendar_day: date = date.today()

    while not stop_event.is_set():
        now = time.monotonic()
        dt = now - last_tick
        last_tick = now
        wall = datetime.now()
        today_d = wall.date()

        if last_calendar_day != today_d:
            last_calendar_day = today_d
            refresh_prayers()
            prayer_power_fired.clear()
            prayer_prewarn_sent.clear()

        idle = _idle_seconds()
        is_busy = idle < settings.idle_threshold_seconds

        quiet, pname = in_prayer_quiet_window(wall, prayer_times, settings.prayer_quiet_window_minutes)

        if quiet and pname:
            key = f"{wall.date()}_{pname}"
            if key not in prayer_power_fired:
                prayer_power_fired.add(key)
                print(f"Prayer window ({pname}): freezing screen...", flush=True)
                ar_name = PRAYER_AR.get(pname, pname)
                with ui.lock:
                    ui.freeze_request_title = f"قوم صلي {ar_name}"
                    ui.freeze_request_minutes = settings.prayer_quiet_window_minutes

        if quiet:
            active_accumulator = 0.0
        elif is_busy:
            active_accumulator += dt
        else:
            pass

        need_seconds = settings.reminder_interval_minutes * 60
        if not quiet and is_busy and active_accumulator >= need_seconds:
            notify_hydration(
                "وقت الشرب والحركة",
                f"قوم اشرب مية — الشاشة هتتجمد لمدة {settings.hydration_break_minutes} دقايق!",
            )
            time.sleep(5)
            if settings.hydration_break_minutes > 0:
                with ui.lock:
                    ui.freeze_request_title = "قوم اشرب واتحرك"
                    ui.freeze_request_minutes = settings.hydration_break_minutes
                
            active_accumulator = 0.0
            hydration_prewarn_sent_this_cycle = False
            last_tick = time.monotonic()
            continue

        # ——— واجهة العداد (إنجليزي، صندوق جانبي) + تنبيه الدقيقة ———
        np = next_prayer_eta(wall, prayer_times)
        if np:
            p_en_key, _p_at, p_sec = np
            p_label = PRAYER_EN.get(p_en_key, p_en_key)
            line_pray = f"{p_label} in {fmt_duration(p_sec)}"
        else:
            p_sec = None
            line_pray = "Prayer times: —"

        if quiet:
            line_hydr = "Hydration off (prayer)"
            h_sec = None
        elif is_busy:
            h_sec = max(0.0, need_seconds - active_accumulator)
            line_hydr = f"Drink/move in {fmt_duration(h_sec)}"
        else:
            h_sec = None
            line_hydr = "Hydration paused (idle)"

        alert_parts: list[str] = []
        urgent = False
        if np and p_sec is not None and 0 < p_sec <= PREWARN_SECONDS:
            urgent = True
            alert_parts.append("Go pray — screen freezes in <1 min!")

        if (
            not quiet
            and is_busy
            and h_sec is not None
            and 0 < h_sec <= PREWARN_SECONDS
        ):
            urgent = True
            alert_parts.append("Drink water — full reminder in <1 min!")

        alert_text = "  ·  ".join(alert_parts)

        with ui.lock:
            ui.line1 = line_pray
            ui.line2 = line_hydr
            ui.alert = alert_text
            ui.urgent = urgent

        # إشعار منبّه مرة واحدة قبل دقيقة (صلاه / شرب)
        if np and p_sec is not None and 0 < p_sec <= PREWARN_SECONDS:
            _, p_at, _ = np
            pk = f"pray_pre_{p_at.isoformat()}"
            if pk not in prayer_prewarn_sent:
                prayer_prewarn_sent.add(pk)
                sub = f"الشاشة هتتجمد بعد دقيقة لمدة {settings.prayer_quiet_window_minutes} دقايق."
                line = f"قوم صلي — {sub}"
                notify_hydration("تذكير صلاة", line)

        if (
            not quiet
            and is_busy
            and h_sec is not None
            and 0 < h_sec <= PREWARN_SECONDS
            and not hydration_prewarn_sent_this_cycle
        ):
            hydration_prewarn_sent_this_cycle = True
            h_body = f"قوم اشرب مية — الشاشة هتتجمد بعد دقيقة لمدة {settings.hydration_break_minutes} دقايق."
            notify_hydration("قرب موعد الشرب والحركة", h_body)

        if time.time() - last_prayer_fetch > settings.prayer_times_refresh_hours * 3600:
            refresh_prayers()

        stop_event.wait(1.0)


def _unregister_f12_hotkey() -> None:
    try:
        ctypes.windll.user32.UnregisterHotKey(None, 1)
    except Exception:
        pass


def run_overlay(stop_event: threading.Event, ui: UiState) -> None:
    import tkinter as tk
    from tkinter import font as tkfont

    if sys.platform == "win32":
        class _POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        class _MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint32),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_size_t),
                ("time", ctypes.c_uint32),
                ("pt", _POINT),
            ]

    root = tk.Tk()
    root.title("HydrationPrayerBot")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.95)
    bg = "#16161e"
    bg_u = "#3d1518"
    root.configure(bg=bg)

    root.update_idletasks()
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    margin = 12
    current_x = margin
    current_y = sh - 150 - margin - 48
    is_dragged = False

    def on_drag_start(event: tk.Event) -> None:
        nonlocal is_dragged
        is_dragged = True
        root._drag_start_x = event.x
        root._drag_start_y = event.y

    def on_drag_motion(event: tk.Event) -> None:
        nonlocal current_x, current_y
        current_x = event.x_root - getattr(root, "_drag_start_x", 0)
        current_y = event.y_root - getattr(root, "_drag_start_y", 0)
        root.geometry(f"+{current_x}+{current_y}")

    root.geometry(f"+{current_x}+{current_y}")

    f_title = tkfont.Font(family="Segoe UI", size=9, weight="bold")
    f_sub = tkfont.Font(family="Segoe UI", size=8)
    f_alert = tkfont.Font(family="Segoe UI", size=8, weight="bold")
    f_x = tkfont.Font(family="Segoe UI", size=10, weight="bold")

    panel_hidden = [False]

    def hide_panel() -> None:
        """يخفي العداد فقط — البوت والتذكيرات يفضلوا شغالين."""
        root.withdraw()
        panel_hidden[0] = True

    def show_panel() -> None:
        root.deiconify()
        root.lift()
        root.attributes("-topmost", True)
        panel_hidden[0] = False

    def toggle_panel() -> None:
        if panel_hidden[0]:
            show_panel()
        else:
            hide_panel()

    def quit_application() -> None:
        stop_event.set()
        _unregister_f12_hotkey()
        try:
            root.destroy()
        except tk.TclError:
            pass

    def start_f12_hotkey() -> None:
        if sys.platform != "win32":
            return

        VK_F12 = 0x7B
        WM_HOTKEY = 0x0312
        PM_REMOVE = 0x0001
        user32 = ctypes.windll.user32
        if user32.RegisterHotKey(None, 1, 0, VK_F12) == 0:
            print("F12 hotkey unavailable (another app may use it).", flush=True)
            return

        def pump() -> None:
            msg = _MSG()
            while not stop_event.is_set():
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_HOTKEY:
                        root.after(0, toggle_panel)
                time.sleep(0.05)
            _unregister_f12_hotkey()

        threading.Thread(target=pump, daemon=True).start()

    start_f12_hotkey()

    bar = tk.Frame(root, bg=bg)
    bar.pack(fill=tk.BOTH, expand=True)
    btn = tk.Button(
        bar,
        text="×",
        font=f_x,
        fg="#cccccc",
        bg=bg,
        activebackground=bg_u,
        activeforeground="#ffffff",
        bd=0,
        highlightthickness=0,
        command=hide_panel,
        cursor="hand2",
    )
    btn.pack(side=tk.RIGHT, padx=(0, 4), pady=2)
    wrap = tk.Frame(bar, bg=bg)
    wrap.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0))

    l1 = tk.Label(
        wrap,
        text="",
        bg=bg,
        fg="#f0f0f5",
        font=f_title,
        justify=tk.LEFT,
        anchor="w",
    )
    l1.pack(fill=tk.X, pady=(4, 0))
    l2 = tk.Label(
        wrap,
        text="",
        bg=bg,
        fg="#a8c8e8",
        font=f_sub,
        justify=tk.LEFT,
        anchor="w",
    )
    l2.pack(fill=tk.X)
    la = tk.Label(
        wrap,
        text="",
        bg=bg,
        fg="#ff9090",
        font=f_alert,
        justify=tk.LEFT,
        anchor="w",
    )
    la.pack(fill=tk.X, pady=(0, 4))

    def bind_drag(widget: tk.Widget) -> None:
        widget.bind("<Button-1>", on_drag_start, add="+")
        widget.bind("<B1-Motion>", on_drag_motion, add="+")

    bind_drag(root)
    bind_drag(bar)
    bind_drag(wrap)
    bind_drag(l1)
    bind_drag(l2)
    bind_drag(la)

    def show_freeze_window(parent: tk.Tk, title_text: str, minutes: int) -> None:
        top = tk.Toplevel(parent)
        top.attributes("-fullscreen", True)
        top.attributes("-topmost", True)
        top.configure(bg="black")
        
        top.bind("<Escape>", lambda e: "break")
        top.bind("<Alt-F4>", lambda e: "break")
        top.protocol("WM_DELETE_WINDOW", lambda: None)
        
        title_label = tk.Label(
            top,
            text=title_text,
            font=("Arial", 36, "bold"),
            fg="white",
            bg="black"
        )
        title_label.pack(expand=True, pady=(100, 20))
        
        desc_label = tk.Label(
            top,
            text="لن تعمل أي برامج أو مفاتيح حتى انتهاء الوقت",
            font=("Arial", 18),
            fg="gray",
            bg="black"
        )
        desc_label.pack()
        
        counter_label = tk.Label(
            top,
            text="",
            font=("Arial", 48, "bold"),
            fg="yellow",
            bg="black"
        )
        counter_label.pack(pady=50)
        
        remaining_seconds = minutes * 60
        
        def update_counter():
            nonlocal remaining_seconds
            if remaining_seconds <= 0:
                top.destroy()
                return
            m = remaining_seconds // 60
            s = remaining_seconds % 60
            counter_label.config(text=f"{m:02d}:{s:02d}")
            remaining_seconds -= 1
            top.after(1000, update_counter)
            
        update_counter()

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label="Hide panel (bot keeps running)", command=hide_panel)
    menu.add_command(label="Quit application", command=quit_application)

    def pop_menu(event: tk.Event) -> None:
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    root.bind("<Button-3>", pop_menu)

    def tick() -> None:
        if stop_event.is_set():
            root.destroy()
            return
        f_title = ""
        f_mins = 0
        with ui.lock:
            l1.config(text=ui.line1)
            l2.config(text=ui.line2)
            la.config(text=ui.alert)
            use_u = ui.urgent and bool(ui.alert)
            c = bg_u if use_u else bg
            root.config(bg=c)
            bar.config(bg=c)
            wrap.config(bg=c)
            btn.config(bg=c, activebackground=bg_u if use_u else bg)
            l1.config(bg=c)
            l2.config(bg=c)
            la.config(bg=c)
            
            f_title = ui.freeze_request_title
            f_mins = ui.freeze_request_minutes
            ui.freeze_request_title = ""
            ui.freeze_request_minutes = 0

        if f_mins > 0:
            show_freeze_window(root, f_title, f_mins)
            
        # إعادة حساب الحجم والموضع تلقائياً حسب المحتوى
        root.update_idletasks()
        new_w = root.winfo_reqwidth()
        new_h = root.winfo_reqheight()
        
        nonlocal current_x, current_y, is_dragged
        if not is_dragged:
            current_x = margin
            sh = root.winfo_screenheight()
            current_y = sh - new_h - margin - 48
            
        root.geometry(f"{new_w}x{new_h}+{current_x}+{current_y}")
        root.after(120, tick)

    tick()
    root.mainloop()


def startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "HydrationPrayerBot.vbs"


def install_windows_startup() -> Path:
    """اختصار في بدء التشغيل يستخدم pythonw (بدون نافذة)."""
    script_py = Path(__file__).resolve()
    work_dir = script_py.parent
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pythonw = Path(sys.executable)

    vbs_path = startup_shortcut_path()
    vbs_path.parent.mkdir(parents=True, exist_ok=True)

    # Use VBScript to run pythonw silently, avoiding .lnk unicode corruption issues
    vbs_content = f'Set WshShell = CreateObject("WScript.Shell")\n'
    vbs_content += f'WshShell.CurrentDirectory = "{work_dir}"\n'
    vbs_content += f'WshShell.Run """{pythonw}"" ""{script_py}""", 0, False\n'
    
    # Write with utf-16 to avoid any encoding problems in vbscript
    vbs_path.write_text(vbs_content, encoding="utf-16")
    
    # Remove old corrupt .lnk if exists
    old_lnk = vbs_path.with_suffix(".lnk")
    if old_lnk.exists():
        try:
            old_lnk.unlink()
        except Exception:
            pass
            
    print(f"Startup script installed: {vbs_path}", flush=True)
    return vbs_path


def uninstall_windows_startup() -> bool:
    vbs = startup_shortcut_path()
    lnk = vbs.with_suffix(".lnk")
    removed = False
    for p in (vbs, lnk):
        if p.is_file():
            p.unlink()
            print(f"Removed: {p}", flush=True)
            removed = True
    if not removed:
        print(f"No startup shortcut at {vbs}", flush=True)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Hydration + prayer-aware reminders (Windows)")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument(
        "--install-startup",
        action="store_true",
        help="Create Startup folder shortcut (runs with Windows, no console window).",
    )
    parser.add_argument(
        "--uninstall-startup",
        action="store_true",
        help="Remove Startup shortcut.",
    )
    args = parser.parse_args()

    if args.uninstall_startup:
        uninstall_windows_startup()
        return 0
    if args.install_startup:
        install_windows_startup()
        return 0

    cfg = load_config(args.config)
    settings = merge_settings(cfg)

    if not Path(args.config).is_file():
        print(
            f"Config not found at {args.config}. Copy config.example.yaml to config.yaml.",
            file=sys.stderr,
        )

    stop = threading.Event()
    ui = UiState()
    th = threading.Thread(target=run_loop, args=(settings, stop, ui), daemon=True)
    th.start()
    
    print(
        f"Running: every {settings.reminder_interval_minutes} min active time, "
        f"city={settings.city}, prayer quiet={settings.prayer_quiet_window_minutes} min. "
        f"X hides the panel only - F12 toggles - Right-click -> Quit application to stop.",
        flush=True,
    )
    try:
        run_overlay(stop, ui)
    finally:
        stop.set()
        th.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())