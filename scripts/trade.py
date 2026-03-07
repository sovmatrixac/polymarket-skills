# 该脚本用于通过 Polymarket CLOB 客户端发起一笔交易（如下单示例）。
# 使用示例：
#   python trade.py --shares 100 --price 12.34 --token-id 0xabc123
#
# 参数说明：
#   --shares   整数，必填，表示下单份数（shares）。
#   --price    浮点数，必填，表示每份价格。
#   --token-id 字符串，必填，要交易的 token_id。
#
# 运行前置条件：
#   - 已安装依赖库：python-dotenv、py_clob_client、py_builder_signing_sdk 等。
#   - 当前目录下配置 .env 文件或环境变量：
#       PRIVATE_KEY: 用于签名的 EOA 私钥
#       FUNDER_ADDRESS: 对应的 Polymarket 代理钱包地址
#       POLY_BUILDER_API_KEY / POLY_BUILDER_SECRET / POLY_BUILDER_PASSPHRASE: Builder API 凭证
#   - 请确认链路和 API Key 已正确配置；脚本实际运行会发起真实交易请求，请谨慎使用。

import argparse
import os
import sys
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs
from py_clob_client.order_builder.constants import BUY, SELL
from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds


load_dotenv()  # 用于加载 .env 文件


def parse_args() -> argparse.Namespace:
    """解析命令行参数并进行基础校验。"""
    parser = argparse.ArgumentParser(
        description="使用命令行参数指定 shares、price 和 token_id 发起 Polymarket 交易示例脚本。",
    )
    parser.add_argument(
        "--shares",
        type=int,
        required=True,
        help="交易份数（整数，必填，必须大于 0）",
    )
    parser.add_argument(
        "--price",
        type=float,
        required=True,
        help="每份价格（浮点数，必填，必须大于 0）",
    )
    parser.add_argument(
        "--token-id",
        dest="token_id",
        type=str,
        required=True,
        help="要交易的 token_id（字符串，必填，不能为空）",
    )
    parser.add_argument(
        "--side",
        type=str,
        default="BUY",
        choices=["BUY", "SELL"],
        help="交易方向：BUY（买入）或 SELL（卖出），默认BUY",
    )

    args = parser.parse_args()

    if args.shares <= 0:
        print("错误：shares 必须为大于 0 的整数。")
        sys.exit(1)

    if args.price <= 0:
        print("错误：price 必须为大于 0 的浮点数。")
        sys.exit(1)

    if not args.token_id or not args.token_id.strip():
        print("错误：token_id 不能为空。")
        sys.exit(1)

    return args


def main() -> None:
    args = parse_args()

    # --- 1. 配置 ---
    host = "https://clob.polymarket.com"
    chain_id = 137  # Polygon Mainnet
    private_key = os.getenv("PRIVATE_KEY")  # **你的 EOA 私钥** (用于签名，非 Builder Key)
    funder_address = os.getenv("FUNDER_ADDRESS")  # **你的 Polymarket 代理钱包地址**

    # 出于安全考虑，不在日志中打印私钥或资金地址，仅确认已加载。
    print("已加载交易凭证（PRIVATE_KEY, FUNDER_ADDRESS）。")

    if not private_key or not funder_address:
        raise ValueError("请在 .env 文件中配置 PRIVATE_KEY 和 FUNDER_ADDRESS")

    # --- 2. 用户 L1 -> L2 凭证派生 ---
    temp_client = ClobClient(host, key=private_key, chain_id=chain_id)
    user_api_creds = temp_client.create_or_derive_api_creds()

    # --- 3. 配置 Builder 凭证 ---
    builder_creds = BuilderApiKeyCreds(
        key=os.getenv("POLY_BUILDER_API_KEY"),
        secret=os.getenv("POLY_BUILDER_SECRET"),
        passphrase=os.getenv("POLY_BUILDER_PASSPHRASE"),
    )
    builder_config = BuilderConfig(local_builder_creds=builder_creds)

    # --- 4. 初始化最终交易客户端 ---
    client = ClobClient(
        host,
        key=private_key,
        chain_id=chain_id,
        creds=user_api_creds,
        signature_type=1,  # **关键：邮箱登录用户使用 signature_type = 1**
        funder=funder_address,  # **关键：你的 Polymarket 代理钱包地址**
        builder_config=builder_config,  # **关键：传入 Builder 配置**
    )

    print("ClobClient 初始化成功 (Builder 模式)！")

    # --- 5. 下单 ---
    try:
        side = BUY if args.side == "BUY" else SELL
        order_response = client.create_and_post_order(
            OrderArgs(
                token_id=args.token_id,
                price=args.price,  # 价格
                size=args.shares,  # 数量 (shares)
                side=side,
            )
        )
        print(f"{args.side}单提交成功: {order_response}")
    except Exception as e:
        print(f"{args.side}单提交失败: {e}")


if __name__ == "__main__":
    main()

