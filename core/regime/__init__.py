"""
Regime 检测模块 — 市场环境四维评估与模式判定。

主要入口:
    from core.regime import detect_regime, get_latest_regime, RegimeResult, VALID_REGIME_MODES
"""

from .constants import VALID_REGIME_MODES
from .detector import RegimeResult, detect_regime, get_latest_regime, reload_params

__all__ = [
    "VALID_REGIME_MODES",
    "RegimeResult",
    "detect_regime",
    "get_latest_regime",
    "reload_params",
]
