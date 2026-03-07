"""CLI helper for listing active Polymarket markets via the Gamma markets API.

This script fetches all *active* and *not closed* markets from
``https://gamma-api.polymarket.com/markets`` and prints a concise summary
per market, sorted by the "Yes" probability.

Key features
------------

* Uses the Gamma ``/markets`` endpoint with ``active=true&closed=false``.
* Computes the Yes/No probabilities using ``outcomePrices`` when
  available; otherwise falls back to ``lastTradePrice`` as an estimate.
* Supports sorting in ascending or descending order by Yes probability.
* Supports a configurable output limit (default: 100 rows).
* Designed to be safe and CLI-friendly: missing fields are handled
  gracefully and rendered as ``N/A`` when appropriate.

Usage examples
--------------

::

    # 默认：按 Yes 概率降序显示前 100 条
    python get_markets.py

    # 按 Yes 概率升序显示前 20 条
    python get_markets.py -n 20 --asc

The module can also be imported and the :func:`list_active_markets`
function reused from other Python code.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests


GAMMA_API_BASE_URL = "https://gamma-api.polymarket.com"
MARKETS_ENDPOINT = f"{GAMMA_API_BASE_URL}/markets"

# Default number of rows to display in the CLI
DEFAULT_LIMIT = 100

# Pagination settings for the Gamma markets endpoint. In practice the
# number of active markets is modest, but we keep this configurable and
# robust to future growth.
MAX_PAGE_SIZE = 500
MAX_PAGES = 20
REQUEST_TIMEOUT = 10  # seconds


class PolymarketGammaAPIError(RuntimeError):
    """Raised when the Polymarket Gamma markets API fails or returns bad data."""


@dataclass
class MarketView:
    """Typed, CLI-friendly view of a single Gamma market entry."""

    title: str
    question: str
    yes_prob: Optional[float]
    no_prob: Optional[float]
    volume: float
    liquidity: float
    last_price: Optional[float]
    best_bid: Optional[float]
    best_ask: Optional[float]
    token_yes: Optional[str]
    token_no: Optional[str]
    end_date: Optional[str]

    @property
    def display_title(self) -> str:
        """Return a human-friendly title for CLI display."""

        return self.title or self.question


def _to_float(value: Any, default: float = 0.0) -> float:
    """Best-effort conversion to ``float``.

    Strings, ints, and floats are accepted. On failure, ``default`` is
    returned.
    """

    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_optional_float(value: Any) -> Optional[float]:
    """Best-effort conversion to ``float``, returning ``None`` on failure."""

    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result):  # pragma: no cover - defensive
        return None
    return result


def _ensure_list(value: Any) -> Optional[List[Any]]:
    """Normalize a value that may be a list, tuple or JSON-encoded string.

    Gamma fields such as ``outcomePrices``, ``shortOutcomes`` and
    ``clobTokenIds`` can be either arrays or JSON-encoded arrays in a
    string. This helper normalizes them into a Python list.
    """

    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Prefer JSON decoding, with a fallback to simple comma-separated
        # lists for robustness.
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parts = [part.strip() for part in text.split(",") if part.strip()]
            return parts or None
        else:
            if isinstance(parsed, (list, tuple)):
                return list(parsed)
            # Single scalar value
            return [parsed]
    return None


def _parse_outcome_prices(raw: Any) -> Optional[List[float]]:
    """Parse the ``outcomePrices`` field into a list of floats, if possible."""

    seq = _ensure_list(raw)
    if not seq:
        return None

    prices: List[float] = []
    for item in seq:
        try:
            prices.append(float(item))
        except (TypeError, ValueError):
            continue

    return prices or None


def _parse_outcomes(raw: Any) -> Optional[List[str]]:
    """Parse the outcomes/shortOutcomes field into a list of strings."""

    seq = _ensure_list(raw)
    if not seq:
        return None
    return [str(item) for item in seq]


def _parse_clob_token_ids(raw: Any) -> List[str]:
    """Parse ``clobTokenIds`` into a list of token ID strings."""

    seq = _ensure_list(raw) or []
    return [str(item) for item in seq]


def _compute_yes_no_probs(market: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Compute Yes/No probabilities for a given market.

    Priority:

    1. Use ``outcomePrices`` when available, trying to locate the
       ``"Yes"`` and ``"No"`` outcomes.
    2. If not available or not usable, fall back to ``lastTradePrice`` as
       a Yes probability estimate and derive No as ``1 - Yes``.

    Values are normalized into the ``[0, 1]`` range. When a probability
    cannot be derived, ``None`` is returned for that side.
    """

    prices = _parse_outcome_prices(
        market.get("outcomePrices")
        or market.get("outcome_prices")
    )

    outcomes = _parse_outcomes(
        market.get("outcomes")
        or market.get("shortOutcomes")
        or market.get("short_outcomes")
    )

    last_price = _to_optional_float(
        market.get("lastTradePrice") or market.get("last_trade_price")
    )

    yes_prob: Optional[float] = None
    no_prob: Optional[float] = None

    yes_index: Optional[int] = None
    no_index: Optional[int] = None

    if outcomes:
        for idx, name in enumerate(outcomes):
            lowered = name.strip().lower()
            if lowered == "yes":
                yes_index = idx
            elif lowered == "no":
                no_index = idx

    # If we have exactly two outcomes but no explicit "Yes" label,
    # assume index 0 is the "Yes"-like outcome as a reasonable
    # fallback.
    if yes_index is None and outcomes and len(outcomes) == 2:
        yes_index = 0
        if no_index is None:
            no_index = 1

    if prices is not None and yes_index is not None and 0 <= yes_index < len(prices):
        try:
            yes_prob = float(prices[yes_index])
        except (TypeError, ValueError):
            yes_prob = None

        if no_index is not None and 0 <= no_index < len(prices):
            try:
                no_prob = float(prices[no_index])
            except (TypeError, ValueError):
                no_prob = None
        elif yes_prob is not None:
            no_prob = 1.0 - yes_prob

    elif last_price is not None:
        yes_prob = last_price
        no_prob = 1.0 - last_price

    def _normalize(prob: Optional[float]) -> Optional[float]:
        if prob is None:
            return None
        try:
            p = float(prob)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return None
        if math.isnan(p):  # pragma: no cover - defensive
            return None
        # Some integrations may return 0-100; rescale if so.
        if p > 1.0 and p <= 100.0:
            p = p / 100.0
        # Clamp into [0, 1]
        if p < 0.0:
            p = 0.0
        elif p > 1.0:
            p = 1.0
        return p

    yes_prob = _normalize(yes_prob)
    no_prob = _normalize(no_prob)
    return yes_prob, no_prob


def _parse_market_item(item: Dict[str, Any]) -> MarketView:
    """Convert a raw Gamma market JSON object into :class:`MarketView`."""

    title = str(item.get("title") or "")
    question = str(item.get("question") or title)

    yes_prob, no_prob = _compute_yes_no_probs(item)

    last_price = _to_optional_float(
        item.get("lastTradePrice") or item.get("last_trade_price")
    )
    best_bid = _to_optional_float(item.get("bestBid") or item.get("best_bid"))
    best_ask = _to_optional_float(item.get("bestAsk") or item.get("best_ask"))

    volume_raw = (
        item.get("volume")
        or item.get("volume24hr")
        or item.get("volume_24hr")
    )
    volume = _to_float(volume_raw, default=0.0)

    liquidity_raw = (
        item.get("liquidity")
        or item.get("liquidity24hr")
        or item.get("liquidity_24hr")
    )
    liquidity = _to_float(liquidity_raw, default=0.0)

    token_ids = _parse_clob_token_ids(
        item.get("clobTokenIds") or item.get("clob_token_ids")
    )
    token_yes = token_ids[0] if len(token_ids) >= 1 else None
    token_no = token_ids[1] if len(token_ids) >= 2 else None

    end_date_raw = (
        item.get("endDate")
        or item.get("end_date")
        or item.get("closeDate")
        or item.get("close_date")
    )
    end_date = str(end_date_raw) if end_date_raw is not None else None

    return MarketView(
        title=title,
        question=question,
        yes_prob=yes_prob,
        no_prob=no_prob,
        volume=volume,
        liquidity=liquidity,
        last_price=last_price,
        best_bid=best_bid,
        best_ask=best_ask,
        token_yes=token_yes,
        token_no=token_no,
        end_date=end_date,
    )


def fetch_active_markets() -> List[Dict[str, Any]]:
    """Fetch all active, non-closed markets from Gamma ``/markets``.

    The function performs lightweight pagination using ``limit`` and
    ``offset`` parameters, and deduplicates markets by their ``id``
    field where available. This keeps HTTP usage modest while still
    being robust if the number of markets grows over time.

    Any HTTP or decoding issues are raised as
    :class:`PolymarketGammaAPIError`.
    """

    all_markets: List[Dict[str, Any]] = []
    seen_ids: set[Any] = set()

    offset = 0
    for _ in range(MAX_PAGES):
        params = {
            "active": "true",
            "closed": "false",
            "limit": MAX_PAGE_SIZE,
            "offset": offset,
        }
        try:
            resp = requests.get(MARKETS_ENDPOINT, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:  # 网络错误
            raise PolymarketGammaAPIError(f"调用 Gamma /markets 失败: {exc}") from exc

        if resp.status_code != 200:
            raise PolymarketGammaAPIError(
                f"Gamma /markets 返回非 200 状态码: {resp.status_code}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise PolymarketGammaAPIError("Gamma /markets 返回内容不是合法 JSON") from exc

        if not isinstance(data, list):
            raise PolymarketGammaAPIError("Gamma /markets 返回结构异常，预期为列表")

        if not data:
            break

        new_items = 0
        for item in data:
            if not isinstance(item, dict):
                continue
            market_id = item.get("id")
            if market_id is not None and market_id in seen_ids:
                continue
            all_markets.append(item)
            if market_id is not None:
                seen_ids.add(market_id)
            new_items += 1

        # If we didn't see any new items, or this page is smaller than
        # our requested page size, we assume we've reached the end.
        if new_items == 0 or len(data) < MAX_PAGE_SIZE:
            break

        offset += MAX_PAGE_SIZE

    return all_markets


def list_active_markets(limit: int = DEFAULT_LIMIT, ascending: bool = False) -> List[MarketView]:
    """Return active markets sorted by Yes probability.

    Parameters
    ----------
    limit:
        Maximum number of markets to return. Values ``<= 0`` fall back
        to :data:`DEFAULT_LIMIT`.
    ascending:
        If ``True``, sort by Yes probability in ascending order. If
        ``False`` (默认), sort in descending order.

    Returns
    -------
    list[MarketView]
        Parsed and sorted market views suitable for CLI or further
        processing.
    """

    raw_markets = fetch_active_markets()

    views: List[MarketView] = []
    for item in raw_markets:
        if not isinstance(item, dict):
            continue
        try:
            views.append(_parse_market_item(item))
        except Exception:  # pragma: no cover - defensive
            # Skip malformed entries but keep processing others.
            continue

    def sort_key(m: MarketView) -> Tuple[int, float]:
        p = m.yes_prob
        if not isinstance(p, (int, float)) or math.isnan(p):
            # Put markets without a usable Yes probability at the end.
            return (1, 0.0)
        return (0, float(p) if ascending else -float(p))

    views.sort(key=sort_key)

    if limit is None or limit <= 0:
        limit = DEFAULT_LIMIT

    if limit and limit < len(views):
        views = views[:limit]

    return views


def _format_percent(value: Optional[float]) -> str:
    """Format a probability (0-1) as a percentage with one decimal place."""

    if not isinstance(value, (int, float)) or math.isnan(value):
        return "N/A"
    return f"{float(value) * 100:.1f}%"


def _format_price(value: Optional[float]) -> str:
    """Format a price field with three decimal places."""

    if not isinstance(value, (int, float)) or math.isnan(value):
        return "N/A"
    return f"{float(value):.3f}"


def _format_currency(amount: float) -> str:
    """Format a monetary value as dollars with thousand separators."""

    # Treat negative/zero amounts as 0 for display purposes.
    if not isinstance(amount, (int, float)) or math.isnan(amount) or amount <= 0:
        return "$0.00"
    return f"${float(amount):,.2f}"


def format_market_for_cli(market: MarketView) -> str:
    """Format a :class:`MarketView` as a single CLI line.

    The line is composed of key fields separated strictly by ``" | "``
    as requested.
    """

    title = market.display_title
    yes_str = _format_percent(market.yes_prob)
    no_str = _format_percent(market.no_prob)
    volume_str = _format_currency(market.volume)
    liquidity_str = _format_currency(market.liquidity)
    last_str = _format_price(market.last_price)
    best_bid_str = _format_price(market.best_bid)
    best_ask_str = _format_price(market.best_ask)
    token_yes = market.token_yes or "N/A"
    token_no = market.token_no or "N/A"
    end_date = market.end_date or ""

    parts = [
        f"title: {title}",
        f"Yes: {yes_str}",
        f"No: {no_str}",
        f"Volume: {volume_str}",
        f"Liquidity: {liquidity_str}",
        f"Last: {last_str}",
        f"token_id(Yes): {token_yes}",
        f"token_id(No): {token_no}",
        f"bestBid: {best_bid_str}",
        f"bestAsk: {best_ask_str}",
        f"endDate: {end_date}",
    ]

    return " | ".join(parts)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Fetch active markets from Gamma and print them line by line.
    """

    parser = argparse.ArgumentParser(
        description=(
            "列出所有活跃未关闭的 Polymarket 市场，按 Yes 概率排序并输出关键信息。"
        )
    )
    parser.add_argument(
        "-n",
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="要展示的市场数量上限（默认 100）。",
    )

    order_group = parser.add_mutually_exclusive_group()
    order_group.add_argument(
        "--asc",
        action="store_true",
        help="按 Yes 概率升序排序。",
    )
    order_group.add_argument(
        "--desc",
        action="store_true",
        help="按 Yes 概率降序排序（默认）。",
    )

    args = parser.parse_args(argv)

    limit = args.limit if args.limit and args.limit > 0 else DEFAULT_LIMIT

    # 默认按 Yes 概率降序排序
    if args.asc:
        ascending = True
    else:
        ascending = False

    try:
        markets = list_active_markets(limit=limit, ascending=ascending)
    except PolymarketGammaAPIError as exc:
        print(f"Gamma API error: {exc}")
        return 1

    if not markets:
        print("未找到活跃未关闭的市场。")
        return 0

    for m in markets:
        print(format_market_for_cli(m))

    return 0


if __name__ == "__main__":  # CLI入口
    import sys

    raise SystemExit(main())

