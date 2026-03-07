"""使用 Polymarket CLOB Python 客户端（Builder 模式）查询当前账户的 USDC 抵押余额和授权额度。

本脚本通过 L2 方法 ``get_balance_allowance`` 查询 CLOB 视角下的余额与 allowance，
默认资产类型为 ``COLLATERAL``（USDC 抵押品），链为 Polygon (chain_id=137)。

运行前置条件
------------

1. 安装依赖（示例）::

    pip install python-dotenv py_clob_client py_builder_signing_sdk

2. 在仓库根目录下配置 `.env` 文件或直接设置环境变量：

    - ``PRIVATE_KEY``: 用于签名的 EOA 私钥（十六进制字符串，建议带 0x 前缀）
    - ``FUNDER_ADDRESS``: Polymarket 代理钱包/资金地址（即在网站上看到的 Profile 地址）
    - ``POLY_BUILDER_API_KEY``: Builder API Key
    - ``POLY_BUILDER_SECRET``: Builder API Secret
    - ``POLY_BUILDER_PASSPHRASE``: Builder API Passphrase

   如无特殊需要，链与 CLOB Host 使用默认配置：

    - host: ``https://clob.polymarket.com``
    - chain_id: ``137`` (Polygon 主网)

用法示例
--------

无参数时，查询 COLLATERAL 余额与授权并以人类可读方式输出::

    python balanece.py

以 JSON 格式输出（便于脚本继续处理）::

    python balanece.py --json

"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_builder_signing_sdk.config import BuilderApiKeyCreds, BuilderConfig

# BalanceAllowanceParams / AssetType 在较新版本的 py_clob_client 中提供。
# 为了兼容旧版本，这里使用 try/except 在运行时优雅降级为 dict 调用。
try:  # pragma: no cover - 导入是否存在取决于安装的版本
    from py_clob_client.clob_types import BalanceAllowanceParams, AssetType  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001 - 兼容 ImportError / AttributeError 等
    BalanceAllowanceParams = None  # type: ignore[assignment]
    AssetType = None  # type: ignore[assignment]


# 默认 CLOB Host 与链 ID，可通过环境变量覆盖。
DEFAULT_HOST = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137  # Polygon Mainnet


@dataclass
class ClientConfig:
    """用于初始化 CLOB 客户端的配置。"""

    host: str
    chain_id: int
    private_key: str
    funder_address: str
    builder_api_key: str
    builder_secret: str
    builder_passphrase: str


def _load_client_config() -> ClientConfig:
    """从环境变量加载并校验 CLOB / Builder 相关配置。

    若关键环境变量缺失或为空，抛出 ValueError 以便上层捕获并打印友好错误信息。
    """

    host = os.getenv("POLY_CLOB_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST

    chain_id_raw = (
        os.getenv("POLY_CHAIN_ID")
        or os.getenv("CHAIN_ID")
        or str(DEFAULT_CHAIN_ID)
    )
    try:
        chain_id = int(chain_id_raw)
    except (TypeError, ValueError) as exc:  # pragma: no cover - 防御性
        raise ValueError(
            f"CHAIN_ID/POLY_CHAIN_ID 配置非法（当前值: {chain_id_raw!r}），"
            "请设置为整数，例如 137。"
        ) from exc

    private_key = (os.getenv("PRIVATE_KEY") or "").strip()
    funder_address = (os.getenv("FUNDER_ADDRESS") or "").strip()
    builder_api_key = (os.getenv("POLY_BUILDER_API_KEY") or "").strip()
    builder_secret = (os.getenv("POLY_BUILDER_SECRET") or "").strip()
    builder_passphrase = (os.getenv("POLY_BUILDER_PASSPHRASE") or "").strip()

    missing = []
    if not private_key:
        missing.append("PRIVATE_KEY")
    if not funder_address:
        missing.append("FUNDER_ADDRESS")
    if not builder_api_key:
        missing.append("POLY_BUILDER_API_KEY")
    if not builder_secret:
        missing.append("POLY_BUILDER_SECRET")
    if not builder_passphrase:
        missing.append("POLY_BUILDER_PASSPHRASE")

    if missing:
        raise ValueError(
            "缺少必需的环境变量: "
            + ", ".join(missing)
            + "。请在 .env 或系统环境中设置后重试。"
        )

    return ClientConfig(
        host=host,
        chain_id=chain_id,
        private_key=private_key,
        funder_address=funder_address,
        builder_api_key=builder_api_key,
        builder_secret=builder_secret,
        builder_passphrase=builder_passphrase,
    )


def _init_clob_client(cfg: ClientConfig) -> ClobClient:
    """按照 Builder 模式初始化 CLOB 客户端并返回。

    步骤：
    1. 使用 EOA 私钥与链 ID 创建临时客户端，派生/获取用户 L2 API 凭证；
    2. 基于 Builder API Key/Secret/Passphrase 构造 :class:`BuilderConfig`；
    3. 携带 L2 凭证 + BuilderConfig 初始化最终的 :class:`ClobClient`，
       设置 ``signature_type=1``（Email/Magic / Proxy 钱包）与 ``funder`` 地址。
    """

    # 1. 用户 L1 -> L2 凭证派生
    temp_client = ClobClient(cfg.host, key=cfg.private_key, chain_id=cfg.chain_id)
    user_api_creds = temp_client.create_or_derive_api_creds()

    # 2. Builder API 凭证配置
    builder_creds = BuilderApiKeyCreds(
        key=cfg.builder_api_key,
        secret=cfg.builder_secret,
        passphrase=cfg.builder_passphrase,
    )
    builder_config = BuilderConfig(local_builder_creds=builder_creds)

    # 3. 最终带 Builder 的 CLOB 客户端
    client = ClobClient(
        cfg.host,
        key=cfg.private_key,
        chain_id=cfg.chain_id,
        creds=user_api_creds,
        signature_type=1,  # 邮箱登录 / Magic Link 用户通常为 1
        funder=cfg.funder_address,
        builder_config=builder_config,
    )

    return client


def _query_collateral_balance_allowance(client: ClobClient) -> Dict[str, Any]:
    """查询 COLLATERAL（USDC 抵押品）的余额与授权额度。

    返回包含 balance / allowance 的原始响应 dict 或 dict 化结果。
    """

    # 优先使用 py_clob_client 提供的类型；若当前版本不存在则回退为字典参数。
    if BalanceAllowanceParams is not None and AssetType is not None:  # type: ignore[truthy-function]
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)  # type: ignore[call-arg]
    else:
        params = {"asset_type": "COLLATERAL"}

    # get_balance_allowance 为 L2 方法，需要已配置 creds/funder。
    resp: Any = client.get_balance_allowance(params)  # type: ignore[arg-type]

    # 兼容两种返回形式：dict 或具备属性的对象。
    if isinstance(resp, dict):
        balance = resp.get("balance")
        allowance = resp.get("allowance")
    else:
        balance = getattr(resp, "balance", None)
        allowance = getattr(resp, "allowance", None)

    return {
        "asset_type": "COLLATERAL",
        "balance": "" if balance is None else str(balance),
        "allowance": "" if allowance is None else str(allowance),
    }


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    当前仅暴露一个 ``--json`` 选项用于控制输出格式：

    - 默认：人类可读文本输出；
    - ``--json``: 输出单行 JSON，字段包含 ``asset_type`` / ``balance`` /
      ``allowance`` / ``funder``。
    """

    parser = argparse.ArgumentParser(
        description=(
            "查询当前 Polymarket 账户在 Polygon 上的 USDC 抵押余额与授权额度 "
            "（CLOB L2 / Builder 模式）。"
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 格式输出完整结果（适合脚本继续处理）。",
    )
    return parser.parse_args()


def main() -> int:
    """CLI 入口函数。"""

    # 尝试加载 .env 中的配置（若存在则自动注入到环境变量）。
    load_dotenv()

    args = parse_args()

    try:
        cfg = _load_client_config()
    except ValueError as exc:
        # 关键环境变量缺失或非法时给出清晰错误并退出。
        print(f"配置错误：{exc}")
        return 1

    try:
        client = _init_clob_client(cfg)
    except Exception as exc:  # pragma: no cover - 实际依赖外部服务
        print(f"初始化 ClobClient 失败：{exc}")
        return 1

    try:
        result = _query_collateral_balance_allowance(client)
    except Exception as exc:  # pragma: no cover - 实际调用依赖网络/API
        print(f"查询余额与授权失败：{exc}")
        return 1

    # 将 funder 地址一并放入结果，便于脚本消费。
    result_with_funder: Dict[str, Any] = {
        **result,
        "funder": cfg.funder_address,
    }

    if args.json:
        # 以 JSON 形式输出，方便其他脚本解析。
        print(json.dumps(result_with_funder, ensure_ascii=False))
        return 0

    # 默认人类可读输出。
    print("Polymarket 账户余额（CLOB 视角 / L2）")
    print("--------------------------------")
    print(f"资金地址 (funder): {result_with_funder['funder']}")
    print(f"资产类型 (asset_type): {result_with_funder['asset_type']}")
    print(f"余额 in dollar(balance): {str(float(result_with_funder['balance'])/1000000.0)}")
    #print(f"授权额度 (allowance): {result_with_funder['allowance']}")

    return 0


if __name__ == "__main__":  # CLI 入口
    raise SystemExit(main())

