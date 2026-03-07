#!/usr/bin/env python3
"""读取交易计划与账户信息，通过钉钉机器人发送摘要通知。

本脚本聚合以下信息：

- 交易计划文件 ``trades_plan.json`` 中待执行的交易列表；
- 当前 USDC 余额（调用 ``balance.py --json``）；
- 当前持仓数量及市值摘要（调用 ``positions.get_all_positions``）。

然后构造一段文本，通过 curl 调用钉钉机器人 Webhook 发送。

Webhook URL 读取优先级：

1. 环境变量 ``DINGTALK_WEBHOOK_URL``；
2. 若未设置，则回退为示例地址（用户提供的固定 token）。

用法示例（在 Skill 根目录下）：

.. code-block:: bash

    # 使用默认 trades_plan.json 和环境变量中的 Webhook 地址
    python3 scripts/notify_dingtalk.py

    # 指定计划文件并附加自定义备注
    python3 scripts/notify_dingtalk.py \
        --plan trades_plan.json \
        --extra "本轮为定时任务执行，尚未实际下单，仅为计划预览。"

本脚本不会执行真实交易，仅发送信息通知。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List

from positions import get_all_positions  # type: ignore[import]

DEFAULT_PLAN_PATH = "trades_plan.json"
# 用户在原始需求中提供的示例 Webhook，作为兜底配置。
FALLBACK_WEBHOOK_URL = (
    "https://oapi.dingtalk.com/robot/send?access_token="
    "11cea4096a9a2994a1a4ca77dfb6311cddcb9aa5a1cb36333d616131206c4a01"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "读取交易计划、余额与持仓摘要，构造文本并通过钉钉 Webhook 发送通知。"
        )
    )
    parser.add_argument(
        "--plan",
        type=str,
        default=DEFAULT_PLAN_PATH,
        help=f"交易计划 JSON 文件路径（默认 {DEFAULT_PLAN_PATH}）。",
    )
    parser.add_argument(
        "--extra",
        type=str,
        default="",
        help="可选：附加到消息末尾的自定义文本。",
    )
    return parser.parse_args()


def _load_json_file(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"未找到计划文件: {path}")
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("计划文件格式不合法，预期为 JSON 对象。")
    return data


def _call_balance_via_cli() -> Dict[str, Any]:
    """通过 balance.py --json 获取当前 USDC 余额。"""

    script_path = os.path.join(os.path.dirname(__file__), "balance.py")
    proc = subprocess.run(
        [sys.executable, script_path, "--json"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(f"balance.py 调用失败: {stderr}")

    try:
        return json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:  # pragma: no cover - 依赖外部脚本
        raise RuntimeError("解析 balance.py 输出 JSON 失败") from exc


def _parse_usdc_from_balance(data: Dict[str, Any]) -> float:
    raw = data.get("balance")
    if raw in (None, ""):
        return 0.0
    try:
        micro = Decimal(str(raw))
        usdc = micro / Decimal("1000000")
        return float(usdc)
    except (InvalidOperation, TypeError, ValueError):  # pragma: no cover - 防御性
        return 0.0


def _summarize_positions(funder: str) -> Dict[str, Any]:
    """返回持仓摘要：数量与当前价值总和。"""

    try:
        positions = get_all_positions(funder)
    except Exception as exc:  # pragma: no cover - 依赖外部服务
        raise RuntimeError(f"获取持仓失败: {exc}") from exc

    total_value = 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        try:
            total_value += float(p.get("current_value", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue

    return {
        "position_count": len(positions),
        "total_current_value": total_value,
    }


def _build_message(
    *,
    plan: Dict[str, Any],
    balance_info: Dict[str, Any],
    positions_summary: Dict[str, Any],
    extra: str,
) -> str:
    funder = plan.get("funder") or balance_info.get("funder") or os.getenv("FUNDER_ADDRESS")
    trades: List[Dict[str, Any]] = list(plan.get("trades") or [])

    usdc_balance = _parse_usdc_from_balance(balance_info)

    # 持仓摘要
    pos_cnt = positions_summary.get("position_count", 0)
    total_value = positions_summary.get("total_current_value", 0.0)

    lines: List[str] = []
    lines.append("[Polymarket 交易计划通知]")
    lines.append(f"时间: {datetime.now(timezone.utc).isoformat()}")
    if funder:
        lines.append(f"资金地址: {funder}")
    lines.append(f"账户 USDC 余额: {usdc_balance:.4f}")
    lines.append(f"当前持仓数量: {pos_cnt}")
    lines.append(f"当前持仓市值 (Data API current_value 合计): {total_value:.4f}")
    lines.append("")

    lines.append(f"本轮计划交易笔数: {len(trades)}")

    if not trades:
        lines.append("当前没有待执行的交易计划（trades_plan.json 为空或未生成）。")
    else:
        lines.append("计划明细 (仅为计划，执行前需额外 web_search 校验):")
        for idx, t in enumerate(trades, start=1):
            title = str(t.get("market_title", ""))[:80]
            token_id = t.get("token_id")
            price = t.get("price")
            shares = t.get("shares")
            notional = t.get("notional")
            yes_prob = t.get("yes_prob")
            end_date = t.get("endDate")

            lines.append(
                f"{idx}. {title}\n"
                f"   token_id: {token_id}\n"
                f"   Yes 概率: {yes_prob}\n"
                f"   价格: {price}  份数: {shares}  名义金额: {notional}\n"
                f"   endDate: {end_date}"
            )

    if extra:
        lines.append("")
        lines.append("[备注]")
        lines.append(extra)

    return "\n".join(lines)


def _send_dingtalk_message(webhook_url: str, content: str) -> None:
    payload = {
        "msgtype": "text",
        "text": {"content": content},
    }
    data = json.dumps(payload, ensure_ascii=False)

    # 直接调用 curl 命令，符合用户原始调用方式
    proc = subprocess.run(
        [
            "curl",
            webhook_url,
            "-H",
            "Content-Type: application/json",
            "-d",
            data,
        ],
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        raise RuntimeError(
            f"调用钉钉 Webhook 失败（退出码 {proc.returncode}）: {proc.stderr.strip()}"
        )


def _main() -> int:
    args = _parse_args()

    try:
        plan = _load_json_file(args.plan)
    except Exception as exc:
        print(f"读取交易计划失败: {exc}", file=sys.stderr)
        return 1

    try:
        balance_info = _call_balance_via_cli()
    except Exception as exc:
        print(f"查询余额失败: {exc}", file=sys.stderr)
        return 1

    funder = plan.get("funder") or balance_info.get("funder") or os.getenv("FUNDER_ADDRESS")
    if not funder:
        print(
            "警告：无法确定资金地址（funder），持仓摘要将跳过。",
            file=sys.stderr,
        )
        positions_summary = {"position_count": 0, "total_current_value": 0.0}
    else:
        try:
            positions_summary = _summarize_positions(funder)
        except Exception as exc:
            print(f"获取持仓摘要失败: {exc}", file=sys.stderr)
            positions_summary = {"position_count": 0, "total_current_value": 0.0}

    message = _build_message(
        plan=plan,
        balance_info=balance_info,
        positions_summary=positions_summary,
        extra=args.extra,
    )

    webhook_url = os.getenv("DINGTALK_WEBHOOK_URL") or FALLBACK_WEBHOOK_URL

    try:
        _send_dingtalk_message(webhook_url, message)
    except Exception as exc:
        print(f"发送钉钉消息失败: {exc}", file=sys.stderr)
        return 1

    print("已通过钉钉 Webhook 发送交易计划通知。")
    return 0


if __name__ == "__main__":  # CLI 入口
    raise SystemExit(_main())
