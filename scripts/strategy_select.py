#!/usr/bin/env python3
"""基于 Gamma 市场数据筛选高胜率 Polymarket 机会。

此脚本复用 ``get_markets.py`` 中的 `list_active_markets`，从 Gamma
``/markets`` 接口获取**活跃且未关闭**的市场，并执行以下逻辑：

1. 只保留 Yes 概率在给定区间内的市场（默认 [0.95, 0.99)）。
2. 排除 endDate 缺失、格式异常或已过期的市场。
3. 计算简单的评分 score，用于后续排序：

   score = (预期收益 / 剩余天数) × 流动性权重 × 成交量权重

   其中：
   - 预期收益 ~= (1 - yes_prob)
   - 剩余天数 = (endDate - 当前时间) 按天换算
   - 流动性/成交量权重是基于 ``liquidity``/``volume`` 的平滑缩放因子

4. 输出 JSON 列表，每个元素包含：

   - title
   - token_yes
   - token_no
   - yes_prob
   - best_ask
   - endDate
   - liquidity
   - volume
   - score

用法示例（在 Skill 根目录下）：

.. code-block:: bash

    # 按默认区间筛选，并打印前 50 条按 score 排序的候选市场
    python3 scripts/strategy_select.py --top-n 50

    # 调整 Yes 概率区间
    python3 scripts/strategy_select.py --min-yes 0.96 --max-yes 0.985 --top-n 20

    # 将结果写入文件
    python3 scripts/strategy_select.py --top-n 50 --output strategy_candidates.json

该脚本只做离线筛选和排序，不执行任何交易。
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from get_markets import (  # type: ignore[import]
    MarketView,
    PolymarketGammaAPIError,
    list_active_markets,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "从 Gamma /markets 获取活跃未关闭市场，筛选 Yes 概率位于指定区间的候选，\n"
            "排除已过期/异常 endDate，并按简单评分 score 排序后输出 JSON。"
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="从 Gamma API 获取的市场数量上限（默认 500，用于 list_active_markets）。",
    )
    parser.add_argument(
        "--min-yes",
        type=float,
        default=0.95,
        help="Yes 概率下限（含，默认 0.95）。",
    )
    parser.add_argument(
        "--max-yes",
        type=float,
        default=0.99,
        help="Yes 概率上限（不含，默认 0.99）。",
    )
    parser.add_argument(
        "--min-liquidity",
        type=float,
        default=0.0,
        help="最小流动性过滤阈值（默认 0，不过滤）。",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=50,
        help="最终输出的候选市场数量上限（默认 50）。",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="可选：将结果写入指定 JSON 文件；不指定则输出到 stdout。",
    )
    return parser.parse_args()


def _parse_end_datetime(value: Optional[str]) -> Optional[datetime]:
    """将 Gamma 返回的 endDate 字符串解析为 UTC 时间。

    Gamma API 通常返回 ISO8601 风格的字符串，如 ``"2024-02-01T12:00:00.000Z"``。
    这里采用尽量宽松的解析策略：

    - 先尝试 `fromisoformat`（去掉尾部 ``Z`` 后）；
    - 再尝试常见的 ``"%Y-%m-%dT%H:%M:%S.%fZ"`` / ``"%Y-%m-%dT%H:%M:%SZ"`` 格式；
    - 解析失败返回 ``None``。
    """

    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    # 统一处理尾部 Z
    z_stripped = text[:-1] if text.endswith("Z") else text

    # 1) 优先尝试 fromisoformat
    try:
        dt = datetime.fromisoformat(z_stripped)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        pass

    # 2) 常见格式兜底
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    return None


def _liquidity_weight(liquidity: float) -> float:
    """根据流动性计算平滑权重，范围约在 [1.0, 2.0) 内。

    对于非常小的流动性，权重接近 1；随着流动性增大，权重缓慢上升，
    避免单一高流动性市场完全主导排序。
    """

    if not isinstance(liquidity, (int, float)) or math.isnan(liquidity) or liquidity <= 0:
        return 1.0
    return 1.0 + (float(liquidity) / (float(liquidity) + 1_000.0))


def _volume_weight(volume: float) -> float:
    """根据成交量计算平滑权重，范围约在 [1.0, 2.0) 内。"""

    if not isinstance(volume, (int, float)) or math.isnan(volume) or volume <= 0:
        return 1.0
    return 1.0 + (float(volume) / (float(volume) + 1_000.0))


def _compute_score(market: MarketView, now: datetime) -> Optional[float]:
    """为单个市场计算评分 score。

    - 若 `yes_prob` 缺失/非法，则返回 ``None``；
    - 若 `end_date` 无法解析或已过期，则返回 ``None``；
    - 若剩余天数过小（<= 0），则返回 ``None``；

    返回值为非负浮点数；越大表示在单位时间内的期望收益越高且流动性/成交量更好。
    """

    yes_prob = market.yes_prob
    if not isinstance(yes_prob, (int, float)) or math.isnan(yes_prob):
        return None

    end_dt = _parse_end_datetime(market.end_date)
    if end_dt is None:
        return None

    # 只考虑未来市场
    if end_dt <= now:
        return None

    days_remaining = (end_dt - now).total_seconds() / 86400.0
    if days_remaining <= 0:
        return None

    expected_return = max(0.0, 1.0 - float(yes_prob))
    if expected_return <= 0:
        return None

    lw = _liquidity_weight(market.liquidity)
    vw = _volume_weight(market.volume)

    score = expected_return / days_remaining * lw * vw
    return float(score)


def select_markets(
    *,
    limit: int = 500,
    min_yes: float = 0.95,
    max_yes: float = 0.99,
    min_liquidity: float = 0.0,
    top_n: int = 50,
) -> List[Dict[str, Any]]:
    """以编程方式返回打分后的候选市场列表。

    返回的列表已按 score 从大到小排序。
    """

    if limit <= 0:
        limit = 500
    if top_n <= 0:
        top_n = 50

    now = datetime.now(timezone.utc)

    try:
        markets = list_active_markets(limit=limit, ascending=False)
    except PolymarketGammaAPIError as exc:  # pragma: no cover - 外部服务错误
        raise RuntimeError(f"调用 Gamma markets API 失败: {exc}") from exc

    results: List[Dict[str, Any]] = []

    for m in markets:
        # 1) 基于 Yes 概率初筛
        yes = m.yes_prob
        if not isinstance(yes, (int, float)) or math.isnan(yes):
            continue
        if yes < min_yes or yes >= max_yes:
            continue

        # 2) 流动性过滤
        liq = m.liquidity if isinstance(m.liquidity, (int, float)) else 0.0
        if liq < min_liquidity:
            continue

        # 3) 计算评分并排除无效项
        score = _compute_score(m, now)
        if score is None or score <= 0:
            continue

        item: Dict[str, Any] = {
            "title": m.display_title,
            "token_yes": m.token_yes,
            "token_no": m.token_no,
            "yes_prob": float(yes),
            "best_ask": float(m.best_ask) if isinstance(m.best_ask, (int, float)) else None,
            "endDate": m.end_date,
            "liquidity": float(liq),
            "volume": float(m.volume) if isinstance(m.volume, (int, float)) else 0.0,
            "score": score,
        }
        results.append(item)

    # 按 score 从大到小排序
    results.sort(key=lambda x: float(x.get("score", 0.0)), reverse=True)

    if len(results) > top_n:
        results = results[:top_n]

    return results


def _main() -> int:
    args = _parse_args()

    try:
        candidates = select_markets(
            limit=args.limit,
            min_yes=args.min_yes,
            max_yes=args.max_yes,
            min_liquidity=args.min_liquidity,
            top_n=args.top_n,
        )
    except Exception as exc:
        # 该脚本通常在自动化流水线中使用，因此错误信息保持简洁明了
        print(f"strategy_select 失败: {exc}")
        return 1

    output_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "min_yes": args.min_yes,
        "max_yes": args.max_yes,
        "min_liquidity": args.min_liquidity,
        "count": len(candidates),
        "items": candidates,
    }

    text = json.dumps(output_data, ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)

    return 0


if __name__ == "__main__":  # CLI 入口
    raise SystemExit(_main())
