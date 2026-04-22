"""
Regime 常量定义。

所有消费 regime_mode 的模块必须对照此集合做合法性校验。
"""

VALID_REGIME_MODES: frozenset[str] = frozenset([
    "offense",
    "cautious_offense",
    "defense",
    "risk_off",
])
