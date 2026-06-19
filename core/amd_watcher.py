"""
amd_watcher.py — AMD Quarter Theory background scanner
=======================================================
Completely independent daemon thread — mirrors fvg_watcher.py design.
Does NOT depend on the EA file or chart lines.

Responsibilities:
  1. Calculate current AMD phase at every level every scan_interval seconds
  2. Draw colored boxes on chart for each level
  3. Draw info table in top-right corner showing current phase at all levels
  4. Log phase changes (when you move into a new AMD phase)
  5. Expose latest_status and latest_boxes for GUI to read (thread-safe)

Hot-update via update_settings() — no restart needed.
"""

import threading
import time as _time
import logging
import MetaTrader5 as mt5

from core.amd_detector import (
    get_current_amd_status,
    get_amd_boxes,
    draw_amd_on_chart,
    clear_amd_on_chart,
    AMDStatus,
    AMDPhase,
    PHASE_NAMES,
)

log = logging.getLogger("amd_watcher")

# All available levels in order from smallest to largest
ALL_LEVELS = ["1M", "5M", "1H", "4H", "Day", "Week", "Month", "Quarter"]

# Default levels to draw boxes for (1M is too noisy by default)
DEFAULT_LEVELS = ["1H", "4H", "Day", "Week", "Month", "Quarter"]


class AMDWatcher(threading.Thread):
    """
    Standalone daemon thread that calculates and displays
    AMD Quarter Theory phases on the chart.

    Usage:
        watcher = AMDWatcher(symbol="XAUUSD", ...)
        watcher.start()
        watcher.stop()

    Read current status:
        status = watcher.get_status()   # AMDStatus object
        boxes  = watcher.get_boxes()    # list of AMDPhase objects
    """

    def __init__(
        self,
        symbol:           str,
        visible_levels:   list  = None,
        show_all_phases:  bool  = False,   # False = current phase only per level
        scan_interval:    float = 10.0,    # AMD phases change slowly — 10s is fine
        draw_on_chart:    bool  = True,
        log_fn=None,
    ):
        super().__init__(daemon=True)
        self.symbol          = symbol
        self.visible_levels  = visible_levels or DEFAULT_LEVELS
        self.show_all_phases = show_all_phases
        self.scan_interval   = scan_interval
        self.draw_on_chart   = draw_on_chart
        self._log            = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop_event     = threading.Event()

        # Public state
        self.latest_status: AMDStatus      = AMDStatus()
        self.latest_boxes:  list           = []
        self._lock                         = threading.Lock()

        # Track phase changes for logging
        self._last_status: AMDStatus       = AMDStatus()
        self._initialized                  = False

    # ── Public API ────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()

    def get_status(self) -> AMDStatus:
        """Thread-safe snapshot of current AMD status."""
        with self._lock:
            return self.latest_status

    def get_boxes(self) -> list:
        """Thread-safe snapshot of current AMD boxes."""
        with self._lock:
            return list(self.latest_boxes)

    def update_settings(
        self,
        visible_levels:  list  = None,
        show_all_phases: bool  = None,
        draw_on_chart:   bool  = None,
    ):
        """Hot-update settings without restarting the thread."""
        if visible_levels  is not None: self.visible_levels  = visible_levels
        if show_all_phases is not None: self.show_all_phases = show_all_phases
        if draw_on_chart   is not None: self.draw_on_chart   = draw_on_chart
        self._initialized = False  # Force re-log on next cycle

    # ── Main loop ─────────────────────────────────────────────────

    def run(self):
        self._log(
            f"🟩  AMD Watcher started | {self.symbol} | "
            f"levels={','.join(self.visible_levels)} | "
            f"mode={'all phases' if self.show_all_phases else 'current only'}",
            "INFO"
        )

        # Small startup delay
        self._stop_event.wait(2.0)

        while not self._stop_event.is_set():
            try:
                self._cycle()
            except Exception as e:
                self._log(f"💥 AMD scan error: {type(e).__name__}: {e}", "ERROR")

            self._stop_event.wait(self.scan_interval)

        # Cleanup
        if self.draw_on_chart:
            try:
                clear_amd_on_chart(self.symbol)
            except Exception:
                pass

        self._log("🟩  AMD Watcher stopped", "INFO")

    # ── Single scan cycle ─────────────────────────────────────────

    def _cycle(self):
        # 1. Calculate current status
        status = get_current_amd_status(self.symbol)
        if not status:
            return

        # 2. Get boxes for visible levels
        boxes = get_amd_boxes(self.symbol, self.visible_levels)

        # 3. Update shared state
        with self._lock:
            self.latest_status = status
            self.latest_boxes  = boxes

        # 4. Log phase changes
        self._log_changes(status)

        # 5. Draw on chart
        if self.draw_on_chart:
            try:
                draw_amd_on_chart(
                    symbol           = self.symbol,
                    boxes            = boxes,
                    status           = status,
                    show_current_only= not self.show_all_phases,
                )
            except Exception:
                pass

    # ── Logging ───────────────────────────────────────────────────

    def _log_changes(self, status: AMDStatus):
        prev = self._last_status

        changes = []

        def _check(level, cur, prv):
            if cur != prv and prv != "?":
                full = PHASE_NAMES.get(cur, cur)
                color = {"A": "🟩", "M": "🟥", "D": "🟦", "C": "⬜"}.get(cur, "⬜")
                changes.append(f"{color} {level}: {prv} → {cur} ({full})")

        _check("1M",     status.minute,  prev.minute)
        _check("5M",     status.m5,      prev.m5)
        _check("1H",     status.h1,      prev.h1)
        _check("4H",     status.h4,      prev.h4)
        _check("Day",    status.day,     prev.day)
        _check("Week",   status.week,    prev.week)
        _check("Month",  status.month,   prev.month)
        _check("Quarter",status.quarter, prev.quarter)

        if changes:
            for c in changes:
                self._log(f"🟩  AMD Phase Change | {c}", "NEW")

        if not self._initialized:
            self._initialized = True
            self._log(
                f"🟩  AMD | "
                f"Y:{status.year} {status.quarter} "
                f"Month:{status.month} Week:{status.week} "
                f"Day:{status.day} "
                f"Session:{status.h4} Hour:{status.h1} "
                f"5min:{status.m5} 1min:{status.minute}",
                "NEW"
            )

        self._last_status = status