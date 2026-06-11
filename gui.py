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
    LOT_SIZE, LOT_MULTIPLIER, MAX_ROUNDS,
    ORDER_DISTANCE_PIPS, TP_RR_RATIO, MAGIC_NUMBER,
)
from core.watcher import WatcherThread
from core.position_monitor import SourceState
from core.order_manager import lot_for_round

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
        self.spin_lot.valueChanged.connect(self._update_lot_preview)
        _row("📦 Base Lot:", self.spin_lot, cl, "Starting lot size (round 1)")

        # Lot multiplier
        self.spin_mult = QDoubleSpinBox()
        self.spin_mult.setRange(1.01, 3.00); self.spin_mult.setSingleStep(0.05)
        self.spin_mult.setValue(LOT_MULTIPLIER); self.spin_mult.setDecimals(2)
        self.spin_mult.valueChanged.connect(self._update_lot_preview)
        _row("✖️ Multiplier:", self.spin_mult, cl, "Lot × this after each SL hit (1.20 = +20%)")

        # Max rounds
        self.spin_rounds = QSpinBox()
        self.spin_rounds.setRange(1, 20); self.spin_rounds.setValue(MAX_ROUNDS)
        self.spin_rounds.valueChanged.connect(self._update_lot_preview)
        _row("🔁 Max Rounds:", self.spin_rounds, cl, "Maximum martingale iterations (default 9)")

        # TP (R:R)
        tp_row = QHBoxLayout(); tp_row.setSpacing(6)
        lbl_tp = _lbl("🎯 TP (R:R):"); lbl_tp.setFixedWidth(100)
        tp_row.addWidget(lbl_tp)
        self.chk_tp = QCheckBox()
        self.chk_tp.setChecked(TP_RR_RATIO > 0)
        self.chk_tp.setToolTip("Enable Take Profit (unchecked = no TP)")
        tp_row.addWidget(self.chk_tp)
        self.spin_tp = QDoubleSpinBox()
        self.spin_tp.setRange(0.1, 20.0); self.spin_tp.setSingleStep(0.5)
        self.spin_tp.setValue(max(TP_RR_RATIO, 2.0)); self.spin_tp.setDecimals(1)
        self.spin_tp.setEnabled(self.chk_tp.isChecked())
        self.chk_tp.toggled.connect(self.spin_tp.setEnabled)
        tp_row.addWidget(self.spin_tp)
        cl.addLayout(tp_row)

        # Follow moved lines
        self.chk_follow = QCheckBox("Follow moved lines")
        self.chk_follow.setChecked(True)
        self.chk_follow.setToolTip(
            "When you drag a line on the chart, the bot resets and re-watches from the new position")
        cl.addWidget(self.chk_follow)

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

        # ── Lot Schedule group ────────────────────────────────────
        grp_lots = QGroupBox("📊  Lot Schedule")
        ll = QVBoxLayout(grp_lots); ll.setSpacing(4)
        self.lbl_lots = QLabel()
        self.lbl_lots.setStyleSheet(
            f"color:{C['txt2']};font-family:Consolas;font-size:10px;")
        self.lbl_lots.setWordWrap(True)
        ll.addWidget(self.lbl_lots)
        vl.addWidget(grp_lots)
        self._update_lot_preview()

        # ── Active Sequences group ────────────────────────────────
        grp_seq = QGroupBox("🔥  Active Sequences")
        sl = QVBoxLayout(grp_seq); sl.setSpacing(2)
        self.lbl_sequences = QLabel("—  No active sequences")
        self.lbl_sequences.setStyleSheet(
            f"color:{C['txt2']};font-size:11px;font-family:Consolas;")
        self.lbl_sequences.setWordWrap(True)
        sl.addWidget(self.lbl_sequences)
        vl.addWidget(grp_seq)

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
            ["Line Name", "Price", "State", "Round", "Direction", "Next Lot"])
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
        self.tbl_pending = QTableWidget(0, 5)
        self.tbl_pending.setHorizontalHeaderLabels(
            ["Ticket", "Type", "Entry", "SL", "TP"])
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

        # Push runtime config overrides
        import config as cfg
        cfg.ORDER_DISTANCE_PIPS = self.spin_dist.value()
        cfg.LOT_SIZE            = lot
        cfg.LOT_MULTIPLIER      = self.spin_mult.value()
        cfg.MAX_ROUNDS          = self.spin_rounds.value()
        cfg.TP_RR_RATIO         = self.spin_tp.value() if self.chk_tp.isChecked() else 0.0

        self.lbl_sym_hdr.setText(sym)

        self._worker = WatcherThread(symbol=sym, lot_size=lot, follow_enabled=follow)
        self._worker.sig.on_log(    lambda m, l: self._sig.log_line.emit(m, l))
        self._worker.sig.on_status( lambda s:    self._sig.status.emit(s))
        self._worker.sig.on_state(  lambda s:    self._sig.state.emit(s))
        self._worker.sig.on_candle( lambda c:    self._sig.candle.emit(c))
        self._worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._on_status("🟡  Starting…")

    def _stop(self):
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
            st    = s["state"]
            dir_  = s.get("direction") or "—"
            rnd   = s["round"]
            next_lot = lot_for_round(rnd + 1 if rnd > 0 else 1,
                                     self.spin_lot.value()) if st in (
                SourceState.PENDING, SourceState.ACTIVE) else self.spin_lot.value()

            state_color = {
                SourceState.IDLE:      C['txt2'],
                SourceState.PENDING:   C['orange'],
                SourceState.ACTIVE:    C['green'],
                SourceState.EXHAUSTED: C['red'],
            }.get(st, C['txt'])
            dir_color = (C['green'] if dir_ == "BUY" else
                         C['red']   if dir_ == "SELL" else C['txt2'])

            vals = [
                (s["name"][:30],         C['txt']),
                (f"{s['price']:.5f}",    C['cyan']),
                (st,                     state_color),
                (str(rnd) if rnd else "—", C['gold']),
                (dir_,                   dir_color),
                (f"{next_lot:.2f}",      C['purple']),
            ]
            for c, (v, clr) in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setForeground(QColor(clr))
                self.src_table.setItem(r, c, it)

            if st in (SourceState.PENDING, SourceState.ACTIVE):
                icon = "🟢" if dir_ == "BUY" else ("🔴" if dir_ == "SELL" else "🟡")
                active_lines.append(
                    f"{icon} {s['name'][:16]} R{rnd} {dir_} lot={next_lot:.2f}")

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
            # Pending
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
                                        f"{o.tp:.5f}"]):
                    it = QTableWidgetItem(v); it.setForeground(clr)
                    self.tbl_pending.setItem(r, c, it)

            # Positions
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
        except Exception:
            pass

    def _on_symbol_changed(self, sym: str):
        self.lbl_sym_hdr.setText(sym)

    def _update_lot_preview(self):
        base  = self.spin_lot.value()
        mult  = self.spin_mult.value()
        maxr  = self.spin_rounds.value()
        lines = []
        lot   = base
        for i in range(1, maxr + 1):
            lines.append(f"R{i}: {lot:.2f}")
            lot = round(lot * mult, 2)
        # 5 per row
        rows = []
        for i in range(0, len(lines), 5):
            rows.append("  ".join(lines[i:i+5]))
        self.lbl_lots.setText("\n".join(rows))

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
