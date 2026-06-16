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

# ── Palette (matches v1 exactly) ─────────────────────────────────
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
    log_line = pyqtSignal(str, str)
    status   = pyqtSignal(str)
    state    = pyqtSignal(list)
    candle   = pyqtSignal(dict)
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
        self.setMinimumSize(860, 620)
        self.setStyleSheet(SS)

        self._worker: Optional[WatcherThread] = None
        self._fvg_worker: Optional[FVGWatcher] = None
        self._sig = Sig()
        self._sig.log_line.connect(self._on_log)
        self._sig.status.connect(self._on_status)
        self._sig.state.connect(self._on_state)
        self._sig.candle.connect(self._on_candle)

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

        QTimer.singleShot(200, self._init_price)

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

        # Symbol
        self.sym_combo = QComboBox(); self.sym_combo.setEditable(True)
        self.sym_combo.addItems([
            "EURUSD", "XAUUSD", "GBPUSD", "USDJPY", "EURUSD_i",
            "XAUUSD_i", "GBPUSD_i", "NAS100", "US30", "BTCUSD",
        ])
        self.sym_combo.setCurrentText(WATCH_SYMBOL)
        self.sym_combo.currentTextChanged.connect(self._on_symbol_changed)
        _row("🎯 Symbol:", self.sym_combo, cl, "MT5 symbol to watch")

        # Order distance
        self.spin_dist = QDoubleSpinBox()
        self.spin_dist.setRange(0.1, 100.0); self.spin_dist.setSingleStep(0.5)
        self.spin_dist.setValue(ORDER_DISTANCE_PIPS); self.spin_dist.setDecimals(1)
        _row("📏 Distance (pips):", self.spin_dist, cl,
             "Pips above line for BUY-STOP, below for SELL-STOP")

        # Base lot
        self.spin_lot = QDoubleSpinBox()
        self.spin_lot.setRange(0.01, 100.0); self.spin_lot.setSingleStep(0.01)
        self.spin_lot.setValue(LOT_SIZE); self.spin_lot.setDecimals(2)
        
        _row("📦 Base Lot:", self.spin_lot, cl, "Starting lot size (round 1)")

        # Balance TP %
        self.spin_balance_tp = QDoubleSpinBox()
        self.spin_balance_tp.setRange(1.0, 100.0); self.spin_balance_tp.setSingleStep(1.0)
        self.spin_balance_tp.setValue(10.0); self.spin_balance_tp.setDecimals(1)
        self.spin_balance_tp.setSuffix(" %")
        _row("💰 Balance TP:", self.spin_balance_tp, cl,
             "Close all & stop when balance grows by this % from start")

        # Follow moved lines
        self.chk_follow = QCheckBox("Follow moved lines")
        self.chk_follow.setChecked(True)
        self.chk_follow.setToolTip(
            "When you drag a line on the chart, the bot resets and re-watches from the new position")
        cl.addWidget(self.chk_follow)

        # Resume previous session
        self.chk_resume = QCheckBox("Resume previous session")
        self.chk_resume.setChecked(False)
        self.chk_resume.setToolTip(
            "On start, scan MT5 for existing bot positions/orders\n"
            "and resume monitoring them without re-entering.\n"
            "Use this if the bot stopped unexpectedly.")
        self.chk_resume.setStyleSheet(f"color:{C['orange']};")
        cl.addWidget(self.chk_resume)

        cl.addWidget(_hline())

        # Start / Stop
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

        # Enable toggle
        self.chk_fvg = QCheckBox("Enable FVG detection")
        self.chk_fvg.setChecked(True)
        self.chk_fvg.setToolTip("Scan candles for FVG patterns and draw rectangles on chart")
        fv.addWidget(self.chk_fvg)

        # Min gap pips (quality filter)
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

        # Lookback candles
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

        # Max rectangles to draw
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

        # FVG count label
        self.lbl_fvg_count = QLabel("FVGs: —")
        self.lbl_fvg_count.setStyleSheet(
            f"color:{C['cyan']};font-family:Consolas;font-size:10px;")
        fv.addWidget(self.lbl_fvg_count)

        vl.addWidget(grp_fvg)

        # ── Cancel All group ──────────────────────────────────────
        grp_cancel = QGroupBox("🛑  Emergency")
        ecl = QVBoxLayout(grp_cancel)
        self.btn_cancel_all = QPushButton("🗑️  Cancel All Bot Orders")
        self.btn_cancel_all.setObjectName("btn_cancel")
        self.btn_cancel_all.setMinimumHeight(30)
        self.btn_cancel_all.clicked.connect(self._cancel_all)
        ecl.addWidget(self.btn_cancel_all)
        vl.addWidget(grp_cancel)

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

        # Summary mini-cards row
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

        # Sources table
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

        # Summary mini-cards
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

        # Pending orders table
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

        # Open positions table
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
        cfg.ORDER_DISTANCE_PIPS  = self.spin_dist.value()
        cfg.LOT_SIZE             = lot
        cfg.TP_RR_RATIO          = 0.0
        cfg.BALANCE_TP_RATIO     = 1.0 + self.spin_balance_tp.value() / 100.0

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
        self._worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._on_status("🟡  Starting…")

        # Start FVG watcher if enabled
        if self.chk_fvg.isChecked():
            self._fvg_worker = FVGWatcher(
                symbol       = sym,
                min_gap_pips = self.spin_fvg_gap.value(),
                lookback     = self.spin_fvg_lookback.value(),
                max_draw     = self.spin_fvg_max.value(),
                scan_interval= 5.0,
                log_fn       = lambda m, l="INFO": self._sig.log_line.emit(m, l),
            )
            self._fvg_worker.start()

    def _stop(self):
        if self._fvg_worker:
            self._fvg_worker.stop()
            self._fvg_worker = None
        if self._worker:
            self._worker.stop()
            self._worker = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self._on_status("⚫  Stopped")

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
            st  = s["state"]
            rnd = s["round"]
            # direction field is now "B:0.02 S:0.04"
            dir_str  = s.get("direction") or "—"
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
            # Pending orders
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

            # Open positions
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

            # Update balance display
            acct = mt5.account_info()
            if acct:
                # Load session start balance from file (same file watcher.py saves)
                sym          = self.sym_combo.currentText().strip() or WATCH_SYMBOL
                pct          = self.spin_balance_tp.value()
                start_bal    = acct.balance  # fallback
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

    def _on_fvg_settings_changed(self):
        """Hot-update FVG watcher when spinboxes change."""
        if self._fvg_worker:
            self._fvg_worker.update_settings(
                min_gap_pips = self.spin_fvg_gap.value(),
                lookback     = self.spin_fvg_lookback.value(),
                max_draw     = self.spin_fvg_max.value(),
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