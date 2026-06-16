"""
confluence_watcher.py — OB+FVG Confluence background scanner
=============================================================
Coordinates OBWatcher and FVGWatcher to find and display
Order Blocks that have a Fair Value Gap immediately after them.

Design:
  - Reads latest_obs from OBWatcher and latest_fvgs from FVGWatcher
    (no re-scanning MT5 — reuses already-computed data)
  - Runs its own scan loop every scan_interval seconds
  - When enabled: suppresses individual OB and FVG chart rectangles,
    draws only confluence zones
  - When disabled: clears confluence rects, OB/FVG watchers
    resume drawing their own rectangles on next cycle

Hot-update via update_settings().
"""

import threading
import time as _time
import logging
import MetaTrader5 as mt5

from core.ob_fvg_confluence import (
    find_confluences,
    draw_confluences_on_chart,
    clear_confluences_on_chart,
    ConfluenceZone,
)
from core.ob_detector import get_pip_size

log = logging.getLogger("confluence_watcher")

TIMEFRAME_NAMES = {
    mt5.TIMEFRAME_M1:  "M1",
    mt5.TIMEFRAME_M5:  "M5",
    mt5.TIMEFRAME_M15: "M15",
    mt5.TIMEFRAME_M30: "M30",
    mt5.TIMEFRAME_H1:  "H1",
    mt5.TIMEFRAME_H4:  "H4",
    mt5.TIMEFRAME_D1:  "D1",
}

TIMEFRAME_SEC = {
    mt5.TIMEFRAME_M1:  60,
    mt5.TIMEFRAME_M5:  300,
    mt5.TIMEFRAME_M15: 900,
    mt5.TIMEFRAME_M30: 1800,
    mt5.TIMEFRAME_H1:  3600,
    mt5.TIMEFRAME_H4:  14400,
    mt5.TIMEFRAME_D1:  86400,
}


class ConfluenceWatcher(threading.Thread):
    """
    Reads from OBWatcher + FVGWatcher, finds confluence zones,
    draws them on chart.

    Usage:
        watcher = ConfluenceWatcher(
            symbol      = "EURUSD",
            ob_watcher  = ob_worker,
            fvg_watcher = fvg_worker,
            ...
        )
        watcher.start()
        watcher.stop()   # clears chart, OB/FVG watchers resume normal drawing
    """

    def __init__(
        self,
        symbol:             str,
        ob_watcher,                       # OBWatcher instance
        fvg_watcher,                      # FVGWatcher instance
        timeframe=None,
        max_candles_after:  int   = 10,
        require_direction:  bool  = True,
        scan_interval:      float = 5.0,
        max_draw:           int   = 20,
        log_fn=None,
    ):
        super().__init__(daemon=True)
        self.symbol            = symbol
        self.ob_watcher        = ob_watcher
        self.fvg_watcher       = fvg_watcher
        self.timeframe         = timeframe or mt5.TIMEFRAME_M1
        self.max_candles_after = max_candles_after
        self.require_direction = require_direction
        self.scan_interval     = scan_interval
        self.max_draw          = max_draw
        self._log              = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop_event       = threading.Event()

        # Suppress individual OB/FVG drawing while confluence mode is on
        self.ob_watcher.draw_on_chart  = False
        self.fvg_watcher.draw_on_chart = False

        # Public state
        self.latest_zones: list[ConfluenceZone] = []
        self._lock         = threading.Lock()
        self._last_count   = -1
        self._known_names  = set()

    # ── Public API ────────────────────────────────────────────────

    def stop(self):
        self._stop_event.set()

    def get_zones(self) -> list:
        """Thread-safe snapshot of active confluence zones."""
        with self._lock:
            return [z for z in self.latest_zones if not z.mitigated]

    def update_settings(
        self,
        max_candles_after: int  = None,
        require_direction: bool = None,
        max_draw:          int  = None,
    ):
        if max_candles_after is not None: self.max_candles_after = max_candles_after
        if require_direction is not None: self.require_direction = require_direction
        if max_draw          is not None: self.max_draw          = max_draw
        self._last_count  = -1
        self._known_names = set()

    # ── Main loop ─────────────────────────────────────────────────

    def run(self):
        tf_name  = TIMEFRAME_NAMES.get(self.timeframe, str(self.timeframe))
        tf_sec   = TIMEFRAME_SEC.get(self.timeframe, 60)
        pip_size = get_pip_size(self.symbol)

        self._log(
            f"🟡  Confluence Watcher started | {self.symbol} {tf_name} | "
            f"max_after={self.max_candles_after} candles | "
            f"direction_match={self.require_direction}", "INFO"
        )

        # Small startup delay
        self._stop_event.wait(3.0)

        while not self._stop_event.is_set():
            try:
                self._cycle(pip_size, tf_sec)
            except Exception as e:
                self._log(f"💥 Confluence scan error: {type(e).__name__}: {e}", "ERROR")

            self._stop_event.wait(self.scan_interval)

        # Cleanup: clear confluence rects, restore OB/FVG drawing
        try:
            clear_confluences_on_chart(self.symbol)
        except Exception:
            pass

        # Re-enable individual OB/FVG drawing
        self.ob_watcher.draw_on_chart  = True
        self.fvg_watcher.draw_on_chart = True

        self._log("🟡  Confluence Watcher stopped — OB/FVG drawing restored", "INFO")

    # ── Single scan cycle ─────────────────────────────────────────

    def _cycle(self, pip_size: float, tf_sec: int):
        # Pull latest data from both watchers (thread-safe)
        obs  = self.ob_watcher.get_obs()        # non-mitigated OBs
        fvgs = self.fvg_watcher.get_fvgs()      # all FVGs

        if not obs or not fvgs:
            if self._last_count != 0:
                self._last_count = 0
                self._log(
                    f"🟡  Confluence: waiting for data | "
                    f"OBs={len(obs)} FVGs={len(fvgs)}", "INFO"
                )
            return

        zones = find_confluences(
            obs                = obs,
            fvgs               = fvgs,
            pip_size           = pip_size,
            max_candles_after  = self.max_candles_after,
            timeframe_sec      = tf_sec,
            require_direction  = self.require_direction,
        )

        with self._lock:
            self.latest_zones = zones

        # Log when count changes or new zones appear
        self._log_changes(zones)

        # Draw on chart (suppresses individual OB/FVG rects)
        try:
            draw_confluences_on_chart(
                symbol       = self.symbol,
                zones        = zones,
                max_draw     = self.max_draw,
                timeframe    = self.timeframe,
                also_draw_ob = False,
                also_draw_fvg= False,
            )
        except Exception:
            pass

    # ── Logging ───────────────────────────────────────────────────

    def _log_changes(self, zones: list):
        active = [z for z in zones if not z.mitigated]
        count  = len(active)

        new_zones = [z for z in active if z.name not in self._known_names]
        for z in new_zones:
            self._known_names.add(z.name)

        if count == self._last_count and not new_zones:
            return

        self._last_count = count

        if not active:
            self._log("🟡  Confluence: 0 zones found — try wider candle window or lower pip thresholds", "INFO")
            return

        bull = sum(1 for z in active if z.kind == "BULL")
        bear = sum(1 for z in active if z.kind == "BEAR")

        # Top 3 by score
        top3 = active[:3]
        top3_str = "  ".join(
            f"{'🟡' if z.kind == 'BULL' else '🟣'}"
            f"OB{z.ob.impulse_pips:.1f}p+FVG{z.fvg_gap_pips:.1f}p"
            for z in top3
        )

        self._log(
            f"🟡  Confluence: {count} zones | "
            f"🟡{bull} bull  🟣{bear} bear | "
            f"top: {top3_str}", "NEW"
        )

        # Log each new zone individually
        for z in new_zones:
            self._log(f"   {z.summary()}", "NEW")