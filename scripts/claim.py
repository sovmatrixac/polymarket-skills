"""Tools for claiming Polymarket winnings (gasless first, on-chain as fallback).

The main entry point is::

    claim_all_winnings(
        private_key: str,
        user_address: str,
        dry_run: bool = True,
        fallback_rpc_url: str | None = None,
        max_gas_price_gwei: int | None = None,
    ) -> dict

High-level behaviour
--------------------

* Always uses :mod:`positions` to discover positions for ``user_address``.
* Filters to ``redeemable`` positions as reported by the Data API.
* When ``dry_run`` is ``True`` (默认行为，安全优先):

  - **不会** 调用任何外部 builder/gasless 服务或链上 RPC；
  - 仅基于 Data API 结果汇总“待领取明细”和“总金额”；
  - 返回结构 ``{"success": [], "failed": [], "total_amount": float, "pending": [...]}``。

* When ``dry_run`` is ``False`` (执行模式):

  1. **主路径：gasless/builder**

     - 优先尝试使用 *gasless* 方案领取盈利；
     - 当前实现优先尝试集成 ``polymarket-apis`` 包中的
       ``PolymarketGaslessWeb3Client``（如已安装且配置完整）；
     - 需要 Builder API 凭证等配置，请通过环境变量提供：

       - ``POLY_BUILDER_API_KEY``
       - ``POLY_BUILDER_SECRET``
       - ``POLY_BUILDER_PASSPHRASE``

  2. **回退路径：链上 RPC + web3**

     - 若 gasless/builder 不可用或执行失败，且传入了
       ``fallback_rpc_url`` 且已安装 ``web3``，则尝试使用链上
       ``redeemPositions`` 调用进行领取；
     - 在链上模式下，如果设置了 ``max_gas_price_gwei``，则在
       gas 价格超过上限时跳过该笔领取，并记录失败原因。

* 无论使用何种路径，返回值均为::

    {
        "success": [...],   # 成功领取的条目
        "failed":  [...],   # 失败或被跳过的条目
        "total_amount": float,  # 所有可领取头寸的当前价值总和
        # 在 dry_run=True 时额外包含:
        # "pending": [...],
    }

The actual *on-chain* transactions are only sent when ``dry_run=False`` and
an appropriate execution path (gasless or on-chain fallback) is available.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from polymarket_apis.types.clob_types import ApiCreds

from dotenv import load_dotenv

from positions import (
    PolymarketAPIError,
    InvalidAddressError,
    _normalize_address,  # type: ignore[attr-defined]
    is_valid_evm_address,
    get_all_positions,
)


# 常量：Polygon 主网 USDC.e 与 Conditional Tokens Framework (CTF) 地址
POLYGON_CHAIN_ID = 137
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
ZERO_BYTES32 = (
    "0x0000000000000000000000000000000000000000000000000000000000000000"
)


@dataclass
class ClaimablePosition:
    """A lightweight view over a redeemable position for claim logic."""

    market_question: str
    outcome: str
    quantity: float
    current_value: float
    condition_id: str
    asset: Optional[str]
    negative_risk: bool
    outcome_index: Optional[int]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ClaimablePosition":
        condition_id = data.get("condition_id") or data.get("conditionId")
        if not condition_id:
            raise ValueError("缺少 condition_id，无法用于 claim")

        return cls(
            market_question=str(data.get("market_question", "")),
            outcome=str(data.get("outcome", "")),
            quantity=float(data.get("quantity", 0.0) or 0.0),
            current_value=float(data.get("current_value", 0.0) or 0.0),
            condition_id=str(condition_id),
            asset=(str(data["asset"]) if data.get("asset") is not None else None),
            negative_risk=bool(data.get("negative_risk", False)),
            outcome_index=(
                int(data["outcome_index"])
                if data.get("outcome_index") is not None
                else None
            ),
        )

    def to_summary_dict(self) -> Dict[str, Any]:
        """Return a compact dict suitable for API/CLI output."""

        return {
            "market_question": self.market_question,
            "outcome": self.outcome,
            "quantity": self.quantity,
            "current_value": self.current_value,
            "condition_id": self.condition_id,
            "asset": self.asset,
            "negative_risk": self.negative_risk,
            "outcome_index": self.outcome_index,
        }


def _normalize_private_key(private_key: str) -> str:
    pk = private_key.strip()
    if not pk:
        raise ValueError("私钥不能为空")
    if not pk.startswith("0x") and not pk.startswith("0X"):
        pk = "0x" + pk
    return pk


def _collect_redeemable_positions(user_address: str) -> Tuple[List[ClaimablePosition], float]:
    """Fetch all positions and filter down to redeemable ones.

    Returns a list of :class:`ClaimablePosition` and the total current
    value of all redeemable positions.
    """

    raw_positions = get_all_positions(user_address)

    redeemable: List[ClaimablePosition] = []
    total_value = 0.0
    for item in raw_positions:
        if not isinstance(item, dict):
            continue
        if not item.get("redeemable"):
            continue
        try:
            pos = ClaimablePosition.from_dict(item)
        except ValueError:
            # 对于缺少关键字段的条目，直接跳过
            continue
        if pos.quantity <= 0:
            continue
        redeemable.append(pos)
        total_value += pos.current_value

    return redeemable, float(total_value)


def _group_by_condition(
    positions: Iterable[ClaimablePosition],
) -> Dict[str, List[ClaimablePosition]]:
    grouped: Dict[str, List[ClaimablePosition]] = {}
    for pos in positions:
        grouped.setdefault(pos.condition_id, []).append(pos)
    return grouped


def _claim_via_gasless_builder(
    private_key: str,
    grouped_by_condition: Dict[str, List[ClaimablePosition]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Attempt to claim via gasless/builder path using polymarket-apis.

    This implementation is intentionally defensive:

    * The dependency on ``polymarket-apis`` is optional. If the package is
      not installed or misconfigured, a RuntimeError is raised to allow the
      caller to fall back to the on-chain path.
    * It assumes mostly binary markets (YES/NO) when constructing the
      "amounts" array used by ``PolymarketGaslessWeb3Client.redeem_position``.
      For more complex multi-outcome markets, CTF still correctly redeems
      based on the user's balances, but some edge-cases may require
      refinement.
    """

    try:
        from polymarket_apis import PolymarketGaslessWeb3Client  # type: ignore
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError(
            "polymarket-apis 未安装，无法使用 gasless/builder 路径。"
        ) from exc

    pk = _normalize_private_key(private_key)

    # 默认使用签名类型 1（Email/Proxy 钱包），可通过环境变量覆盖。
    signature_type_str = os.getenv("POLY_SIGNATURE_TYPE", "1")
    try:
        signature_type = int(signature_type_str)
    except ValueError:
        signature_type = 1

    chain_id_str = os.getenv("POLYGON_CHAIN_ID", str(POLYGON_CHAIN_ID))
    try:
        chain_id = int(chain_id_str)
    except ValueError:
        chain_id = POLYGON_CHAIN_ID

    builder_creds = ApiCreds(
        key=os.getenv("POLY_BUILDER_API_KEY"),
        secret=os.getenv("POLY_BUILDER_SECRET"),
        passphrase=os.getenv("POLY_BUILDER_PASSPHRASE")
    )

    # 出于安全考虑，不在日志中打印 Builder API 凭证，仅确认已加载。
    print("已加载 Builder API 凭证 (POLY_BUILDER_API_KEY / SECRET / PASSPHRASE)。")

    client = PolymarketGaslessWeb3Client(
        private_key=pk,
        signature_type=signature_type,
        chain_id=chain_id,
        builder_creds=builder_creds,
    )

    success: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    for condition_id, positions in grouped_by_condition.items():
        # 构造 amounts 数组 [yes_amount, no_amount]，主要适配二元市场。
        amounts = [0.0, 0.0]
        neg_risk = any(p.negative_risk for p in positions)

        for pos in positions:
            if pos.outcome_index is None:
                continue
            if pos.outcome_index in (0, 1):
                amounts[pos.outcome_index] += pos.quantity

        try:
            # polymarket-apis 内部会构造并通过 Relayer 提交 gasless 交易。
            result = client.redeem_position(
                condition_id=condition_id,
                amounts=amounts,
                neg_risk=neg_risk,
            )
            success.append(
                {
                    "condition_id": condition_id,
                    "mode": "gasless",
                    "result": str(result),
                }
            )
        except Exception as exc:  # pragma: no cover - 外部依赖行为
            failed.append(
                {
                    "condition_id": condition_id,
                    "mode": "gasless",
                    "error": str(exc),
                }
            )

    return success, failed


def _claim_via_onchain_web3(
    private_key: str,
    grouped_by_condition: Dict[str, List[ClaimablePosition]],
    rpc_url: str,
    max_gas_price_gwei: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Claim winnings directly on-chain using Web3 + RPC fallback.

    This path is only used when gasless/builder is unavailable or fails and
    a fallback RPC URL is provided. It assumes ``web3`` is installed.
    """

    try:
        from web3 import Web3  # type: ignore
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise RuntimeError(
            "web3 未安装，无法使用链上回退模式。请安装 web3 或仅使用 gasless/builder 路径。"
        ) from exc

    if not rpc_url:
        raise RuntimeError("未提供 fallback_rpc_url，无法使用链上回退模式。")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():  # pragma: no cover - 网络相关
        raise RuntimeError("无法连接到提供的 Polygon RPC 节点。")

    pk = _normalize_private_key(private_key)
    account = w3.eth.account.from_key(pk)

    ctf_abi = [
        {
            "name": "redeemPositions",
            "type": "function",
            "stateMutability": "nonpayable",
            "inputs": [
                {"name": "collateralToken", "type": "address"},
                {"name": "parentCollectionId", "type": "bytes32"},
                {"name": "conditionId", "type": "bytes32"},
                {"name": "indexSets", "type": "uint256[]"},
            ],
            "outputs": [],
        }
    ]

    ctf_contract = w3.eth.contract(address=CTF_ADDRESS, abi=ctf_abi)

    success: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []

    nonce = w3.eth.get_transaction_count(account.address)

    for condition_id, positions in grouped_by_condition.items():
        # 构造 indexSets：对每个 outcome_index 取 1 << index，适配多 outcome 市场。
        index_sets = set()
        for pos in positions:
            if pos.outcome_index is None:
                continue
            if pos.outcome_index >= 0:
                index_sets.add(1 << pos.outcome_index)

        if not index_sets:
            # 回退为典型的二元市场 [1, 2]
            index_sets = {1, 2}

        gas_price = w3.eth.gas_price
        if max_gas_price_gwei is not None:
            max_price_wei = int(max_gas_price_gwei) * 10**9
            if gas_price > max_price_wei:
                failed.append(
                    {
                        "condition_id": condition_id,
                        "mode": "onchain",
                        "error": (
                            "当前 gas price 超过 max_gas_price_gwei 限制，跳过该笔领取"
                        ),
                    }
                )
                continue

        try:
            tx = ctf_contract.functions.redeemPositions(
                USDC_E_ADDRESS,
                ZERO_BYTES32,
                condition_id,
                sorted(index_sets),
            ).build_transaction(
                {
                    "from": account.address,
                    "nonce": nonce,
                    "gasPrice": gas_price,
                    "chainId": w3.eth.chain_id,
                }
            )

            # 估算 gas，上限兜底。
            try:
                gas_estimate = w3.eth.estimate_gas(tx)
                tx["gas"] = int(gas_estimate * 1.2)
            except Exception:  # pragma: no cover - 估算依赖链上状态
                tx["gas"] = 500_000

            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            nonce += 1

            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=600)
            if receipt.status == 1:
                success.append(
                    {
                        "condition_id": condition_id,
                        "mode": "onchain",
                        "tx_hash": tx_hash.hex(),
                    }
                )
            else:
                failed.append(
                    {
                        "condition_id": condition_id,
                        "mode": "onchain",
                        "tx_hash": tx_hash.hex(),
                        "error": "交易执行失败（status != 1）",
                    }
                )
        except Exception as exc:  # pragma: no cover - 链上行为
            failed.append(
                {
                    "condition_id": condition_id,
                    "mode": "onchain",
                    "error": str(exc),
                }
            )

    return success, failed


def claim_all_winnings(
    private_key: str,
    user_address: str,
    dry_run: bool = True,
    fallback_rpc_url: Optional[str] = None,
    max_gas_price_gwei: Optional[int] = None,
) -> Dict[str, Any]:
    """Claim all redeemable Polymarket winnings for a given user.

    Parameters
    ----------
    private_key:
        Hex-encoded private key for the wallet controlling ``user_address``.
        Both with and without ``0x`` prefix are accepted.
    user_address:
        Wallet address whose positions should be claimed.
    dry_run:
        If ``True`` (默认), **不发送任何外部 claim 调用**，仅基于 Data API
        结果计算“待领取明细”和“总金额”。
    fallback_rpc_url:
        Optional Polygon RPC URL used **only** when gasless/builder is not
        available or fails. If not provided, on-chain fallback is skipped.
    max_gas_price_gwei:
        Optional gas price cap in gwei. Only applied in the on-chain fallback
        path. When set and the current gas price exceeds the cap, the
        corresponding claim is skipped and recorded as failed.

    Returns
    -------
    dict
        A dictionary with the following keys:

        - ``success``: list of successfully claimed entries (empty in dry-run)
        - ``failed``: list of failed or skipped entries
        - ``total_amount``: float sum of ``current_value`` for all
          redeemable positions
        - ``pending``: (仅在 ``dry_run=True`` 时) 列出所有可领取头寸的概要信息
    """

    load_dotenv()

    normalized_address = _normalize_address(user_address)
    if not is_valid_evm_address(normalized_address):
        raise InvalidAddressError(f"钱包地址格式无效: {user_address!r}")

    _ = _normalize_private_key(private_key)  # 仅做早期校验

    try:
        redeemable_positions, total_value = _collect_redeemable_positions(
            normalized_address
        )
    except PolymarketAPIError as exc:
        return {
            "success": [],
            "failed": [
                {
                    "condition_id": None,
                    "mode": "data-api",
                    "error": str(exc),
                }
            ],
            "total_amount": 0.0,
        }

    if not redeemable_positions:
        return {"success": [], "failed": [], "total_amount": 0.0}

    if dry_run:
        # 仅返回计划领取的明细和总金额，不进行任何外部调用。
        pending = [p.to_summary_dict() for p in redeemable_positions]
        return {
            "success": [],
            "failed": [],
            "total_amount": total_value,
            "pending": pending,
        }

    grouped = _group_by_condition(redeemable_positions)

    all_success: List[Dict[str, Any]] = []
    all_failed: List[Dict[str, Any]] = []

    # 1) 优先尝试 gasless/builder 路径
    gasless_success: List[Dict[str, Any]] = []
    gasless_failed: List[Dict[str, Any]] = []
    gasless_error: Optional[str] = None

    try:
        gasless_success, gasless_failed = _claim_via_gasless_builder(
            private_key, grouped
        )
    except RuntimeError as exc:
        gasless_error = str(exc)
        print(f"Gasless/builder 路径不可用，原因: {gasless_error}")

    all_success.extend(gasless_success)
    all_failed.extend(gasless_failed)

    handled_conditions = {entry["condition_id"] for entry in gasless_success}

    # 2) 对于尚未成功的条件，尝试链上回退路径（如果配置了 fallback_rpc_url）
    remaining_for_onchain: Dict[str, List[ClaimablePosition]] = {}
    for condition_id, positions in grouped.items():
        if condition_id not in handled_conditions:
            remaining_for_onchain[condition_id] = positions

    if remaining_for_onchain and fallback_rpc_url:
        try:
            onchain_success, onchain_failed = _claim_via_onchain_web3(
                private_key,
                remaining_for_onchain,
                rpc_url=fallback_rpc_url,
                max_gas_price_gwei=max_gas_price_gwei,
            )
            all_success.extend(onchain_success)
            all_failed.extend(onchain_failed)
        except RuntimeError as exc:
            # 链上回退整体失败，记录一条汇总错误。
            all_failed.append(
                {
                    "condition_id": None,
                    "mode": "onchain",
                    "error": str(exc),
                }
            )

    # 如果 gasless 一开始就不可用，将该信息也体现在 failed 列表中，方便调试。
    if gasless_error is not None and not gasless_failed:
        all_failed.append(
            {
                "condition_id": None,
                "mode": "gasless",
                "error": gasless_error,
            }
        )

    return {
        "success": all_success,
        "failed": all_failed,
        "total_amount": total_value,
    }


if __name__ == "__main__":  # 简单 CLI 示例
    import argparse
    import json

    load_dotenv()

    parser = argparse.ArgumentParser(
        description=(
            "一键 Claim 所有可领取盈利（默认 dry-run，只展示将要领取的明细和总金额）。"
        )
    )
    parser.add_argument(
        "--user-address",
        "--address",
        dest="user_address",
        default=os.getenv("FUNDER_ADDRESS") or os.getenv("USER_ADDRESS"),
        help=(
            "要查询和领取的地址，默认从环境变量 FUNDER_ADDRESS 或 USER_ADDRESS 读取。"
        ),
    )
    parser.add_argument(
        "--private-key",
        dest="private_key",
        default=os.getenv("PRIVATE_KEY"),
        help="用于签名的私钥，默认从环境变量 PRIVATE_KEY 读取。",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="实际发起 gasless / 链上 Claim，而不仅仅是 dry-run。",
    )
    parser.add_argument(
        "--fallback-rpc-url",
        dest="fallback_rpc_url",
        default=os.getenv("POLYGON_RPC_URL"),
        help="可选：链上回退模式使用的 Polygon RPC URL。",
    )
    parser.add_argument(
        "--max-gas-price-gwei",
        dest="max_gas_price_gwei",
        type=int,
        default=None,
        help="可选：仅在链上回退模式下生效的单笔 gas 价格上限（gwei）。",
    )

    args = parser.parse_args()

    if not args.user_address:
        parser.error(
            "必须通过 --user-address 或环境变量 FUNDER_ADDRESS / USER_ADDRESS 指定地址。"
        )
    if not args.private_key:
        parser.error(
            "必须通过 --private-key 或环境变量 PRIVATE_KEY 提供私钥。"
        )

    result = claim_all_winnings(
        private_key=args.private_key,
        user_address=args.user_address,
        dry_run=not args.execute,
        fallback_rpc_url=args.fallback_rpc_url,
        max_gas_price_gwei=args.max_gas_price_gwei,
    )

    print(json.dumps(result, ensure_ascii=False, indent=2))

