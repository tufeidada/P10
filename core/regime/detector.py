"""
Regime 检测器 — 市场环境判断主编排模块。

综合趋势、波动率、广度、流动性四个维度得分，
映射到四种市场模式(offense/cautious_offense/defense/risk_off)，
并写入 regime_daily 表。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import structlog
import yaml

from db.connection import db_execute, db_query_one

from core.invariants import assert_in

from .breadth import compute_breadth_score
from .constants import VALID_REGIME_MODES
from .liquidity import compute_liquidity_score
from .trend import compute_trend_score
from .volatility import compute_volatility_score

logger = structlog.get_logger(__name__)

# 配置文件路径
_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
_REGIME_PARAMS_PATH = _CONFIG_DIR / "regime_params.yaml"


@dataclass
class RegimeResult:
    """Regime 检测结果。

    Attributes:
        trade_date: 交易日。
        market: 市场代码。
        trend_score: 趋势得分 (0-100)。
        volatility_score: 波动率得分 (0-100)。
        breadth_score: 广度得分 (0-100)。
        liquidity_score: 流动性得分 (0-100)。
        regime_mode: 市场模式 (offense/cautious_offense/defense/risk_off)。
        trend_direction: 趋势方向 (up/neutral/down)。
        volatility_env: 波动率环境 (low/high)。
        signal_threshold_adj: 信号阈值调节系数。
        max_position_pct: 最大仓位比例。
        dimension_weights: 各维度权重。
        detail: 详细信息（包含各子维度明细）。
    """

    trade_date: date
    market: str
    trend_score: float
    volatility_score: float
    breadth_score: float
    liquidity_score: float
    regime_mode: str
    trend_direction: str
    volatility_env: str
    signal_threshold_adj: float
    max_position_pct: float
    dimension_weights: dict[str, float]
    detail: dict[str, Any] = field(default_factory=dict)


@lru_cache(maxsize=1)
def _load_regime_params() -> dict[str, Any]:
    """加载 regime 参数配置（带缓存）。

    Returns:
        regime_params.yaml 的完整内容字典。

    Raises:
        FileNotFoundError: 配置文件不存在。
    """
    with open(_REGIME_PARAMS_PATH, "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)
    logger.info("regime_params_loaded", path=str(_REGIME_PARAMS_PATH))
    return params


def reload_params() -> None:
    """清除缓存，强制重新加载参数（支持热重载）。"""
    _load_regime_params.cache_clear()
    logger.info("regime_params_cache_cleared")


def _classify_trend(trend_score: float, params: dict[str, Any]) -> str:
    """根据趋势得分判定方向。

    Args:
        trend_score: 趋势得分 0-100。
        params: regime 参数配置。

    Returns:
        "up" / "neutral" / "down"。
    """
    thresholds = params["thresholds"]
    if trend_score > thresholds["trend_up"]:
        return "up"
    elif trend_score < thresholds["trend_down"]:
        return "down"
    return "neutral"


def _classify_volatility(volatility_score: float, params: dict[str, Any]) -> str:
    """根据波动率得分判定环境。

    Args:
        volatility_score: 波动率得分 0-100。
        params: regime 参数配置。

    Returns:
        "low" / "high"。
    """
    threshold = params["thresholds"]["volatility_high"]
    return "high" if volatility_score > threshold else "low"


def _map_regime_mode(trend_direction: str, volatility_env: str) -> str:
    """2x2 矩阵映射 regime 模式。

    趋势↑ + 波动低 → offense
    趋势↑ + 波动高 → cautious_offense
    趋势↓/中性 + 波动低 → defense
    趋势↓/中性 + 波动高 → risk_off

    Args:
        trend_direction: 趋势方向。
        volatility_env: 波动率环境。

    Returns:
        regime 模式字符串。
    """
    if trend_direction == "up":
        return "offense" if volatility_env == "low" else "cautious_offense"
    else:
        return "defense" if volatility_env == "low" else "risk_off"


def _apply_corrections(
    mode: str,
    breadth_score: float,
    liquidity_score: float,
    params: dict[str, Any],
) -> str:
    """应用修正规则。

    - breadth < 30 且 offense → cautious_offense
    - liquidity > 70 且 defense → cautious_offense

    Args:
        mode: 初始 regime 模式。
        breadth_score: 广度得分。
        liquidity_score: 流动性得分。
        params: regime 参数配置。

    Returns:
        修正后的 regime 模式。
    """
    corrections = params.get("corrections", {})
    original_mode = mode

    breadth_threshold = corrections.get("breadth_low_downgrade", 30)
    if mode == "offense" and breadth_score < breadth_threshold:
        mode = "cautious_offense"
        logger.info(
            "regime_correction_applied",
            rule="breadth_low_downgrade",
            original=original_mode,
            corrected=mode,
            breadth_score=breadth_score,
            threshold=breadth_threshold,
        )

    liquidity_threshold = corrections.get("liquidity_high_upgrade", 70)
    if mode == "defense" and liquidity_score > liquidity_threshold:
        mode = "cautious_offense"
        logger.info(
            "regime_correction_applied",
            rule="liquidity_high_upgrade",
            original=original_mode,
            corrected=mode,
            liquidity_score=liquidity_score,
            threshold=liquidity_threshold,
        )

    return mode


async def detect_regime(
    market: str = "CN",
    trade_date: date | None = None,
) -> RegimeResult:
    """检测市场 regime 并保存到数据库。

    综合四维得分，通过 2x2 矩阵 + 修正规则，判定当前市场模式，
    并将结果写入 regime_daily 表。

    Args:
        market: 市场代码，默认 "CN"。
        trade_date: 交易日，默认今天。

    Returns:
        RegimeResult 数据类实例。
    """
    if trade_date is None:
        trade_date = date.today()

    params = _load_regime_params()

    logger.info("regime_detection_start", market=market, trade_date=str(trade_date))

    # 计算四维得分
    trend_score = await compute_trend_score(market, trade_date)
    volatility_score = await compute_volatility_score(market, trade_date)
    breadth_score = await compute_breadth_score(market, trade_date)
    liquidity_score = await compute_liquidity_score(market, trade_date)

    # 分类
    trend_direction = _classify_trend(trend_score, params)
    volatility_env = _classify_volatility(volatility_score, params)

    # 2x2 矩阵映射
    regime_mode = _map_regime_mode(trend_direction, volatility_env)

    # 修正规则
    regime_mode = _apply_corrections(regime_mode, breadth_score, liquidity_score, params)

    # 产出合法性校验：非法 mode 在写 DB 前即爆，不允许静默写入脏数据
    assert_in(regime_mode, VALID_REGIME_MODES, "regime_detector.output")

    # 读取该模式的参数
    regime_config = params["regimes"][regime_mode]
    signal_threshold_adj = regime_config["signal_threshold_adj"]
    max_position_pct = regime_config["max_position_pct"]
    dimension_weights = regime_config["weights"]

    detail = {
        "trend_direction": trend_direction,
        "volatility_env": volatility_env,
        "scores": {
            "trend": trend_score,
            "volatility": volatility_score,
            "breadth": breadth_score,
            "liquidity": liquidity_score,
        },
    }

    result = RegimeResult(
        trade_date=trade_date,
        market=market,
        trend_score=trend_score,
        volatility_score=volatility_score,
        breadth_score=breadth_score,
        liquidity_score=liquidity_score,
        regime_mode=regime_mode,
        trend_direction=trend_direction,
        volatility_env=volatility_env,
        signal_threshold_adj=signal_threshold_adj,
        max_position_pct=max_position_pct,
        dimension_weights=dimension_weights,
        detail=detail,
    )

    # 写入 regime_daily (upsert)
    await db_execute(
        """
        INSERT INTO regime_daily (
            trade_date, market,
            trend_score, volatility_score, breadth_score, liquidity_score,
            regime_mode, trend_direction, volatility_env,
            signal_threshold_adj, max_position_pct,
            dimension_weights, detail
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
        ON CONFLICT (trade_date, market) DO UPDATE SET
            trend_score = EXCLUDED.trend_score,
            volatility_score = EXCLUDED.volatility_score,
            breadth_score = EXCLUDED.breadth_score,
            liquidity_score = EXCLUDED.liquidity_score,
            regime_mode = EXCLUDED.regime_mode,
            trend_direction = EXCLUDED.trend_direction,
            volatility_env = EXCLUDED.volatility_env,
            signal_threshold_adj = EXCLUDED.signal_threshold_adj,
            max_position_pct = EXCLUDED.max_position_pct,
            dimension_weights = EXCLUDED.dimension_weights,
            detail = EXCLUDED.detail
        """,
        trade_date,
        market,
        trend_score,
        volatility_score,
        breadth_score,
        liquidity_score,
        regime_mode,
        trend_direction,
        volatility_env,
        signal_threshold_adj,
        max_position_pct,
        json.dumps(dimension_weights),
        json.dumps(detail),
    )

    logger.info(
        "regime_detected",
        market=market,
        trade_date=str(trade_date),
        regime_mode=regime_mode,
        trend_score=trend_score,
        volatility_score=volatility_score,
        breadth_score=breadth_score,
        liquidity_score=liquidity_score,
        trend_direction=trend_direction,
        volatility_env=volatility_env,
        signal_threshold_adj=signal_threshold_adj,
        max_position_pct=max_position_pct,
    )

    return result


async def get_latest_regime(market: str = "CN") -> RegimeResult | None:
    """获取最新的 regime 检测结果。

    从 regime_daily 表读取指定市场最近一条记录。

    Args:
        market: 市场代码，默认 "CN"。

    Returns:
        RegimeResult 实例，无记录时返回 None。
    """
    row = await db_query_one(
        """
        SELECT
            trade_date, market,
            trend_score, volatility_score, breadth_score, liquidity_score,
            regime_mode, trend_direction, volatility_env,
            signal_threshold_adj, max_position_pct,
            dimension_weights, detail
        FROM regime_daily
        WHERE market = $1
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        market,
    )

    if row is None:
        logger.info("regime_not_found", market=market)
        return None

    # dimension_weights 和 detail 可能是 str 或 dict (asyncpg 自动解析 JSONB)
    weights = row["dimension_weights"]
    if isinstance(weights, str):
        weights = json.loads(weights)

    detail = row["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)

    return RegimeResult(
        trade_date=row["trade_date"],
        market=row["market"],
        trend_score=float(row["trend_score"]),
        volatility_score=float(row["volatility_score"]),
        breadth_score=float(row["breadth_score"]),
        liquidity_score=float(row["liquidity_score"]),
        regime_mode=row["regime_mode"],
        trend_direction=row["trend_direction"],
        volatility_env=row["volatility_env"],
        signal_threshold_adj=float(row["signal_threshold_adj"]),
        max_position_pct=float(row["max_position_pct"]),
        dimension_weights=weights,
        detail=detail or {},
    )
