# 55 Phase 5C 路线图：Product Search / Price Compare Provider Baseline

## 背景

Phase 5A 已完成 Assistant Capability Routing Baseline，Phase 5B 已完成 text-first capabilities：

```text
direct_chat
image_generation
```

当前下一步不建议继续升级意图识别，也不建议转向 Vision-only hardening。更合理的方向是进入业务能力阶段：

```text
product_search
price_compare
```

这两个能力是助理 Agent 中非常关键的外部工具能力：

- 用户可以纯文本搜索商品。
- 用户可以基于图片/视频理解结果搜索同款或相似款。
- 用户可以对搜索结果进行价格、平台、相似度、预算排序。
- 后续可以和 image_generation、render_3d、多轮记忆组合成完整业务闭环。

## Phase 5C 总目标

Phase 5C 的目标是让商品搜索和比价从 Mock-only 能力升级为可替换 Provider 的能力模块，但默认仍保持离线、Mock、可测试。

核心目标：

1. 定义 Product Search Provider Adapter contract。
2. 定义 Price Compare Provider Adapter contract。
3. 统一 ProductResult、PriceOffer、RankingReason 等 schema。
4. 支持 text-only product search。
5. 支持 vision/video summary → product search。
6. 支持 product_search → price_compare 多步链路。
7. 增加 smoke 脚本，但默认不联网、不调用真实 Provider。
8. 扩展 eval/API 覆盖。
9. 保持 API Key、安全、日志、真实商品数据边界清晰。

## 能力边界

Phase 5C 只处理两个 capability：

```text
product_search
price_compare
```

允许的输入来源：

- 纯文本商品搜索，例如“帮我找 500 元以内的白色运动鞋”。
- 纯文本价格比较，例如“比较一下 iPhone 15 和 iPhone 16 的价格”。
- `image_understanding` 或 `video_understanding` 输出的结构化摘要。
- `product_search` 输出的候选商品列表，作为 `price_compare` 输入。

Phase 5C 不升级意图识别。当前仍使用已有规则路由和 LangGraph 编排；本阶段只强化商品搜索与比价的 Provider 边界、schema、mock/local 验证和手动 smoke 入口。

## 不做什么

Phase 5C 暂不做：

- 真实电商平台深度接入。
- 爬虫。
- 登录态、cookie、反爬处理。
- 购买、下单、支付。
- 商品推荐算法训练。
- 3D 渲染 Provider。
- Memory hardening。
- Harness Engineering。
- 默认联网搜索。
- 默认使用真实商品 API。

## 推荐 Provider 类型

Phase 5C 可以支持 Provider skeleton，但不强制真实调用：

```text
mock
local_json
http_search
```

### mock

默认测试和默认运行路径。

### local_json

适合本地 demo，使用一个小型商品 JSON 文件，不联网。

### http_search

真实或半真实商品搜索服务的 HTTP Adapter skeleton。只有用户显式配置环境变量时才启用。

`http_search` 不代表爬虫，不处理登录态、cookie、反爬、购买、下单或支付。
默认 `python -m pytest` 和 `python scripts/run_evals.py` 必须保持离线。

## Phase 5C 任务顺序

```text
055 Phase 5C Product Search / Price Compare Roadmap
056 Product Search Provider Adapter
057 Price Compare Provider Adapter
058 Product Result and Ranking Contracts
059 Product Search / Price Compare Smoke Scripts
060 Product Search Evals and API Coverage
061 Phase 5C Review
```

## 与后续阶段关系

Phase 5C 完成后，建议再决定：

```text
5D Render 3D Capability
5E Provider Hardening / Retry / Cost / Trace Query
5F Memory Hardening
5G Real Data Regression
5H Harness Engineering
```

不要在 Phase 5C 一次性接入所有真实能力。
