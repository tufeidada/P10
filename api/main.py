"""
P10-AlphaRadar FastAPI Backend

Routes:
    GET /api/health          - Data freshness for each source
    GET /api/regime          - CN + US regime status
    GET /api/universe        - All active stocks with latest judgment scores
    GET /api/analysis/{symbol}         - Most recent complete judgment
    GET /api/analysis/{symbol}/history - All judgments for a symbol
    GET /api/bars/{symbol}             - Daily OHLCV + moving averages
    GET /api/signals                   - Intraday signals (by date)
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: init/close DB pool
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    from db.connection import init_pool, close_pool
    await init_pool()
    logger.info("api_startup_complete")
    yield
    await close_pool()
    logger.info("api_shutdown_complete")


# ---------------------------------------------------------------------------
# App + CORS
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AlphaRadar API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _convert_value(v: Any) -> Any:
    """Convert a single value from asyncpg types to JSON-serializable types."""
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (date_cls, datetime)):
        return str(v)
    if isinstance(v, dict):
        return {k: _convert_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_convert_value(item) for item in v]
    return v


def to_json(row: Any) -> dict[str, Any] | None:
    """Recursively convert an asyncpg.Record or dict to a JSON-safe dict.

    Args:
        row: asyncpg.Record, dict, or None.

    Returns:
        JSON-serializable dict, or None if input is None.
    """
    if row is None:
        return None
    # asyncpg.Record supports dict() via mapping protocol
    try:
        items = dict(row).items()
    except TypeError:
        return row
    result: dict[str, Any] = {}
    for k, v in items:
        if v is None:
            result[k] = None
        elif isinstance(v, Decimal):
            result[k] = float(v)
        elif isinstance(v, (date_cls, datetime)):
            result[k] = str(v)
        elif isinstance(v, dict):
            result[k] = _convert_value(v)
        elif isinstance(v, list):
            result[k] = [_convert_value(item) for item in v]
        elif isinstance(v, str):
            # JSONB fields come back as strings from asyncpg — try to parse
            stripped = v.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                try:
                    result[k] = json.loads(v)
                except json.JSONDecodeError:
                    result[k] = v
            else:
                result[k] = v
        else:
            result[k] = v
    return result


def _today_cn() -> date_cls:
    """Return today's date in Asia/Shanghai timezone."""
    # Use UTC+8 offset
    tz_cn = timezone(timedelta(hours=8))
    return datetime.now(tz=tz_cn).date()


def _yesterday() -> date_cls:
    return _today_cn() - timedelta(days=1)


# ---------------------------------------------------------------------------
# Helper: factor contribution decomposition (A1)
# ---------------------------------------------------------------------------

_DIM_TO_SCORE_FIELD = {
    "technical":   "technical_score",
    "fundamental": "fundamental_score",
    "flow":        "flow_score",
    "sentiment":   "sentiment_score",
}

# Fallback weights if neither signal_sources nor regime_at_time carries them
_DEFAULT_WEIGHTS = {
    "technical": 0.30,
    "fundamental": 0.35,
    "flow": 0.20,
    "sentiment": 0.15,
}


def _resolve_weights(row: dict[str, Any]) -> tuple[dict[str, float], str]:
    """Pick the most authoritative weight set available on a judgment row.

    Priority:
      1. signal_sources.effective_weights  (post-2026-05-26 redistribution-aware)
      2. signal_sources.weights            (regime config base weights)
      3. regime_at_time.dimension_weights  (legacy fallback)
      4. _DEFAULT_WEIGHTS

    Returns:
        (weights, source_label) — source_label identifies which path was taken.
    """
    ss = row.get("signal_sources") or {}
    if isinstance(ss, dict):
        eff = ss.get("effective_weights")
        if isinstance(eff, dict) and eff:
            return {k: float(v) for k, v in eff.items()}, "effective_weights"
        base = ss.get("weights")
        if isinstance(base, dict) and base:
            return {k: float(v) for k, v in base.items()}, "signal_sources.weights"
    rat = row.get("regime_at_time") or {}
    if isinstance(rat, dict):
        dw = rat.get("dimension_weights")
        if isinstance(dw, str):
            try:
                dw = json.loads(dw)
            except json.JSONDecodeError:
                dw = None
        if isinstance(dw, dict) and dw:
            return {k: float(v) for k, v in dw.items()}, "regime_at_time"
    return dict(_DEFAULT_WEIGHTS), "fallback_default"


def _compute_factor_contributions(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decompose composite_score into per-dimension contributions.

    Formula:
        composite ≈ baseline_50 + Σ_i (score_i - 50) * weight_i

    Each factor's "contribution" is its delta from neutral (50) times its
    effective weight, so positive contributions push composite > 50 and
    negative contributions pull it below.

    The residual (composite - baseline - Σ contributions) is also returned;
    a non-trivial residual indicates the stored weights don't exactly match
    the formula that produced the composite score (e.g. one of the score
    fields was NULL and replaced with NEUTRAL_SCORE=50 at compute time, or
    has_social redistribution wasn't captured for a legacy judgment).

    Returns:
        Dict with baseline/factors/composite_stored/composite_recomputed/
        residual/weights_source, or None if row is missing required fields.
    """
    if row is None:
        return None
    composite = row.get("composite_score")
    if composite is None:
        return None
    composite = float(composite)

    weights, source = _resolve_weights(row)
    baseline = 50.0

    factors: dict[str, dict[str, float]] = {}
    contribs_sum = 0.0
    for dim, score_field in _DIM_TO_SCORE_FIELD.items():
        raw_score = row.get(score_field)
        # Match composite.py runtime behavior: NULL → 50 (NEUTRAL)
        score = float(raw_score) if raw_score is not None else 50.0
        w = float(weights.get(dim, 0.0))
        contrib = (score - 50.0) * w
        factors[dim] = {
            "score": round(score, 2),
            "weight": round(w, 4),
            "contribution": round(contrib, 3),
            "score_missing": raw_score is None,
        }
        contribs_sum += contrib

    composite_recomputed = baseline + contribs_sum
    residual = composite - composite_recomputed

    return {
        "baseline": baseline,
        "factors": factors,
        "composite_stored": round(composite, 2),
        "composite_recomputed": round(composite_recomputed, 3),
        "residual": round(residual, 3),
        "weights_source": source,
    }


# ---------------------------------------------------------------------------
# Helper: data freshness check
# ---------------------------------------------------------------------------

def _freshness_status(latest: date_cls | None, today: date_cls) -> dict[str, Any]:
    """Return a freshness dict given the latest available date."""
    if latest is None:
        return {"latest_date": None, "status": "warning"}
    days_behind = (today - latest).days
    if days_behind > 5:
        status = "error"
    elif days_behind > 2:
        status = "warning"
    else:
        status = "ok"
    return {"latest_date": str(latest), "status": status}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health() -> JSONResponse:
    """Return data freshness for each core source table.

    Returns:
        JSON with per-source freshness and overall status.
    """
    from db.connection import db_query_val

    today = _today_cn()
    yesterday = _yesterday()

    sources_cfg = [
        ("market_bars_daily", "trade_date"),
        ("features_daily", "trade_date"),
        ("fundamentals_daily", "trade_date"),
        ("northbound_daily", "trade_date"),
        ("macro_indicators", "report_date"),
    ]

    sources: dict[str, Any] = {}
    overall_ok = True

    for table, date_col in sources_cfg:
        try:
            val = await db_query_val(
                f"SELECT MAX({date_col}) FROM {table}"  # noqa: S608 — col/table are literals
            )
            latest: date_cls | None = val if val is None else (val if isinstance(val, date_cls) else date_cls.fromisoformat(str(val)))
            info = _freshness_status(latest, yesterday)  # compare against yesterday as baseline
        except Exception as exc:
            logger.warning("health_check_failed", table=table, error=str(exc))
            info = {"latest_date": None, "status": "error"}

        sources[table] = info
        if info["status"] != "ok":
            overall_ok = False

    return JSONResponse(
        content={
            "status": "ok" if overall_ok else "degraded",
            "sources": sources,
            "last_checked": datetime.now(tz=timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        }
    )


@app.get("/api/regime")
async def regime() -> JSONResponse:
    """Return the latest regime snapshot for CN and US markets.

    Returns:
        JSON with CN and US regime dicts (or null if no data).
    """
    from db.connection import db_query_one

    result: dict[str, Any] = {}
    for market in ("CN", "US"):
        try:
            row = await db_query_one(
                "SELECT * FROM regime_daily WHERE market = $1 ORDER BY trade_date DESC LIMIT 1",
                market,
            )
            result[market.lower()] = to_json(row)
        except Exception as exc:
            logger.error("regime_query_failed", market=market, error=str(exc))
            result[market.lower()] = None

    return JSONResponse(content=result)


@app.get("/api/universe")
async def universe() -> JSONResponse:
    """Return all active stocks with their latest judgment scores and today's signals.

    Returns:
        JSON with a list of stock dicts enriched with scores and latest signal.
    """
    from db.connection import db_query

    today = _today_cn()

    sql = """
        SELECT
            su.symbol,
            su.name,
            su.market,
            su.industry,
            j.composite_score,
            j.technical_score,
            j.fundamental_score,
            j.flow_score,
            j.sentiment_score,
            j.direction,
            j.confidence,
            j.judgment_date,
            bar.latest_close,
            bar.latest_pct_chg,
            bar.latest_bar_date,
            -- latest intraday signal today
            (
                SELECT signal_type
                FROM intraday_signals ins
                WHERE ins.symbol = su.symbol
                  AND ins.signal_time::date = $1
                ORDER BY ins.signal_time DESC
                LIMIT 1
            ) AS latest_signal
        FROM stock_universe su
        LEFT JOIN LATERAL (
            SELECT *
            FROM judgments jj
            WHERE jj.symbol = su.symbol
            ORDER BY jj.judgment_date DESC
            LIMIT 1
        ) j ON true
        LEFT JOIN LATERAL (
            SELECT close AS latest_close, pct_chg AS latest_pct_chg, trade_date AS latest_bar_date
            FROM market_bars_daily mbd
            WHERE mbd.symbol = su.symbol
            ORDER BY mbd.trade_date DESC
            LIMIT 1
        ) bar ON true
        WHERE su.active = TRUE
        ORDER BY j.composite_score DESC NULLS LAST, su.symbol
    """

    try:
        rows = await db_query(sql, today)
        stocks = [to_json(r) for r in rows]
    except Exception as exc:
        logger.error("universe_query_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to fetch universe") from exc

    return JSONResponse(content={"stocks": stocks})


@app.get("/api/analysis/{symbol}")
async def analysis_latest(symbol: str) -> JSONResponse:
    """Return the most recent complete judgment for a symbol.

    Args:
        symbol: Stock symbol, e.g. 000001.SZ.

    Returns:
        JSON judgment dict or 404.
    """
    from db.connection import db_query_one

    sql = """
        SELECT
            id, symbol, judgment_date, timeframe,
            technical_score, fundamental_score, flow_score, sentiment_score,
            composite_score, direction, confidence,
            logic_text, suggested_action,
            entry_zone_low, entry_zone_high, stop_loss, target_price,
            signal_sources, regime_at_time,
            rule_signal_strength, llm_direction, llm_signal_strength,
            llm_reasoning, llm_risks, llm_extra_advice,
            llm_vote_consensus, llm_vote_total_calls
        FROM judgments
        WHERE symbol = $1
        ORDER BY judgment_date DESC
        LIMIT 1
    """

    try:
        row = await db_query_one(sql, symbol.upper())
    except Exception as exc:
        logger.error("analysis_query_failed", symbol=symbol, error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    if row is None:
        raise HTTPException(status_code=404, detail=f"No judgment found for {symbol}")

    content = to_json(row)
    if content:
        content["factor_contributions"] = _compute_factor_contributions(content)
    return JSONResponse(content=content)


@app.get("/api/analysis/{symbol}/factor_contribution")
async def analysis_factor_contribution(symbol: str) -> JSONResponse:
    """Standalone factor-contribution breakdown for the latest judgment.

    Lighter-weight than /api/analysis/{symbol} — only returns the decomposition
    fields, useful for the frontend's waterfall chart that doesn't need the
    full judgment payload.
    """
    from db.connection import db_query_one

    row = await db_query_one(
        """
        SELECT
            symbol, market, judgment_date,
            technical_score, fundamental_score, flow_score, sentiment_score,
            composite_score, direction, confidence,
            signal_sources, regime_at_time, rule_signal_strength
        FROM judgments
        WHERE symbol = $1
        ORDER BY judgment_date DESC
        LIMIT 1
        """,
        symbol.upper(),
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"No judgment found for {symbol}")

    content = to_json(row) or {}
    decomp = _compute_factor_contributions(content)
    return JSONResponse(content={
        "symbol": content.get("symbol"),
        "market": content.get("market"),
        "judgment_date": content.get("judgment_date"),
        "composite_score": content.get("composite_score"),
        "direction": content.get("direction"),
        "confidence": content.get("confidence"),
        "rule_signal_strength": content.get("rule_signal_strength"),
        "decomposition": decomp,
    })


@app.get("/api/analysis/{symbol}/history")
async def analysis_history(symbol: str) -> JSONResponse:
    """Return all judgments for a symbol, ordered newest first (max 20).

    Args:
        symbol: Stock symbol.

    Returns:
        JSON with list of lightweight judgment summaries.
    """
    from db.connection import db_query

    sql = """
        SELECT
            id, judgment_date, direction, confidence,
            composite_score, technical_score, fundamental_score,
            flow_score, sentiment_score,
            actual_ret_1d, actual_ret_5d, actual_ret_10d, actual_ret_20d,
            actual_max_up_20d, actual_max_dd_20d,
            is_correct, error_category, rule_signal_strength
        FROM judgments
        WHERE symbol = $1
        ORDER BY judgment_date DESC
        LIMIT 20
    """

    try:
        rows = await db_query(sql, symbol.upper())
    except Exception as exc:
        logger.error("analysis_history_failed", symbol=symbol, error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    return JSONResponse(content={"judgments": [to_json(r) for r in rows]})


@app.get("/api/bars/{symbol}")
async def bars(symbol: str) -> JSONResponse:
    """Return last 250 daily OHLCV bars and computed moving averages.

    Args:
        symbol: Stock symbol.

    Returns:
        JSON with bars list and moving_averages dict (ma5/20/60/150).
    """
    from db.connection import db_query

    sql = """
        SELECT trade_date, open, high, low, close, volume
        FROM market_bars_daily
        WHERE symbol = $1
        ORDER BY trade_date DESC
        LIMIT 250
    """

    try:
        rows = await db_query(sql, symbol.upper())
    except Exception as exc:
        logger.error("bars_query_failed", symbol=symbol, error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    if not rows:
        raise HTTPException(status_code=404, detail=f"No bar data found for {symbol}")

    # Rows come back newest-first; reverse to chronological order for MA calc
    rows_asc = list(reversed(rows))

    bar_list: list[dict[str, Any]] = []
    closes: list[float] = []

    for r in rows_asc:
        bar_list.append(
            {
                "time": str(r["trade_date"]),
                "open": float(r["open"]) if r["open"] is not None else None,
                "high": float(r["high"]) if r["high"] is not None else None,
                "low": float(r["low"]) if r["low"] is not None else None,
                "close": float(r["close"]) if r["close"] is not None else None,
                "volume": int(r["volume"]) if r["volume"] is not None else None,
            }
        )
        closes.append(float(r["close"]) if r["close"] is not None else float("nan"))

    closes_arr = np.array(closes, dtype=float)

    def rolling_mean(arr: np.ndarray, window: int) -> list[float | None]:
        """Compute rolling mean with None-padding for initial periods."""
        result: list[float | None] = []
        for i in range(len(arr)):
            if i < window - 1:
                result.append(None)
            else:
                chunk = arr[i - window + 1 : i + 1]
                if np.isnan(chunk).any():
                    result.append(None)
                else:
                    result.append(float(np.mean(chunk)))
        return result

    moving_averages = {
        "ma5": rolling_mean(closes_arr, 5),
        "ma20": rolling_mean(closes_arr, 20),
        "ma60": rolling_mean(closes_arr, 60),
        "ma150": rolling_mean(closes_arr, 150),
    }

    return JSONResponse(
        content={
            "symbol": symbol.upper(),
            "bars": bar_list,
            "moving_averages": moving_averages,
        }
    )


@app.get("/api/signals")
async def signals(
    date: str | None = Query(default=None, description="Date string YYYY-MM-DD or 'today'"),
) -> JSONResponse:
    """Return intraday signals for a given date (default: today).

    Args:
        date: Target date as YYYY-MM-DD string or 'today'.

    Returns:
        JSON with signal list and a buy/sell summary.
    """
    from db.connection import db_query

    # Resolve target date
    today = _today_cn()
    if date is None or date == "today":
        target_date = today
    else:
        try:
            target_date = date_cls.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=f"Invalid date format: {date!r}. Use YYYY-MM-DD."
            ) from exc

    sql = """
        SELECT
            id, symbol, signal_type, strength, trigger_rule,
            price_at_signal, signal_time, basis_judgment_id
        FROM intraday_signals
        WHERE signal_time::date = $1
        ORDER BY signal_time DESC
    """

    try:
        rows = await db_query(sql, target_date)
    except Exception as exc:
        logger.error("signals_query_failed", date=str(target_date), error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    signal_list = [to_json(r) for r in rows]

    # Build summary counts
    summary: dict[str, int] = {}
    for s in signal_list:
        stype = s.get("signal_type") or "unknown"
        summary[stype] = summary.get(stype, 0) + 1

    return JSONResponse(content={"signals": signal_list, "summary": summary})


# ---------------------------------------------------------------------------
# Alias routes (frontend uses different URL conventions)
# ---------------------------------------------------------------------------

@app.get("/api/candidates")
async def get_candidates(limit: int = Query(100)):
    """Alias for /api/universe — used by the frontend."""
    from db.connection import db_query
    sql = """
        SELECT
            su.symbol, su.name, su.market, su.industry,
            j.composite_score, j.technical_score, j.fundamental_score,
            j.flow_score, j.sentiment_score, j.direction, j.confidence,
            j.judgment_date::text AS judgment_date,
            j.rule_signal_strength, j.llm_direction, j.llm_signal_strength,
            bar.latest_close,
            bar.latest_pct_chg,
            bar.latest_bar_date::text AS latest_bar_date,
            (
                SELECT json_build_object(
                    'signal_type', si.signal_type,
                    'strength', si.strength,
                    'signal_time', si.signal_time::text
                )
                FROM intraday_signals si
                WHERE si.symbol = su.symbol
                  AND si.signal_time::date = CURRENT_DATE
                  AND si.strength IN ('strong','moderate')
                ORDER BY si.signal_time DESC LIMIT 1
            ) AS latest_signal
        FROM stock_universe su
        LEFT JOIN LATERAL (
            SELECT composite_score, technical_score, fundamental_score,
                   flow_score, sentiment_score, direction, confidence, judgment_date,
                   rule_signal_strength, llm_direction, llm_signal_strength
            FROM judgments j2
            WHERE j2.symbol = su.symbol
            ORDER BY j2.judgment_date DESC, j2.created_at DESC
            LIMIT 1
        ) j ON TRUE
        LEFT JOIN LATERAL (
            -- market_bars_daily has no pct_chg column; compute via LAG over last 2 bars.
            SELECT close AS latest_close,
                   CASE WHEN prev_close IS NOT NULL AND prev_close <> 0
                        THEN ROUND(((close - prev_close) / prev_close * 100.0)::numeric, 2)
                        ELSE NULL END AS latest_pct_chg,
                   trade_date AS latest_bar_date
            FROM (
                SELECT close, trade_date,
                       LAG(close) OVER (ORDER BY trade_date) AS prev_close
                FROM market_bars_daily
                WHERE symbol = su.symbol
                ORDER BY trade_date DESC
                LIMIT 2
            ) recent_bars
            ORDER BY trade_date DESC
            LIMIT 1
        ) bar ON TRUE
        WHERE su.active = TRUE
        ORDER BY j.composite_score DESC NULLS LAST
        LIMIT $1
    """
    rows = await db_query(sql, limit)
    stocks = [to_json(r) for r in rows]
    return JSONResponse(content={"stocks": stocks, "total": len(stocks)})


@app.get("/api/regime/latest")
async def get_regime_latest():
    """Alias for /api/regime — used by the frontend."""
    return await regime()


@app.get("/api/signals/today")
async def get_signals_today():
    """Alias for /api/signals?date=today — used by the frontend."""
    return await signals(date="today")


# ---------------------------------------------------------------------------
# Phase 6 frontend routes
# ---------------------------------------------------------------------------

@app.get("/api/review/weekly/latest")
async def review_weekly_latest() -> JSONResponse:
    """Return the most recent weekly review report.

    Returns:
        JSON review report dict or 404.
    """
    from db.connection import db_query_one

    try:
        row = await db_query_one(
            "SELECT * FROM review_reports WHERE report_type='weekly' ORDER BY created_at DESC LIMIT 1"
        )
    except Exception as exc:
        logger.error("review_weekly_query_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    if row is None:
        raise HTTPException(status_code=404, detail="No weekly review report found")

    return JSONResponse(content=to_json(row))


@app.get("/api/review/monthly/latest")
async def review_monthly_latest() -> JSONResponse:
    """Return the most recent monthly review report.

    Returns:
        JSON review report dict or 404.
    """
    from db.connection import db_query_one

    try:
        row = await db_query_one(
            "SELECT * FROM review_reports WHERE report_type='monthly' ORDER BY created_at DESC LIMIT 1"
        )
    except Exception as exc:
        logger.error("review_monthly_query_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    if row is None:
        raise HTTPException(status_code=404, detail="No monthly review report found")

    return JSONResponse(content=to_json(row))


@app.get("/api/performance")
async def performance() -> JSONResponse:
    """Return system-wide judgment performance statistics.

    Returns:
        JSON with accuracy rates, alpha vs benchmark, best/worst rules.
    """
    from db.connection import db_query_one, db_query_val, db_query

    try:
        # Overall judgment counts
        counts_row = await db_query_one(
            """
            SELECT
                COUNT(*)                                    AS total_judgments,
                COUNT(*) FILTER (WHERE is_correct IS NOT NULL) AS verified,
                COUNT(*) FILTER (WHERE is_correct = TRUE)  AS correct
            FROM judgments
            """
        )
        total = int(counts_row["total_judgments"]) if counts_row else 0
        verified = int(counts_row["verified"]) if counts_row else 0
        correct = int(counts_row["correct"]) if counts_row else 0
        total_accuracy = round(correct / verified, 4) if verified > 0 else None

        # Short / mid horizon accuracy from latest review_reports
        acc_row = await db_query_one(
            "SELECT accuracy_short, accuracy_mid FROM review_reports ORDER BY created_at DESC LIMIT 1"
        )
        short_accuracy = float(acc_row["accuracy_short"]) if acc_row and acc_row["accuracy_short"] is not None else None
        mid_accuracy = float(acc_row["accuracy_mid"]) if acc_row and acc_row["accuracy_mid"] is not None else None

        # Latest alpha vs benchmark from review_reports
        alpha_row = await db_query_one(
            "SELECT alpha_vs_benchmark FROM review_reports ORDER BY created_at DESC LIMIT 1"
        )
        alpha = float(alpha_row["alpha_vs_benchmark"]) if alpha_row and alpha_row["alpha_vs_benchmark"] is not None else None

        # HS300 benchmark entries
        bench_rows = await db_query(
            """
            SELECT benchmark_name AS name, cumulative_return, trade_date
            FROM benchmark_daily
            WHERE benchmark_name ILIKE '%hs300%'
            ORDER BY trade_date DESC
            LIMIT 30
            """
        )
        benchmarks = [to_json(r) for r in bench_rows]

        # Best rule
        best_row = await db_query_one(
            """
            SELECT rule_name, accuracy, total_signals AS total
            FROM signal_quality_tracker
            ORDER BY accuracy DESC
            LIMIT 1
            """
        )

        # Worst rule (require at least a few samples to avoid noise)
        worst_row = await db_query_one(
            """
            SELECT rule_name, accuracy, total_signals AS total
            FROM signal_quality_tracker
            WHERE total_signals >= 5
            ORDER BY accuracy ASC
            LIMIT 1
            """
        )

    except Exception as exc:
        logger.error("performance_query_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    return JSONResponse(
        content={
            "total_judgments": total,
            "verified": verified,
            "correct": correct,
            "total_accuracy": total_accuracy,
            "short_accuracy": short_accuracy,
            "mid_accuracy": mid_accuracy,
            "alpha_vs_benchmark": alpha,
            "benchmarks": benchmarks,
            "best_rule": to_json(best_row),
            "worst_rule": to_json(worst_row),
        }
    )


@app.get("/api/quality")
async def quality(limit: int = Query(default=20, ge=1, le=200)) -> JSONResponse:
    """Return signal quality rankings ordered by accuracy descending.

    Args:
        limit: Maximum number of rules to return (default 20).

    Returns:
        JSON with list of rule quality dicts.
    """
    from db.connection import db_query

    sql = """
        SELECT
            rule_name, market, regime_mode,
            total_signals, correct_signals, accuracy,
            avg_return, ic_value, ir_value, period_end
        FROM signal_quality_tracker
        ORDER BY accuracy DESC
        LIMIT $1
    """

    try:
        rows = await db_query(sql, limit)
    except Exception as exc:
        logger.error("quality_query_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    return JSONResponse(content={"rules": [to_json(r) for r in rows]})


@app.get("/api/wiki/{page_path:path}")
async def wiki_page(page_path: str) -> JSONResponse:
    """Return the content of a wiki page from the filesystem.

    Args:
        page_path: Relative path within the wiki/ directory, e.g. 'strategies/behavioral_traps.md'.

    Returns:
        JSON with page content and metadata. exists=false if not found.
    """
    # Project root is two levels up from api/main.py
    project_root = Path(__file__).parent.parent
    wiki_root = project_root / "wiki"
    # Resolve target path and ensure it stays inside wiki_root (path traversal guard)
    try:
        target = (wiki_root / page_path).resolve()
        wiki_root_resolved = wiki_root.resolve()
        target.relative_to(wiki_root_resolved)  # raises ValueError if outside
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid page path")

    if not target.exists() or not target.is_file():
        return JSONResponse(
            content={
                "page_path": page_path,
                "content": None,
                "exists": False,
                "last_modified": None,
            }
        )

    try:
        content = target.read_text(encoding="utf-8")
        last_modified = datetime.fromtimestamp(
            target.stat().st_mtime, tz=timezone(timedelta(hours=8))
        ).isoformat(timespec="seconds")
    except Exception as exc:
        logger.error("wiki_read_failed", page_path=page_path, error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to read wiki page") from exc

    return JSONResponse(
        content={
            "page_path": page_path,
            "content": content,
            "exists": True,
            "last_modified": last_modified,
        }
    )


@app.get("/api/data-quality")
async def data_quality() -> JSONResponse:
    """Return data freshness for all sources, including row counts per table.

    Returns:
        JSON with per-source freshness, row counts, and overall status.
    """
    from db.connection import db_query_val

    today = _today_cn()
    yesterday = _yesterday()

    sources_cfg = [
        ("market_bars_daily", "trade_date"),
        ("features_daily", "trade_date"),
        ("fundamentals_daily", "trade_date"),
        ("northbound_daily", "trade_date"),
        ("macro_indicators", "report_date"),
    ]

    sources: dict[str, Any] = {}
    overall_ok = True

    for table, date_col in sources_cfg:
        try:
            val = await db_query_val(
                f"SELECT MAX({date_col}) FROM {table}"  # noqa: S608
            )
            latest: date_cls | None = (
                val if val is None
                else (val if isinstance(val, date_cls) else date_cls.fromisoformat(str(val)))
            )
            info = _freshness_status(latest, yesterday)
        except Exception as exc:
            logger.warning("data_quality_freshness_failed", table=table, error=str(exc))
            info = {"latest_date": None, "status": "error"}

        # Row count
        try:
            row_count = await db_query_val(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            info["row_count"] = int(row_count) if row_count is not None else 0
        except Exception as exc:
            logger.warning("data_quality_count_failed", table=table, error=str(exc))
            info["row_count"] = None

        sources[table] = info
        if info["status"] != "ok":
            overall_ok = False

    return JSONResponse(
        content={
            "status": "ok" if overall_ok else "degraded",
            "sources": sources,
            "last_checked": datetime.now(tz=timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        }
    )


@app.get("/api/experience")
async def experience(
    status: str | None = Query(default=None, description="Filter by status, e.g. 'active'"),
    market: str | None = Query(default=None, description="Filter by market, e.g. 'CN' or 'US'"),
) -> JSONResponse:
    """Return experience store entries, optionally filtered by status and market.

    Args:
        status: Optional status filter (e.g. 'active', 'deprecated').
        market: Optional market filter (e.g. 'CN', 'US').

    Returns:
        JSON with list of experience entries ordered newest first.
    """
    from db.connection import db_query

    # Build query dynamically based on provided filters
    conditions: list[str] = []
    params: list[Any] = []

    if status is not None:
        params.append(status)
        conditions.append(f"status = ${len(params)}")

    if market is not None:
        params.append(market.upper())
        conditions.append(f"market = ${len(params)}")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = f"""
        SELECT
            id, category, market, status,
            content_text, discovery_date,
            applied_count, last_validated
        FROM experience_store
        {where_clause}
        ORDER BY created_at DESC
    """  # noqa: S608 — table/conditions use only parameterized values

    try:
        rows = await db_query(sql, *params)
    except Exception as exc:
        logger.error("experience_query_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    return JSONResponse(content={"experiences": [to_json(r) for r in rows]})


# ---------------------------------------------------------------------------
# Stats summary (for StatusBar)
# ---------------------------------------------------------------------------

@app.get("/api/stats/summary")
async def stats_summary() -> JSONResponse:
    """Return high-level performance stats for the status bar.

    Returns:
        JSON with total_judgments, accuracy_pct (or null), cost_today_cny.
    """
    from db.connection import db_query_val

    try:
        total = await db_query_val(
            "SELECT COUNT(*) FROM judgments WHERE fundamental_bug_affected IS NOT TRUE"
        )
        verified = await db_query_val(
            "SELECT COUNT(*) FROM judgments WHERE is_correct IS NOT NULL"
        )
        correct = await db_query_val(
            "SELECT COUNT(*) FROM judgments WHERE is_correct = TRUE"
        )
        cost_today = await db_query_val(
            "SELECT COALESCE(SUM(cost_cny),0) FROM llm_cost_log WHERE DATE(call_time)=CURRENT_DATE"
        )
    except Exception as exc:
        logger.error("stats_summary_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    verified_n = int(verified or 0)
    correct_n = int(correct or 0)
    accuracy_pct = round(correct_n / verified_n * 100, 1) if verified_n > 0 else None

    return JSONResponse(content={
        "total_judgments": int(total or 0),
        "verified": verified_n,
        "accuracy_pct": accuracy_pct,
        "alpha_pct": None,
        "cost_today_cny": float(cost_today or 0),
    })


# ---------------------------------------------------------------------------
# Quality tracking (M5.4)
# ---------------------------------------------------------------------------

@app.get("/api/quality-tracking")
async def quality_tracking() -> JSONResponse:
    """Return quality tracking data: rule accuracy, LLM accuracy, divergence stats, alpha.

    Returns:
        JSON with rule_accuracy, llm_accuracy, divergence, alpha sections.
    """
    from db.connection import db_query, db_query_one

    try:
        # Rule accuracy by direction
        rule_rows = await db_query(
            """
            SELECT
                direction,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE is_correct = TRUE) AS correct,
                COUNT(*) FILTER (WHERE is_correct IS NOT NULL) AS evaluated,
                AVG(actual_ret_10d) AS avg_ret_10d
            FROM judgments
            WHERE direction IS NOT NULL
            GROUP BY direction
            ORDER BY direction
            """
        )

        # LLM accuracy by llm_direction
        llm_rows = await db_query(
            """
            SELECT
                llm_direction,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE is_correct = TRUE) AS correct,
                COUNT(*) FILTER (WHERE is_correct IS NOT NULL) AS evaluated,
                AVG(actual_ret_10d) AS avg_ret_10d,
                AVG(llm_vote_consensus) FILTER (WHERE llm_vote_consensus IS NOT NULL) AS avg_vote_consensus
            FROM judgments
            WHERE llm_direction IS NOT NULL AND llm_direction != 'unknown'
            GROUP BY llm_direction
            ORDER BY llm_direction
            """
        )

        # Divergence stats: rule vs LLM
        divergence = await db_query_one(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE llm_direction = 'bullish'
                      AND direction IN ('neutral', 'bearish')
                ) AS llm_more_aggressive,
                COUNT(*) FILTER (
                    WHERE direction = 'bullish'
                      AND llm_direction IN ('neutral', 'bearish')
                ) AS llm_more_conservative,
                COUNT(*) FILTER (
                    WHERE direction = llm_direction
                ) AS fully_aligned
            FROM judgments
            WHERE llm_direction IS NOT NULL AND llm_direction NOT IN ('unknown', '')
              AND direction IS NOT NULL
            """
        )

        # Daily divergence trend (last 30 days)
        divergence_trend = await db_query(
            """
            SELECT
                judgment_date::text AS date,
                COUNT(*) AS total,
                COUNT(*) FILTER (
                    WHERE llm_direction = 'bullish' AND direction IN ('neutral','bearish')
                ) AS llm_aggressive,
                COUNT(*) FILTER (
                    WHERE direction = 'bullish' AND llm_direction IN ('neutral','bearish')
                ) AS llm_conservative,
                COUNT(*) FILTER (WHERE direction = llm_direction) AS aligned
            FROM judgments
            WHERE llm_direction IS NOT NULL AND llm_direction NOT IN ('unknown','')
              AND judgment_date >= CURRENT_DATE - INTERVAL '30 days'
            GROUP BY judgment_date
            ORDER BY judgment_date DESC
            LIMIT 30
            """
        )

        # Alpha placeholder (composite score > 65 vs benchmark)
        alpha = await db_query_one(
            """
            SELECT
                COUNT(*) FILTER (WHERE composite_score >= 65) AS high_conviction_count,
                AVG(actual_ret_10d) FILTER (WHERE composite_score >= 65) AS high_conviction_avg_ret,
                AVG(actual_ret_10d) AS overall_avg_ret,
                COUNT(*) FILTER (WHERE actual_ret_10d IS NOT NULL) AS evaluated
            FROM judgments
            """
        )

    except Exception as exc:
        logger.error("quality_tracking_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    def _accuracy(row: dict) -> float | None:
        evaluated = int(row.get("evaluated") or 0)
        correct = int(row.get("correct") or 0)
        return round(correct / evaluated, 3) if evaluated > 0 else None

    div = dict(divergence) if divergence else {}
    total_div = int(div.get("total") or 0)

    return JSONResponse(content={
        "rule_accuracy": [
            {
                "direction": r["direction"],
                "total": int(r["total"]),
                "evaluated": int(r["evaluated"] or 0),
                "accuracy": _accuracy(dict(r)),
                "avg_ret_10d": float(r["avg_ret_10d"]) if r["avg_ret_10d"] is not None else None,
            }
            for r in rule_rows
        ],
        "llm_accuracy": [
            {
                "direction": r["llm_direction"],
                "total": int(r["total"]),
                "evaluated": int(r["evaluated"] or 0),
                "accuracy": _accuracy(dict(r)),
                "avg_ret_10d": float(r["avg_ret_10d"]) if r["avg_ret_10d"] is not None else None,
                "avg_vote_consensus": float(r["avg_vote_consensus"]) if r["avg_vote_consensus"] is not None else None,
            }
            for r in llm_rows
        ],
        "divergence": {
            "total": total_div,
            "llm_more_aggressive": int(div.get("llm_more_aggressive") or 0),
            "llm_more_conservative": int(div.get("llm_more_conservative") or 0),
            "fully_aligned": int(div.get("fully_aligned") or 0),
            "llm_aggressive_ratio": round(int(div.get("llm_more_aggressive") or 0) / total_div, 3) if total_div > 0 else None,
            "llm_conservative_ratio": round(int(div.get("llm_more_conservative") or 0) / total_div, 3) if total_div > 0 else None,
            "aligned_ratio": round(int(div.get("fully_aligned") or 0) / total_div, 3) if total_div > 0 else None,
        },
        "divergence_trend": [to_json(r) for r in divergence_trend],
        "alpha": {
            "high_conviction_count": int(alpha["high_conviction_count"] or 0) if alpha else 0,
            "high_conviction_avg_ret": float(alpha["high_conviction_avg_ret"]) if alpha and alpha["high_conviction_avg_ret"] is not None else None,
            "overall_avg_ret": float(alpha["overall_avg_ret"]) if alpha and alpha["overall_avg_ret"] is not None else None,
            "evaluated": int(alpha["evaluated"] or 0) if alpha else 0,
        },
    })


# ---------------------------------------------------------------------------
# Scheduler status (for HealthPage + Block 4)
# ---------------------------------------------------------------------------

@app.get("/api/scheduler/status")
async def scheduler_status() -> JSONResponse:
    """Return scheduler health: heartbeat, 24h job stats, LLM cost.

    Returns:
        JSON with heartbeat, jobs list, llm_cost sections.
    """
    from db.connection import db_query, db_query_one, db_query_val

    try:
        hb = await db_query_one(
            "SELECT beat_time, pid, jobs_count FROM scheduler_heartbeat ORDER BY beat_time DESC LIMIT 1"
        )
        job_rows = await db_query(
            """
            SELECT job_name,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status='success') AS success,
                   COUNT(*) FILTER (WHERE status='failed') AS failed,
                   COUNT(*) FILTER (WHERE status='skipped') AS skipped,
                   MAX(trigger_time) AS last_run
            FROM scheduler_job_log
            WHERE trigger_time > NOW() - INTERVAL '24 hours'
            GROUP BY job_name
            ORDER BY job_name
            """
        )
        cost_today = await db_query_val(
            "SELECT COALESCE(SUM(cost_cny),0) FROM llm_cost_log WHERE DATE(call_time)=CURRENT_DATE"
        )
        cost_total = await db_query_val(
            "SELECT COALESCE(SUM(cost_cny),0) FROM llm_cost_log"
        )
        cost_7d_avg = await db_query_val(
            """
            SELECT COALESCE(SUM(cost_cny), 0) / GREATEST(COUNT(DISTINCT DATE(call_time)), 1)
            FROM llm_cost_log
            WHERE call_time >= NOW() - INTERVAL '7 days'
            """
        )
        last_composite = await db_query_one(
            "SELECT job_name, status, trigger_time FROM scheduler_job_log "
            "WHERE job_name LIKE 'run_composite%' ORDER BY trigger_time DESC LIMIT 1"
        )
        llm_quality = await db_query_one(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE signal_sources->>'llm_direction' = 'unknown') AS unknown_count
            FROM judgments
            WHERE judgment_date = CURRENT_DATE
            """
        )
    except Exception as exc:
        logger.error("scheduler_status_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Database error") from exc

    now_utc = datetime.now(tz=timezone.utc)
    heartbeat: dict[str, Any] = {}
    if hb and hb["beat_time"]:
        hb_time = hb["beat_time"]
        hb_utc = hb_time if hb_time.tzinfo else hb_time.replace(tzinfo=timezone.utc)
        lag_min = int((now_utc - hb_utc).total_seconds() / 60)
        heartbeat = {
            "beat_time": str(hb_utc),
            "lag_min": lag_min,
            "healthy": lag_min < 35,
            "pid": hb["pid"],
            "jobs_count": hb["jobs_count"],
        }
    else:
        heartbeat = {"beat_time": None, "lag_min": None, "healthy": False, "pid": None, "jobs_count": 0}

    return JSONResponse(content={
        "heartbeat": heartbeat,
        "jobs": [to_json(r) for r in job_rows],
        "last_composite": to_json(last_composite),
        "llm_cost": {
            "today_cny": float(cost_today or 0),
            "total_cny": float(cost_total or 0),
            "daily_avg_7d_cny": float(cost_7d_avg or 0),
            "monthly_est_cny": float(cost_7d_avg or 0) * 30,
            "budget_cny": 100.0,
        },
        "llm_quality": {
            "today_total": int(llm_quality["total"]) if llm_quality else 0,
            "unknown_count": int(llm_quality["unknown_count"]) if llm_quality else 0,
            "unknown_ratio": (
                round(int(llm_quality["unknown_count"]) / int(llm_quality["total"]), 3)
                if llm_quality and int(llm_quality["total"]) > 0 else None
            ),
        },
    })


# ---------------------------------------------------------------------------
# Mount frontend static files (must be LAST — catches all unmatched routes)
# ---------------------------------------------------------------------------

_dist = Path(__file__).parent.parent / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
    logger.info("static_files_mounted", path=str(_dist))
