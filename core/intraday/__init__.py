"""
core.intraday — 盘中监控与信号检测模块公开接口。

导出:
    SignalDetector  : 检测买卖点信号
    IntradaySignal  : 盘中信号数据类
    IntradayCalibrator : 盘中矫正器
    FactorCalculator   : 盘中因子计算器
    IntradayFactors    : 盘中因子数据类
"""

from .signal_detector import SignalDetector, IntradaySignal
from .calibrator import IntradayCalibrator
from .factors import FactorCalculator, IntradayFactors

__all__ = [
    "SignalDetector",
    "IntradaySignal",
    "IntradayCalibrator",
    "FactorCalculator",
    "IntradayFactors",
]
