#!/usr/bin/env python3
"""将策略筛选、去重和仓位控制整合为一条离线交易决策管线。

本脚本不会直接发起任何真实交易，只负责基于当前市场与账户状态
生成一份交易计划文件 ``trades_plan.json``，供后续人工或其他
Agent 在完成 web_search 验证后再调用 ``trade.py`` 执行。

整体流程：

1. 调用 ``strategy_select.select_markets`` 获取高胜率候选市场；
2. 基于当前持仓（positions.get_all_positions）调用
   ``deduplicate.deduplicate_by_positions`` 过滤掉重复 token/市场；
3. 对剩余候选按 score 由高到低排序，截取至多 N 条（默认 5 条）；
4. 针对每个候选，调用 ``risk_sizing.compute_sizing`` 计算单笔最大
   资金与份数；
5. 生成包含 token_id（优先使用 token_no）、price、shares 等字段的
   交易计划，并写入 JSON 文件 ``trades_plan.json``。

用法示例（在 Skill 根目录下）：

.. code-block:: bash

    # 生成默认交易计划（最多 5 笔，每笔 5% 资金）
    python3 scripts/exec_pipeline.py

    # 调整最大交易笔数与风险系数
    python3 scripts/exec_pipeline.py --max-trades 3 --risk-fraction 0.03

生成的 trades_plan.json 结构示例：

.. code-block:: json

    {
      "generated_at": "2026-03-03T12:00:00+00:00",
      "funder": "0x...",
      "risk_fraction": 0.05,
      "max_trades": 5,
      "currency": "USDC",
      "trades": [
        {
          "market_title": "...",
          "token_id": "0x...",
          "side": "BUY",
          "yes_prob": 0.97,
          "price": 0.97,
          "shares": 10,
          "notional": 9.7,
          "endDate": "2024-02-01T12:00:00.000Z",
          "score": 0.0123
        }
      ]
    }

**重要约束：**

- 每笔交易的资金上限由 ``risk_fraction`` 和当前余额共同决定；
- 每次运行最多生成 ``max_trades`` 条交易（默认 5 条）；
- 该脚本不会调用 ``web_search``，也不会执行真实交易；
  web_search 校验与交易执行应在 Skill 文档中明确由上层流程完成。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

from deduplicate import deduplicate_by_positions  # type: ignore[import]
from risk_sizing import compute_sizing  # type: ignore[import]
from strategy_select import select_markets  # type: ignore[import]


DEFAULT_MAX_TRADES = 5
DEFAULT_RISK_FRACTION = 0.05


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "整合策略筛选、去重与仓位控制，生成离线交易计划 trades_plan.json，\n"
            "不直接发起真实交易。"
        )
    )
    parser.add_argument(
        "--max-trades",
        type=int,
        default=DEFAULT_MAX_TRADES,
        help="单次运行最多生成的交易笔数（默认 5）。",
    )
    parser.add_argument(
        "--risk-fraction",
        type=float,
        default=DEFAULT_RISK_FRACTION,
        help="单笔资金上限占总余额比例（默认 0.05，即 5%%）。",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.0,
        help="可选：过滤掉 score 低于该阈值的候选（默认不过滤）。",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="trades_plan.json",
        help="交易计划输出文件路径（默认 trades_plan.json）。",
    )
    return parser.parse_args()


def _select_and_dedup_candidates(
    funder: str,
    max_trades: int,
    min_score: float,
) -> List[Dict[str, Any]]:
    """拉取候选市场并结合当前持仓进行去重，返回待进一步 sizing 的候选列表。"""

    # 1) 获取按 score 排序的候选市场（数量适当放大，方便管道后续过滤）
    raw_candidates_wrapper = select_markets(top_n=max_trades * 10 or 50)
    items = list(raw_candidates_wrapper)

    # 2) 为去重逻辑补充 token_id 字段（优先使用 token_no）
    enriched: List[Dict[str, Any]] = []
    for item in items:
        token_no = item.get("token_no")
        token_yes = item.get("token_yes")
        token_id = token_no or token_yes
        if not token_id:
            # 缺少 token 信息的条目无法交易，直接丢弃
            continue
        new_item = dict(item)
        new_item["token_id"] = token_id
        enriched.append(new_item)

    # 3) 基于当前持仓进行去重
    dedup_result = deduplicate_by_positions(funder, enriched)
    filtered = dedup_result.get("filtered", [])

    # 4) 按 score 再次排序并截断
    def _score_key(x: Dict[str, Any]) -> float:
        try:
            return float(x.get("score", 0.0))
        except Exception:  # pragma: no cover - 防御性
            return 0.0

    filtered.sort(key=_score_key, reverse=True)

    if min_score > 0:
        filtered = [x for x in filtered if _score_key(x) >= min_score]

    if max_trades > 0 and len(filtered) > max_trades * 2:
        filtered = filtered[: max_trades * 2]

    return filtered


def build_trades_plan(
    *,
    funder: str,
    risk_fraction: float,
    max_trades: int,
    min_score: float,
    output_path: str,
) -> Dict[str, Any]:
    """生成交易计划并写入 JSON 文件，返回计划内容。"""

    candidates = _select_and_dedup_candidates(
        funder=funder,
        max_trades=max_trades,
        min_score=min_score,
    )

    trades: List[Dict[str, Any]] = []

    for item in candidates:
        if len(trades) >= max_trades:
            break

        price = item.get("best_ask")
        try:
            price_f = float(price)
        except (TypeError, ValueError):
            # 缺少有效报价时跳过该市场
            continue
        if price_f <= 0:
            continue

        sizing = compute_sizing(price=price_f, risk_fraction=risk_fraction)
        max_shares = int(sizing.get("max_shares", 0))
        if max_shares <= 0:
            # 在风险限制下不足 1 份，跳过该候选
            continue

        token_id = item.get("token_id")
        if not token_id:
            continue

        notional = price_f * max_shares

        trade_entry: Dict[str, Any] = {
            "market_title": item.get("title"),
            "token_id": token_id,
            # trade.py 中示例为 "看好 Yes 时 Sell No"，但底层侧别由 trade.py 决定，
            # 这里统一使用 BUY 以与脚本内部默认保持一致。
            "side": "BUY",
            "yes_prob": item.get("yes_prob"),
            "price": price_f,
            "shares": max_shares,
            "notional": notional,
            "endDate": item.get("endDate"),
            "score": item.get("score"),
        }
        trades.append(trade_entry)

    plan: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "funder": funder,
        "risk_fraction": float(risk_fraction),
        "max_trades": int(max_trades),
        "currency": "USDC",
        "min_score": float(min_score),
        "trades": trades,
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
        f.write("\n")

    return plan


def _main() -> int:
    args = _parse_args()

    funder = os.getenv("FUNDER_ADDRESS")
    if not funder:
        print(
            "错误：未在环境变量 FUNDER_ADDRESS 中找到资金地址，无法构建交易计划。",
            file=sys.stderr,
        )
        return 1

    if args.max_trades <= 0:
        print("警告：max-trades <= 0，默认使用 5。", file=sys.stderr)
        args.max_trades = DEFAULT_MAX_TRADES

    if args.risk_fraction <= 0 or args.risk_fraction > 1:
        print(
            "错误：risk-fraction 必须在 (0, 1] 区间内，例如 0.05 表示 5%%。",
            file=sys.stderr,
        )
        return 1

    try:
        plan = build_trades_plan(
            funder=funder,
            risk_fraction=args.risk_fraction,
            max_trades=args.max_trades,
            min_score=args.min_score,
            output_path=args.output,
        )
    except Exception as exc:
        print(f"exec_pipeline 失败: {exc}", file=sys.stderr)
        return 1

    # 将计划回显到 stdout，便于上游日志记录
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # CLI 入口
    raise SystemExit(_main())
