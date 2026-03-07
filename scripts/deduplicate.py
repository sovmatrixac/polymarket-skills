#!/usr/bin/env python3
"""基于 Polymarket 当前持仓对候选 token 进行去重过滤。

本脚本通过 ``positions.get_all_positions`` 查询指定地址当前在
Polymarket 的所有持仓，并根据 `asset` / `condition_id` 字段过滤掉
候选列表中已持有的同一市场/同一 token，避免重复建仓。

候选列表以 JSON 形式传入，支持两种使用方式：

1. 作为库函数从其他脚本中调用：

   .. code-block:: python

       from deduplicate import deduplicate_by_positions

       result = deduplicate_by_positions(funder_address, candidates)
       filtered = result["filtered"]
       skipped = result["skipped"]

2. 作为 CLI 使用，在 Skill 根目录下：

   .. code-block:: bash

       # 从文件读取候选列表（结构见下文）
       python3 scripts/deduplicate.py --input strategy_candidates.json --funder 0x...

候选元素推荐结构（与 ``strategy_select.py`` 输出对齐）：

.. code-block:: json

    {
      "title": "...",
      "token_yes": "0x...",
      "token_no": "0x...",
      "yes_prob": 0.97,
      "best_ask": 0.97,
      "endDate": "2024-02-01T12:00:00.000Z",
      "liquidity": 12345.0,
      "volume": 6789.0,
      "score": 0.0123
    }

在去重时，本脚本会按照如下顺序推断候选的 token_id：

1. 若条目已有 `token_id` 字段，则直接使用；
2. 否则优先使用 `token_no`，退化为使用 `token_yes`。

输出 JSON 结构：

.. code-block:: json

    {
      "funder": "0x...",
      "total": 10,
      "filtered_count": 7,
      "skipped_count": 3,
      "filtered": [ ... ],
      "skipped": [
        { "item": { ... }, "reasons": ["already_has_token", "already_in_condition"] }
      ]
    }
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

from positions import (  # type: ignore[import]
    InvalidAddressError,
    PolymarketAPIError,
    get_all_positions,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "根据当前 Polymarket 持仓记录，对候选 token 列表进行去重，避免在\n"
            "同一市场/同一 token 上重复建仓。"
        )
    )
    parser.add_argument(
        "--input",
        type=str,
        default="",
        help=(
            "候选列表 JSON 文件路径。如果不指定，则从 stdin 读取 JSON 内容。"
        ),
    )
    parser.add_argument(
        "--funder",
        type=str,
        default="",
        help=(
            "资金地址（钱包地址）。未指定时，自动从环境变量 FUNDER_ADDRESS 读取。"
        ),
    )
    return parser.parse_args()


def _load_candidates_from_stream(stream: Any) -> List[Dict[str, Any]]:
    """从给定文本流中解析候选列表。

    支持两种格式：

    - 直接是列表：``[{...}, {...}]``；
    - 包裹在对象中：``{"items": [...]} ``（与 strategy_select 输出一致）。
    """

    text = stream.read()
    if not text.strip():
        return []

    data = json.loads(text)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [item for item in data["items"] if isinstance(item, dict)]

    raise ValueError("输入 JSON 格式不符合预期，应为列表或包含 items 字段的对象。")


def _normalize_hex(value: Any) -> str:
    """将可能的十六进制字符串标准化为统一小写形式。"""

    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.startswith("0x") or text.startswith("0X"):
        return text.lower()
    return "0x" + text.lower()


def deduplicate_by_positions(
    funder_address: str,
    candidates: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """以编程方式对候选列表进行去重。

    返回结构同 CLI 输出：包含 filtered / skipped 等字段。
    """

    if not funder_address:
        raise ValueError("funder_address 不能为空")

    # 拉取当前持仓
    try:
        positions = get_all_positions(funder_address)
    except InvalidAddressError as exc:
        raise RuntimeError(f"钱包地址格式无效: {exc}") from exc
    except PolymarketAPIError as exc:  # pragma: no cover - 依赖外部服务
        raise RuntimeError(f"调用 Polymarket Data API 失败: {exc}") from exc

    held_assets = set()
    held_conditions = set()

    for p in positions:
        if not isinstance(p, dict):
            continue
        asset = _normalize_hex(p.get("asset"))
        if asset:
            held_assets.add(asset)
        cond = _normalize_hex(p.get("condition_id") or p.get("conditionId"))
        if cond:
            held_conditions.add(cond)

    filtered: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    for item in candidates:
        # 推断候选 token_id
        token_id = _normalize_hex(
            item.get("token_id")
            or item.get("token_no")
            or item.get("token_yes")
            or item.get("token")
        )
        condition_id = _normalize_hex(item.get("condition_id"))

        reasons: List[str] = []

        if token_id and token_id in held_assets:
            reasons.append("already_has_token")

        if condition_id and condition_id in held_conditions:
            reasons.append("already_in_condition")

        if reasons:
            skipped.append({"item": item, "reasons": reasons})
        else:
            filtered.append(item)

    return {
        "funder": funder_address,
        "total": len(candidates),
        "filtered_count": len(filtered),
        "skipped_count": len(skipped),
        "filtered": filtered,
        "skipped": skipped,
    }


def _main() -> int:
    args = _parse_args()

    funder = args.funder or os.getenv("FUNDER_ADDRESS")
    if not funder:
        print(
            "错误：未通过 --funder 或环境变量 FUNDER_ADDRESS 指定资金地址。",
            file=sys.stderr,
        )
        return 1

    try:
        if args.input:
            with open(args.input, "r", encoding="utf-8") as f:
                candidates = _load_candidates_from_stream(f)
        else:
            candidates = _load_candidates_from_stream(sys.stdin)
    except Exception as exc:
        print(f"读取候选列表失败: {exc}", file=sys.stderr)
        return 1

    try:
        result = deduplicate_by_positions(funder, candidates)
    except Exception as exc:
        print(f"deduplicate 失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # CLI 入口
    raise SystemExit(_main())
