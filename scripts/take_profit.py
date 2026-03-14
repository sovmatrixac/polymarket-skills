#!/usr/bin/env python3
"""
自动止盈策略脚本：
1. 当持仓收益达到理论最大收益的60%时触发止盈
2. 优先卖出剩余到期时间更长的标的，提前收回资金用于其他投资
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import SELL
from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds

from positions import get_all_positions

# 优先加载脚本所在目录上级（技能根目录）的.env文件，自动覆盖现有环境变量
dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(dotenv_path=dotenv_path, override=True)
import subprocess

def send_dingtalk_notification(content: str) -> None:
    """发送钉钉通知"""
    webhook_url = os.getenv("DINGTALK_WEBHOOK_URL") or "https://oapi.dingtalk.com/robot/send?access_token=11cea4096a9a2994a1a4ca77dfb6311cddcb9aa5a1cb36333d616131206c4a01"
    payload = {
        "msgtype": "text",
        "text": {"content": content},
    }
    data = json.dumps(payload, ensure_ascii=False)
    try:
        subprocess.run(
            ["curl", webhook_url, "-H", "Content-Type: application/json", "-d", data],
            capture_output=True,
            text=True,
            timeout=10
        )
    except Exception as e:
        print(f"发送钉钉通知失败: {e}")

load_dotenv()

GAMMA_API_BASE_URL = "https://gamma-api.polymarket.com"
MARKETS_ENDPOINT = f"{GAMMA_API_BASE_URL}/markets"
TAKE_PROFIT_THRESHOLD = 0.6  # 达到理论最大收益的60%时止盈


def _get_market_details(condition_id: str) -> Dict[str, Any] | None:
    """根据condition_id获取市场详情，包括最新价格、到期时间"""
    try:
        params = {"condition_id": condition_id, "active": "true"}
        resp = requests.get(MARKETS_ENDPOINT, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception as e:
        print(f"获取市场详情失败: {e}")
    return None


def _calculate_profit(position: Dict[str, Any], market: Dict[str, Any]) -> Dict[str, Any]:
    """计算持仓收益情况"""
    avg_price = position["avg_price"]
    quantity = position["quantity"]
    outcome = position["outcome"]
    
    # 理论最大收益
    max_theoretical_profit = (1.0 - avg_price) * quantity
    
    # 获取当前价格
    current_price = 0.0
    if outcome.lower() == "yes":
        current_price = float(market.get("bestBid") or market.get("best_bid") or 0.0)
    else:
        # No侧当前价格 = 1 - Yes侧的bestAsk
        yes_ask = float(market.get("bestAsk") or market.get("best_ask") or 1.0)
        current_price = 1.0 - yes_ask
    
    # 当前收益
    current_profit = (current_price - avg_price) * quantity
    profit_rate = current_profit / max_theoretical_profit if max_theoretical_profit > 0 else 0.0
    
    # 剩余到期天数
    end_date_str = market.get("endDate") or market.get("end_date")
    days_remaining = 9999  # 默认时间很长
    if end_date_str:
        try:
            if end_date_str.endswith("Z"):
                end_date_str = end_date_str[:-1] + "+00:00"
            end_dt = datetime.fromisoformat(end_date_str)
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            days_remaining = (end_dt - now).total_seconds() / 86400.0
        except Exception as e:
            print(f"解析到期时间失败: {e}")
    
    return {
        "max_profit": max_theoretical_profit,
        "current_profit": current_profit,
        "profit_rate": profit_rate,
        "current_price": current_price,
        "days_remaining": days_remaining,
        "should_take_profit": profit_rate >= TAKE_PROFIT_THRESHOLD and current_profit > 0
    }


def _execute_sell(position: Dict[str, Any], market: Dict[str, Any], profit_info: Dict[str, Any]) -> bool:
    """执行卖出止盈操作"""
    try:
        host = "https://clob.polymarket.com"
        chain_id = 137
        private_key = os.getenv("PRIVATE_KEY")
        funder_address = os.getenv("FUNDER_ADDRESS")
        
        if not private_key or not funder_address:
            print("缺少交易凭证，无法执行卖出")
            return False
        
        # 初始化客户端
        temp_client = ClobClient(host, key=private_key, chain_id=chain_id)
        user_api_creds = temp_client.create_or_derive_api_creds()
        
        builder_creds = BuilderApiKeyCreds(
            key=os.getenv("POLY_BUILDER_API_KEY"),
            secret=os.getenv("POLY_BUILDER_SECRET"),
            passphrase=os.getenv("POLY_BUILDER_PASSPHRASE"),
        )
        builder_config = BuilderConfig(local_builder_creds=builder_creds)
        
        client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            creds=user_api_creds,
            signature_type=1,
            funder=funder_address,
            builder_config=builder_config,
        )
        
        # 获取要卖出的token_id
        outcome = position["outcome"]
        token_ids = market.get("clobTokenIds") or market.get("clob_token_ids")
        if not token_ids or not isinstance(token_ids, list) or len(token_ids) < 2:
            print("无法获取token_id，无法卖出")
            return False
        
        token_id = token_ids[0] if outcome.lower() == "yes" else token_ids[1]
        quantity = position["quantity"]
        price = profit_info["current_price"]
        
        # 提交卖单
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=quantity,
            side=SELL,
        )
        order_response = client.create_and_post_order(order_args)
        
        if order_response.get("success") and order_response.get("status") == "matched":
            print(f"止盈成功: {position['market_question']}, 收益: {profit_info['current_profit']:.4f} USDC")
            return True
        else:
            print(f"止盈失败: {order_response.get('errorMsg', '未知错误')}")
            return False
    except Exception as e:
        print(f"执行卖出操作失败: {e}")
        return False


def run_take_profit(dry_run: bool = False) -> List[Dict[str, Any]]:
    """执行止盈逻辑，返回所有止盈成功的记录"""
    funder_address = os.getenv("FUNDER_ADDRESS")
    if not funder_address:
        print("缺少FUNDER_ADDRESS环境变量")
        return []
    
    try:
        positions = get_all_positions(funder_address)
    except Exception as e:
        print(f"获取持仓失败: {e}")
        return []
    
    # 筛选可止盈标的
    profit_candidates: List[Dict[str, Any]] = []
    for pos in positions:
        if pos["quantity"] <= 0 or pos["redeemable"]:
            continue  # 空仓或者已经可以领取的跳过
        
        market = _get_market_details(pos["condition_id"])
        if not market:
            continue
        
        profit_info = _calculate_profit(pos, market)
        if profit_info["should_take_profit"]:
            profit_candidates.append({
                "position": pos,
                "market": market,
                "profit_info": profit_info
            })
    
    # 按剩余到期时间从长到短排序，优先止盈时间远的
    profit_candidates.sort(key=lambda x: x["profit_info"]["days_remaining"], reverse=True)
    
    # 执行止盈
    success_profits: List[Dict[str, Any]] = []
    total_profit = 0.0
    for candidate in profit_candidates:
        pos = candidate["position"]
        profit_info = candidate["profit_info"]
        if dry_run:
            print(f"模拟止盈: {pos['market_question'][:50]}..., 收益: {profit_info['current_profit']:.4f} USDC, 剩余天数: {profit_info['days_remaining']:.1f}")
            success_profits.append(candidate)
            total_profit += profit_info["current_profit"]
            continue
        
        if _execute_sell(pos, candidate["market"], profit_info):
            success_profits.append(candidate)
            total_profit += profit_info["current_profit"]
    
    # 发送钉钉通知
    if success_profits:
        content = f"📈 Polymarket自动止盈通知\n✅ 本次共止盈{len(success_profits)}笔持仓，总收益：{total_profit:.4f} USDC\n\n明细：\n"
        for idx, cand in enumerate(success_profits, 1):
            pos = cand["position"]
            profit = cand["profit_info"]
            content += f"{idx}. {pos['market_question'][:60]}\n   收益：{profit['current_profit']:.4f} USDC，收益率：{profit['profit_rate']*100:.1f}%\n   剩余到期天数：{profit['days_remaining']:.1f}天\n\n"
        send_dingtalk_notification(content)
    
    return success_profits


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="自动止盈脚本：达到理论收益60%时优先止盈到期时间远的持仓",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="模拟运行，不实际执行卖出操作",
    )
    return parser.parse_args()


def _main() -> int:
    args = _parse_args()
    success = run_take_profit(dry_run=args.dry_run)
    print(f"止盈完成，共成功止盈{len(success)}笔持仓")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
