#!/usr/bin/env python3
"""
LLM 模型对比测试

用贵州茅台的真实分析数据，并发调用多个模型，
对比分析质量、响应速度、token 用量和费用。
"""

import asyncio
import sys
import time
from collections import OrderedDict

import httpx

# ============================================================
# 可用模型配置（已验证可调通的）
# ============================================================

ARK_KEY = "ark-d9d9d42a-0da9-437c-aeaf-3d02851206c5-e40a5"
QWEN_KEY = "sk-87af220f2e1f4b55a31a203fc94dd5b6"

MODELS = OrderedDict([
    # ── Doubao (火山引擎方舟) ──
    ("doubao-seed-2.0-pro", {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": ARK_KEY,
        "model": "doubao-seed-2-0-pro-260215",
        "provider": "Doubao",
        "pricing": "输入¥0.4/M 输出¥0.8/M",
        "note": "豆包最强，Seed 2.0 架构",
    }),
    ("doubao-1.5-lite", {
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "api_key": ARK_KEY,
        "model": "doubao-1-5-lite-32k-250115",
        "provider": "Doubao",
        "pricing": "输入¥0.15/M 输出¥0.3/M",
        "note": "豆包轻量版，性价比高",
    }),
    # ── Qwen (通义千问) ──
    ("qwen3-235b", {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": QWEN_KEY,
        "model": "qwen3-235b-a22b",
        "provider": "Qwen",
        "pricing": "输入¥4/M 输出¥16/M (思考免费)",
        "note": "Qwen3 最强旗舰 MoE 235B",
    }),
    ("qwen-plus", {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": QWEN_KEY,
        "model": "qwen-plus",
        "provider": "Qwen",
        "pricing": "输入¥0.8/M 输出¥2/M",
        "note": "Qwen 中端，均衡性价比",
    }),
    ("qwen-max", {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": QWEN_KEY,
        "model": "qwen-max",
        "provider": "Qwen",
        "pricing": "输入¥2/M 输出¥6/M",
        "note": "Qwen 高端，长文本能力强",
    }),
    ("qwen-turbo", {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": QWEN_KEY,
        "model": "qwen-turbo-latest",
        "provider": "Qwen",
        "pricing": "输入¥0.3/M 输出¥0.6/M",
        "note": "Qwen 最便宜，速度最快",
    }),
])

# ============================================================
# 测试 Prompt — 贵州茅台真实分析数据
# ============================================================

SYSTEM = "你是专业的A股投资分析师，擅长多维度数据分析。回答简洁专业有逻辑，不要输出思考过程。"

PROMPT = """请基于以下多维度数据，为 600519.SH 贵州茅台 生成投资分析。

## 当前市场环境 (Regime)
模式: risk_off (避险模式) — 趋势中性，波动率极高，流动性偏紧
趋势: 52/100 | 波动率: 92/100 | 宽度: 99/100 | 流动性: 19/100

## 各维度评分
- 技术面 47/100: 日线横盘整理，Stage 1（底部蓄力），MA150附近震荡。支撑1435，阻力1510。RS Rank 69。
- 基本面 54/100: ROE 34.5%连续改善，但营收-1.2%、净利润-4.5%。PE 20.4x历史50分位。负债率16%。
- 资金面 62/100: 主力5日净流入+0.7亿，北向5日净买入，融资余额↓0.7%。

## 关键数据
收盘1509.66 | 支撑1435 | 阻力1510 | ATR 39.4 | 布林收窄

## 要求
1. 一致性和矛盾分析
2. 短期(1-2周)方向 + 置信度(0-1)
3. 具体买卖价位
4. 判错最可能原因
5. 200-300字，直接输出分析，不要说"好的"之类的废话"""


async def call_model(name: str, cfg: dict) -> dict:
    """调用单个模型。"""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": PROMPT},
    ]
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 800,
    }
    headers = {
        "Authorization": f"Bearer {cfg['api_key']}",
        "Content-Type": "application/json",
    }

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                f"{cfg['base_url']}/chat/completions",
                headers=headers,
                json=body,
            )
            elapsed = time.time() - start

            if resp.status_code != 200:
                err = resp.text[:200]
                try:
                    err = resp.json().get("error", {}).get("message", err)[:200]
                except Exception:
                    pass
                return {"name": name, "error": f"HTTP {resp.status_code}: {err}", "elapsed": elapsed}

            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})

            return {
                "name": name,
                "provider": cfg["provider"],
                "content": content,
                "elapsed": round(elapsed, 2),
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
                "pricing": cfg["pricing"],
                "note": cfg["note"],
            }
    except Exception as e:
        return {"name": name, "error": str(e)[:200], "elapsed": round(time.time() - start, 2)}


async def main():
    print("=" * 70)
    print("  LLM 模型对比 — 贵州茅台投资分析 (6 模型并发)")
    print("=" * 70)

    # 并发调用所有模型
    tasks = [call_model(name, cfg) for name, cfg in MODELS.items()]
    results = await asyncio.gather(*tasks)

    # 输出每个模型结果
    for r in results:
        print(f"\n{'━' * 70}")
        cfg = MODELS.get(r["name"], {})
        print(f"📌 {r['name']}  ({cfg.get('note', '')})")
        print(f"   定价: {cfg.get('pricing', 'N/A')}")
        print(f"{'━' * 70}")

        if "error" in r:
            print(f"  ❌ {r['error']}")
            continue

        print(f"  ⏱ 耗时 {r['elapsed']}s  |  Token: {r['prompt_tokens']}+{r['completion_tokens']}={r['total_tokens']}")
        print()
        # 清理 thinking tags
        content = r["content"]
        if "<think>" in content:
            import re
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        print(content)

    # ── 汇总表 ──
    print(f"\n\n{'=' * 70}")
    print("  📊 对比汇总")
    print(f"{'=' * 70}")
    print(f"{'模型':<22} {'耗时':>6} {'输入':>7} {'输出':>7} {'总计':>7} {'定价'}")
    print("─" * 70)

    for r in results:
        if "error" in r:
            print(f"{r['name']:<22} {'ERR':>6}    ❌ {r.get('error', '')[:30]}")
        else:
            print(
                f"{r['name']:<22} {r['elapsed']:>5.1f}s"
                f" {r['prompt_tokens']:>7}"
                f" {r['completion_tokens']:>7}"
                f" {r['total_tokens']:>7}"
                f"  {MODELS[r['name']]['pricing']}"
            )

    # ── 每万 token 成本估算 ──
    print(f"\n{'=' * 70}")
    print("  💰 单次分析成本估算 (基于实测 token)")
    print(f"{'=' * 70}")
    cost_map = {
        "doubao-seed-2.0-pro": (0.4, 0.8),
        "doubao-1.5-lite": (0.15, 0.3),
        "qwen3-235b": (4.0, 16.0),
        "qwen-plus": (0.8, 2.0),
        "qwen-max": (2.0, 6.0),
        "qwen-turbo": (0.3, 0.6),
    }
    for r in results:
        if "error" in r or r["name"] not in cost_map:
            continue
        ci, co = cost_map[r["name"]]
        cost = r["prompt_tokens"] * ci / 1_000_000 + r["completion_tokens"] * co / 1_000_000
        daily_30 = cost * 30  # 假设每天分析 30 只票
        monthly = daily_30 * 22  # 22 个交易日
        print(f"  {r['name']:<22} 单次 ¥{cost:.5f}  |  日30次 ¥{daily_30:.3f}  |  月 ¥{monthly:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
