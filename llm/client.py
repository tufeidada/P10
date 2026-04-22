"""
LLM 客户端 — 统一封装 DeepSeek / Doubao / Qwen API。

使用 httpx 直接调用 OpenAI-compatible API（不依赖 openai SDK）。
支持重试、超时、自动降级、成本追踪与日预算控制。

三个 Provider 及用途:
  - deepseek (DeepSeek V3.2 via Ark): 主力分析师，质量最高
  - doubao (Doubao Seed 2.0 via Ark): 备用分析师，质量同级
  - qwen (Qwen-Turbo via DashScope): 轻量任务，速度最快，成本最低

降级链: deepseek → doubao → qwen

Usage:
    client = LLMClient()
    text = await client.chat(
        [{"role": "user", "content": "分析..."}],
        symbol="600519.SH", market="CN"
    )
    data = await client.chat_json([{"role": "user", "content": "..."}])
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# 重试配置（M7 规格）
_MAX_RETRIES: int = 2
_RETRY_DELAY: float = 5.0   # 5 秒间隔
_REQUEST_TIMEOUT: float = 60.0

# 降级链: deepseek → doubao → qwen
_FALLBACK_CHAIN: dict[str, list[str]] = {
    "deepseek": ["doubao", "qwen"],
    "doubao": ["deepseek", "qwen"],
    "qwen": ["deepseek", "doubao"],
}

# 预算配置缓存
_budget_config: dict[str, Any] | None = None


def _load_budget_config() -> dict[str, Any]:
    global _budget_config
    if _budget_config is None:
        import yaml
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "config", "llm_budget.yaml"
        )
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                _budget_config = yaml.safe_load(f)
        except Exception:
            _budget_config = {"daily_budget_cny": 100.0, "cost_per_1k_tokens": {}}
    return _budget_config


def _calc_cost(provider: str, tokens_in: int, tokens_out: int) -> float:
    """根据 provider 和 token 数计算成本（人民币元）。"""
    cfg = _load_budget_config()
    rates = cfg.get("cost_per_1k_tokens", {}).get(provider, {})
    rate_in = rates.get("input", 0.001)
    rate_out = rates.get("output", 0.002)
    return round((tokens_in * rate_in + tokens_out * rate_out) / 1000.0, 4)


class LLMError(Exception):
    """LLM 调用失败异常。"""
    pass


class LLMClient:
    """统一 LLM 客户端，支持 DeepSeek / Doubao / Qwen。"""

    def __init__(self) -> None:
        self._configs: dict[str, dict[str, str]] = {
            "deepseek": {
                "api_key": os.environ.get("DEEPSEEK_API_KEY", ""),
                "base_url": os.environ.get(
                    "DEEPSEEK_BASE_URL",
                    "https://ark.cn-beijing.volces.com/api/v3",
                ),
                "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-v3-2-251201"),
            },
            "doubao": {
                "api_key": os.environ.get("DOUBAO_API_KEY", ""),
                "base_url": os.environ.get(
                    "DOUBAO_BASE_URL",
                    "https://ark.cn-beijing.volces.com/api/v3",
                ),
                "model": os.environ.get("DOUBAO_MODEL", "doubao-seed-2-0-pro-260215"),
            },
            "qwen": {
                "api_key": os.environ.get("QWEN_API_KEY", ""),
                "base_url": os.environ.get(
                    "QWEN_BASE_URL",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                ),
                "model": os.environ.get("QWEN_MODEL", "qwen-turbo-latest"),
            },
        }

    def is_configured(self, model: str = "deepseek") -> bool:
        """Check if a model's API key is configured (not placeholder).

        Args:
            model: Provider name ("deepseek", "doubao", or "qwen").

        Returns:
            True if the API key looks real.
        """
        cfg = self._configs.get(model)
        if not cfg:
            return False
        key = cfg.get("api_key", "")
        if not key:
            return False
        placeholders = {
            "", "your_deepseek_api_key", "your_qwen_api_key",
            "your_doubao_api_key", "your_embedding_api_key", "sk-xxx",
        }
        return key not in placeholders

    async def _check_daily_budget(self) -> None:
        """检查当日 LLM 成本是否超过预算。

        Raises:
            InvariantViolation: 超过日预算时抛出并推送 Telegram critical。
        """
        try:
            from db.connection import db_query_val
            from datetime import date
            today = date.today()
            total = await db_query_val(
                "SELECT COALESCE(SUM(cost_cny), 0) FROM llm_cost_log "
                "WHERE DATE(call_time) = $1 AND status = 'success'",
                today,
            )
            budget = _load_budget_config().get("daily_budget_cny", 100.0)
            if float(total or 0) > float(budget):
                from core.invariants import InvariantViolation
                msg = (
                    f"LLM 日成本 ¥{total:.2f} 超过预算 ¥{budget:.2f}，"
                    "停止调用 LLM，请检查 llm_cost_log 表"
                )
                logger.error("llm_budget_exceeded", total=float(total), budget=budget)
                try:
                    from bot.telegram_bot import TelegramPusher
                    await TelegramPusher().send(
                        f"🚨 <b>LLM BUDGET EXCEEDED</b>\n<code>{msg}</code>"
                    )
                except Exception:
                    pass
                raise InvariantViolation(msg)
        except Exception as e:
            from core.invariants import InvariantViolation
            if isinstance(e, InvariantViolation):
                raise
            # DB 不可用时不阻塞 LLM 调用
            logger.warning("llm_budget_check_error", error=str(e))

    async def _log_cost(
        self,
        provider: str,
        symbol: str | None,
        market: str | None,
        usage: dict[str, int],
        status: str,
        error: str | None = None,
    ) -> None:
        """将 LLM 调用成本写入 llm_cost_log（尽力，失败不阻塞）。"""
        try:
            from db.connection import db_execute
            tokens_in = usage.get("prompt_tokens", 0)
            tokens_out = usage.get("completion_tokens", 0)
            cost = _calc_cost(provider, tokens_in, tokens_out) if status == "success" else 0.0
            model_name = self._configs.get(provider, {}).get("model", provider)
            await db_execute(
                """
                INSERT INTO llm_cost_log
                    (model, symbol, market, tokens_in, tokens_out, cost_cny, status, error)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                model_name, symbol, market,
                tokens_in, tokens_out, cost,
                status, error,
            )
        except Exception as e:
            logger.warning("llm_cost_log_error", error=str(e))

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str = "deepseek",
        temperature: float = 0.0,
        max_tokens: int = 1000,
        symbol: str | None = None,
        market: str | None = None,
    ) -> str:
        """Send chat completion request with retry and fallback.

        Args:
            messages: OpenAI-format messages.
            model: Primary model provider ("deepseek" or "qwen").
            temperature: Sampling temperature.
            max_tokens: Max response tokens.
            symbol: 证券代码（用于成本追踪）。
            market: 市场代码（用于成本追踪）。

        Returns:
            Response text string.

        Raises:
            LLMError: If all retries and fallback fail.
            InvariantViolation: 超过日预算。
        """
        await self._check_daily_budget()

        # Build provider chain: primary → fallbacks
        chain = [model] + _FALLBACK_CHAIN.get(model, [])
        last_error: str = ""

        for provider in chain:
            cfg = self._configs.get(provider)
            if not cfg or not self.is_configured(provider):
                continue
            try:
                text, usage = await self._call_with_retries(
                    cfg, messages, temperature, max_tokens, provider
                )
                # Strip thinking tags (Doubao/Qwen sometimes include them)
                if "<think>" in text:
                    import re
                    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
                await self._log_cost(provider, symbol, market, usage, "success")
                return text
            except LLMError as e:
                last_error = str(e)
                logger.warning("llm_provider_failed", provider=provider, error=last_error)
                await self._log_cost(provider, symbol, market, {}, "failed", last_error[:200])

        raise LLMError(f"All LLM providers failed (chain={chain}): {last_error}")

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        model: str = "deepseek",
        temperature: float = 0.1,
        max_tokens: int = 1000,
        symbol: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any]:
        """Chat with JSON output enforcement.

        Adds system instruction to return valid JSON. Parses response,
        retries once if JSON parse fails.

        Args:
            messages: OpenAI-format messages.
            model: Model provider name.
            temperature: Sampling temperature.
            max_tokens: Max response tokens.
            symbol: 证券代码（用于成本追踪）。
            market: 市场代码（用于成本追踪）。

        Returns:
            Parsed JSON dict.

        Raises:
            LLMError: If call fails or JSON parsing fails after retry.
            InvariantViolation: 超过日预算。
        """
        json_instruction = {
            "role": "system",
            "content": (
                "你必须以合法的 JSON 格式回复，不要包含 markdown 代码块标记。"
                "确保输出是可直接 json.loads() 解析的。"
            ),
        }
        augmented = [json_instruction] + messages

        for attempt in range(2):
            raw_text = await self.chat(
                augmented, model=model, temperature=temperature,
                max_tokens=max_tokens, symbol=symbol, market=market,
            )
            # Strip potential markdown fences
            cleaned = raw_text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                cleaned = "\n".join(lines).strip()

            try:
                return json.loads(cleaned)
            except json.JSONDecodeError as e:
                logger.warning(
                    "llm_json_parse_error",
                    attempt=attempt + 1,
                    error=str(e),
                    raw_preview=raw_text[:200],
                )
                if attempt == 0:
                    augmented[-1] = {
                        "role": augmented[-1]["role"],
                        "content": augmented[-1]["content"]
                        + "\n\n请注意：你上次返回的不是合法JSON，请务必只返回JSON。",
                    }

        raise LLMError(f"JSON parse failed after 2 attempts, raw: {raw_text[:300]}")

    async def _call_with_retries(
        self,
        config: dict[str, str],
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        model_label: str,
    ) -> tuple[str, dict[str, int]]:
        """Call API with retries.

        Args:
            config: Provider config dict.
            messages: Chat messages.
            temperature: Sampling temperature.
            max_tokens: Max tokens.
            model_label: Label for logging.

        Returns:
            Tuple of (response_text, usage_dict).

        Raises:
            LLMError: If all retries exhausted.
        """
        last_error: Exception | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                text, usage = await self._call_api(
                    config, messages, temperature, max_tokens
                )
                logger.info(
                    "llm_call_success",
                    model=model_label,
                    attempt=attempt + 1,
                    tokens=usage.get("total_tokens", 0),
                )
                return text, usage
            except Exception as e:
                last_error = e
                logger.warning(
                    "llm_call_retry",
                    model=model_label,
                    attempt=attempt + 1,
                    error=str(e),
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_DELAY)

        raise LLMError(
            f"{model_label} failed after {_MAX_RETRIES + 1} attempts: {last_error}"
        )

    async def _call_api(
        self,
        config: dict[str, str],
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
    ) -> tuple[str, dict[str, int]]:
        """Low-level API call to OpenAI-compatible endpoint.

        Args:
            config: Provider config with api_key, base_url, model.
            messages: Chat messages.
            temperature: Sampling temperature.
            max_tokens: Max tokens.

        Returns:
            Tuple of (response_text, usage_dict).

        Raises:
            LLMError: On HTTP error or unexpected response format.
        """
        url = f"{config['base_url'].rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {config['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": config["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "top_p": 1.0 if temperature == 0.0 else 0.9,
        }

        t0 = time.monotonic()

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.post(url, headers=headers, json=payload)

        elapsed = round(time.monotonic() - t0, 2)

        if resp.status_code != 200:
            body_preview = resp.text[:500]
            raise LLMError(
                f"API returned {resp.status_code}: {body_preview}"
            )

        try:
            data = resp.json()
        except Exception as e:
            raise LLMError(f"Failed to parse API response JSON: {e}")

        choices = data.get("choices", [])
        if not choices:
            raise LLMError(f"API returned empty choices: {data}")

        text = choices[0].get("message", {}).get("content", "")

        usage = data.get("usage", {})
        usage_dict = {
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        }

        logger.debug(
            "llm_api_call",
            model=config["model"],
            elapsed_s=elapsed,
            prompt_tokens=usage_dict["prompt_tokens"],
            completion_tokens=usage_dict["completion_tokens"],
        )

        return text, usage_dict
