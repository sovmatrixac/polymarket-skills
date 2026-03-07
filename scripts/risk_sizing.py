#!/usr/bin/env python3
"""基于当前 USDC 余额计算单笔交易的最大资金与份数。

本脚本通过调用同目录下的 ``balance.py``（以 ``--json`` 方式）获取
Polymarket CLOB 视角下的 USDC 抵押余额，并按照给定的风险系数
(``risk_fraction``，默认 5%) 计算：

- 单笔交易允许使用的最大资金（max_per_trade）
- 在给定价格 price 下的最大可买入份数（max_shares）

注意：
- balance.py 返回的 ``balance`` 字段以 1e-6 精度计价（脚本内部会
  自动除以 1_000_000 转为 USDC 单位）。
- 若在风险限制下无法买入至少 1 份，则 `max_shares` 返回 0。

用法示例（在 Skill 根目录下）：

.. code-block:: bash

    # 以价格 0.97 计算最大可交易份数（默认风险系数 5%）
    python3 scripts/risk_sizing.py --price 0.97

    # 调整风险系数为 3%
    python3 scripts/risk_sizing.py --price 0.97 --risk-fraction 0.03

该脚本只进行仓位测算，不会发起任何真实交易。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Dict


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "基于 balance.py 输出的 USDC 余额，按给定 price 和风险系数计算单笔\n"
            "最大资金 (max_per_trade) 与最大份数 (max_shares)。"
        )
    )
    parser.add_argument(
        "--price",
        type=float,
        required=True,
        help="目标挂单价格（每份价格，必须大于 0）。",
    )
    parser.add_argument(
        "--risk-fraction",
        type=float,
        default=0.05,
        help="单笔最大资金占总余额比例（默认 0.05，即 5%%）。",
    )
    return parser.parse_args()


def _load_balance_via_cli() -> Dict[str, Any]:
    """通过子进程调用 balance.py 获取 JSON 结果。

    balance.py 需与本脚本位于同一目录，并支持 ``--json`` 选项。
    返回的字典至少包含：balance（字符串）和 funder（地址）。
    """

    script_path = os.path.join(os.path.dirname(__file__), "balance.py")

    proc = subprocess.run(
        [sys.executable, script_path, "--json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"balance.py 调用失败（退出码 {proc.returncode}）: {stderr}")

    output = proc.stdout.strip()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:  # pragma: no cover - 依赖外部脚本
        raise RuntimeError("解析 balance.py 输出 JSON 失败") from exc

    return data


def _parse_usdc_balance(balance_raw: Any) -> Decimal:
    """将 balance.py 返回的 balance 字段解析为 USDC 计价的 Decimal。

    balance_raw 通常是一个字符串，表示以 1e-6 精度计价的余额。
    若解析失败，则返回 0。
    """

    if balance_raw in (None, ""):
        return Decimal("0")

    try:
        micro = Decimal(str(balance_raw))
    except (InvalidOperation, TypeError, ValueError):  # pragma: no cover - 防御性
        return Decimal("0")

    # balance.py 的人类可读输出使用 balance/1_000_000
    return micro / Decimal("1000000")


def compute_sizing(price: float, risk_fraction: float = 0.05) -> Dict[str, Any]:
    """以编程方式计算单笔最大资金与份数。

    返回字典结构：

    .. code-block:: json

        {
          "funder": "0x...",
          "balance_usdc": 123.45,
          "risk_fraction": 0.05,
          "max_per_trade": 6.1725,
          "price": 0.97,
          "max_shares": 6
        }
    """

    if price <= 0:
        raise ValueError("price 必须大于 0")
    if risk_fraction <= 0 or risk_fraction > 1:
        raise ValueError("risk_fraction 必须在 (0, 1] 区间内")

    data = _load_balance_via_cli()

    balance_usdc = _parse_usdc_balance(data.get("balance"))
    funder = data.get("funder")

    max_per_trade = (balance_usdc * Decimal(str(risk_fraction))).quantize(Decimal("0.0000001"))

    # 计算可买入份数，使用 floor，若在风险限制下不足 1 份，则返回 0
    price_dec = Decimal(str(price))
    if price_dec <= 0:
        max_shares = 0
    else:
        max_shares = int(max_per_trade // price_dec)
        if max_shares < 0:
            max_shares = 0

    return {
        "funder": funder,
        "balance_usdc": float(balance_usdc),
        "risk_fraction": float(risk_fraction),
        "max_per_trade": float(max_per_trade),
        "price": float(price),
        "max_shares": max_shares,
    }


def _main() -> int:
    args = _parse_args()

    try:
        result = compute_sizing(price=args.price, risk_fraction=args.risk_fraction)
    except Exception as exc:
        print(f"risk_sizing 失败: {exc}")
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # CLI 入口
    raise SystemExit(_main())
