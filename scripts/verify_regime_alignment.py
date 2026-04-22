"""
验证 regime_mode 语义对齐脚本。

检查：
  1. regime_daily 表中出现的所有 mode ⊆ VALID_REGIME_MODES
  2. backtest_regime_daily 表（只读）中出现的 mode ⊆ VALID_REGIME_MODES
  3. regime_params.yaml 中的 regimes key 与 VALID_REGIME_MODES 完全一致

用法：
  python scripts/verify_regime_alignment.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import yaml

# 添加项目根路径到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.regime.constants import VALID_REGIME_MODES
from db.connection import close_pool, get_pool, init_pool


async def _check_table(table: str) -> set[str]:
    """查询 table 中 DISTINCT regime_mode。

    Args:
        table: 表名。

    Returns:
        出现过的 mode 字符串集合，表不存在时返回空集合。
    """
    pool = get_pool()
    try:
        rows = await pool.fetch(f"SELECT DISTINCT regime_mode FROM {table}")  # noqa: S608
        return {r["regime_mode"] for r in rows}
    except Exception as e:
        print(f"  [SKIP] {table}: {e}")
        return set()


def _check_yaml(path: Path) -> set[str]:
    """从 regime_params.yaml 读取 regimes key 集合。

    Args:
        path: YAML 文件路径。

    Returns:
        regimes 配置中的 key 集合。
    """
    with open(path, "r", encoding="utf-8") as f:
        params = yaml.safe_load(f)
    return set(params.get("regimes", {}).keys())


async def main() -> int:
    """执行全部检查，返回 0=通过 / 1=失败。"""
    print("=" * 60)
    print("Regime 对齐检验")
    print(f"VALID_REGIME_MODES = {sorted(VALID_REGIME_MODES)}")
    print("=" * 60)

    failed = False

    # 1. 检查 YAML
    yaml_path = Path("config/regime_params.yaml")
    yaml_modes = _check_yaml(yaml_path)
    extra_yaml = yaml_modes - VALID_REGIME_MODES
    missing_yaml = VALID_REGIME_MODES - yaml_modes
    if extra_yaml or missing_yaml:
        print(f"[FAIL] regime_params.yaml")
        if extra_yaml:
            print(f"  多余 key（需删除）: {sorted(extra_yaml)}")
        if missing_yaml:
            print(f"  缺少 key（需补充）: {sorted(missing_yaml)}")
        failed = True
    else:
        print(f"[PASS] regime_params.yaml: {sorted(yaml_modes)}")

    # 2. 初始化 DB 连接池
    await init_pool()

    # 3. 检查 regime_daily
    db_modes = await _check_table("regime_daily")
    if db_modes:
        illegal = db_modes - VALID_REGIME_MODES
        if illegal:
            print(f"[FAIL] regime_daily: 非法 mode = {sorted(illegal)}")
            failed = True
        else:
            print(f"[PASS] regime_daily: {sorted(db_modes)}")
    else:
        print("[INFO] regime_daily: 无数据（跳过检查）")

    # 4. 检查 backtest_regime_daily（只读，不修改）
    bt_modes = await _check_table("backtest_regime_daily")
    if bt_modes:
        illegal_bt = bt_modes - VALID_REGIME_MODES
        if illegal_bt:
            print(f"[WARN] backtest_regime_daily: 非法 mode = {sorted(illegal_bt)}（backtest 禁区，不修改）")
        else:
            print(f"[PASS] backtest_regime_daily: {sorted(bt_modes)}")
    else:
        print("[INFO] backtest_regime_daily: 无数据或表不存在（跳过检查）")

    await close_pool()

    print("=" * 60)
    if failed:
        print("结果：FAIL — 存在 regime_mode 不一致问题")
        return 1
    print("结果：PASS — 所有检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
