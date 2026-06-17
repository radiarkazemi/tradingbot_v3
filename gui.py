"""
╔══════════════════════════════════════════════════════════════════╗
║  TraderBot v2 — GUI                                             ║
║  2-Order Martingale Bot                                         ║
║  python gui.py                                                  ║
╚══════════════════════════════════════════════════════════════════╝
"""
import sys
import os
import threading
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import MetaTrader5 as mt5
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QGroupBox, QTextEdit, QFrame,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
    QDoubleSpinBox, QSpinBox, QComboBox, QSplitter, QSizePolicy,
    QProgressBar, QCheckBox, QScrollArea, QLineEdit,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject
from PyQt5.QtGui import QColor, QFont

os.makedirs("logs", exist_ok=True)

from config import (
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER,
    WATCH_SYMBOL, SCAN_INTERVAL_SEC,
    LOT_SIZE, ORDER_DISTANCE_PIPS, MAGIC_NUMBER,
)
from core.watcher import WatcherThread
from core.position_monitor import SourceState
from core.fvg_watcher import FVGWatcher
from core.ob_watcher import OBWatcher
from core.confluence_watcher import ConfluenceWatcher
from core.amd_watcher import AMDWatcher, ALL_LEVELS, DEFAULT_LEVELS
from core.mtf_fvg_watcher import MTFFVGWatcher

# ── Palette ───────────────────────────────────────────────────────
C = {
    "bg":       "#0D1117",
    "panel":    "#161B22",
    "card":     "#1C2333",
    "input":    "#141D2E",
    "border":   "#2A3550",
    "border_hi":"#4A6090",
    "txt":      "#E8EDF5",
    "txt2":     "#8B9BB4",
    "txt3":     "#4A5568",
    "gold":     "#F5A623",
    "green":    "#00D97E",
    "green_dk": "#003D22",
    "red":      "#FF4560",
    "red_dk":   "#3D0015",
    "orange":   "#FF8C00",
    "cyan":     "#00BCD4",
    "blue":     "#2979FF",
    "purple":   "#B388FF",
    "aqua":     "#00FFFF",
    "magenta":  "#FF00FF",
    "yellow":   "#FFD700",
    "amd_a":    "#00C853",
    "amd_m":    "#FF1744",
    "amd_d":    "#2979FF",
}

SS = f"""
QWidget      {{ background:{C['bg']};color:{C['txt']};font-family:'Segoe UI';font-size:12px; }}
QMainWindow  {{ background:{C['bg']}; }}
QLabel       {{ background:transparent; }}
QGroupBox    {{ background:{C['card']};border:1px solid {C['border']};border-radius:6px;
                margin-top:14px;padding:8px 6px 6px 6px;
                font-size:10px;font-weight:bold;color:{C['txt2']}; }}
QGroupBox::title {{ subcontrol-origin:margin;left:10px;padding:0 4px; }}
QPushButton  {{ background:{C['card']};color:{C['txt']};border:1px solid {C['border']};
                border-radius:5px;padding:6px 14px; }}
QPushButton:hover   {{ background:{C['border']};border-color:{C['border_hi']}; }}
QPushButton:pressed {{ background:{C['bg']}; }}
QPushButton:disabled{{ color:{C['txt3']};border-color:{C['card']}; }}
QPushButton#btn_start {{ background:{C['green_dk']};color:{C['green']};
    border:1px solid {C['green']};font-weight:bold;font-size:13px; }}
QPushButton#btn_start:hover {{ background:{C['green']};color:#000; }}
QPushButton#btn_stop {{ background:{C['red_dk']};color:{C['red']};
    border:1px solid {C['red']};font-weight:bold;font-size:13px; }}
QPushButton#btn_stop:hover {{ background:{C['red']};color:#fff; }}
QPushButton#btn_cancel {{ background:{C['red_dk']};color:{C['red']};border:1px solid {C['red']}; }}
QPushButton#btn_cancel:hover {{ background:{C['red']};color:#fff; }}
QDoubleSpinBox,QSpinBox,QComboBox,QLineEdit {{
    background:{C['input']};color:{C['txt']};
    border:1px solid {C['border']};border-radius:4px;padding:4px 7px;min-height:26px; }}
QDoubleSpinBox::up-button,QDoubleSpinBox::down-button,
QSpinBox::up-button,QSpinBox::down-button {{ background:{C['border']};border:none;width:16px; }}
QComboBox::drop-down {{ border:none;width:20px; }}
QComboBox QAbstractItemView {{ background:{C['card']};color:{C['txt']};
    selection-background-color:{C['border']}; }}
QTextEdit {{ background:{C['bg']};color:{C['txt']};border:1px solid {C['border']};
             border-radius:4px;font-family:'Consolas';font-size:11px; }}
QTableWidget {{ background:{C['bg']};color:{C['txt']};border:1px solid {C['border']};
                border-radius:4px;gridline-color:{C['border']};
                alternate-background-color:{C['panel']}; }}
QTableWidget::item {{ padding:4px 8px; }}
QTableWidget::item:selected {{ background:{C['border']}; }}
QHeaderView::section {{ background:{C['card']};color:{C['txt2']};padding:5px 8px;
    border:none;border-right:1px solid {C['border']};
    border-bottom:1px solid {C['border']};font-size:10px;font-weight:bold; }}
QTabWidget::pane {{ background:{C['panel']};border:1px solid {C['border']};border-radius:4px; }}
QTabBar::tab {{ background:{C['card']};color:{C['txt2']};padding:6px 18px;
    border:1px solid {C['border']};border-bottom:none;
    border-radius:4px 4px 0 0;margin-right:2px; }}
QTabBar::tab:selected {{ background:{C['panel']};color:{C['gold']};border-bottom:2px solid {C['gold']}; }}
QTabBar::tab:hover:!selected {{ color:{C['txt']}; }}
QScrollArea {{ border:none;background:transparent; }}
QScrollBar:vertical {{ background:{C['bg']};width:6px; }}
QScrollBar::handle:vertical {{ background:{C['border']};border-radius:3px;min-height:20px; }}
QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0; }}
QCheckBox {{ color:{C['txt2']}; }}
QCheckBox::indicator {{ width:14px;height:14px;border:1px solid {C['border']};border-radius:3px;background:{C['input']}; }}
QCheckBox::indicator:checked {{ background:{C['cyan']};border-color:{C['cyan']}; }}
"""

# ── Qt Signal Bridge ──────────────────────────────────────────────
class Sig(QObject):
    log_line   = pyqtSignal(str, str)
    status     = pyqtSignal(str)
    state      = pyqtSignal(list)
    candle     = pyqtSignal(dict)
    balance_tp = pyqtSignal()   # ← NEW: fired when balance TP hit → GUI stops cleanly

# ── Helpers ───────────────────────────────────────────────────────
def _vline():
    f = QFrame(); f.setFrameShape(QFrame.VLine)
    f.setStyleSheet(f"color:{C['border']};"); return f

def _hline():
    f = QFrame(); f.setFrameShape(QFrame.HLine)
    f.setStyleSheet(f"color:{C['border']};"); return f

# ── Main Window ───────────────────────────────────────────────────
class GUI(QMainWindow):

    def __init__(self):
        super().__init__()
        self.setWindowTitle("TraderBot v2 — 2-Order Martingale")
        self.setMinimumSize(860, 640)
        self.setStyleSheet(SS)

        self._worker:            Optional[WatcherThread]     = None
        self._fvg_worker:        Optional[FVGWatcher]        = None
        self._ob_worker:         Optional[OBWatcher]         = None
        self._confluence_worker: Optional[ConfluenceWatcher] = None
        self._amd_worker:        Optional[AMDWatcher]         = None
        self._mtf_fvg_worker:    Optional[MTFFVGWatcher]      = None

        self._sig = Sig()
        self._sig.log_line.connect(self._on_log)
        self._sig.status.connect(self._on_status)
        self._sig.state.connect(self._on_state)
        self._sig.candle.connect(self._on_candle)
        # ← NEW: balance TP fires on Qt main thread — safe to stop all watchers
        self._sig.balance_tp.connect(self._on_balance_tp_reached)

        self._last_candle: dict = {}

        self._build_ui()

        # Price ticker
        self._pt = QTimer()
        self._pt.timeout.connect(self._refresh_price)
        self._pt.start(1000)

        # Orders auto-refresh
        self._ot = QTimer()
        self._ot.timeout.connect(self._refresh_orders)
        self._ot.start(3000)

        # Indicator count refresh
        self._ct = QTimer()
        self._ct.timeout.connect(self._refresh_indicator_counts)
        self._ct.start(5000)

        QTimer.singleShot(200, self._init_price)

        # AMD status refresh
        self._at = QTimer()
        self._at.timeout.connect(self._refresh_amd_status)
        self._at.start(10000)

    # ── UI Build ──────────────────────────────────────────────────

    def _build_ui(self):
        root = QWidget(); self.setCentralWidget(root)
        vl = QVBoxLayout(root)
        vl.setSpacing(6); vl.setContentsMargins(10, 10, 10, 10)
        vl.addWidget(self._build_header())

        spl = QSplitter(Qt.Horizontal)
        spl.addWidget(self._build_left())
        spl.addWidget(self._build_right())
        spl.setSizes([330, 700])
        spl.setCollapsible(0, False)
        spl.setCollapsible(1, False)
        spl.widget(0).setMinimumWidth(280)
        vl.addWidget(spl, 1)
        vl.addWidget(self._build_statusbar())

    def _build_header(self):
        w = QFrame()
        w.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};border-radius:6px;")
        hl = QHBoxLayout(w); hl.setContentsMargins(14, 8, 14, 8)
        t = QLabel("📈  TraderBot  <span style='color:#4A5568;font-size:10px;'>v2</span>")
        t.setStyleSheet(f"color:{C['gold']};font-size:16px;font-weight:bold;")
        hl.addWidget(t)
        hl.addStretch()
        self.lbl_price = QLabel("Price: —")
        self.lbl_price.setStyleSheet(
            f"color:{C['cyan']};font-family:Consolas;font-size:14px;font-weight:bold;")
        hl.addWidget(self.lbl_price)
        hl.addWidget(_vline())
        self.lbl_sym_hdr = QLabel(WATCH_SYMBOL)
        self.lbl_sym_hdr.setStyleSheet(f"color:{C['txt2']};font-size:12px;")
        hl.addWidget(self.lbl_sym_hdr)
        hl.addWidget(_vline())
        self.lbl_ea_status = QLabel("EA: —")
        self.lbl_ea_status.setStyleSheet(f"color:{C['txt3']};font-size:10px;")
        self.lbl_ea_status.setToolTip("ObjectExporter EA file status")
        hl.addWidget(self.lbl_ea_status)
        hl.addWidget(_vline())
        self.lbl_status = QLabel("⚫  Stopped")
        self.lbl_status.setStyleSheet(f"color:{C['txt2']};font-size:11px;")
        hl.addWidget(self.lbl_status)
        return w

    # ── Left Panel ────────────────────────────────────────────────

    def _build_left(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        w = QWidget(); vl = QVBoxLayout(w)
        vl.setSpacing(8); vl.setContentsMargins(0, 0, 4, 0)
        scroll.setWidget(w)

        def _lbl(text, tip=""):
            l = QLabel(text); l.setStyleSheet(f"color:{C['txt2']};font-size:11px;")
            if tip: l.setToolTip(tip)
            return l

        def _row(label, widget, grp_layout, tip=""):
            hl = QHBoxLayout(); hl.setSpacing(8)
            lw = _lbl(label, tip); lw.setFixedWidth(100)
            hl.addWidget(lw)
            widget.setMinimumWidth(100)
            widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            hl.addWidget(widget)
            grp_layout.addLayout(hl)

        # ── Bot Control group ─────────────────────────────────────
        grp_ctrl = QGroupBox("⚙️  Bot Control")
        cl = QVBoxLayout(grp_ctrl); cl.setSpacing(6)

        self.sym_combo = QComboBox(); self.sym_combo.setEditable(True)
        self.sym_combo.addItems([
            "EURUSD", "XAUUSD", "GBPUSD", "USDJPY", "EURUSD_i",
            "XAUUSD_i", "GBPUSD_i", "NAS100", "US30", "BTCUSD",
        ])
        self.sym_combo.setCurrentText(WATCH_SYMBOL)
        self.sym_combo.currentTextChanged.connect(self._on_symbol_changed)
        _row("🎯 Symbol:", self.sym_combo, cl, "MT5 symbol to watch")

        self.spin_dist = QDoubleSpinBox()
        self.spin_dist.setRange(0.1, 100.0); self.spin_dist.setSingleStep(0.5)
        self.spin_dist.setValue(ORDER_DISTANCE_PIPS); self.spin_dist.setDecimals(1)
        _row("📏 Distance (pips):", self.spin_dist, cl,
             "Pips above line for BUY-STOP, below for SELL-STOP")

        self.spin_lot = QDoubleSpinBox()
        self.spin_lot.setRange(0.01, 100.0); self.spin_lot.setSingleStep(0.01)
        self.spin_lot.setValue(LOT_SIZE); self.spin_lot.setDecimals(2)
        _row("📦 Base Lot:", self.spin_lot, cl, "Starting lot size (round 1)")

        self.spin_balance_tp = QDoubleSpinBox()
        self.spin_balance_tp.setRange(1.0, 100.0); self.spin_balance_tp.setSingleStep(1.0)
        self.spin_balance_tp.setValue(10.0); self.spin_balance_tp.setDecimals(1)
        self.spin_balance_tp.setSuffix(" %")
        _row("💰 Balance TP:", self.spin_balance_tp, cl,
             "Close all & stop when balance grows by this % from start")

        self.chk_follow = QCheckBox("Follow moved lines")
        self.chk_follow.setChecked(True)
        self.chk_follow.setToolTip(
            "When you drag a line on the chart, the bot resets and re-watches "
            "from the new position")
        cl.addWidget(self.chk_follow)

        self.chk_resume = QCheckBox("Resume previous session")
        self.chk_resume.setChecked(False)
        self.chk_resume.setToolTip(
            "On start, scan MT5 for existing bot positions/orders\n"
            "and resume monitoring them without re-entering.\n"
            "Use this if the bot stopped unexpectedly.")
        self.chk_resume.setStyleSheet(f"color:{C['orange']};")
        cl.addWidget(self.chk_resume)

        cl.addWidget(_hline())

        self.btn_start = QPushButton("▶  Start Watcher")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.setMinimumHeight(38)
        self.btn_start.clicked.connect(self._start)
        cl.addWidget(self.btn_start)

        self.btn_stop = QPushButton("■  Stop Watcher")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setMinimumHeight(38)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        cl.addWidget(self.btn_stop)

        vl.addWidget(grp_ctrl)

        # ── Active Sequences group ────────────────────────────────
        grp_seq = QGroupBox("🔥  Active Sequences")
        sl = QVBoxLayout(grp_seq); sl.setSpacing(2)
        self.lbl_sequences = QLabel("—  No active sequences")
        self.lbl_sequences.setStyleSheet(
            f"color:{C['txt2']};font-size:11px;font-family:Consolas;")
        self.lbl_sequences.setWordWrap(True)
        sl.addWidget(self.lbl_sequences)
        vl.addWidget(grp_seq)

        # ── Balance TP progress ───────────────────────────────────
        grp_bal = QGroupBox("💰  Balance Progress")
        bl = QVBoxLayout(grp_bal); bl.setSpacing(4)
        self.lbl_balance = QLabel("Balance: —")
        self.lbl_balance.setStyleSheet(f"color:{C['gold']};font-family:Consolas;font-size:11px;")
        bl.addWidget(self.lbl_balance)
        self.lbl_balance_target = QLabel("Target: —")
        self.lbl_balance_target.setStyleSheet(f"color:{C['txt2']};font-size:10px;")
        bl.addWidget(self.lbl_balance_target)
        vl.addWidget(grp_bal)

        # ── FVG Settings group ────────────────────────────────────
        grp_fvg = QGroupBox("📐  Fair Value Gaps (FVG)")
        fv = QVBoxLayout(grp_fvg); fv.setSpacing(6)

        self.chk_fvg = QCheckBox("Enable FVG detection")
        self.chk_fvg.setChecked(True)
        self.chk_fvg.setToolTip("Scan candles for FVG patterns and draw rectangles on chart")
        self.chk_fvg.stateChanged.connect(self._on_fvg_toggled)
        fv.addWidget(self.chk_fvg)

        fvg_gap_row = QHBoxLayout(); fvg_gap_row.setSpacing(8)
        lbl_gap = _lbl("📏 Min Gap (pips):")
        lbl_gap.setFixedWidth(100)
        lbl_gap.setToolTip(
            "Minimum FVG size in pips.\n"
            "↑ Increase → fewer FVGs, higher quality\n"
            "↓ Decrease → more FVGs, more noise\n"
            "Recommended: 2-5 for M1, 5-15 for M15"
        )
        fvg_gap_row.addWidget(lbl_gap)
        self.spin_fvg_gap = QDoubleSpinBox()
        self.spin_fvg_gap.setRange(0.5, 200.0)
        self.spin_fvg_gap.setSingleStep(0.5)
        self.spin_fvg_gap.setValue(3.0)
        self.spin_fvg_gap.setDecimals(1)
        self.spin_fvg_gap.setSuffix(" pips")
        self.spin_fvg_gap.valueChanged.connect(self._on_fvg_settings_changed)
        fvg_gap_row.addWidget(self.spin_fvg_gap)
        fv.addLayout(fvg_gap_row)

        fvg_lb_row = QHBoxLayout(); fvg_lb_row.setSpacing(8)
        lbl_lb = _lbl("🕯 Lookback:")
        lbl_lb.setFixedWidth(100)
        lbl_lb.setToolTip("How many candles to scan for FVGs")
        fvg_lb_row.addWidget(lbl_lb)
        self.spin_fvg_lookback = QSpinBox()
        self.spin_fvg_lookback.setRange(10, 1000)
        self.spin_fvg_lookback.setSingleStep(50)
        self.spin_fvg_lookback.setValue(200)
        self.spin_fvg_lookback.valueChanged.connect(self._on_fvg_settings_changed)
        fvg_lb_row.addWidget(self.spin_fvg_lookback)
        fv.addLayout(fvg_lb_row)

        fvg_max_row = QHBoxLayout(); fvg_max_row.setSpacing(8)
        lbl_max = _lbl("🔲 Max Rects:")
        lbl_max.setFixedWidth(100)
        lbl_max.setToolTip("Maximum FVG rectangles drawn on chart (newest first)")
        fvg_max_row.addWidget(lbl_max)
        self.spin_fvg_max = QSpinBox()
        self.spin_fvg_max.setRange(1, 200)
        self.spin_fvg_max.setSingleStep(5)
        self.spin_fvg_max.setValue(30)
        self.spin_fvg_max.valueChanged.connect(self._on_fvg_settings_changed)
        fvg_max_row.addWidget(self.spin_fvg_max)
        fv.addLayout(fvg_max_row)

        self.lbl_fvg_count = QLabel("FVGs: —")
        self.lbl_fvg_count.setStyleSheet(
            f"color:{C['cyan']};font-family:Consolas;font-size:10px;")
        fv.addWidget(self.lbl_fvg_count)

        vl.addWidget(grp_fvg)

        # ── OB Settings group ─────────────────────────────────────
        grp_ob = QGroupBox("🟦  Order Blocks (OB)")
        ov = QVBoxLayout(grp_ob); ov.setSpacing(6)

        self.chk_ob = QCheckBox("Enable OB detection")
        self.chk_ob.setChecked(True)
        self.chk_ob.setToolTip(
            "Scan candles for Order Block patterns\n"
            "Classic: last opposite candle before strong impulse\n"
            "BOS-based: candle that caused structure break\n"
            "Aqua = Bullish OB  |  Magenta = Bearish OB\n"
            "Mitigated zones auto-removed from chart"
        )
        self.chk_ob.stateChanged.connect(self._on_ob_toggled)
        ov.addWidget(self.chk_ob)

        ob_imp_row = QHBoxLayout(); ob_imp_row.setSpacing(8)
        lbl_imp = _lbl("📏 Min Impulse:")
        lbl_imp.setFixedWidth(100)
        lbl_imp.setToolTip(
            "Minimum impulse to confirm an OB.\n"
            "Both conditions required:\n"
            "  1. Close beyond OB candle high/low\n"
            "  2. Move ≥ this many pips from OB boundary\n"
            "Recommended: 2-5 for M1, 5-15 for M15"
        )
        ob_imp_row.addWidget(lbl_imp)
        self.spin_ob_impulse = QDoubleSpinBox()
        self.spin_ob_impulse.setRange(0.5, 200.0)
        self.spin_ob_impulse.setSingleStep(0.5)
        self.spin_ob_impulse.setValue(3.0)
        self.spin_ob_impulse.setDecimals(1)
        self.spin_ob_impulse.setSuffix(" pips")
        self.spin_ob_impulse.valueChanged.connect(self._on_ob_settings_changed)
        ob_imp_row.addWidget(self.spin_ob_impulse)
        ov.addLayout(ob_imp_row)

        ob_lb_row = QHBoxLayout(); ob_lb_row.setSpacing(8)
        lbl_ob_lb = _lbl("🕯 Lookback:")
        lbl_ob_lb.setFixedWidth(100)
        lbl_ob_lb.setToolTip("How many candles to scan for OB patterns")
        ob_lb_row.addWidget(lbl_ob_lb)
        self.spin_ob_lookback = QSpinBox()
        self.spin_ob_lookback.setRange(10, 1000)
        self.spin_ob_lookback.setSingleStep(50)
        self.spin_ob_lookback.setValue(200)
        self.spin_ob_lookback.valueChanged.connect(self._on_ob_settings_changed)
        ob_lb_row.addWidget(self.spin_ob_lookback)
        ov.addLayout(ob_lb_row)

        ob_sw_row = QHBoxLayout(); ob_sw_row.setSpacing(8)
        lbl_ob_sw = _lbl("📐 Swing Bars:")
        lbl_ob_sw.setFixedWidth(100)
        lbl_ob_sw.setToolTip(
            "Bars each side to confirm a swing high/low.\n"
            "Used by the BOS-based OB method only.\n"
            "Higher = fewer, stronger swing points"
        )
        ob_sw_row.addWidget(lbl_ob_sw)
        self.spin_ob_swing = QSpinBox()
        self.spin_ob_swing.setRange(2, 20)
        self.spin_ob_swing.setSingleStep(1)
        self.spin_ob_swing.setValue(5)
        self.spin_ob_swing.valueChanged.connect(self._on_ob_settings_changed)
        ob_sw_row.addWidget(self.spin_ob_swing)
        ov.addLayout(ob_sw_row)

        ob_max_row = QHBoxLayout(); ob_max_row.setSpacing(8)
        lbl_ob_max = _lbl("🔲 Max Rects:")
        lbl_ob_max.setFixedWidth(100)
        lbl_ob_max.setToolTip("Maximum OB rectangles drawn on chart (newest first)")
        ob_max_row.addWidget(lbl_ob_max)
        self.spin_ob_max = QSpinBox()
        self.spin_ob_max.setRange(1, 200)
        self.spin_ob_max.setSingleStep(5)
        self.spin_ob_max.setValue(20)
        self.spin_ob_max.valueChanged.connect(self._on_ob_settings_changed)
        ob_max_row.addWidget(self.spin_ob_max)
        ov.addLayout(ob_max_row)

        self.lbl_ob_count = QLabel("OBs: —")
        self.lbl_ob_count.setStyleSheet(
            f"color:{C['aqua']};font-family:Consolas;font-size:10px;")
        ov.addWidget(self.lbl_ob_count)

        vl.addWidget(grp_ob)

        # ── Confluence Settings ───────────────────────────────────
        grp_conf = QGroupBox("🟡  OB + FVG Confluence")
        grp_conf.setStyleSheet(
            f"QGroupBox {{ background:{C['card']};border:1px solid {C['yellow']};"
            f"border-radius:6px;margin-top:14px;padding:8px 6px 6px 6px;"
            f"font-size:10px;font-weight:bold;color:{C['yellow']}; }}"
            f"QGroupBox::title {{ subcontrol-origin:margin;left:10px;padding:0 4px; }}"
        )
        cv = QVBoxLayout(grp_conf); cv.setSpacing(6)

        self.chk_confluence = QCheckBox("Enable OB+FVG Confluence mode")
        self.chk_confluence.setChecked(False)
        self.chk_confluence.setStyleSheet(f"color:{C['yellow']};font-weight:bold;")
        self.chk_confluence.setToolTip(
            "Show only Order Blocks that have a Fair Value Gap\n"
            "appearing right after them — highest confluence zones.\n\n"
            "When ON:\n"
            "  • Individual OB and FVG rectangles are hidden\n"
            "  • Only combined confluence zones are drawn\n"
            "  • Gold outline = Bullish confluence\n"
            "  • Purple outline = Bearish confluence\n\n"
            "Requires both OB and FVG detection to be enabled."
        )
        self.chk_confluence.stateChanged.connect(self._on_confluence_toggled)
        cv.addWidget(self.chk_confluence)

        conf_win_row = QHBoxLayout(); conf_win_row.setSpacing(8)
        lbl_conf_win = _lbl("🕯 FVG Window:")
        lbl_conf_win.setFixedWidth(100)
        lbl_conf_win.setToolTip(
            "Max candles after the OB candle for an FVG to count as confluence.\n"
            "Smaller = stricter (FVG must be right after OB)\n"
            "Larger  = looser  (FVG can be further away)\n"
            "Recommended: 5-15 for M1"
        )
        conf_win_row.addWidget(lbl_conf_win)
        self.spin_conf_window = QSpinBox()
        self.spin_conf_window.setRange(1, 100)
        self.spin_conf_window.setSingleStep(1)
        self.spin_conf_window.setValue(10)
        self.spin_conf_window.setSuffix(" bars")
        self.spin_conf_window.valueChanged.connect(self._on_confluence_settings_changed)
        conf_win_row.addWidget(self.spin_conf_window)
        cv.addLayout(conf_win_row)

        self.chk_conf_direction = QCheckBox("Match FVG direction to OB")
        self.chk_conf_direction.setChecked(True)
        self.chk_conf_direction.setToolTip(
            "When enabled: Bullish OB must have Bullish FVG after it (and vice versa)\n"
            "When disabled: any FVG after OB counts regardless of direction"
        )
        self.chk_conf_direction.stateChanged.connect(self._on_confluence_settings_changed)
        cv.addWidget(self.chk_conf_direction)

        conf_max_row = QHBoxLayout(); conf_max_row.setSpacing(8)
        lbl_conf_max = _lbl("🔲 Max Rects:")
        lbl_conf_max.setFixedWidth(100)
        lbl_conf_max.setToolTip("Maximum confluence rectangles drawn on chart")
        conf_max_row.addWidget(lbl_conf_max)
        self.spin_conf_max = QSpinBox()
        self.spin_conf_max.setRange(1, 100)
        self.spin_conf_max.setSingleStep(5)
        self.spin_conf_max.setValue(20)
        self.spin_conf_max.valueChanged.connect(self._on_confluence_settings_changed)
        conf_max_row.addWidget(self.spin_conf_max)
        cv.addLayout(conf_max_row)

        self.lbl_conf_count = QLabel("Confluence: —")
        self.lbl_conf_count.setStyleSheet(
            f"color:{C['yellow']};font-family:Consolas;font-size:10px;")
        cv.addWidget(self.lbl_conf_count)

        vl.addWidget(grp_conf)

        # ── Cancel All group ──────────────────────────────────────
        grp_cancel = QGroupBox("🛑  Emergency")
        ecl = QVBoxLayout(grp_cancel)
        self.btn_cancel_all = QPushButton("🗑️  Cancel All Bot Orders")
        self.btn_cancel_all.setObjectName("btn_cancel")
        self.btn_cancel_all.setMinimumHeight(30)
        self.btn_cancel_all.clicked.connect(self._cancel_all)
        ecl.addWidget(self.btn_cancel_all)
        vl.addWidget(grp_cancel)



        # ── MTF FVG Confluence ────────────────────────────────────
        grp_mtf = QGroupBox("🟡  MTF FVG Confluence (15M+5M+1M)")
        grp_mtf.setStyleSheet(
            f"QGroupBox {{ background:{C['card']};border:1px solid {C['gold']};"
            f"border-radius:6px;margin-top:14px;padding:8px 6px 6px 6px;"
            f"font-size:10px;font-weight:bold;color:{C['gold']}; }}"
            f"QGroupBox::title {{ subcontrol-origin:margin;left:10px;padding:0 4px; }}"
        )
        mv = QVBoxLayout(grp_mtf); mv.setSpacing(6)

        self.chk_mtf = QCheckBox("Enable MTF FVG detection")
        self.chk_mtf.setChecked(False)
        self.chk_mtf.setToolTip(
            "Finds price zones where 15M + 5M + 1M FVGs all overlap\n"
            "in the same direction simultaneously.\n\n"
            "Triggered on every completed 1M candle.\n"
            "🟡 Gold box = Bullish confluence entry zone\n"
            "🟣 Purple box = Bearish confluence entry zone\n"
            "Zones auto-removed when price enters them."
        )
        self.chk_mtf.stateChanged.connect(self._on_mtf_toggled)
        mv.addWidget(self.chk_mtf)

        mtf_gap_row = QHBoxLayout(); mtf_gap_row.setSpacing(8)
        lbl_mtf_gap = _lbl("📏 Min Gap (pips):"); lbl_mtf_gap.setFixedWidth(100)
        mtf_gap_row.addWidget(lbl_mtf_gap)
        self.spin_mtf_gap = QDoubleSpinBox()
        self.spin_mtf_gap.setRange(0.1, 50.0); self.spin_mtf_gap.setSingleStep(0.1)
        self.spin_mtf_gap.setValue(1.0); self.spin_mtf_gap.setDecimals(1)
        self.spin_mtf_gap.setSuffix(" pips")
        self.spin_mtf_gap.setToolTip("Minimum FVG size on each timeframe to qualify")
        self.spin_mtf_gap.valueChanged.connect(self._on_mtf_settings_changed)
        mtf_gap_row.addWidget(self.spin_mtf_gap)
        mv.addLayout(mtf_gap_row)

        mtf_lb15_row = QHBoxLayout(); mtf_lb15_row.setSpacing(8)
        lbl_mtf_lb15 = _lbl("🕯 15M Lookback:"); lbl_mtf_lb15.setFixedWidth(100)
        mtf_lb15_row.addWidget(lbl_mtf_lb15)
        self.spin_mtf_lb15 = QSpinBox()
        self.spin_mtf_lb15.setRange(10, 200); self.spin_mtf_lb15.setSingleStep(10)
        self.spin_mtf_lb15.setValue(50)
        self.spin_mtf_lb15.valueChanged.connect(self._on_mtf_settings_changed)
        mtf_lb15_row.addWidget(self.spin_mtf_lb15)
        mv.addLayout(mtf_lb15_row)

        mtf_lb5_row = QHBoxLayout(); mtf_lb5_row.setSpacing(8)
        lbl_mtf_lb5 = _lbl("🕯 5M Lookback:"); lbl_mtf_lb5.setFixedWidth(100)
        mtf_lb5_row.addWidget(lbl_mtf_lb5)
        self.spin_mtf_lb5 = QSpinBox()
        self.spin_mtf_lb5.setRange(10, 500); self.spin_mtf_lb5.setSingleStep(20)
        self.spin_mtf_lb5.setValue(100)
        self.spin_mtf_lb5.valueChanged.connect(self._on_mtf_settings_changed)
        mtf_lb5_row.addWidget(self.spin_mtf_lb5)
        mv.addLayout(mtf_lb5_row)

        mtf_lb1_row = QHBoxLayout(); mtf_lb1_row.setSpacing(8)
        lbl_mtf_lb1 = _lbl("🕯 1M Lookback:"); lbl_mtf_lb1.setFixedWidth(100)
        mtf_lb1_row.addWidget(lbl_mtf_lb1)
        self.spin_mtf_lb1 = QSpinBox()
        self.spin_mtf_lb1.setRange(10, 1000); self.spin_mtf_lb1.setSingleStep(50)
        self.spin_mtf_lb1.setValue(200)
        self.spin_mtf_lb1.valueChanged.connect(self._on_mtf_settings_changed)
        mtf_lb1_row.addWidget(self.spin_mtf_lb1)
        mv.addLayout(mtf_lb1_row)

        mtf_max_row = QHBoxLayout(); mtf_max_row.setSpacing(8)
        lbl_mtf_max = _lbl("🔲 Max Zones:"); lbl_mtf_max.setFixedWidth(100)
        mtf_max_row.addWidget(lbl_mtf_max)
        self.spin_mtf_max = QSpinBox()
        self.spin_mtf_max.setRange(1, 50); self.spin_mtf_max.setSingleStep(5)
        self.spin_mtf_max.setValue(20)
        self.spin_mtf_max.valueChanged.connect(self._on_mtf_settings_changed)
        mtf_max_row.addWidget(self.spin_mtf_max)
        mv.addLayout(mtf_max_row)

        self.lbl_mtf_count = QLabel("MTF FVG: —")
        self.lbl_mtf_count.setStyleSheet(
            f"color:{C['gold']};font-family:Consolas;font-size:10px;")
        mv.addWidget(self.lbl_mtf_count)

        vl.addWidget(grp_mtf)

        # ── AMD Quarter Theory ────────────────────────────────────
        grp_amd = QGroupBox("🟩  AMD Quarter Theory")
        grp_amd.setStyleSheet(
            f"QGroupBox {{ background:{C['card']};border:1px solid {C['amd_a']};"
            f"border-radius:6px;margin-top:14px;padding:8px 6px 6px 6px;"
            f"font-size:10px;font-weight:bold;color:{C['amd_a']}; }}"
            f"QGroupBox::title {{ subcontrol-origin:margin;left:10px;padding:0 4px; }}"
        )
        av = QVBoxLayout(grp_amd); av.setSpacing(6)

        self.chk_amd = QCheckBox("Enable AMD detection")
        self.chk_amd.setChecked(False)
        self.chk_amd.setToolTip(
            "Quarter Theory AMD phases on chart\n"
            "🟩 A = Accumulation  🟥 M = Manipulation  🟦 D = Distribution\n\n"
            "Boxes drawn for each level. Info table top-right of chart.\n"
            "Year → Quarter → Month → Week → Day → 4H → 1H → 5M → 1M"
        )
        self.chk_amd.stateChanged.connect(self._on_amd_toggled)
        av.addWidget(self.chk_amd)

        # Show all phases or current only
        self.chk_amd_all = QCheckBox("Show all phases (not just current)")
        self.chk_amd_all.setChecked(False)
        self.chk_amd_all.setToolTip(
            "When ON: draws all A/M/D/C boxes for the full period\n"
            "When OFF: draws only the currently active phase box per level"
        )
        self.chk_amd_all.stateChanged.connect(self._on_amd_settings_changed)
        av.addWidget(self.chk_amd_all)

        # Level checkboxes
        av.addWidget(_lbl("📊 Visible levels:"))
        self._amd_level_checks = {}
        level_colors = {
            "1M":      C['txt3'],
            "5M":      C['txt2'],
            "1H":      C['cyan'],
            "4H":      C['blue'],
            "Day":     C['gold'],
            "Week":    C['orange'],
            "Month":   C['amd_m'],
            "Quarter": C['purple'],
        }
        for level in ["1M","5M","1H","4H","Day","Week","Month","Quarter"]:
            chk = QCheckBox(level)
            chk.setChecked(level in DEFAULT_LEVELS)
            chk.setStyleSheet(f"color:{level_colors.get(level, C['txt2'])};")
            chk.stateChanged.connect(self._on_amd_settings_changed)
            self._amd_level_checks[level] = chk
            av.addWidget(chk)

        # AMD status display
        self.lbl_amd_status = QLabel("AMD: —")
        self.lbl_amd_status.setStyleSheet(
            f"color:{C['amd_a']};font-family:Consolas;font-size:10px;")
        self.lbl_amd_status.setWordWrap(True)
        av.addWidget(self.lbl_amd_status)

        vl.addWidget(grp_amd)

        vl.addStretch()
        return scroll

    # ── Right Panel ───────────────────────────────────────────────

    def _build_right(self):
        w = QWidget(); vl = QVBoxLayout(w)
        vl.setSpacing(0); vl.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._tab_log(),     "📋  Log")
        self.tabs.addTab(self._tab_sources(), "📌  Sources")
        self.tabs.addTab(self._tab_orders(),  "📊  Orders")
        vl.addWidget(self.tabs)
        return w

    def _tab_log(self):
        w = QWidget(); vl = QVBoxLayout(w)
        vl.setContentsMargins(4, 4, 4, 4); vl.setSpacing(4)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QTextEdit.NoWrap)
        vl.addWidget(self.log_view)
        btn = QPushButton("Clear Log")
        btn.setFixedHeight(24)
        btn.clicked.connect(self.log_view.clear)
        vl.addWidget(btn, alignment=Qt.AlignRight)
        return w

    def _tab_sources(self):
        w = QWidget(); vl = QVBoxLayout(w)
        vl.setContentsMargins(6, 6, 6, 6); vl.setSpacing(6)

        row = QHBoxLayout(); row.setSpacing(8)
        self._src_cards = {}
        for key, label, color in [
            ("total",     "LINES",      C['cyan']),
            ("idle",      "IDLE",       C['txt2']),
            ("pending",   "PENDING",    C['orange']),
            ("active",    "ACTIVE",     C['green']),
            ("exhausted", "EXHAUSTED",  C['red']),
        ]:
            f = QFrame()
            f.setStyleSheet(
                f"background:{C['card']};border:1px solid {C['border']};border-radius:6px;")
            fv = QVBoxLayout(f); fv.setContentsMargins(8, 4, 8, 4); fv.setSpacing(0)
            lt = QLabel(label)
            lt.setStyleSheet(f"color:{C['txt3']};font-size:8px;font-weight:bold;")
            lt.setAlignment(Qt.AlignCenter)
            lv = QLabel("0")
            lv.setStyleSheet(
                f"color:{color};font-size:15px;font-weight:bold;font-family:Consolas;")
            lv.setAlignment(Qt.AlignCenter)
            fv.addWidget(lt); fv.addWidget(lv)
            self._src_cards[key] = lv
            row.addWidget(f)
        vl.addLayout(row)

        self.src_table = QTableWidget(0, 6)
        self.src_table.setHorizontalHeaderLabels(
            ["Line Name", "Price", "State", "Round", "BUY Lot", "SELL Lot"])
        self.src_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, 6):
            self.src_table.horizontalHeader().setSectionResizeMode(
                i, QHeaderView.ResizeToContents)
        self.src_table.setAlternatingRowColors(True)
        self.src_table.setEditTriggers(QTableWidget.NoEditTriggers)
        vl.addWidget(self.src_table)
        return w

    def _tab_orders(self):
        w = QWidget(); vl = QVBoxLayout(w)
        vl.setContentsMargins(6, 6, 6, 6); vl.setSpacing(6)

        row = QHBoxLayout(); row.setSpacing(8)
        self._ord_cards = {}
        for key, label, color in [
            ("pending",   "PENDING",    C['cyan']),
            ("buy_pos",   "BUY POS",    C['green']),
            ("sell_pos",  "SELL POS",   C['red']),
            ("total_pnl", "OPEN P&L",   C['purple']),
        ]:
            f = QFrame()
            f.setStyleSheet(
                f"background:{C['card']};border:1px solid {C['border']};border-radius:6px;")
            fv = QVBoxLayout(f); fv.setContentsMargins(8, 4, 8, 4); fv.setSpacing(0)
            lt = QLabel(label)
            lt.setStyleSheet(f"color:{C['txt3']};font-size:8px;font-weight:bold;")
            lt.setAlignment(Qt.AlignCenter)
            lv = QLabel("—")
            lv.setStyleSheet(
                f"color:{color};font-size:15px;font-weight:bold;font-family:Consolas;")
            lv.setAlignment(Qt.AlignCenter)
            fv.addWidget(lt); fv.addWidget(lv)
            self._ord_cards[key] = lv
            row.addWidget(f)
        vl.addLayout(row)

        grp_pend = QGroupBox("🔵  Pending Orders")
        pv = QVBoxLayout(grp_pend); pv.setContentsMargins(4, 4, 4, 4)
        self.tbl_pending = QTableWidget(0, 6)
        self.tbl_pending.setHorizontalHeaderLabels(
            ["Ticket", "Type", "Entry", "SL", "Volume", "TP"])
        self.tbl_pending.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_pending.setAlternatingRowColors(True)
        self.tbl_pending.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tbl_pending.setMaximumHeight(160)
        pv.addWidget(self.tbl_pending)
        vl.addWidget(grp_pend)

        grp_pos = QGroupBox("🟢  Open Positions")
        posv = QVBoxLayout(grp_pos); posv.setContentsMargins(4, 4, 4, 4)
        self.tbl_positions = QTableWidget(0, 6)
        self.tbl_positions.setHorizontalHeaderLabels(
            ["Ticket", "Type", "Entry", "SL", "Volume", "P&L"])
        self.tbl_positions.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tbl_positions.setAlternatingRowColors(True)
        self.tbl_positions.setEditTriggers(QTableWidget.NoEditTriggers)
        posv.addWidget(self.tbl_positions)
        vl.addWidget(grp_pos)

        btn_ref = QPushButton("🔄  Refresh Now")
        btn_ref.setFixedHeight(26)
        btn_ref.clicked.connect(self._refresh_orders)
        vl.addWidget(btn_ref, alignment=Qt.AlignRight)
        return w

    # ── Status Bar ────────────────────────────────────────────────

    def _build_statusbar(self):
        w = QFrame()
        w.setStyleSheet(
            f"background:{C['panel']};border:1px solid {C['border']};border-radius:4px;")
        w.setFixedHeight(28)
        hl = QHBoxLayout(w); hl.setContentsMargins(10, 0, 10, 0)
        self.lbl_sb = QLabel("Ready")
        self.lbl_sb.setStyleSheet(f"color:{C['txt2']};font-size:10px;")
        hl.addWidget(self.lbl_sb)
        hl.addStretch()
        self.lbl_candle = QLabel("Candle: —")
        self.lbl_candle.setStyleSheet(
            f"color:{C['txt3']};font-size:10px;font-family:Consolas;")
        hl.addWidget(self.lbl_candle)
        return w

    # ── Control Handlers ──────────────────────────────────────────

    def _start(self):
        sym    = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        lot    = self.spin_lot.value()
        follow = self.chk_follow.isChecked()

        import config as cfg
        cfg.ORDER_DISTANCE_PIPS = self.spin_dist.value()
        cfg.LOT_SIZE            = lot
        cfg.TP_RR_RATIO         = 0.0
        cfg.BALANCE_TP_RATIO    = 1.0 + self.spin_balance_tp.value() / 100.0

        self.lbl_sym_hdr.setText(sym)

        self._worker = WatcherThread(
            symbol         = sym,
            lot_size       = lot,
            follow_enabled = follow,
            resume_enabled = self.chk_resume.isChecked(),
        )
        self._worker.sig.on_log(    lambda m, l: self._sig.log_line.emit(m, l))
        self._worker.sig.on_status( lambda s:    self._sig.status.emit(s))
        self._worker.sig.on_state(  lambda s:    self._sig.state.emit(s))
        self._worker.sig.on_candle( lambda c:    self._sig.candle.emit(c))
        # ← NEW: wire balance TP signal so GUI can stop all watchers cleanly
        self._worker.sig.on_stop(   lambda:      self._sig.balance_tp.emit())
        self._worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._on_status("🟡  Starting…")

        # Start FVG watcher if enabled
        if self.chk_fvg.isChecked():
            self._fvg_worker = FVGWatcher(
                symbol        = sym,
                min_gap_pips  = self.spin_fvg_gap.value(),
                lookback      = self.spin_fvg_lookback.value(),
                max_draw      = self.spin_fvg_max.value(),
                scan_interval = 5.0,
                log_fn        = lambda m, l="INFO": self._sig.log_line.emit(m, l),
            )
            self._fvg_worker.start()

        # Start OB watcher if enabled
        if self.chk_ob.isChecked():
            self._ob_worker = OBWatcher(
                symbol           = sym,
                min_impulse_pips = self.spin_ob_impulse.value(),
                lookback         = self.spin_ob_lookback.value(),
                swing_lookback   = self.spin_ob_swing.value(),
                max_draw         = self.spin_ob_max.value(),
                scan_interval    = 5.0,
                log_fn           = lambda m, l="INFO": self._sig.log_line.emit(m, l),
            )
            self._ob_worker.start()

        # Start MTF FVG watcher if enabled
        if self.chk_mtf.isChecked():
            self._start_mtf(sym)

        # Start AMD watcher if enabled
        if self.chk_amd.isChecked():
            self._start_amd(sym)

        # Start Confluence watcher if enabled (requires both OB and FVG)
        if self.chk_confluence.isChecked():
            if self._ob_worker and self._fvg_worker:
                self._start_confluence(sym)
            else:
                self.chk_confluence.blockSignals(True)
                self.chk_confluence.setChecked(False)
                self.chk_confluence.blockSignals(False)
                self._on_log(
                    f"{datetime.now().strftime('%H:%M:%S')}  "
                    f"⚠️  Confluence disabled: enable OB and FVG first, then check Confluence",
                    "WARN"
                )

    def _stop(self):
        """Stop all watchers cleanly in the correct order."""
        # Confluence first (re-enables OB/FVG draw_on_chart)
        self._stop_confluence()
        if self._amd_worker:
            self._amd_worker.stop()
            self._amd_worker = None
        if self._mtf_fvg_worker:
            self._mtf_fvg_worker.stop()
            self._mtf_fvg_worker = None
        # FVG and OB before MT5 shutdown
        if self._fvg_worker:
            self._fvg_worker.stop()
            self._fvg_worker = None
        if self._ob_worker:
            self._ob_worker.stop()
            self._ob_worker  = None
        # Main watcher last — calls mt5.shutdown() at end of its run()
        if self._worker:
            self._worker.stop()
            self._worker = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._on_status("⚫  Stopped")

    # ← NEW: called on Qt main thread when balance TP fires
    def _on_balance_tp_reached(self):
        """Stop all watchers cleanly after balance TP. Called on Qt main thread."""
        self._on_log(
            f"{datetime.now().strftime('%H:%M:%S')}  "
            f"🎯  Balance TP reached — stopping all watchers", "NEW"
        )
        # Stop in correct order: confluence → FVG/OB/AMD/MTF → watcher exits naturally
        self._stop_confluence()
        if self._mtf_fvg_worker:
            self._mtf_fvg_worker.stop()
            self._mtf_fvg_worker = None
        if self._amd_worker:
            self._amd_worker.stop()
            self._amd_worker = None
        if self._mtf_fvg_worker:
            self._mtf_fvg_worker.stop()
            self._mtf_fvg_worker = None
        if self._fvg_worker:
            self._fvg_worker.stop()
            self._fvg_worker = None
        if self._ob_worker:
            self._ob_worker.stop()
            self._ob_worker  = None
        # Don't call _worker.stop() — it already set its own stop event.
        # Just clear the reference; mt5.shutdown() runs at end of watcher.run().
        self._worker = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._on_status("🎯  Balance TP — session complete")

    def _start_confluence(self, sym: str = None):
        if sym is None:
            sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        if not self._ob_worker or not self._fvg_worker:
            self._on_log(
                f"{datetime.now().strftime('%H:%M:%S')}  "
                f"⚠️  Confluence needs both OB and FVG enabled — "
                f"enable them first, then activate Confluence", "WARN"
            )
            self.chk_confluence.blockSignals(True)
            self.chk_confluence.setChecked(False)
            self.chk_confluence.blockSignals(False)
            return
        if self._confluence_worker:
            return

        self._confluence_worker = ConfluenceWatcher(
            symbol            = sym,
            ob_watcher        = self._ob_worker,
            fvg_watcher       = self._fvg_worker,
            max_candles_after = self.spin_conf_window.value(),
            require_direction = self.chk_conf_direction.isChecked(),
            scan_interval     = 5.0,
            max_draw          = self.spin_conf_max.value(),
            log_fn            = lambda m, l="INFO": self._sig.log_line.emit(m, l),
        )
        self._confluence_worker.start()

    def _stop_confluence(self):
        if self._confluence_worker:
            self._confluence_worker.stop()
            self._confluence_worker = None

    def _cancel_all(self):
        sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        try:
            orders = mt5.orders_get(symbol=sym) or []
            cancelled = 0
            for o in orders:
                if o.magic == MAGIC_NUMBER:
                    res = mt5.order_send({"action": mt5.TRADE_ACTION_REMOVE, "order": o.ticket})
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        cancelled += 1
            ts = datetime.now().strftime("%H:%M:%S")
            self._on_log(f"{ts}  🗑️  Cancelled {cancelled} bot orders", "WARN")
        except Exception as e:
            self._on_log(f"Cancel error: {e}", "ERROR")

    # ── Toggle Handlers (live enable/disable) ─────────────────────

    def _on_fvg_toggled(self, state):
        if not self._worker:
            return
        sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        if state == Qt.Checked:
            if not self._fvg_worker:
                self._fvg_worker = FVGWatcher(
                    symbol        = sym,
                    min_gap_pips  = self.spin_fvg_gap.value(),
                    lookback      = self.spin_fvg_lookback.value(),
                    max_draw      = self.spin_fvg_max.value(),
                    scan_interval = 5.0,
                    log_fn        = lambda m, l="INFO": self._sig.log_line.emit(m, l),
                )
                self._fvg_worker.start()
        else:
            if self._confluence_worker:
                self._stop_confluence()
                self.chk_confluence.blockSignals(True)
                self.chk_confluence.setChecked(False)
                self.chk_confluence.blockSignals(False)
                self.lbl_conf_count.setText("Confluence: — (FVG disabled)")
            if self._fvg_worker:
                self._fvg_worker.stop()
                self._fvg_worker = None
            self.lbl_fvg_count.setText("FVGs: — (disabled)")

    def _on_ob_toggled(self, state):
        if not self._worker:
            return
        sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        if state == Qt.Checked:
            if not self._ob_worker:
                self._ob_worker = OBWatcher(
                    symbol           = sym,
                    min_impulse_pips = self.spin_ob_impulse.value(),
                    lookback         = self.spin_ob_lookback.value(),
                    swing_lookback   = self.spin_ob_swing.value(),
                    max_draw         = self.spin_ob_max.value(),
                    scan_interval    = 5.0,
                    log_fn           = lambda m, l="INFO": self._sig.log_line.emit(m, l),
                )
                self._ob_worker.start()
        else:
            if self._confluence_worker:
                self._stop_confluence()
                self.chk_confluence.blockSignals(True)
                self.chk_confluence.setChecked(False)
                self.chk_confluence.blockSignals(False)
                self.lbl_conf_count.setText("Confluence: — (OB disabled)")
            if self._ob_worker:
                self._ob_worker.stop()
                self._ob_worker = None
            self.lbl_ob_count.setText("OBs: — (disabled)")

    def _on_confluence_toggled(self, state):
        if not self._worker:
            return
        if state == Qt.Checked:
            if not self._ob_worker or not self._fvg_worker:
                self._on_log(
                    f"{datetime.now().strftime('%H:%M:%S')}  "
                    f"⚠️  Enable OB and FVG first, then activate Confluence", "WARN"
                )
                self.chk_confluence.blockSignals(True)
                self.chk_confluence.setChecked(False)
                self.chk_confluence.blockSignals(False)
                return
            self._start_confluence()
        else:
            self._stop_confluence()
            self.lbl_conf_count.setText("Confluence: — (disabled)")

    # ── Signal Handlers ───────────────────────────────────────────

    def _on_log(self, msg: str, level: str = "INFO"):
        colors = {"ERROR": C['red'], "WARN": C['orange'], "NEW": C['green']}
        color = colors.get(level, C['txt'])
        self.log_view.append(f'<span style="color:{color};">{msg}</span>')
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())
        self.lbl_sb.setText(msg[:100])

    def _on_status(self, msg: str):
        self.lbl_status.setText(msg)

    def _on_state(self, states: list):
        counts = {SourceState.IDLE: 0, SourceState.PENDING: 0,
                  SourceState.ACTIVE: 0, SourceState.EXHAUSTED: 0}
        for s in states:
            counts[s["state"]] = counts.get(s["state"], 0) + 1

        self._src_cards["total"].setText(str(len(states)))
        self._src_cards["idle"].setText(str(counts[SourceState.IDLE]))
        self._src_cards["pending"].setText(str(counts[SourceState.PENDING]))
        self._src_cards["active"].setText(str(counts[SourceState.ACTIVE]))
        self._src_cards["exhausted"].setText(str(counts[SourceState.EXHAUSTED]))

        self.src_table.setRowCount(len(states))
        active_lines = []
        for r, s in enumerate(states):
            st       = s["state"]
            rnd      = s["round"]
            buy_lot  = s.get("buy_lot",  s.get("lot", 0.0))
            sell_lot = s.get("sell_lot", s.get("lot", 0.0))

            state_color = {
                SourceState.IDLE:      C['txt2'],
                SourceState.PENDING:   C['orange'],
                SourceState.ACTIVE:    C['green'],
                SourceState.EXHAUSTED: C['red'],
            }.get(st, C['txt'])

            vals = [
                (s["name"][:30],            C['txt']),
                (f"{s['price']:.5f}",       C['cyan']),
                (st,                        state_color),
                (str(rnd) if rnd else "—",  C['gold']),
                (f"{buy_lot:.2f}",          C['green']),
                (f"{sell_lot:.2f}",         C['red']),
            ]
            for c, (v, clr) in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setForeground(QColor(clr))
                self.src_table.setItem(r, c, it)

            if st in (SourceState.PENDING, SourceState.ACTIVE):
                active_lines.append(
                    f"📌 {s['name'][:14]} R{rnd} | BUY {buy_lot:.2f} SELL {sell_lot:.2f}")

        self.lbl_sequences.setText(
            "\n".join(active_lines) if active_lines else "—  No active sequences"
        )

    def _on_candle(self, candle: dict):
        self._last_candle = candle
        h = candle.get("CANDLE_H", 0.0)
        l = candle.get("CANDLE_L", 0.0)
        c = candle.get("CANDLE_C", 0.0)
        if h:
            self.lbl_candle.setText(
                f"Candle  H:{h:.5f}  L:{l:.5f}  C:{c:.5f}")
        bid = candle.get("BID", 0.0)
        if bid:
            self.lbl_ea_status.setText(f"EA: ✅  bid={bid:.5f}")
            self.lbl_ea_status.setStyleSheet(f"color:{C['green']};font-size:10px;")

    def _refresh_price(self):
        sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        try:
            tick = mt5.symbol_info_tick(sym)
            if tick:
                self.lbl_price.setText(f"{sym}  {tick.bid:.5f}")
        except Exception:
            pass

    def _refresh_orders(self):
        sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        try:
            orders   = mt5.orders_get(symbol=sym) or []
            bot_ord  = [o for o in orders if o.magic == MAGIC_NUMBER]
            self.tbl_pending.setRowCount(len(bot_ord))
            for r, o in enumerate(bot_ord):
                is_buy = o.type == 2
                clr    = QColor(C['green'] if is_buy else C['red'])
                for c, v in enumerate([str(o.ticket),
                                        "BUY-STOP" if is_buy else "SELL-STOP",
                                        f"{o.price_open:.5f}",
                                        f"{o.sl:.5f}",
                                        f"{o.volume_current:.2f}",
                                        f"{o.tp:.5f}"]):
                    it = QTableWidgetItem(v); it.setForeground(clr)
                    self.tbl_pending.setItem(r, c, it)

            positions = mt5.positions_get(symbol=sym) or []
            bot_pos   = [p for p in positions if p.magic == MAGIC_NUMBER]
            self.tbl_positions.setRowCount(len(bot_pos))
            total_pnl = 0.0
            buys = sells = 0
            for r, p in enumerate(bot_pos):
                is_buy = p.type == 0
                clr    = QColor(C['green'] if is_buy else C['red'])
                pnl_c  = QColor(C['green'] if p.profit >= 0 else C['red'])
                total_pnl += p.profit
                if is_buy: buys += 1
                else: sells += 1
                vals = [str(p.ticket), "BUY" if is_buy else "SELL",
                        f"{p.price_open:.5f}", f"{p.sl:.5f}",
                        f"{p.volume:.2f}", f"{p.profit:+.2f}"]
                cols = [clr, clr, clr, clr, clr, pnl_c]
                for c, (v, co) in enumerate(zip(vals, cols)):
                    it = QTableWidgetItem(v); it.setForeground(co)
                    self.tbl_positions.setItem(r, c, it)

            self._ord_cards["pending"].setText(str(len(bot_ord)))
            self._ord_cards["buy_pos"].setText(str(buys))
            self._ord_cards["sell_pos"].setText(str(sells))
            pnl_color = C['green'] if total_pnl >= 0 else C['red']
            self._ord_cards["total_pnl"].setText(f"{total_pnl:+.2f}")
            self._ord_cards["total_pnl"].setStyleSheet(
                f"color:{pnl_color};font-size:15px;font-weight:bold;font-family:Consolas;")

            acct = mt5.account_info()
            if acct:
                pct       = self.spin_balance_tp.value()
                start_bal = acct.balance
                try:
                    import json as _json, os as _os
                    _f = f"start_balance_{sym}.json"
                    if _os.path.exists(_f):
                        saved = _json.load(open(_f))
                        start_bal = saved.get("start_balance", acct.balance)
                except Exception:
                    pass
                target = start_bal * (1.0 + pct / 100.0)
                profit = acct.balance - start_bal
                profit_color = C['green'] if profit >= 0 else C['red']
                self.lbl_balance.setText(
                    f"Balance: {acct.balance:.2f}  "
                    f"<span style='color:{profit_color};'>({profit:+.2f})</span>"
                )
                self.lbl_balance_target.setText(
                    f"Start: {start_bal:.2f}  Target: {target:.2f}  (+{pct:.0f}%)"
                )
        except Exception:
            pass

    def _refresh_indicator_counts(self):
        """Update FVG, OB, Confluence, MTF FVG, and AMD count labels."""
        self._refresh_mtf_count()
        try:
            if self._fvg_worker:
                fvgs = self._fvg_worker.get_fvgs()
                bull = sum(1 for f in fvgs if f.kind == "BULL")
                bear = sum(1 for f in fvgs if f.kind == "BEAR")
                self.lbl_fvg_count.setText(
                    f"FVGs: {len(fvgs)} total  🟢{bull} bull  🔴{bear} bear")
            elif not self.chk_fvg.isChecked():
                self.lbl_fvg_count.setText("FVGs: — (disabled)")
        except Exception:
            pass

        try:
            if self._ob_worker:
                active_obs = self._ob_worker.get_obs()
                all_obs    = self._ob_worker.get_all_obs()
                mitigated  = sum(1 for ob in all_obs if ob.mitigated)
                bull       = sum(1 for ob in active_obs if ob.kind == "BULL")
                bear       = sum(1 for ob in active_obs if ob.kind == "BEAR")
                self.lbl_ob_count.setText(
                    f"OBs: {len(active_obs)} active  "
                    f"🟦{bull} bull  🟣{bear} bear  "
                    f"({mitigated} mitigated)"
                )
            elif not self.chk_ob.isChecked():
                self.lbl_ob_count.setText("OBs: — (disabled)")
        except Exception:
            pass

        try:
            if self._confluence_worker:
                zones = self._confluence_worker.get_zones()
                bull  = sum(1 for z in zones if z.kind == "BULL")
                bear  = sum(1 for z in zones if z.kind == "BEAR")
                self.lbl_conf_count.setText(
                    f"Confluence: {len(zones)} zones  🟡{bull} bull  🟣{bear} bear"
                )
            elif not self.chk_confluence.isChecked():
                self.lbl_conf_count.setText("Confluence: — (disabled)")
        except Exception:
            pass

    # ── Settings change handlers ──────────────────────────────────


    def _start_amd(self, sym: str = None):
        if sym is None:
            sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        if self._amd_worker:
            return
        levels = [lv for lv, chk in self._amd_level_checks.items() if chk.isChecked()]
        self._amd_worker = AMDWatcher(
            symbol          = sym,
            visible_levels  = levels or DEFAULT_LEVELS,
            show_all_phases = self.chk_amd_all.isChecked(),
            scan_interval   = 10.0,
            draw_on_chart   = True,
            log_fn          = lambda m, l="INFO": self._sig.log_line.emit(m, l),
        )
        self._amd_worker.start()

    def _on_amd_toggled(self, state):
        if not self._worker:
            return
        sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        if state == Qt.Checked:
            self._start_amd(sym)
        else:
            if self._amd_worker:
                self._amd_worker.stop()
                self._amd_worker = None
            self.lbl_amd_status.setText("AMD: — (disabled)")

    def _on_amd_settings_changed(self):
        if self._amd_worker:
            levels = [lv for lv, chk in self._amd_level_checks.items() if chk.isChecked()]
            self._amd_worker.update_settings(
                visible_levels  = levels or DEFAULT_LEVELS,
                show_all_phases = self.chk_amd_all.isChecked(),
            )

    def _refresh_amd_status(self):
        """Update AMD status label from live watcher."""
        try:
            if self._amd_worker:
                s = self._amd_worker.get_status()
                if s:
                    def ic(label):
                        if "(" in label and ")" in label:
                            ph = label[label.index("(")+1:label.index(")")]
                            return {"A":"🟩","M":"🟥","D":"🟦","C":"⬜"}.get(ph,"⬜")
                        return "⬜"
                    self.lbl_amd_status.setText(
                        f"Y:{s.year}  {ic(s.quarter)}{s.quarter}  "
                        f"{ic(s.month)}{s.month}  {ic(s.week)}{s.week}\n"
                        f"{ic(s.day)}{s.day}  4H:{ic(s.h4)}{s.h4}  "
                        f"1H:{ic(s.h1)}{s.h1}\n"
                        f"5M:{ic(s.m5)}{s.m5}  1M:{ic(s.minute)}{s.minute}"
                    )
            elif not self.chk_amd.isChecked():
                self.lbl_amd_status.setText("AMD: — (disabled)")
        except Exception:
            pass

    def _start_mtf(self, sym=None):
        if sym is None:
            sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        if self._mtf_fvg_worker:
            return
        from core.order_manager import get_pip_size
        import config as cfg
        pip = get_pip_size(sym)
        self._mtf_fvg_worker = MTFFVGWatcher(
            symbol        = sym,
            pip_size      = pip,
            min_gap_pips  = self.spin_mtf_gap.value(),
            lookback_15m  = self.spin_mtf_lb15.value(),
            lookback_5m   = self.spin_mtf_lb5.value(),
            lookback_1m   = self.spin_mtf_lb1.value(),
            max_zones     = self.spin_mtf_max.value(),
            max_draw      = self.spin_mtf_max.value(),
            draw_on_chart = True,
            poll_interval = 1.0,
            log_fn        = lambda m, l="INFO": self._sig.log_line.emit(m, l),
        )
        self._mtf_fvg_worker.start()

    def _on_mtf_toggled(self, state):
        if not self._worker:
            return
        sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        if state == Qt.Checked:
            self._start_mtf(sym)
        else:
            if self._mtf_fvg_worker:
                self._mtf_fvg_worker.stop()
                self._mtf_fvg_worker = None
            self.lbl_mtf_count.setText("MTF FVG: — (disabled)")

    def _on_mtf_settings_changed(self):
        if self._mtf_fvg_worker:
            self._mtf_fvg_worker.update_settings(
                min_gap_pips = self.spin_mtf_gap.value(),
                lookback_15m = self.spin_mtf_lb15.value(),
                lookback_5m  = self.spin_mtf_lb5.value(),
                lookback_1m  = self.spin_mtf_lb1.value(),
                max_zones    = self.spin_mtf_max.value(),
                max_draw     = self.spin_mtf_max.value(),
            )

    def _on_fvg_settings_changed(self):
        if self._fvg_worker:
            self._fvg_worker.update_settings(
                min_gap_pips = self.spin_fvg_gap.value(),
                lookback     = self.spin_fvg_lookback.value(),
                max_draw     = self.spin_fvg_max.value(),
            )

    def _refresh_mtf_count(self):
        try:
            if self._mtf_fvg_worker:
                zones = self._mtf_fvg_worker.get_zones()
                all_z = self._mtf_fvg_worker.get_all_zones()
                mit   = sum(1 for z in all_z if z.mitigated)
                bull  = sum(1 for z in zones if z.kind == "BULL")
                bear  = sum(1 for z in zones if z.kind == "BEAR")
                self.lbl_mtf_count.setText(
                    f"MTF FVG: {len(zones)} active  "
                    f"🟡{bull} bull  🟣{bear} bear  ({mit} mitigated)"
                )
            elif not self.chk_mtf.isChecked():
                self.lbl_mtf_count.setText("MTF FVG: — (disabled)")
        except Exception:
            pass

    def _on_ob_settings_changed(self):
        if self._ob_worker:
            self._ob_worker.update_settings(
                min_impulse_pips = self.spin_ob_impulse.value(),
                lookback         = self.spin_ob_lookback.value(),
                swing_lookback   = self.spin_ob_swing.value(),
                max_draw         = self.spin_ob_max.value(),
            )

    def _on_confluence_settings_changed(self):
        if self._confluence_worker:
            self._confluence_worker.update_settings(
                max_candles_after = self.spin_conf_window.value(),
                require_direction = self.chk_conf_direction.isChecked(),
                max_draw          = self.spin_conf_max.value(),
            )

    def _on_symbol_changed(self, sym: str):
        self.lbl_sym_hdr.setText(sym)

    def _init_price(self):
        sym = self.sym_combo.currentText().strip() or WATCH_SYMBOL
        try:
            if mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
                tick = mt5.symbol_info_tick(sym)
                if tick:
                    self.lbl_price.setText(f"{sym}  {tick.bid:.5f}")
        except Exception:
            pass

    def closeEvent(self, event):
        self._stop()
        event.accept()

# ── Entry Point ───────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = GUI()
    win.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()