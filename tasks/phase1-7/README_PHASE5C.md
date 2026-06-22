# Phase 5C Tasks：Product Search / Price Compare Provider Baseline

Phase 5C 从 Task 055 开始。该阶段聚焦：

```text
product_search
price_compare
```

## 执行顺序

```text
055 Phase 5C Product Search / Price Compare Roadmap
056 Product Search Provider Adapter
057 Price Compare Provider Adapter
058 Product Result and Ranking Contracts
059 Product Search / Price Compare Smoke Scripts
060 Product Search Evals and API Coverage
061 Phase 5C Review
```

## 执行规则

- 每次只执行一个 task。
- 先阅读该 task 的 Read first。
- 不跨 task 实现。
- 不升级意图识别。
- 不接入 3D render。
- 不做 Vision hardening。
- 不进入 Harness Engineering。
- 默认使用 MockAdapter 或 LocalJsonAdapter。
- 默认测试不得联网。
- 默认测试不得调用真实 Provider。
- 不自动安装依赖。
- 不写入 API Key。
- 不创建包含真实密钥的 `.env` 或 `.env.local`。
- 不提交大规模真实商品数据。
- 不写爬虫。
- 不处理登录、cookie、下单、支付。
- 不提交大规模真实商品数据、日志、大文件或真实 Provider 输出样本。
- 真实 Provider 只能由用户显式运行 smoke 脚本或 env-gated integration tests 触发。
- 修改源码、测试、文档优先使用 apply_patch。
- 完成后运行测试并停止。

## 默认验收

```bash
python -m pytest
python scripts/run_evals.py
```
