"""
周度/月度复盘引擎。

每周六 10:00 自动运行；每月 1 日同样运行月度复盘（report_type='monthly'）。

依赖：
  - db.connection        — 数据库查询
  - llm.client           — LLMClient（DeepSeek 主力）
  - llm.wiki_manager     — WikiManager.add_experience()
  - bot.telegram_bot     — TelegramPusher.send_html()
"""

from __future__ import annotations

import json
import re
from calendar import monthrange
from datetime import date, timedelta
from typing import Any

import structlog

from db.connection import db_execute, db_query, db_query_one, db_query_val

logger = structlog.get_logger(__name__)

# 用于从 LLM 输出中识别"规律"标注的正则
_RULE_PATTERN = re.compile(
    r"【规律】(.+?)(?=【|\n\n|\Z)",
    re.DOTALL,
)

# 用于宽泛抽取含关键词句子的正则（备用策略）
_INSIGHT_KEYWORDS = re.compile(
    r"(规律|发现|建议|准确率|信号|当.{1,10}时)"
)


class Reviewer:
    """周度复盘引擎。

    每周六 10:00 由调度器调用 run_weekly_review()。
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_weekly_review(
        self,
        market: str = "CN",
        week_end_date: date | None = None,
    ) -> int:
        """生成周度复盘报告并写入数据库。

        Args:
            market: 市场代码，'CN' 或 'US'。
            week_end_date: 本周最后一个交易日（默认上一个周五）。

        Returns:
            新建的 review_reports 记录 ID。
        """
        if week_end_date is None:
            # 找到上一个周五（或今天如果今天是周六）
            today = date.today()
            # weekday(): Monday=0 … Saturday=5, Sunday=6
            days_since_friday = (today.weekday() - 4) % 7
            week_end_date = today - timedelta(days=days_since_friday)

        week_start_date = week_end_date - timedelta(days=7)

        logger.info(
            "weekly_review_start",
            market=market,
            week_start=str(week_start_date),
            week_end=str(week_end_date),
        )

        # 1. 汇总判断统计
        stats = await self._gather_judgment_stats(
            market, week_start_date, week_end_date
        )

        # 2. 信号质量数据
        signal_stats = await self._gather_signal_quality(
            market, week_start_date, week_end_date
        )

        # 3. 计算 alpha
        alpha = await self._compute_alpha(
            market, week_start_date, week_end_date, stats
        )

        # 4. 构建 Prompt 并调用 LLM
        prompt_msgs = self._build_review_prompt(
            stats, signal_stats, alpha, market,
            week_start_date, week_end_date,
        )
        llm_analysis = ""
        try:
            from llm.client import LLMClient
            llm = LLMClient()
            if llm.is_configured():
                llm_analysis = await llm.chat(
                    prompt_msgs, model="deepseek", max_tokens=1500
                )
        except Exception as e:
            logger.warning("weekly_review_llm_error", error=str(e))

        if not llm_analysis:
            # 降级：纯定量摘要
            acc = stats.get("accuracy", 0.0)
            total = stats.get("total", 0)
            llm_analysis = (
                f"本周共 {total} 条判断，可验证准确率 {acc:.0%}，"
                f"Alpha vs 基准 {alpha:+.1%}。LLM 分析不可用。"
            )

        # 5. 从 LLM 输出中抽取规律并保存
        exp_count = await self._extract_and_save_experiences(
            llm_analysis, market, week_end_date
        )
        logger.info("weekly_review_experiences_saved", count=exp_count)

        # 6. 构建完整 Markdown 报告
        full_report_md = self._build_full_report_md(
            stats, signal_stats, llm_analysis, alpha,
            week_start_date, week_end_date, market,
        )

        # 7. 提取关键发现和建议（简单解析）
        key_findings = self._extract_key_findings(llm_analysis)
        suggested_changes = self._extract_suggested_changes(llm_analysis)

        # 8. 写入 review_reports 表
        short_acc = stats.get("accuracy_short")
        mid_acc = stats.get("accuracy_mid")

        report_id: int = await db_query_val(
            """
            INSERT INTO review_reports (
                report_type, report_date, market,
                total_judgments, accuracy_short, accuracy_mid,
                alpha_vs_benchmark,
                summary_text, key_findings, suggested_changes,
                full_report_md
            ) VALUES (
                'weekly', $1, $2,
                $3, $4, $5,
                $6,
                $7, $8::jsonb, $9::jsonb,
                $10
            )
            RETURNING id
            """,
            week_end_date,
            market,
            stats.get("total", 0),
            short_acc,
            mid_acc,
            alpha,
            llm_analysis[:500],
            json.dumps(key_findings, ensure_ascii=False),
            json.dumps(suggested_changes, ensure_ascii=False),
            full_report_md,
        )

        logger.info(
            "weekly_review_saved",
            report_id=report_id,
            market=market,
            week_end=str(week_end_date),
        )

        # 9. 推送 Telegram 摘要
        await self._push_weekly_summary(
            stats, llm_analysis, alpha,
            week_start_date, week_end_date, report_id,
        )

        return report_id

    # ------------------------------------------------------------------
    # LLM Prompt construction
    # ------------------------------------------------------------------

    def _build_review_prompt(
        self,
        stats: dict[str, Any],
        signal_stats: list[dict[str, Any]],
        alpha: float,
        market: str,
        week_start: date,
        week_end: date,
    ) -> list[dict[str, str]]:
        """构建给 LLM 的复盘 Prompt。

        Args:
            stats: 来自 _gather_judgment_stats 的统计字典。
            signal_stats: 来自 _gather_signal_quality 的信号质量列表。
            alpha: 相对基准超额收益（小数）。
            market: 市场代码。
            week_start: 本周开始日期。
            week_end: 本周结束日期。

        Returns:
            OpenAI-format messages 列表。
        """
        total = stats.get("total", 0)
        verified = stats.get("verified", 0)
        correct = stats.get("correct", 0)
        accuracy = stats.get("accuracy", 0.0)
        short_acc = stats.get("accuracy_short", 0.0) or 0.0
        mid_acc = stats.get("accuracy_mid", 0.0) or 0.0

        # 错误分类汇总
        error_breakdown_lines: list[str] = []
        for cat, cnt in stats.get("error_categories", {}).items():
            error_breakdown_lines.append(f"  - {cat}: {cnt} 次")
        error_breakdown = (
            "\n".join(error_breakdown_lines) if error_breakdown_lines else "  暂无错误分类数据"
        )

        # 信号质量汇总
        sig_lines: list[str] = []
        for s in signal_stats[:5]:  # 只展示前 5 条
            rule = s.get("rule_name", "unknown")
            acc_val = s.get("accuracy", 0.0) or 0.0
            vol = s.get("signal_count", 0)
            sig_lines.append(f"  - {rule}: 准确率 {acc_val:.0%}，触发 {vol} 次")
        signal_quality_summary = (
            "\n".join(sig_lines) if sig_lines else "  暂无信号质量数据"
        )

        user_content = (
            f"你是一位投资系统的复盘教练。以下是本周的判断记录和实际结果。\n\n"
            f"## 本周统计 ({week_start} ~ {week_end})\n"
            f"判断总数: {total} | 可验证: {verified} | 准确: {correct} ({accuracy:.0%})\n"
            f"短期准确率: {short_acc:.0%} | 中期准确率: {mid_acc:.0%}\n"
            f"Alpha vs {'沪深300' if market == 'CN' else 'S&P500'}: {alpha:+.1%}\n\n"
            f"## 错误分析\n{error_breakdown}\n\n"
            f"## 信号质量\n{signal_quality_summary}\n\n"
            f"请分析：\n"
            f"1. 哪些判断做对了，核心原因是什么\n"
            f"2. 哪些判断做错了，是哪个维度出了问题（技术/基本面/资金/情绪）\n"
            f"3. 有没有发现值得记录的新规律（用\u201c\u3010规律\u3011\u201d标注）\n"
            f"4. 对系统参数有什么调整建议\n"
            f"用 300-500 字完成，语言直接，不要客套。"
        )

        return [{"role": "user", "content": user_content}]

    # ------------------------------------------------------------------
    # Report building
    # ------------------------------------------------------------------

    def _build_full_report_md(
        self,
        stats: dict[str, Any],
        signal_stats: list[dict[str, Any]],
        llm_analysis: str,
        alpha: float,
        week_start: date,
        week_end: date,
        market: str,
    ) -> str:
        """生成完整 Markdown 格式复盘报告（存入数据库）。

        Args:
            stats: 判断统计字典。
            signal_stats: 信号质量列表。
            llm_analysis: LLM 分析文本。
            alpha: 超额收益。
            week_start: 周开始日期。
            week_end: 周结束日期。
            market: 市场代码。

        Returns:
            Markdown 字符串。
        """
        total = stats.get("total", 0)
        verified = stats.get("verified", 0)
        correct = stats.get("correct", 0)
        accuracy = stats.get("accuracy", 0.0)
        short_acc = stats.get("accuracy_short", 0.0) or 0.0
        mid_acc = stats.get("accuracy_mid", 0.0) or 0.0

        # 信号质量表格
        sig_table_rows = ""
        for s in signal_stats[:10]:
            rule = s.get("rule_name", "unknown")
            acc_val = s.get("accuracy", 0.0) or 0.0
            vol = s.get("signal_count", 0)
            sig_table_rows += f"| {rule} | {acc_val:.0%} | {vol} |\n"

        sig_table = ""
        if sig_table_rows:
            sig_table = (
                "| 规则 | 准确率 | 触发次数 |\n"
                "|------|--------|----------|\n"
                + sig_table_rows
            )
        else:
            sig_table = "_暂无信号质量数据_"

        # 错误分类
        error_lines = ""
        for cat, cnt in stats.get("error_categories", {}).items():
            error_lines += f"- {cat}: {cnt} 次\n"
        if not error_lines:
            error_lines = "_暂无错误分类数据_\n"

        benchmark_name = "沪深300" if market == "CN" else "S&P500"

        report = (
            f"# 周度复盘报告 — {market}\n\n"
            f"**周期**: {week_start} ~ {week_end}\n\n"
            f"---\n\n"
            f"## 一、核心指标\n\n"
            f"| 指标 | 数值 |\n"
            f"|------|------|\n"
            f"| 判断总数 | {total} |\n"
            f"| 可验证 | {verified} |\n"
            f"| 准确 | {correct} ({accuracy:.0%}) |\n"
            f"| 短期准确率 | {short_acc:.0%} |\n"
            f"| 中期准确率 | {mid_acc:.0%} |\n"
            f"| Alpha vs {benchmark_name} | {alpha:+.1%} |\n\n"
            f"---\n\n"
            f"## 二、错误分析\n\n"
            f"{error_lines}\n"
            f"---\n\n"
            f"## 三、信号质量\n\n"
            f"{sig_table}\n\n"
            f"---\n\n"
            f"## 四、LLM 复盘分析\n\n"
            f"{llm_analysis}\n\n"
            f"---\n\n"
            f"_生成时间: {date.today()}_\n"
        )
        return report

    # ------------------------------------------------------------------
    # Experience extraction
    # ------------------------------------------------------------------

    async def _extract_and_save_experiences(
        self,
        llm_text: str,
        market: str,
        week_end: date,
    ) -> int:
        """从 LLM 输出中提取【规律】标注并保存到 experience_store。

        查找 「【规律】...」直到下一个【、空行或文本末尾的片段。
        每条长度超过 30 字符的规律调用 WikiManager.add_experience() 保存。

        Args:
            llm_text: LLM 生成的复盘文本。
            market: 市场代码。
            week_end: 周结束日期（作为 evidence 元数据）。

        Returns:
            成功保存的条数。
        """
        saved = 0
        matches = _RULE_PATTERN.findall(llm_text)

        for raw in matches:
            content = raw.strip()
            if len(content) < 30:
                continue

            evidence = {
                "week_end": str(week_end),
                "source": "weekly_review",
            }

            try:
                from llm.wiki_manager import WikiManager
                wm = WikiManager()
                exp_id = await wm.add_experience(
                    content_text=content,
                    category="market_pattern",
                    market=market,
                    evidence=evidence,
                    status="under_review",
                )
                saved += 1
                logger.info(
                    "experience_saved",
                    exp_id=exp_id,
                    market=market,
                    preview=content[:60],
                )

                # Telegram 通知（失败不阻塞）
                try:
                    from bot.telegram_bot import TelegramPusher
                    pusher = TelegramPusher()
                    msg = (
                        f"💡 <b>新规律入库</b> (待审)\n"
                        f"{content[:120]}{'...' if len(content) > 120 else ''}\n"
                        f"<i>来源: {market} 周复盘 {week_end}</i>"
                    )
                    await pusher.send_html(msg)
                except Exception as e:
                    logger.debug("experience_telegram_error", error=str(e))

            except Exception as e:
                logger.warning(
                    "experience_save_error",
                    error=str(e),
                    preview=content[:60],
                )

        return saved

    # ------------------------------------------------------------------
    # Telegram push
    # ------------------------------------------------------------------

    async def _push_weekly_summary(
        self,
        stats: dict[str, Any],
        llm_analysis: str,
        alpha: float,
        week_start: date,
        week_end: date,
        report_id: int,
    ) -> None:
        """推送精简周复盘摘要到 Telegram。

        Args:
            stats: 判断统计字典。
            llm_analysis: LLM 分析文本。
            alpha: 超额收益。
            week_start: 周开始日期。
            week_end: 周结束日期。
            report_id: 已入库的报告 ID。
        """
        total = stats.get("total", 0)
        accuracy = stats.get("accuracy", 0.0)

        # 取 LLM 分析前 150 字符（去除 Markdown 标记后）
        clean_text = re.sub(r"[*#_`\[\]]", "", llm_analysis).strip()
        excerpt = clean_text[:150] + ("..." if len(clean_text) > 150 else "")

        msg = (
            f"📊 <b>周度复盘</b> {week_start} ~ {week_end}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"判断: {total} | 准确率: {accuracy:.0%} | "
            f"Alpha: {alpha:+.1%}\n"
            f"{excerpt}\n"
            f"/review 查看完整报告"
        )

        try:
            from bot.telegram_bot import TelegramPusher
            pusher = TelegramPusher()
            await pusher.send_html(msg)
        except Exception as e:
            logger.warning("weekly_summary_push_error", error=str(e))

    # ------------------------------------------------------------------
    # Data gathering helpers
    # ------------------------------------------------------------------

    async def _gather_judgment_stats(
        self,
        market: str,
        week_start: date,
        week_end: date,
    ) -> dict[str, Any]:
        """查询本周判断记录并汇总统计数据。

        Args:
            market: 市场代码。
            week_start: 周开始日期（含）。
            week_end: 周结束日期（含）。

        Returns:
            统计字典，包含 total, verified, correct, accuracy,
            accuracy_short, accuracy_mid, error_categories。
        """
        stats: dict[str, Any] = {
            "total": 0,
            "verified": 0,
            "correct": 0,
            "accuracy": 0.0,
            "accuracy_short": None,
            "accuracy_mid": None,
            "error_categories": {},
        }

        try:
            # 总数
            total_row = await db_query_one(
                """
                SELECT COUNT(*) AS cnt
                FROM judgments
                WHERE market = $1
                  AND judgment_date BETWEEN $2 AND $3
                """,
                market, week_start, week_end,
            )
            stats["total"] = int(total_row["cnt"]) if total_row else 0

            # 可验证 + 准确率（整体）
            acc_row = await db_query_one(
                """
                SELECT
                    COUNT(*) FILTER (WHERE is_correct IS NOT NULL) AS verified,
                    COUNT(*) FILTER (WHERE is_correct = TRUE)      AS correct
                FROM judgments
                WHERE market = $1
                  AND judgment_date BETWEEN $2 AND $3
                """,
                market, week_start, week_end,
            )
            verified = int(acc_row["verified"]) if acc_row else 0
            correct = int(acc_row["correct"]) if acc_row else 0
            stats["verified"] = verified
            stats["correct"] = correct
            stats["accuracy"] = round(correct / verified, 4) if verified > 0 else 0.0

            # 按 timeframe 分组准确率
            tf_rows = await db_query(
                """
                SELECT
                    timeframe,
                    COUNT(*) FILTER (WHERE is_correct IS NOT NULL) AS verified,
                    COUNT(*) FILTER (WHERE is_correct = TRUE)      AS correct
                FROM judgments
                WHERE market = $1
                  AND judgment_date BETWEEN $2 AND $3
                  AND is_correct IS NOT NULL
                GROUP BY timeframe
                """,
                market, week_start, week_end,
            )
            for row in tf_rows:
                v = int(row["verified"]) if row["verified"] else 0
                c = int(row["correct"]) if row["correct"] else 0
                acc_val = round(c / v, 4) if v > 0 else 0.0
                if row["timeframe"] == "short":
                    stats["accuracy_short"] = acc_val
                elif row["timeframe"] == "mid":
                    stats["accuracy_mid"] = acc_val

            # 错误分类（来自 error_category 字段，如存在）
            err_rows = await db_query(
                """
                SELECT error_category, COUNT(*) AS cnt
                FROM judgments
                WHERE market = $1
                  AND judgment_date BETWEEN $2 AND $3
                  AND is_correct = FALSE
                  AND error_category IS NOT NULL
                GROUP BY error_category
                ORDER BY cnt DESC
                """,
                market, week_start, week_end,
            )
            stats["error_categories"] = {
                row["error_category"]: int(row["cnt"]) for row in err_rows
            }

        except Exception as e:
            logger.warning("gather_judgment_stats_error", market=market, error=str(e))

        return stats

    async def _gather_signal_quality(
        self,
        market: str,
        week_start: date,
        week_end: date,
    ) -> list[dict[str, Any]]:
        """查询 signal_quality_tracker 表获取信号质量统计。

        Args:
            market: 市场代码。
            week_start: 周开始日期（含）。
            week_end: 周结束日期（含）。

        Returns:
            信号质量列表，每条包含 rule_name, accuracy, signal_count。
        """
        try:
            rows = await db_query(
                """
                SELECT rule_name,
                       AVG(accuracy)      AS accuracy,
                       SUM(total_signals) AS signal_count
                FROM signal_quality_tracker
                WHERE market = $1
                  AND period_end BETWEEN $2 AND $3
                GROUP BY rule_name
                ORDER BY accuracy DESC NULLS LAST
                LIMIT 20
                """,
                market, week_start, week_end,
            )
            return [
                {
                    "rule_name": row["rule_name"],
                    "accuracy": float(row["accuracy"]) if row["accuracy"] is not None else None,
                    "signal_count": int(row["signal_count"]) if row["signal_count"] else 0,
                }
                for row in rows
            ]
        except Exception as e:
            logger.warning("gather_signal_quality_error", market=market, error=str(e))
            return []

    async def _compute_alpha(
        self,
        market: str,
        week_start: date,
        week_end: date,
        stats: dict[str, Any],
    ) -> float:
        """计算本周相对基准的超额收益。

        "组合收益" = 本周可验证判断 actual_ret_5d 的均值（多头判断取正，空头取负）。
        "基准收益" = benchmark_daily 中 hs300/sp500 本周累计收益差值。

        Args:
            market: 市场代码。
            week_start: 周开始日期。
            week_end: 周结束日期。
            stats: 判断统计（当前未用，保留以备扩展）。

        Returns:
            超额收益（小数，如 0.02 = +2%）。
        """
        benchmark_name = "buy_hold_hs300" if market == "CN" else "buy_hold_sp500"

        # 基准本周累计收益变化
        bench_alpha = 0.0
        try:
            bench_rows = await db_query(
                """
                SELECT trade_date, cumulative_return
                FROM benchmark_daily
                WHERE market = $1
                  AND benchmark_name = $2
                  AND trade_date BETWEEN $3 AND $4
                ORDER BY trade_date
                """,
                market, benchmark_name, week_start, week_end,
            )
            if len(bench_rows) >= 2:
                start_cum = float(bench_rows[0]["cumulative_return"] or 0)
                end_cum = float(bench_rows[-1]["cumulative_return"] or 0)
                # 本周基准收益 ≈ 累计收益的变化
                bench_alpha = round(
                    (1 + end_cum) / (1 + start_cum) - 1, 6
                ) if (1 + start_cum) != 0 else 0.0
            elif len(bench_rows) == 1:
                bench_alpha = float(bench_rows[0]["cumulative_return"] or 0)
        except Exception as e:
            logger.warning("compute_alpha_bench_error", error=str(e))

        # 组合本周收益（已验证判断的 actual_ret_5d 均值）
        portfolio_return = 0.0
        try:
            port_row = await db_query_one(
                """
                SELECT AVG(
                    CASE
                        WHEN direction = 'bullish' THEN actual_ret_5d
                        WHEN direction = 'bearish' THEN -actual_ret_5d
                        ELSE 0
                    END
                ) AS avg_ret
                FROM judgments
                WHERE market = $1
                  AND judgment_date BETWEEN $2 AND $3
                  AND actual_ret_5d IS NOT NULL
                  AND is_correct IS NOT NULL
                """,
                market, week_start, week_end,
            )
            if port_row and port_row["avg_ret"] is not None:
                portfolio_return = round(float(port_row["avg_ret"]), 6)
        except Exception as e:
            logger.warning("compute_alpha_portfolio_error", error=str(e))

        alpha = round(portfolio_return - bench_alpha, 6)
        logger.debug(
            "alpha_computed",
            market=market,
            portfolio=portfolio_return,
            benchmark=bench_alpha,
            alpha=alpha,
        )
        return alpha

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_key_findings(llm_text: str) -> list[str]:
        """从 LLM 输出中提取关键发现列表（简单按行分割）。

        Args:
            llm_text: LLM 生成的复盘文本。

        Returns:
            关键发现字符串列表（最多 10 条）。
        """
        findings: list[str] = []
        for line in llm_text.splitlines():
            line = line.strip().lstrip("•-*123456789. ")
            if len(line) > 20 and any(
                kw in line for kw in ("做对", "做错", "发现", "规律", "准确")
            ):
                findings.append(line[:200])
            if len(findings) >= 10:
                break
        return findings

    @staticmethod
    def _extract_suggested_changes(llm_text: str) -> list[str]:
        """从 LLM 输出中提取调整建议列表。

        Args:
            llm_text: LLM 生成的复盘文本。

        Returns:
            调整建议字符串列表（最多 10 条）。
        """
        suggestions: list[str] = []
        in_suggestions_section = False
        for line in llm_text.splitlines():
            stripped = line.strip()
            if "建议" in stripped and len(stripped) < 20:
                in_suggestions_section = True
                continue
            if in_suggestions_section:
                clean = stripped.lstrip("•-*123456789. ")
                if len(clean) > 10:
                    suggestions.append(clean[:200])
                elif not clean:
                    in_suggestions_section = False
            if len(suggestions) >= 10:
                break
        return suggestions

    # ------------------------------------------------------------------
    # Monthly review
    # ------------------------------------------------------------------

    async def run_monthly_review(
        self,
        market: str = "CN",
        month_end_date: date | None = None,
    ) -> int:
        """Generate monthly review report (report_type='monthly').

        Triggered every 1st of month. Covers the full previous calendar month.

        Args:
            market: Market code, 'CN' or 'US'.
            month_end_date: Last day of the month being reviewed. Defaults to
                the last day of the previous calendar month.

        Returns:
            Newly created review_reports record ID.
        """
        if month_end_date is None:
            today = date.today()
            # Last day of previous month
            first_of_this_month = today.replace(day=1)
            month_end_date = first_of_this_month - timedelta(days=1)

        # First day of that same month
        month_start_date = month_end_date.replace(day=1)
        month_str = month_end_date.strftime("%Y-%m")

        logger.info(
            "monthly_review_start",
            market=market,
            month_start=str(month_start_date),
            month_end=str(month_end_date),
        )

        # 1. 汇总判断统计（30 天窗口，复用 _gather_judgment_stats）
        stats = await self._gather_judgment_stats(
            market, month_start_date, month_end_date
        )

        # 2. 各维度 IC（60 天，跨越月份以获得足够样本）
        ic_data = await self.compute_dimension_ics(market, days=60)

        # 3. 建议权重
        weight_data = self.compute_suggested_weights(ic_data)

        # 4. 信号质量趋势（当月）
        signal_trends = await self._gather_signal_quality(
            market, month_start_date, month_end_date
        )

        # 5. 体制切换及 wiki 使用情况（丰富 context，失败不阻塞）
        regime_timeliness: dict[str, Any] = {}
        try:
            regime_timeliness = await self._check_regime_switching_timeliness(
                market, month_start_date, month_end_date
            )
        except Exception as e:
            logger.warning("monthly_review_regime_timeliness_error", error=str(e))

        wiki_usage: dict[str, Any] = {}
        try:
            wiki_usage = await self._check_wiki_experience_usage(market)
        except Exception as e:
            logger.warning("monthly_review_wiki_usage_error", error=str(e))

        # 6. Alpha（整月）
        alpha = await self._compute_alpha(
            market, month_start_date, month_end_date, stats
        )

        # 7. 构建月度 LLM Prompt 并调用
        prompt_msgs = self._build_monthly_prompt(
            stats, ic_data, weight_data, signal_trends, month_str
        )
        llm_analysis = ""
        try:
            from llm.client import LLMClient
            llm = LLMClient()
            if llm.is_configured():
                llm_analysis = await llm.chat(
                    prompt_msgs, model="deepseek", max_tokens=2000
                )
        except Exception as e:
            logger.warning("monthly_review_llm_error", error=str(e))

        if not llm_analysis:
            acc = stats.get("accuracy", 0.0)
            total = stats.get("total", 0)
            llm_analysis = (
                f"{month_str} 月度复盘：共 {total} 条判断，准确率 {acc:.0%}，"
                f"Alpha {alpha:+.1%}。LLM 分析不可用。"
            )

        # 8. 从 LLM 输出提取并保存规律
        exp_count = await self._extract_and_save_experiences(
            llm_analysis, market, month_end_date
        )
        logger.info("monthly_review_experiences_saved", count=exp_count)

        # 9. 构建完整 Markdown 报告
        full_report_md = self._build_full_monthly_report_md(
            stats, ic_data, weight_data, signal_trends, llm_analysis,
            alpha, month_start_date, month_end_date, market,
            regime_timeliness, wiki_usage,
        )

        # 10. 关键发现 / 建议（含权重建议写入 key_findings）
        key_findings = self._extract_key_findings(llm_analysis)
        suggested_changes = self._extract_suggested_changes(llm_analysis)

        # 把 IC 和建议权重一并存入 key_findings
        findings_payload: dict[str, Any] = {
            "texts": key_findings,
            "dimension_ics": ic_data,
            "suggested_weights": weight_data,
            "regime_timeliness": regime_timeliness,
            "wiki_usage": wiki_usage,
        }

        # 11. 写入 review_reports
        short_acc = stats.get("accuracy_short")
        mid_acc = stats.get("accuracy_mid")

        report_id: int = await db_query_val(
            """
            INSERT INTO review_reports (
                report_type, report_date, market,
                total_judgments, accuracy_short, accuracy_mid,
                alpha_vs_benchmark,
                summary_text, key_findings, suggested_changes,
                full_report_md
            ) VALUES (
                'monthly', $1, $2,
                $3, $4, $5,
                $6,
                $7, $8::jsonb, $9::jsonb,
                $10
            )
            RETURNING id
            """,
            month_end_date,
            market,
            stats.get("total", 0),
            short_acc,
            mid_acc,
            alpha,
            llm_analysis[:500],
            json.dumps(findings_payload, ensure_ascii=False),
            json.dumps(suggested_changes, ensure_ascii=False),
            full_report_md,
        )

        logger.info(
            "monthly_review_saved",
            report_id=report_id,
            market=market,
            month=month_str,
        )

        # 12. Telegram 推送
        await self._push_monthly_summary(
            stats, llm_analysis, alpha, ic_data, weight_data,
            month_str, report_id,
        )

        return report_id

    # ------------------------------------------------------------------
    # IC computation
    # ------------------------------------------------------------------

    async def compute_dimension_ics(
        self,
        market: str = "CN",
        days: int = 60,
    ) -> dict[str, float]:
        """Compute IC (rank correlation) for each analysis dimension.

        Queries judgments with actual_ret_10d IS NOT NULL in the last ``days``
        days and computes Spearman rank correlation between each score dimension
        and the realised 10-day return.

        Args:
            market: Market code, 'CN' or 'US'.
            days: Look-back window in calendar days.

        Returns:
            Dictionary mapping dimension name to IC value, e.g.
            ``{"technical": 0.35, "fundamental": 0.28, "flow": 0.15, "sentiment": 0.12}``.
            Returns 0.0 for any dimension with fewer than 5 valid pairs.
        """
        cutoff = date.today() - timedelta(days=days)
        ics: dict[str, float] = {
            "technical": 0.0,
            "fundamental": 0.0,
            "flow": 0.0,
            "sentiment": 0.0,
        }

        try:
            rows = await db_query(
                """
                SELECT
                    technical_score,
                    fundamental_score,
                    flow_score,
                    sentiment_score,
                    actual_ret_10d
                FROM judgments
                WHERE market = $1
                  AND judgment_date >= $2
                  AND actual_ret_10d IS NOT NULL
                """,
                market, cutoff,
            )
        except Exception as e:
            logger.warning("compute_dimension_ics_query_error", market=market, error=str(e))
            return ics

        if not rows:
            return ics

        # Collect arrays per dimension, filtering NULLs pair-wise
        dim_scores: dict[str, list[float]] = {
            "technical": [],
            "fundamental": [],
            "flow": [],
            "sentiment": [],
        }
        dim_returns: dict[str, list[float]] = {
            "technical": [],
            "fundamental": [],
            "flow": [],
            "sentiment": [],
        }
        col_map = {
            "technical": "technical_score",
            "fundamental": "fundamental_score",
            "flow": "flow_score",
            "sentiment": "sentiment_score",
        }

        for row in rows:
            ret_val = row["actual_ret_10d"]
            if ret_val is None:
                continue
            ret_f = float(ret_val)
            for dim, col in col_map.items():
                score_val = row[col]
                if score_val is not None:
                    dim_scores[dim].append(float(score_val))
                    dim_returns[dim].append(ret_f)

        # Compute Spearman IC for each dimension
        try:
            from scipy.stats import spearmanr
        except ImportError:
            logger.warning(
                "compute_dimension_ics_scipy_missing",
                note="scipy not installed; falling back to numpy rank correlation",
            )
            spearmanr = None  # type: ignore[assignment]

        for dim in ("technical", "fundamental", "flow", "sentiment"):
            scores = dim_scores[dim]
            rets = dim_returns[dim]
            n = len(scores)
            if n < 5:
                logger.debug(
                    "compute_dimension_ics_insufficient_data",
                    dim=dim,
                    n=n,
                    market=market,
                )
                continue

            try:
                if spearmanr is not None:
                    corr, _ = spearmanr(scores, rets)
                    ics[dim] = round(float(corr) if corr == corr else 0.0, 4)  # NaN guard
                else:
                    # Numpy fallback: rank then Pearson
                    import numpy as np
                    s_arr = np.array(scores, dtype=float)
                    r_arr = np.array(rets, dtype=float)

                    def _rank(a: "np.ndarray") -> "np.ndarray":  # type: ignore[name-defined]
                        tmp = a.argsort()
                        ranks = np.empty_like(tmp, dtype=float)
                        ranks[tmp] = np.arange(len(a), dtype=float)
                        return ranks

                    s_rank = _rank(s_arr)
                    r_rank = _rank(r_arr)
                    corr_mat = np.corrcoef(s_rank, r_rank)
                    ics[dim] = round(float(corr_mat[0, 1]), 4)
            except Exception as e:
                logger.warning(
                    "compute_dimension_ics_corr_error",
                    dim=dim,
                    error=str(e),
                )

        logger.info(
            "compute_dimension_ics_done",
            market=market,
            days=days,
            ics=ics,
        )
        return ics

    # ------------------------------------------------------------------
    # Suggested weights
    # ------------------------------------------------------------------

    def compute_suggested_weights(
        self,
        ics: dict[str, float],
    ) -> dict[str, float]:
        """Compute suggested dimension weights from IC values.

        Formula:
        - raw_i  = max(ic_i, 0.01)          # floor at 0.01 so no dimension is zero
        - total  = sum(raw)
        - w_i    = 0.10 + 0.60 * (raw_i / total)   # 10% floor + 60% IC-proportional

        The four weights always sum to exactly 1.00. Rounding adjustments are
        applied to the largest-weight dimension to absorb floating-point drift.

        Args:
            ics: Dictionary of dimension IC values
                (may be negative; only the floor matters).

        Returns:
            Dictionary mapping dimension name to suggested weight, summing to 1.00.
            E.g. ``{"technical": 0.32, "fundamental": 0.28, "flow": 0.22, "sentiment": 0.18}``.
        """
        dims = ["technical", "fundamental", "flow", "sentiment"]
        raw = {d: max(ics.get(d, 0.0), 0.01) for d in dims}
        total_raw = sum(raw.values())

        weights: dict[str, float] = {}
        for d in dims:
            weights[d] = round(0.10 + 0.60 * (raw[d] / total_raw), 2)

        # Ensure exact sum == 1.00 by adjusting the dimension with the largest weight
        weight_sum = round(sum(weights.values()), 2)
        diff = round(1.00 - weight_sum, 2)
        if diff != 0.0:
            largest_dim = max(weights, key=lambda d: weights[d])
            weights[largest_dim] = round(weights[largest_dim] + diff, 2)

        logger.debug("compute_suggested_weights", ics=ics, weights=weights)
        return weights

    # ------------------------------------------------------------------
    # Experience validation
    # ------------------------------------------------------------------

    async def validate_experiences(self, market: str = "CN") -> dict[str, int]:
        """Validate active experience entries against recent data.

        Iterates over all experience_store entries with status='active' for the
        given market. Each entry is checked for staleness and, where possible,
        validated against signal_quality_tracker or regime_daily data.

        Rules applied:
        - Skip entries validated within the last 30 days.
        - If created > 90 days ago and not validated in > 60 days: mark evidence
          as ``stale`` (status stays 'active').
        - Deprecate **only** when signal_quality_tracker shows > 10 signals with
          accuracy < 30% for a rule clearly referenced in the experience text.

        Args:
            market: Market code, 'CN' or 'US'.

        Returns:
            Summary counts: ``{"checked": N, "updated": M, "deprecated": K, "stale": J}``.
        """
        result: dict[str, int] = {
            "checked": 0,
            "updated": 0,
            "deprecated": 0,
            "stale": 0,
        }
        today = date.today()

        try:
            experiences = await db_query(
                """
                SELECT id, content_text, evidence, discovery_date, last_validated
                FROM experience_store
                WHERE status = 'active'
                  AND (market = $1 OR market IS NULL)
                ORDER BY last_validated ASC NULLS FIRST
                """,
                market,
            )
        except Exception as e:
            logger.warning("validate_experiences_query_error", market=market, error=str(e))
            return result

        for exp in experiences:
            exp_id: int = exp["id"]
            content: str = exp["content_text"] or ""
            raw_evidence = exp["evidence"]
            evidence: dict[str, Any] = {}
            if raw_evidence is not None:
                if isinstance(raw_evidence, str):
                    try:
                        evidence = json.loads(raw_evidence)
                    except json.JSONDecodeError:
                        evidence = {}
                elif isinstance(raw_evidence, dict):
                    evidence = raw_evidence

            discovery_date_raw = exp["discovery_date"]
            last_validated_raw = exp["last_validated"]

            discovery_dt: date | None = (
                discovery_date_raw
                if isinstance(discovery_date_raw, date)
                else None
            )
            last_validated_dt: date | None = (
                last_validated_raw
                if isinstance(last_validated_raw, date)
                else None
            )

            result["checked"] += 1

            # Skip if validated very recently
            if last_validated_dt is not None:
                days_since_validation = (today - last_validated_dt).days
                if days_since_validation < 30:
                    continue

            # --- Staleness check ---
            is_stale = False
            if discovery_dt is not None:
                age_days = (today - discovery_dt).days
                days_since_val = (
                    (today - last_validated_dt).days
                    if last_validated_dt is not None
                    else age_days
                )
                if age_days > 90 and days_since_val > 60:
                    is_stale = True

            # --- MACD / 金叉 signal quality check ---
            deprecated = False
            if re.search(r"MACD|金叉|死叉", content, re.IGNORECASE):
                try:
                    sqt_rows = await db_query(
                        """
                        SELECT SUM(total_signals) AS total,
                               SUM(correct_signals) AS correct
                        FROM signal_quality_tracker
                        WHERE market = $1
                          AND (rule_name ILIKE '%macd%'
                               OR rule_name ILIKE '%golden%'
                               OR rule_name ILIKE '%death%'
                               OR rule_name ILIKE '%金叉%'
                               OR rule_name ILIKE '%死叉%')
                          AND period_end >= $2
                        """,
                        market,
                        today - timedelta(days=90),
                    )
                    if sqt_rows:
                        total_sigs = int(sqt_rows[0]["total"] or 0)
                        correct_sigs = int(sqt_rows[0]["correct"] or 0)
                        if total_sigs > 10:
                            acc = correct_sigs / total_sigs
                            if acc < 0.30:
                                deprecated = True
                except Exception as e:
                    logger.debug(
                        "validate_experiences_macd_check_error",
                        exp_id=exp_id,
                        error=str(e),
                    )

            # --- Regime / 市场 check ---
            if not deprecated and re.search(r"Regime|市场|趋势", content):
                try:
                    regime_rows = await db_query(
                        """
                        SELECT COUNT(*) AS cnt
                        FROM regime_daily
                        WHERE market = $1
                          AND trade_date >= $2
                        """,
                        market,
                        today - timedelta(days=30),
                    )
                    # We only mark stale if there's truly no regime data for 30 days
                    if regime_rows and int(regime_rows[0]["cnt"] or 0) == 0:
                        is_stale = True
                except Exception as e:
                    logger.debug(
                        "validate_experiences_regime_check_error",
                        exp_id=exp_id,
                        error=str(e),
                    )

            # --- Persist results ---
            try:
                if deprecated:
                    await db_execute(
                        """
                        UPDATE experience_store
                        SET status = 'deprecated',
                            last_validated = $2,
                            evidence = evidence || $3::jsonb
                        WHERE id = $1
                        """,
                        exp_id,
                        today,
                        json.dumps(
                            {"deprecated_reason": "accuracy < 30% on >10 signals",
                             "deprecated_date": str(today)},
                            ensure_ascii=False,
                        ),
                    )
                    result["deprecated"] += 1
                    result["updated"] += 1

                    # Telegram alert for deprecation
                    try:
                        from bot.telegram_bot import TelegramPusher
                        pusher = TelegramPusher()
                        await pusher.send_html(
                            f"⚠️ <b>经验条目已弃用</b>\n"
                            f"ID {exp_id}: {content[:100]}"
                            f"{'...' if len(content) > 100 else ''}\n"
                            f"<i>原因: MACD/信号准确率 &lt;30%（&gt;10 信号）</i>"
                        )
                    except Exception as te:
                        logger.debug("validate_experiences_telegram_error", error=str(te))

                elif is_stale:
                    # Mark stale in evidence, keep status='active'
                    updated_evidence = {
                        **evidence,
                        "stale": True,
                        "stale_marked_date": str(today),
                    }
                    await db_execute(
                        """
                        UPDATE experience_store
                        SET last_validated = $2,
                            evidence = $3::jsonb
                        WHERE id = $1
                        """,
                        exp_id,
                        today,
                        json.dumps(updated_evidence, ensure_ascii=False),
                    )
                    result["stale"] += 1
                    result["updated"] += 1

                else:
                    # Just refresh last_validated
                    await db_execute(
                        """
                        UPDATE experience_store
                        SET last_validated = $2
                        WHERE id = $1
                        """,
                        exp_id,
                        today,
                    )
                    result["updated"] += 1

            except Exception as e:
                logger.warning(
                    "validate_experiences_update_error",
                    exp_id=exp_id,
                    error=str(e),
                )

        logger.info(
            "validate_experiences_done",
            market=market,
            **result,
        )
        return result

    # ------------------------------------------------------------------
    # Monthly prompt
    # ------------------------------------------------------------------

    def _build_monthly_prompt(
        self,
        stats: dict[str, Any],
        ic_data: dict[str, float],
        weight_data: dict[str, float],
        signal_trends: list[dict[str, Any]],
        month_str: str,
    ) -> list[dict[str, str]]:
        """Build monthly review LLM prompt.

        Args:
            stats: Judgment statistics from _gather_judgment_stats.
            ic_data: Dimension IC values from compute_dimension_ics.
            weight_data: Suggested weights from compute_suggested_weights.
            signal_trends: Signal quality list from _gather_signal_quality.
            month_str: Month label in 'YYYY-MM' format.

        Returns:
            OpenAI-format messages list.
        """
        total = stats.get("total", 0)
        verified = stats.get("verified", 0)
        correct = stats.get("correct", 0)
        accuracy = stats.get("accuracy", 0.0)

        tech_ic = ic_data.get("technical", 0.0)
        fund_ic = ic_data.get("fundamental", 0.0)
        flow_ic = ic_data.get("flow", 0.0)
        sent_ic = ic_data.get("sentiment", 0.0)

        sug_tech = weight_data.get("technical", 0.25)
        sug_fund = weight_data.get("fundamental", 0.25)
        sug_flow = weight_data.get("flow", 0.25)
        sug_sent = weight_data.get("sentiment", 0.25)

        # Current weights: read from YAML if possible, else use defaults
        cur_tech = cur_fund = cur_flow = cur_sent = 0.25
        try:
            import yaml, os
            yaml_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "regime_params.yaml"
            )
            yaml_path = os.path.normpath(yaml_path)
            with open(yaml_path, "r", encoding="utf-8") as fh:
                regime_cfg = yaml.safe_load(fh)
            # Use 'offense' regime as the "current default" reference
            w = regime_cfg.get("regimes", {}).get("offense", {}).get("weights", {})
            cur_tech = w.get("technical", 0.35)
            cur_fund = w.get("fundamental", 0.30)
            cur_flow = w.get("flow", 0.20)
            cur_sent = w.get("sentiment", 0.15)
        except Exception:
            pass  # Fall back to default 0.25

        # Signal quality summary (top 5 rules)
        sig_lines: list[str] = []
        for s in signal_trends[:5]:
            rule = s.get("rule_name", "unknown")
            acc_val = s.get("accuracy") or 0.0
            vol = s.get("signal_count", 0)
            sig_lines.append(f"  - {rule}: 准确率 {acc_val:.0%}，触发 {vol} 次")
        signal_quality_summary = (
            "\n".join(sig_lines) if sig_lines else "  暂无信号质量数据"
        )

        user_content = (
            f"你是一位投资系统的月度复盘教练。以下是本月完整数据。\n\n"
            f"## 月度统计 ({month_str})\n"
            f"判断总数: {total} | 可验证: {verified} | 准确: {correct} ({accuracy:.0%})\n\n"
            f"## 各维度预测力 (IC，60天)\n"
            f"技术面: {tech_ic:.3f} | 基本面: {fund_ic:.3f} | "
            f"资金面: {flow_ic:.3f} | 情绪面: {sent_ic:.3f}\n\n"
            f"## 建议权重调整\n"
            f"当前: 技术{cur_tech:.0%} 基本{cur_fund:.0%} 资金{cur_flow:.0%} 情绪{cur_sent:.0%}\n"
            f"建议: 技术{sug_tech:.0%} 基本{sug_fund:.0%} 资金{sug_flow:.0%} 情绪{sug_sent:.0%}\n\n"
            f"## 信号质量趋势\n"
            f"{signal_quality_summary}\n\n"
            f"请分析：\n"
            f"1. 哪个分析维度本月最有预测价值？原因是什么\n"
            f"2. 建议权重调整的逻辑是否合理？有什么需要注意的\n"
            f"3. 有没有发现新的市场规律（用【规律】标注）\n"
            f"4. 接下来一个月的重点关注方向\n"
            f"用 400-600 字完成，语言直接专业。"
        )

        return [{"role": "user", "content": user_content}]

    # ------------------------------------------------------------------
    # Monthly report helpers
    # ------------------------------------------------------------------

    async def _check_regime_switching_timeliness(
        self,
        market: str,
        month_start: date,
        month_end: date,
    ) -> dict[str, Any]:
        """Count regime mode changes during the month.

        Args:
            market: Market code.
            month_start: First day of the month.
            month_end: Last day of the month.

        Returns:
            Dictionary with switch counts and distinct regimes seen.
        """
        try:
            rows = await db_query(
                """
                SELECT trade_date, regime_mode
                FROM regime_daily
                WHERE market = $1
                  AND trade_date BETWEEN $2 AND $3
                ORDER BY trade_date
                """,
                market, month_start, month_end,
            )
        except Exception as e:
            logger.warning(
                "_check_regime_switching_timeliness_error",
                market=market,
                error=str(e),
            )
            return {}

        if not rows:
            return {"switches": 0, "distinct_regimes": [], "data_days": 0}

        modes = [r["regime_mode"] for r in rows]
        switches = sum(1 for i in range(1, len(modes)) if modes[i] != modes[i - 1])
        distinct = list(dict.fromkeys(modes))  # preserve first-seen order

        return {
            "switches": switches,
            "distinct_regimes": distinct,
            "data_days": len(rows),
        }

    async def _check_wiki_experience_usage(
        self,
        market: str,
    ) -> dict[str, Any]:
        """Summarise experience_store usage metrics.

        Args:
            market: Market code.

        Returns:
            Dictionary with counts of active, under_review, deprecated, and
            total applied_count.
        """
        try:
            rows = await db_query(
                """
                SELECT status, COUNT(*) AS cnt, SUM(applied_count) AS total_applied
                FROM experience_store
                WHERE market = $1 OR market IS NULL
                GROUP BY status
                """,
                market,
            )
        except Exception as e:
            logger.warning(
                "_check_wiki_experience_usage_error",
                market=market,
                error=str(e),
            )
            return {}

        summary: dict[str, Any] = {
            "active": 0,
            "under_review": 0,
            "deprecated": 0,
            "total_applied": 0,
        }
        for row in rows:
            status = row["status"]
            cnt = int(row["cnt"] or 0)
            applied = int(row["total_applied"] or 0)
            if status in summary:
                summary[status] = cnt
            summary["total_applied"] += applied

        return summary

    def _build_full_monthly_report_md(
        self,
        stats: dict[str, Any],
        ic_data: dict[str, float],
        weight_data: dict[str, float],
        signal_trends: list[dict[str, Any]],
        llm_analysis: str,
        alpha: float,
        month_start: date,
        month_end: date,
        market: str,
        regime_timeliness: dict[str, Any],
        wiki_usage: dict[str, Any],
    ) -> str:
        """Generate full Markdown monthly report for DB storage.

        Args:
            stats: Judgment statistics.
            ic_data: Dimension IC values.
            weight_data: Suggested weights.
            signal_trends: Signal quality list.
            llm_analysis: LLM analysis text.
            alpha: Alpha vs benchmark.
            month_start: First day of month.
            month_end: Last day of month.
            market: Market code.
            regime_timeliness: Regime switching info.
            wiki_usage: Experience store usage summary.

        Returns:
            Markdown string.
        """
        total = stats.get("total", 0)
        verified = stats.get("verified", 0)
        correct = stats.get("correct", 0)
        accuracy = stats.get("accuracy", 0.0)
        short_acc = stats.get("accuracy_short", 0.0) or 0.0
        mid_acc = stats.get("accuracy_mid", 0.0) or 0.0
        benchmark_name = "沪深300" if market == "CN" else "S&P500"

        # IC table
        ic_table = (
            "| 维度 | IC 值 | 建议权重 |\n"
            "|------|-------|----------|\n"
        )
        dim_label = {
            "technical": "技术面",
            "fundamental": "基本面",
            "flow": "资金面",
            "sentiment": "情绪面",
        }
        for dim in ("technical", "fundamental", "flow", "sentiment"):
            ic_table += (
                f"| {dim_label[dim]} "
                f"| {ic_data.get(dim, 0.0):.3f} "
                f"| {weight_data.get(dim, 0.25):.0%} |\n"
            )

        # Signal quality table
        sig_table_rows = ""
        for s in signal_trends[:10]:
            rule = s.get("rule_name", "unknown")
            acc_val = s.get("accuracy", 0.0) or 0.0
            vol = s.get("signal_count", 0)
            sig_table_rows += f"| {rule} | {acc_val:.0%} | {vol} |\n"
        sig_table = (
            "| 规则 | 准确率 | 触发次数 |\n"
            "|------|--------|----------|\n"
            + sig_table_rows
        ) if sig_table_rows else "_暂无信号质量数据_"

        # Error categories
        error_lines = ""
        for cat, cnt in stats.get("error_categories", {}).items():
            error_lines += f"- {cat}: {cnt} 次\n"
        if not error_lines:
            error_lines = "_暂无错误分类数据_\n"

        # Regime info
        regime_str = ""
        if regime_timeliness:
            switches = regime_timeliness.get("switches", 0)
            distinct = ", ".join(regime_timeliness.get("distinct_regimes", []))
            regime_str = f"体制切换次数: {switches} | 出现体制: {distinct or '—'}\n"
        else:
            regime_str = "_暂无体制数据_\n"

        # Wiki usage
        wiki_str = ""
        if wiki_usage:
            wiki_str = (
                f"活跃: {wiki_usage.get('active', 0)} | "
                f"待审: {wiki_usage.get('under_review', 0)} | "
                f"已弃用: {wiki_usage.get('deprecated', 0)} | "
                f"累计应用: {wiki_usage.get('total_applied', 0)} 次\n"
            )
        else:
            wiki_str = "_暂无 Wiki 数据_\n"

        report = (
            f"# 月度复盘报告 — {market}\n\n"
            f"**周期**: {month_start} ~ {month_end}\n\n"
            f"---\n\n"
            f"## 一、核心指标\n\n"
            f"| 指标 | 数值 |\n"
            f"|------|------|\n"
            f"| 判断总数 | {total} |\n"
            f"| 可验证 | {verified} |\n"
            f"| 准确 | {correct} ({accuracy:.0%}) |\n"
            f"| 短期准确率 | {short_acc:.0%} |\n"
            f"| 中期准确率 | {mid_acc:.0%} |\n"
            f"| Alpha vs {benchmark_name} | {alpha:+.1%} |\n\n"
            f"---\n\n"
            f"## 二、维度预测力 (IC) 与建议权重\n\n"
            f"{ic_table}\n"
            f"---\n\n"
            f"## 三、体制管理\n\n"
            f"{regime_str}\n"
            f"---\n\n"
            f"## 四、Wiki 经验库\n\n"
            f"{wiki_str}\n"
            f"---\n\n"
            f"## 五、信号质量\n\n"
            f"{sig_table}\n\n"
            f"---\n\n"
            f"## 六、错误分析\n\n"
            f"{error_lines}\n"
            f"---\n\n"
            f"## 七、LLM 月度分析\n\n"
            f"{llm_analysis}\n\n"
            f"---\n\n"
            f"_生成时间: {date.today()}_\n"
        )
        return report

    async def _push_monthly_summary(
        self,
        stats: dict[str, Any],
        llm_analysis: str,
        alpha: float,
        ic_data: dict[str, float],
        weight_data: dict[str, float],
        month_str: str,
        report_id: int,
    ) -> None:
        """Push condensed monthly review summary to Telegram.

        Args:
            stats: Judgment statistics.
            llm_analysis: LLM analysis text.
            alpha: Alpha vs benchmark.
            ic_data: Dimension IC values.
            weight_data: Suggested weights.
            month_str: Month label 'YYYY-MM'.
            report_id: Saved report ID.
        """
        total = stats.get("total", 0)
        accuracy = stats.get("accuracy", 0.0)

        # Best IC dimension
        best_dim = max(ic_data, key=lambda d: ic_data[d])
        best_ic = ic_data[best_dim]
        dim_label = {
            "technical": "技术面",
            "fundamental": "基本面",
            "flow": "资金面",
            "sentiment": "情绪面",
        }

        clean_text = re.sub(r"[*#_`\[\]]", "", llm_analysis).strip()
        excerpt = clean_text[:160] + ("..." if len(clean_text) > 160 else "")

        weight_line = (
            f"建议权重: 技{weight_data.get('technical', 0):.0%} "
            f"基{weight_data.get('fundamental', 0):.0%} "
            f"资{weight_data.get('flow', 0):.0%} "
            f"情{weight_data.get('sentiment', 0):.0%}"
        )

        msg = (
            f"📅 <b>月度复盘</b> {month_str}\n"
            f"━━━━━━━━━━━━━━━\n"
            f"判断: {total} | 准确率: {accuracy:.0%} | Alpha: {alpha:+.1%}\n"
            f"最佳维度: {dim_label.get(best_dim, best_dim)} (IC={best_ic:.3f})\n"
            f"{weight_line}\n"
            f"{excerpt}\n"
            f"/review 查看完整报告"
        )

        try:
            from bot.telegram_bot import TelegramPusher
            pusher = TelegramPusher()
            await pusher.send_html(msg)
        except Exception as e:
            logger.warning("monthly_summary_push_error", error=str(e))
