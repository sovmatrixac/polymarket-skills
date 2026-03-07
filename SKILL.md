---
name: openclaw-polymarket-skill
description: 一个为 Openclaw 机器人设计的自动化交易 Skill，专注于在 Polymarket 上执行低风险、高胜率的套利策略。该 Skill 能自动化完成市场发现、机会评估、交易执行、持仓管理和收益收割的全流程，并通过 Webhook 发送交易总结。适用于定时或手动触发，寻找并执行符合“稳定盈利、严控风险”原则的交易机会。
---

# Polymarket 低风险套利交易员

本 Skill 为 Openclaw 机器人设计，用于自动化执行 Polymarket 上的低风险、高胜率交易策略。

核心原则是“稳定盈利、严控风险”，通过筛选高概率市场、合理分配资金、并在交易前进行多重验证来寻找并执行交易机会。

## 1. 凭证与环境配置

**必须**: 在执行任何交易脚本前，确保工作区存在 `.env` 文件，并包含以下变量：

- `PRIVATE_KEY`: 你的 EOA 私钥
- `FUNDER_ADDRESS`: 你的 Polymarket 代理钱包地址
- `POLY_BUILDER_API_KEY`: Builder API Key
- `POLY_BUILDER_SECRET`: Builder API Secret
- `POLY_BUILDER_PASSPHRASE`: Builder API Passphrase
- `DINGTALK_WEBHOOK_URL`: (可选) 钉钉机器人 Webhook 地址，用于接收通知

**必须**: 调用任何与交易、余额、持仓相关的脚本时，在 `bash` 工具中设置 `include_secrets=true`，以确保脚本能访问到 `.env` 文件中的私密信息。

## 2. 核心执行流程（SOP）

Agent 必须严格按照以下步骤顺序执行，不得跳过或颠倒。

### Step 1: 筛选高胜率候选市场

- **动作**: 调用 strategy_select 脚本生成 candidates.json（具体命令见“脚本使用说明”）。
- **目标**: 获取所有活跃市场，筛选出概率在 `[0.95, 0.99)` 区间、尚未过期的候选市场，并基于`预期收益/剩余时间`等维度进行评分排序。
- **产出**: `candidates.json` 文件，包含按 `score` 降序排列的候选市场列表。

### Step 2: 过滤已持仓的重复交易

- **动作**: 调用 deduplicate 脚本，以 candidates.json 为输入，生成 deduped_candidates.json（命令见“脚本使用说明”）。
- **环境**: 此脚本需要 `FUNDER_ADDRESS` 环境变量。
- **目标**: 读取当前持仓，过滤掉已持有相同代币（`asset`）或属于相同条件（`condition_id`）的候选交易，避免重复建仓。
- **产出**: `deduped_candidates.json` 文件，包含去重后的候选列表。

### Step 3: 生成交易计划

- **动作**: 调用 exec_pipeline 脚本生成 trades_plan.json（命令见“脚本使用说明”）。该脚本会使用上一步的 `deduped_candidates.json`（如果存在）或直接调用内部的策略筛选与去重逻辑。
- **目标**: 结合风险控制参数（单笔资金比例、最大交易笔数），为最终候选交易计算具体的投入份数（shares），并生成一份**离线**交易计划。
- **产出**: `trades_plan.json` 文件。这份文件**仅为计划**，不代表已执行。

### Step 4: 人工或二次验证 (Web Search)

- **动作**: 读取 `trades_plan.json`，提取其中每笔交易的 `market_title`。
- **工具**: **必须**使用 `web_search` 工具，以市场标题为关键词进行搜索，交叉验证市场事件是否已有定论。
- **调用示例**: `web_search(query="Will 'Inside Out 2' gross over $120.5M domestically on its opening weekend?")`
- **目标**: 识别并剔除那些事实已定或存在重大反转信息的市场。
- **严禁**: 如果搜索结果（如权威新闻、官方公告）表明事件结果已确定，**必须放弃**该笔交易。

### Step 5: 执行交易

- **动作**: 对通过二次验证的交易，逐一调用 trade 脚本执行真实下单。
- **说明**: 从 `trades_plan.json` 中读取每条交易的 `shares`, `price`, `token_id`，并构造对应的命令（命令示例见“脚本使用说明”中的 trade.py 调用）。
- **约束**:
  - **安全优先**: trade 脚本在集成到 Openclaw 时，推荐默认以 dry-run 方式运行，只打印将要执行的命令；仅在显式确认后才开启真实下单模式。
  - **笔数限制**: 严格遵守 `trades_plan.json` 中的交易列表，不执行计划外的交易。

### Step 6: 发送执行总结

- **动作**: 调用 notify_dingtalk 脚本发送消息通知（命令见“脚本使用说明”）。
- **目标**: 汇总本轮执行情况，包括新交易的详情、当前账户总余额和持仓摘要，并通过钉钉机器人发送通知。
- **产出**: 发送一条钉钉消息。

### Step 7: (可选) 定期检查并领取收益

- **动作**: 可定期（例如每日）调用 claim 脚本领取可兑现盈利（命令见“脚本使用说明”）。
- **目标**: 自动领取所有已结算且可供提取的盈利。

## 3. 脚本使用说明

所有脚本均位于 scripts/ 目录下，应在 Skill 根目录中通过 bash 工具直接调用。

- **scripts/get_markets.py**
  - **用途**: 从 Polymarket Gamma API 获取活跃市场信息。
  - **调用**: python3 scripts/get_markets.py

- **scripts/strategy_select.py**
  - **用途**: 对活跃市场进行筛选、评分和排序，选出符合高胜率策略的候选市场。
  - **调用**: python3 scripts/strategy_select.py --output candidates.json

- **scripts/deduplicate.py**
  - **用途**: 根据现有持仓过滤掉重复的交易机会。
  - **调用**: python3 scripts/deduplicate.py --input candidates.json --output deduped_candidates.json

- **scripts/risk_sizing.py**
  - **用途**: 根据账户余额和风险系数，计算单笔交易的最大投入份数。
  - **调用**: python3 scripts/risk_sizing.py --price <market_price>

- **scripts/exec_pipeline.py**
  - **用途**: 串联策略筛选、去重和风险计算，生成最终的交易计划 trades_plan.json。
  - **调用**: python3 scripts/exec_pipeline.py

- **scripts/trade.py**
  - **用途**: 执行单笔交易。
  - **调用**: python3 scripts/trade.py --shares <数量> --price <价格> --token-id <代币ID>

- **scripts/positions.py**
  - **用途**: 查询并展示当前所有持仓。
  - **调用**: python3 scripts/positions.py

- **scripts/balance.py**
  - **用途**: 查询当前账户的 USDC 余额。
  - **调用**: python3 scripts/balance.py --json

- **scripts/claim.py**
  - **用途**: 领取所有已结算的盈利。
  - **调用**: python3 scripts/claim.py --execute

- **scripts/notify_dingtalk.py**
  - **用途**: 发送包含交易计划和账户状态的钉钉通知。
  - **调用**: python3 scripts/notify_dingtalk.py

## 4. 风险管理与约束

- **必须**: 单笔交易的名义本金（`notional`）不得超过调用 balance 脚本获取的总余额的 5%。
- **必须**: 在一轮执行周期中，最多发起 5 笔新交易。
- **严禁**: 交易 `Yes` 概率等于或高于 99% 的市场，此类市场预期收益过低。
- **严禁**: 交易已关闭、已失效，或经 `web_search` 验证后事件已有明确结果的市场。
- **推荐**: 在单位时间预期收益相似的情况下，优先选择流动性（`liquidity`）更高、成交量（`volume`）更大的市场。
- **安全优先**: 任何涉及真实资金变动的操作（如 trade、claim），都应在调用前再次检查参数与市场状态，结合 `web_search` 结果进行人工复核，仅在确认无误后再执行真实操作。
