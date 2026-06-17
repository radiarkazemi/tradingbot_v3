import threading
import logging

log = logging.getLogger("amd_watcher")

ALL_LEVELS     = ["1M", "5M", "1H", "4H", "Day", "Week", "Month", "Quarter"]
DEFAULT_LEVELS = ["1H", "4H", "Day", "Week", "Month", "Quarter"]


def _extract_phase(field):
    """Extract phase letter from label like 'Q2(M)' → 'M'."""
    if "(" in field and ")" in field:
        return field[field.index("(")+1 : field.index(")")]
    return field


class AMDWatcher(threading.Thread):

    def __init__(self, symbol, visible_levels=None, show_all_phases=False,
                 scan_interval=10.0, draw_on_chart=True, log_fn=None):
        threading.Thread.__init__(self)
        self.daemon           = True
        self.symbol           = symbol
        self.visible_levels   = list(visible_levels or DEFAULT_LEVELS)
        self.show_all_phases  = show_all_phases
        self.scan_interval    = scan_interval
        self.draw_on_chart    = draw_on_chart
        self._log             = log_fn or (lambda msg, level="INFO": log.info(msg))
        self._stop_event      = threading.Event()
        self._lock            = threading.Lock()
        self.latest_status    = None
        self.latest_boxes     = []
        self._last_phases     = {}
        self._amd_ready       = False   # NOT _initialized (reserved by threading.Thread)

    def stop(self):
        self._stop_event.set()

    def get_status(self):
        with self._lock:
            return self.latest_status

    def get_boxes(self):
        with self._lock:
            return list(self.latest_boxes)

    def update_settings(self, visible_levels=None, show_all_phases=None, draw_on_chart=None):
        if visible_levels  is not None: self.visible_levels  = visible_levels
        if show_all_phases is not None: self.show_all_phases = show_all_phases
        if draw_on_chart   is not None: self.draw_on_chart   = draw_on_chart
        self._amd_ready = False

    def run(self):
        try:
            from core.amd_detector import (
                get_current_amd_status, get_amd_boxes,
                draw_amd_on_chart, clear_amd_on_chart, PHASE_NAMES,
            )
        except Exception as e:
            self._log(f"💥 AMD import error: {e}", "ERROR")
            return

        self._log(
            f"🟩  AMD Watcher started | {self.symbol} | "
            f"levels={','.join(self.visible_levels)}", "INFO"
        )
        self._stop_event.wait(2.0)

        while not self._stop_event.is_set():
            try:
                status = get_current_amd_status(self.symbol)
                if status:
                    boxes = get_amd_boxes(self.symbol, self.visible_levels)
                    with self._lock:
                        self.latest_status = status
                        self.latest_boxes  = boxes
                    self._log_changes(status, PHASE_NAMES)
                    if self.draw_on_chart:
                        try:
                            draw_amd_on_chart(
                                self.symbol, boxes, status,
                                show_current_only=not self.show_all_phases,
                            )
                        except Exception as e:
                            self._log(f"⚠️  AMD draw: {e}", "WARN")
            except Exception as e:
                self._log(f"💥 AMD error: {e}", "ERROR")
            self._stop_event.wait(self.scan_interval)

        if self.draw_on_chart:
            try:
                clear_amd_on_chart(self.symbol)
            except Exception:
                pass
        self._log("🟩  AMD Watcher stopped", "INFO")

    def _log_changes(self, status, PHASE_NAMES):
        # Full labels like "Q2(M)", "W3(D)" etc.
        labels = {
            "Quarter": status.quarter,
            "Month":   status.month,
            "Week":    status.week,
            "Day":     status.day,
            "4H":      status.h4,
            "1H":      status.h1,
            "5M":      status.m5,
            "1M":      status.minute,
        }
        icon = {"A": "🟩", "M": "🟥", "D": "🟦", "C": "⬜"}

        if not self._amd_ready:
            self._amd_ready    = True
            self._last_phases  = dict(labels)
            # Log full status on first run
            self._log(
                f"🟩  AMD | Y:{status.year}  "
                f"Q:{status.quarter}  Mo:{status.month}  "
                f"Wk:{status.week}  D:{status.day}  "
                f"4H:{status.h4}  1H:{status.h1}  "
                f"5M:{status.m5}  1M:{status.minute}",
                "NEW"
            )
        else:
            for level, cur_label in labels.items():
                prv_label = self._last_phases.get(level, "?")
                if cur_label != prv_label:
                    phase = _extract_phase(cur_label)
                    ic    = icon.get(phase, "⬜")
                    full  = PHASE_NAMES.get(phase, phase)
                    self._log(
                        f"🟩  AMD Phase Change | {ic} {level}: "
                        f"{prv_label} → {cur_label} ({full})",
                        "NEW"
                    )
            self._last_phases = dict(labels)