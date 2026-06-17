"""
╔══════════════════════════════════════════════════════════════════╗
║         TraderBot v2 — Configuration                            ║
║         2-Order Martingale Bot (Buy-Stop + Sell-Stop)           ║
╚══════════════════════════════════════════════════════════════════╝
"""

# ── MT5 CREDENTIALS ──────────────────────────────────────────────
MT5_LOGIN    = 52936622
MT5_PASSWORD = "@Radiar9841@"
MT5_SERVER   = "Alpari-MT5-Demo"

# ── SYMBOL TO WATCH ──────────────────────────────────────────────
WATCH_SYMBOL = "EURUSD"

# ── SCAN SETTINGS ────────────────────────────────────────────────
SCAN_INTERVAL_SEC = 2

# ── ORDER SETTINGS ───────────────────────────────────────────────
ORDER_DISTANCE_PIPS = 1.5

LOT_SIZE        = 0.01
LOT_MULTIPLIER  = 1.20

TP_RR_RATIO     = 0.0
MAGIC_NUMBER    = 998877

# ── OBJECT FILTERING ─────────────────────────────────────────────
# CRITICAL: every prefix used by any detector/drawer in this bot
# MUST be listed here, or the watcher will treat its own drawn
# rectangles/labels as trader-drawn signal lines and start trading
# on them automatically.
AUTO_OBJECT_PREFIXES = [
    "PA_", "CT", "GB_", "TB2_", "autotrade",
    "FVG_",       # FVG detector rectangles
    "OB_",        # Order Block rectangles
    "OBFVG_",     # OB+FVG Confluence rectangles
    "AMD_",       # AMD Quarter Theory boxes
    "AMDT_",      # AMD info table labels
    "MTFFVG_",    # Multi-timeframe FVG confluence intersection zones
    "MTFFVG5M_",  # Multi-timeframe FVG 5M entry rectangles (NEW)
]

BOT_LINE_PREFIX = "TB2_"

# ── LOGGING ──────────────────────────────────────────────────────
LOG_LEVEL = "INFO"