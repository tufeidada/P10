"""
基本面分析引擎 — 盈利质量、成长性、估值、财务健康四维评分。

根据行业差异化框架（config/industry_frameworks.yaml）对不同行业使用
不同的权重组合，生成综合基本面评分。

Phase 2 模块，供 CompositeAnalyzer 调用。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import structlog
import yaml

from db.connection import db_query, db_query_one, db_query_val

logger = structlog.get_logger(__name__)

# 中性默认分数（无数据时使用）
_NEUTRAL: float = 50.0


@dataclass
class FundamentalAnalysis:
    """基本面分析结果。"""

    symbol: str
    profitability_score: float  # 0-100
    growth_score: float  # 0-100
    valuation_score: float  # 0-100 (lower = cheaper = better for buying)
    health_score: float  # 0-100
    composite_score: float  # 0-100, industry-weighted
    industry_framework: str  # which framework was used
    detail: dict = field(default_factory=dict)  # sub-score details for display


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clip value to [lo, hi]."""
    return max(lo, min(hi, value))


class FundamentalAnalyzer:
    """基本面分析引擎。

    Loads industry frameworks from YAML config.
    Maps symbol -> industry via industry_classify table.
    """

    def __init__(self, config_path: str = "config/industry_frameworks.yaml") -> None:
        self._frameworks = self._load_frameworks(config_path)

    @staticmethod
    def _load_frameworks(path: str) -> dict[str, Any]:
        """加载行业差异化评分框架。

        Args:
            path: YAML 配置文件路径。

        Returns:
            框架配置字典。
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return data.get("frameworks", {})
        except Exception as e:
            logger.warning("industry_frameworks_load_fallback", error=str(e))
            return {
                "default": {
                    "sw1_names": [],
                    "weights": {
                        "profitability": 0.30,
                        "growth": 0.25,
                        "valuation": 0.25,
                        "health": 0.20,
                    },
                },
            }

    def _get_framework(self, sw1_name: str | None) -> tuple[str, dict[str, float]]:
        """Find matching framework for the industry name.

        Args:
            sw1_name: 申万一级行业名称。

        Returns:
            (framework_name, weights) 元组。
        """
        if sw1_name:
            for name, cfg in self._frameworks.items():
                if name == "default":
                    continue
                if sw1_name in cfg.get("sw1_names", []):
                    return name, cfg.get("weights", {})

        default_cfg = self._frameworks.get("default", {})
        return "default", default_cfg.get("weights", {
            "profitability": 0.30,
            "growth": 0.25,
            "valuation": 0.25,
            "health": 0.20,
        })

    async def _get_industry(self, symbol: str) -> tuple[str | None, str | None]:
        """查询股票的行业分类。

        Args:
            symbol: 证券代码。

        Returns:
            (sw1_name, sw1_code) 元组。
        """
        try:
            row = await db_query_one(
                "SELECT sw1_name, sw1_code FROM industry_classify WHERE symbol = $1",
                symbol,
            )
            if row:
                return row["sw1_name"], row["sw1_code"]
        except Exception as e:
            logger.warning("industry_lookup_error", symbol=symbol, error=str(e))
        return None, None

    async def _load_financials(self, symbol: str, quarters: int = 8) -> list[dict]:
        """Load recent quarterly financials.

        Args:
            symbol: 证券代码。
            quarters: 加载最近几个季度的数据。

        Returns:
            按 report_date 降序排列的财务数据字典列表。
        """
        try:
            rows = await db_query(
                """
                SELECT symbol, report_date, announce_date,
                       revenue, revenue_yoy, revenue_qoq,
                       net_profit, np_yoy,
                       gross_margin, net_margin,
                       total_assets, total_liab, debt_ratio, current_ratio,
                       goodwill, ocf, ocf_to_np,
                       roe_ttm, roa_ttm,
                       dupont_npm, dupont_tat, dupont_em
                FROM financials_quarterly
                WHERE symbol = $1
                ORDER BY report_date DESC
                LIMIT $2
                """,
                symbol,
                quarters,
            )
            result = []
            # CN（Tushare）: 已以百分比形式存储（revenue_yoy=10.97 表示 10.97%），无需转换
            # US（yfinance）: 以小数形式存储（revenue_yoy=0.7321 表示 73.21%），需 ×100
            is_cn = any(symbol.endswith(sfx) for sfx in (".SZ", ".SH", ".BJ"))
            pct_fields = {"revenue_yoy", "revenue_qoq", "np_yoy",
                          "gross_margin", "net_margin", "debt_ratio",
                          "roe_ttm", "roa_ttm", "dupont_npm"}
            for r in rows:
                d = dict(r)
                for f in pct_fields:
                    if d.get(f) is not None:
                        d[f] = float(d[f]) if is_cn else float(d[f]) * 100
                result.append(d)
            return result
        except Exception as e:
            logger.warning("load_financials_error", symbol=symbol, error=str(e))
            return []

    async def _load_valuation(self, symbol: str, trade_date: date) -> dict | None:
        """Load PE/PB from fundamentals_daily.

        Args:
            symbol: 证券代码。
            trade_date: 交易日期。

        Returns:
            估值数据字典，或 None。
        """
        try:
            row = await db_query_one(
                """
                SELECT pe_ttm, pb, ps_ttm, total_mv, circ_mv, turnover_rate_f
                FROM fundamentals_daily
                WHERE symbol = $1 AND trade_date <= $2
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                symbol,
                trade_date,
            )
            return dict(row) if row else None
        except Exception as e:
            logger.warning("load_valuation_error", symbol=symbol, error=str(e))
            return None

    async def _calc_pe_industry_percentile(
        self, symbol: str, sw1_name: str, trade_date: date,
    ) -> float:
        """PE percentile within same industry.

        Args:
            symbol: 证券代码。
            sw1_name: 申万一级行业名称。
            trade_date: 交易日期。

        Returns:
            百分位数 0-100，值越高表示 PE 越高（越贵）。
        """
        try:
            # 获取同行业所有股票最新 PE
            rows = await db_query(
                """
                SELECT fd.symbol, fd.pe_ttm
                FROM fundamentals_daily fd
                JOIN industry_classify ic ON fd.symbol = ic.symbol
                WHERE ic.sw1_name = $1
                  AND fd.trade_date = (
                      SELECT MAX(trade_date) FROM fundamentals_daily
                      WHERE symbol = fd.symbol AND trade_date <= $2
                  )
                  AND fd.pe_ttm > 0
                """,
                sw1_name,
                trade_date,
            )
            if not rows or len(rows) < 3:
                return _NEUTRAL

            pe_values = sorted([float(r["pe_ttm"]) for r in rows])
            # 获取当前股票的 PE
            my_pe = None
            for r in rows:
                if r["symbol"] == symbol:
                    my_pe = float(r["pe_ttm"])
                    break

            if my_pe is None or my_pe <= 0:
                return _NEUTRAL

            # 计算百分位
            rank = sum(1 for v in pe_values if v <= my_pe)
            percentile = (rank / len(pe_values)) * 100.0
            return round(percentile, 1)

        except Exception as e:
            logger.warning(
                "pe_industry_percentile_error",
                symbol=symbol,
                error=str(e),
            )
            return _NEUTRAL

    async def _calc_pe_historical_percentile(
        self, symbol: str, trade_date: date, years: int = 3,
    ) -> float:
        """PE percentile within own history.

        Args:
            symbol: 证券代码。
            trade_date: 交易日期。
            years: 回溯年数。

        Returns:
            百分位数 0-100，值越高表示当前 PE 在历史中越高（越贵）。
        """
        try:
            start_date = trade_date - timedelta(days=years * 365)
            rows = await db_query(
                """
                SELECT pe_ttm
                FROM fundamentals_daily
                WHERE symbol = $1
                  AND trade_date BETWEEN $2 AND $3
                  AND pe_ttm > 0
                ORDER BY trade_date
                """,
                symbol,
                start_date,
                trade_date,
            )
            if not rows or len(rows) < 20:
                return _NEUTRAL

            pe_values = [float(r["pe_ttm"]) for r in rows]
            current_pe = pe_values[-1]
            rank = sum(1 for v in pe_values if v <= current_pe)
            percentile = (rank / len(pe_values)) * 100.0
            return round(percentile, 1)

        except Exception as e:
            logger.warning(
                "pe_historical_percentile_error",
                symbol=symbol,
                error=str(e),
            )
            return _NEUTRAL

    def _calc_profitability_score(
        self, financials: list[dict], is_finance: bool = False,
    ) -> tuple[float, dict]:
        """计算盈利质量评分。

        Args:
            financials: 按 report_date 降序的季度财务数据。
            is_finance: 是否为金融行业。

        Returns:
            (score, detail) 元组。
        """
        if not financials:
            return _NEUTRAL, {"score": _NEUTRAL, "note": "no data"}

        latest = financials[0]
        roe_ttm = latest.get("roe_ttm")
        gross_margin = latest.get("gross_margin")
        ocf_to_np = latest.get("ocf_to_np")

        # ROE 基础分
        score = 50.0
        if roe_ttm is not None:
            roe_val = float(roe_ttm)
            if roe_val < 5:
                score = 20.0
            elif roe_val < 10:
                score = 40.0
            elif roe_val < 15:
                score = 60.0
            elif roe_val < 25:
                score = 80.0
            else:
                score = 95.0
        else:
            roe_val = None

        # ROE 趋势（4 季度）
        roe_trend = ""
        roe_values = [
            float(f["roe_ttm"])
            for f in financials[:4]
            if f.get("roe_ttm") is not None
        ]
        if len(roe_values) >= 3:
            # 从旧到新
            roe_values_chrono = list(reversed(roe_values))
            if all(roe_values_chrono[i] < roe_values_chrono[i + 1]
                   for i in range(len(roe_values_chrono) - 1)):
                score += 10
                roe_trend = "improving"
            elif all(roe_values_chrono[i] > roe_values_chrono[i + 1]
                     for i in range(len(roe_values_chrono) - 1)):
                score -= 10
                roe_trend = "declining"
            else:
                roe_trend = "stable"

        # 毛利率稳定性（4 季度 std < 3%）
        gm_values = [
            float(f["gross_margin"])
            for f in financials[:4]
            if f.get("gross_margin") is not None
        ]
        gm_stable = False
        if len(gm_values) >= 3:
            gm_std = statistics.stdev(gm_values)
            if gm_std < 3.0:
                score += 10
                gm_stable = True

        # OCF/NP 比率
        ocf_bonus = 0.0
        if ocf_to_np is not None:
            ocf_val = float(ocf_to_np)
            if ocf_val > 0.8:
                ocf_bonus = 10.0
            elif ocf_val > 0.5:
                ocf_bonus = 5.0
            else:
                ocf_bonus = -5.0
            score += ocf_bonus

        score = _clip(score)

        detail = {
            "score": round(score, 1),
            "roe_ttm": round(roe_val, 2) if roe_val is not None else None,
            "roe_trend": roe_trend,
            "gross_margin": round(float(gross_margin), 2) if gross_margin is not None else None,
            "gm_stable": gm_stable,
            "ocf_to_np": round(float(ocf_to_np), 2) if ocf_to_np is not None else None,
        }
        return round(score, 1), detail

    def _calc_growth_score(self, financials: list[dict]) -> tuple[float, dict]:
        """计算成长性评分。

        Args:
            financials: 按 report_date 降序的季度财务数据。

        Returns:
            (score, detail) 元组。
        """
        if not financials:
            return _NEUTRAL, {"score": _NEUTRAL, "note": "no data"}

        latest = financials[0]
        revenue_yoy = latest.get("revenue_yoy")
        np_yoy = latest.get("np_yoy")
        revenue_qoq = latest.get("revenue_qoq")

        # 营收 YoY 基础分
        score = 50.0
        rev_yoy_val = None
        if revenue_yoy is not None:
            rev_yoy_val = float(revenue_yoy)
            if rev_yoy_val < 0:
                score = 20.0
            elif rev_yoy_val < 10:
                score = 40.0
            elif rev_yoy_val < 20:
                score = 60.0
            elif rev_yoy_val < 30:
                score = 75.0
            else:
                score = 90.0

        # 营收增速趋势（4 季度）
        trend = ""
        rev_yoy_values = [
            float(f["revenue_yoy"])
            for f in financials[:4]
            if f.get("revenue_yoy") is not None
        ]
        if len(rev_yoy_values) >= 3:
            # 从旧到新
            vals_chrono = list(reversed(rev_yoy_values))
            if all(vals_chrono[i] < vals_chrono[i + 1]
                   for i in range(len(vals_chrono) - 1)):
                score += 15
                trend = "accelerating"
            elif all(vals_chrono[i] > vals_chrono[i + 1]
                     for i in range(len(vals_chrono) - 1)):
                score -= 10
                trend = "decelerating"
            else:
                trend = "mixed"

        # NP YoY 加减分
        np_yoy_val = None
        if np_yoy is not None:
            np_yoy_val = float(np_yoy)
            if np_yoy_val > 30:
                score += 10
            elif np_yoy_val > 10:
                score += 5
            elif np_yoy_val < -10:
                score -= 10
            elif np_yoy_val < 0:
                score -= 5

        # 营收 QoQ 改善
        rev_qoq_val = None
        if revenue_qoq is not None:
            rev_qoq_val = float(revenue_qoq)
            if len(financials) >= 2 and financials[1].get("revenue_qoq") is not None:
                prev_qoq = float(financials[1]["revenue_qoq"])
                if rev_qoq_val > 0 and rev_qoq_val > prev_qoq:
                    score += 5

        score = _clip(score)

        detail = {
            "score": round(score, 1),
            "revenue_yoy": round(rev_yoy_val, 2) if rev_yoy_val is not None else None,
            "np_yoy": round(np_yoy_val, 2) if np_yoy_val is not None else None,
            "revenue_qoq": round(rev_qoq_val, 2) if rev_qoq_val is not None else None,
            "trend": trend,
        }
        return round(score, 1), detail

    async def _calc_valuation_score(
        self,
        symbol: str,
        valuation: dict | None,
        sw1_name: str | None,
        trade_date: date,
        growth_score: float,
        np_yoy: float | None,
    ) -> tuple[float, dict]:
        """计算估值评分（LOWER = CHEAPER = better for buying）。

        Args:
            symbol: 证券代码。
            valuation: fundamentals_daily 行数据。
            sw1_name: 行业名称。
            trade_date: 交易日期。
            growth_score: 成长性评分（用于 PEG 计算）。
            np_yoy: 净利润同比增速。

        Returns:
            (score, detail) 元组。
        """
        if not valuation:
            return _NEUTRAL, {"score": _NEUTRAL, "note": "no valuation data"}

        pe_ttm = valuation.get("pe_ttm")
        pb = valuation.get("pb")

        pe_val = float(pe_ttm) if pe_ttm is not None else None
        pb_val = float(pb) if pb is not None else None

        # PE 行业百分位 (40% weight)
        pe_ind_pctile = _NEUTRAL
        if pe_val is not None and pe_val > 0 and sw1_name:
            pe_ind_pctile = await self._calc_pe_industry_percentile(
                symbol, sw1_name, trade_date,
            )
        elif pe_val is not None and pe_val <= 0:
            pe_ind_pctile = 90.0  # 亏损公司，估值不利

        # PE 历史百分位 (30% weight)
        pe_hist_pctile = _NEUTRAL
        if pe_val is not None and pe_val > 0:
            pe_hist_pctile = await self._calc_pe_historical_percentile(
                symbol, trade_date,
            )
        elif pe_val is not None and pe_val <= 0:
            pe_hist_pctile = 90.0

        # PB 行业百分位 (15% weight)
        pb_ind_pctile = _NEUTRAL
        if pb_val is not None and pb_val > 0 and sw1_name:
            try:
                rows = await db_query(
                    """
                    SELECT fd.symbol, fd.pb
                    FROM fundamentals_daily fd
                    JOIN industry_classify ic ON fd.symbol = ic.symbol
                    WHERE ic.sw1_name = $1
                      AND fd.trade_date = (
                          SELECT MAX(trade_date) FROM fundamentals_daily
                          WHERE symbol = fd.symbol AND trade_date <= $2
                      )
                      AND fd.pb > 0
                    """,
                    sw1_name,
                    trade_date,
                )
                if rows and len(rows) >= 3:
                    pb_values = sorted([float(r["pb"]) for r in rows])
                    rank = sum(1 for v in pb_values if v <= pb_val)
                    pb_ind_pctile = round((rank / len(pb_values)) * 100.0, 1)
            except Exception as e:
                logger.warning("pb_percentile_error", symbol=symbol, error=str(e))

        # PEG (15% weight)
        peg_score = _NEUTRAL
        if pe_val is not None and pe_val > 0 and np_yoy is not None and np_yoy > 0:
            peg = pe_val / np_yoy
            if peg < 1:
                peg_score = 20.0
            elif peg < 2:
                peg_score = 50.0
            else:
                peg_score = 80.0

        # 加权合成
        score = (
            pe_ind_pctile * 0.40
            + pe_hist_pctile * 0.30
            + pb_ind_pctile * 0.15
            + peg_score * 0.15
        )
        score = _clip(score)

        detail = {
            "score": round(score, 1),
            "pe_ttm": round(pe_val, 2) if pe_val is not None else None,
            "pb": round(pb_val, 2) if pb_val is not None else None,
            "pe_ind_pctile": round(pe_ind_pctile, 1),
            "pe_hist_pctile": round(pe_hist_pctile, 1),
            "pb_ind_pctile": round(pb_ind_pctile, 1),
            "peg_score": round(peg_score, 1),
        }
        return round(score, 1), detail

    def _calc_health_score(
        self, financials: list[dict], is_finance: bool = False,
    ) -> tuple[float, dict]:
        """计算财务健康评分。

        Args:
            financials: 按 report_date 降序的季度财务数据。
            is_finance: 是否为金融行业（不同的负债率标准）。

        Returns:
            (score, detail) 元组。
        """
        if not financials:
            return _NEUTRAL, {"score": _NEUTRAL, "note": "no data"}

        latest = financials[0]
        debt_ratio = latest.get("debt_ratio")
        current_ratio = latest.get("current_ratio")
        goodwill = latest.get("goodwill")
        total_assets = latest.get("total_assets")

        sub_scores: list[float] = []
        sub_weights: list[float] = []

        # 负债率
        debt_val = None
        debt_sub = _NEUTRAL
        if debt_ratio is not None:
            debt_val = float(debt_ratio)
            if is_finance:
                # 金融行业负债率通常很高，使用不同标准
                if debt_val < 80:
                    debt_sub = 90.0
                elif debt_val < 90:
                    debt_sub = 70.0
                elif debt_val < 95:
                    debt_sub = 50.0
                else:
                    debt_sub = 25.0
            else:
                if debt_val < 30:
                    debt_sub = 90.0
                elif debt_val < 50:
                    debt_sub = 70.0
                elif debt_val < 70:
                    debt_sub = 50.0
                else:
                    debt_sub = 25.0
        sub_scores.append(debt_sub)
        sub_weights.append(0.40)

        # 流动比率（金融行业跳过）
        cr_val = None
        cr_sub = _NEUTRAL
        if not is_finance:
            if current_ratio is not None:
                cr_val = float(current_ratio)
                if cr_val > 2:
                    cr_sub = 90.0
                elif cr_val > 1.5:
                    cr_sub = 75.0
                elif cr_val > 1.0:
                    cr_sub = 50.0
                else:
                    cr_sub = 25.0
            sub_scores.append(cr_sub)
            sub_weights.append(0.30)

        # 商誉风险
        gw_ratio = None
        gw_sub = _NEUTRAL
        if goodwill is not None and total_assets is not None:
            gw = float(goodwill)
            ta = float(total_assets)
            if ta > 0:
                gw_ratio = (gw / ta) * 100.0
                if gw_ratio < 5:
                    gw_sub = 90.0
                elif gw_ratio < 15:
                    gw_sub = 60.0
                else:
                    gw_sub = 30.0
        sub_scores.append(gw_sub)
        sub_weights.append(0.30 if not is_finance else 0.60)

        # 加权平均
        total_weight = sum(sub_weights)
        if total_weight > 0:
            score = sum(s * w for s, w in zip(sub_scores, sub_weights)) / total_weight
        else:
            score = _NEUTRAL

        score = _clip(score)

        detail = {
            "score": round(score, 1),
            "debt_ratio": round(debt_val, 1) if debt_val is not None else None,
            "current_ratio": round(cr_val, 2) if cr_val is not None else None,
            "goodwill_ratio": round(gw_ratio, 1) if gw_ratio is not None else None,
        }
        return round(score, 1), detail

    async def analyze(
        self, symbol: str, trade_date: date | None = None,
    ) -> FundamentalAnalysis:
        """Full fundamental analysis.

        Steps:
        1. Look up symbol's industry from industry_classify
        2. Select the matching framework (or default)
        3. Load latest financials_quarterly rows (up to 8 quarters for trend)
        4. Load latest fundamentals_daily row (PE/PB)
        5. Compute 4 sub-scores
        6. Apply industry-weighted composite
        7. Return FundamentalAnalysis with detail dict

        Args:
            symbol: 证券代码。
            trade_date: 分析日期，默认今天。

        Returns:
            FundamentalAnalysis 分析结果。
        """
        if trade_date is None:
            trade_date = date.today()

        logger.info("fundamental_analyze_start", symbol=symbol, trade_date=str(trade_date))

        # 1. 查询行业分类
        sw1_name, sw1_code = await self._get_industry(symbol)

        # 2. 选择评分框架
        framework_name, weights = self._get_framework(sw1_name)
        is_finance = framework_name == "finance"

        # 3. 加载季度财务数据
        financials = await self._load_financials(symbol, quarters=8)

        # 4. 加载估值数据
        valuation = await self._load_valuation(symbol, trade_date)

        # 无数据时返回中性结果
        if not financials and not valuation:
            logger.warning("fundamental_no_data", symbol=symbol)
            return FundamentalAnalysis(
                symbol=symbol,
                profitability_score=_NEUTRAL,
                growth_score=_NEUTRAL,
                valuation_score=_NEUTRAL,
                health_score=_NEUTRAL,
                composite_score=_NEUTRAL,
                industry_framework=framework_name,
                detail={
                    "profitability": {"score": _NEUTRAL, "note": "no data"},
                    "growth": {"score": _NEUTRAL, "note": "no data"},
                    "valuation": {"score": _NEUTRAL, "note": "no data"},
                    "health": {"score": _NEUTRAL, "note": "no data"},
                    "industry": sw1_name,
                    "framework": framework_name,
                },
            )

        # 5. 计算四维子评分
        prof_score, prof_detail = self._calc_profitability_score(financials, is_finance)
        growth_score, growth_detail = self._calc_growth_score(financials)

        # 提取 np_yoy 用于 PEG 计算
        np_yoy = None
        if financials and financials[0].get("np_yoy") is not None:
            np_yoy = float(financials[0]["np_yoy"])

        val_score, val_detail = await self._calc_valuation_score(
            symbol, valuation, sw1_name, trade_date, growth_score, np_yoy,
        )
        health_score, health_detail = self._calc_health_score(financials, is_finance)

        # 6. 行业权重加权合成
        w_prof = weights.get("profitability", 0.30)
        w_grow = weights.get("growth", 0.25)
        w_val = weights.get("valuation", 0.25)
        w_health = weights.get("health", 0.20)

        # 估值分数反转：valuation_score 越低表示越便宜（越好），
        # 在合成时用 (100 - val_score) 使得便宜的股票得到更高的综合分
        composite = (
            prof_score * w_prof
            + growth_score * w_grow
            + (100.0 - val_score) * w_val
            + health_score * w_health
        )
        composite = _clip(round(composite, 2))

        logger.info(
            "fundamental_analyze_done",
            symbol=symbol,
            framework=framework_name,
            profitability=prof_score,
            growth=growth_score,
            valuation=val_score,
            health=health_score,
            composite=composite,
        )

        return FundamentalAnalysis(
            symbol=symbol,
            profitability_score=prof_score,
            growth_score=growth_score,
            valuation_score=val_score,
            health_score=health_score,
            composite_score=composite,
            industry_framework=framework_name,
            detail={
                "profitability": prof_detail,
                "growth": growth_detail,
                "valuation": val_detail,
                "health": health_detail,
                "industry": sw1_name,
                "framework": framework_name,
            },
        )
