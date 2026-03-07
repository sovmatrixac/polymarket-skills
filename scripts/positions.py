"""Utilities for querying Polymarket positions via the public Data API.

This module provides a single high-level function:

    get_all_positions(user_address: str) -> list[dict]

It validates the provided EVM address, calls Polymarket's Data API
(https://data-api.polymarket.com/positions), and normalizes the response
into a list of dictionaries with friendly field names.

The dictionaries are intended to be convenient for both human inspection
and downstream programmatic use (for example, claim automation).

The output structure is a cleaned-up list of dicts.
Each item contains at least the following keys:

    - market_question
    - outcome
    - quantity
    - avg_price
    - current_value

To support claim logic, several additional fields are also exposed:

    - condition_id
    - asset
    - redeemable
    - negative_risk
    - outcome_index

Address validation errors and network/API errors are raised as Python
exceptions so that callers can distinguish between them and handle
appropriately.

A small CLI is provided under ``if __name__ == "__main__"`` which reads a
default address from the environment (FUNDER_ADDRESS / USER_ADDRESS) via
``python-dotenv`` and prints a human-readable summary of all positions.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv


DATA_API_BASE_URL = "https://data-api.polymarket.com"
POSITIONS_ENDPOINT = f"{DATA_API_BASE_URL}/positions"


class InvalidAddressError(ValueError):
    """Raised when a provided wallet address is not a valid EVM address."""


class PolymarketAPIError(RuntimeError):
    """Raised when the Polymarket Data API request fails or returns bad data."""


def _normalize_address(address: str) -> str:
    """Return a checks-style normalized hex address string.

    Normalization here is minimal: we ensure a ``0x`` prefix and lowercase
    hex characters; we do *not* attempt EIP-55 checksumming to avoid
    introducing extra dependencies.
    """

    addr = address.strip()
    if not addr:
        return addr
    if not addr.startswith("0x") and not addr.startswith("0X"):
        addr = "0x" + addr
    return addr.lower()


def is_valid_evm_address(address: str) -> bool:
    """Lightweight EVM address validation.

    Requirements:
    - starts with ``0x``
    - 42 characters total length
    - remaining 40 characters are hex digits
    """

    addr = _normalize_address(address)
    if not addr.startswith("0x"):
        return False
    if len(addr) != 42:
        return False
    hex_part = addr[2:]
    try:
        int(hex_part, 16)
    except ValueError:
        return False
    return True


@dataclass
class Position:
    """Typed representation of a single Polymarket position entry.

    This class is internal to this module. Callers typically work with the
    dictionary form returned by :func:`get_all_positions`.
    """

    market_question: str
    outcome: str
    quantity: float
    avg_price: float
    current_value: float
    condition_id: str | None
    asset: str | None
    redeemable: bool
    negative_risk: bool
    outcome_index: int | None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to the public dictionary representation."""

        return {
            "market_question": self.market_question,
            "outcome": self.outcome,
            "quantity": self.quantity,
            "avg_price": self.avg_price,
            "current_value": self.current_value,
            "condition_id": self.condition_id,
            "asset": self.asset,
            "redeemable": self.redeemable,
            "negative_risk": self.negative_risk,
            "outcome_index": self.outcome_index,
        }


def _parse_position_item(item: Dict[str, Any]) -> Position:
    """Parse a raw JSON object from /positions into a :class:`Position`.

    The Polymarket Data API schema for ``GET /positions`` (current positions
    for a user) is documented at:

        https://docs.polymarket.com/api-reference/core/get-current-positions-for-a-user

    Example response item fields used here:

        {
          "proxyWallet": "0x...",
          "asset": "<string>",
          "conditionId": "0x...",
          "size": 123,
          "avgPrice": 123,
          "currentValue": 123,
          "title": "<string>",
          "outcome": "<string>",
          "outcomeIndex": 0,
          "redeemable": true,
          "negativeRisk": true,
          ...
        }
    """

    title = item.get("title") or ""
    outcome = item.get("outcome") or ""

    size = item.get("size")
    try:
        quantity = float(size) if size is not None else 0.0
    except (TypeError, ValueError):
        quantity = 0.0

    avg_price_raw = item.get("avgPrice")
    try:
        avg_price = float(avg_price_raw) if avg_price_raw is not None else 0.0
    except (TypeError, ValueError):
        avg_price = 0.0

    current_value_raw = item.get("currentValue")
    try:
        current_value = (
            float(current_value_raw) if current_value_raw is not None else 0.0
        )
    except (TypeError, ValueError):
        current_value = 0.0

    condition_id = item.get("conditionId") or item.get("condition_id")
    asset = item.get("asset")
    redeemable = bool(item.get("redeemable", False))
    negative_risk = bool(item.get("negativeRisk", False))

    outcome_index_raw = item.get("outcomeIndex") or item.get("outcome_index")
    try:
        outcome_index = int(outcome_index_raw) if outcome_index_raw is not None else None
    except (TypeError, ValueError):
        outcome_index = None

    return Position(
        market_question=title,
        outcome=outcome,
        quantity=quantity,
        avg_price=avg_price,
        current_value=current_value,
        condition_id=condition_id,
        asset=asset,
        redeemable=redeemable,
        negative_risk=negative_risk,
        outcome_index=outcome_index,
    )


def get_all_positions(user_address: str) -> List[Dict[str, Any]]:
    """Fetch and normalize all Polymarket positions for a given address.

    Parameters
    ----------
    user_address:
        Polygon/EVM wallet address. Both ``0x``-prefixed and plain hex
        strings are accepted; the function normalizes them internally.

    Returns
    -------
    list[dict]
        A list of dictionaries, each representing one position, with at
        least the following keys:

        - ``market_question``
        - ``outcome``
        - ``quantity``
        - ``avg_price``
        - ``current_value``
        - ``condition_id``
        - ``asset``
        - ``redeemable``
        - ``negative_risk``
        - ``outcome_index``

    Raises
    ------
    InvalidAddressError
        If ``user_address`` is not a valid EVM address.
    PolymarketAPIError
        If the HTTP request fails, the API responds with a non-200 status
        code, or the response body cannot be parsed.
    """

    normalized = _normalize_address(user_address)
    if not is_valid_evm_address(normalized):
        raise InvalidAddressError(f"钱包地址格式无效: {user_address!r}")

    params = {
        "user": normalized,
        # Reasonable defaults; callers can filter/re-sort in memory.
        "limit": 500,
        "sizeThreshold": 0,
        "sortBy": "TOKENS",
        "sortDirection": "DESC",
    }

    try:
        resp = requests.get(POSITIONS_ENDPOINT, params=params, timeout=10)
    except requests.RequestException as exc:  # 网络错误
        raise PolymarketAPIError(f"调用 Polymarket Data API 失败: {exc}") from exc

    if resp.status_code != 200:
        raise PolymarketAPIError(
            f"Polymarket Data API 返回非 200 状态码: {resp.status_code}"
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise PolymarketAPIError("Polymarket Data API 返回内容不是合法 JSON") from exc

    if not isinstance(data, list):
        raise PolymarketAPIError("Polymarket Data API 返回结构异常，预期为列表")

    positions: List[Dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            # 忽略格式异常的条目，但不中断整个列表
            continue
        pos = _parse_position_item(item)
        positions.append(pos.to_dict())

    return positions


def _format_positions_for_cli(positions: List[Dict[str, Any]]) -> str:
    """Format positions as a simple human-readable table for CLI output."""

    if not positions:
        return "该地址下无持仓记录。"

    headers = [
        "市场问题",
        "结果",
        "数量",
        "均价",
        "当前价值",
        "可领取",
    ]
    rows: List[List[str]] = []
    for p in positions:
        rows.append(
            [
                str(p.get("market_question", ""))[:60],
                str(p.get("outcome", "")),
                f"{p.get('quantity', 0):.4f}",
                f"{p.get('avg_price', 0):.4f}",
                f"{p.get('current_value', 0):.4f}",
                "是" if p.get("redeemable") else "否",
            ]
        )

    # Compute column widths
    col_widths = [len(h) for h in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            col_widths[idx] = max(col_widths[idx], len(cell))

    def fmt_row(cols: List[str]) -> str:
        return " | ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(cols))

    sep = "-+-".join("-" * w for w in col_widths)
    lines = [fmt_row(headers), sep]
    for row in rows:
        lines.append(fmt_row(row))

    return "\n".join(lines)


if __name__ == "__main__":  # 简单 CLI 示例
    load_dotenv()

    # 优先使用命令行参数中的地址，其次从 .env 中读取默认地址
    cli_address = sys.argv[1] if len(sys.argv) > 1 else None
    env_address = os.getenv("FUNDER_ADDRESS") or os.getenv("USER_ADDRESS")
    address = cli_address or env_address

    if not address:
        print(
            "未提供钱包地址。请在命令行参数中传入地址，或在 .env 中设置 FUNDER_ADDRESS / USER_ADDRESS。"
        )
        sys.exit(1)

    try:
        positions = get_all_positions(address)
    except InvalidAddressError as exc:
        print(f"Invalid address : {exc}")
        sys.exit(1)
    except PolymarketAPIError as exc:
        print(f"API error: {exc}")
        sys.exit(1)

    print(_format_positions_for_cli(positions))

