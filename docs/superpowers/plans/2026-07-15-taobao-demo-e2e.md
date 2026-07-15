# 淘宝真实商品演示端到端闭环实施计划

## 目标

将好单库真实购物演示默认限制为淘宝，同时保留京东、拼多多适配代码供未来显式启用，完成 `DeepSeek -> product_search -> price_compare -> ShoppingDetailPresenter -> stream.chunk -> run.end` 闭环。

## 实施步骤

1. 在 `ProviderConfig` 增加不可变的好单库启用平台元组，从 `HAODANKU_ENABLED_PLATFORMS` 解析规范平台名，默认回退到 `taobao`。
2. 将同一平台配置传入搜索和比价的 `HaodankuConfig`；请求未指定平台时使用启用集合，指定平台时取交集。
3. 对只请求未启用平台的搜索或比价返回 `provider_platform_disabled`，不发起 HTTP 请求，也不把禁用平台记入 `failed_platforms`。
4. 保留淘宝 PID 转链；未配置 PID 和授权昵称时，仅使用通过平台 URL 安全校验的真实直链并标记 `unverified`。
5. 复用确定性购物 Presenter，最多输出三条合格商品；Gateway 购物响应丢弃模型 delta，只发送一个含唯一 `<detail>` 的完整 chunk。
6. 更新 `.env.example`、工具调用与 Gateway 权威文档；先运行目标测试，再运行 fast 套件、`git diff --check`，最后执行显式 opt-in 的真实 Provider 探针和 DeepSeek Gateway 验证。

## 验收条件

- 默认和模型多平台请求都只产生淘宝 `supersearch` HTTP 调用。
- 只请求禁用平台时 HTTP 调用数为零，错误码为 `provider_platform_disabled`。
- 搜索与比价 factory 传递完全相同的平台元组；显式三平台配置仍通过原多平台测试。
- 真实探针报告 `requested_platforms == succeeded_platforms == ["taobao"]`、`failed_platforms == []`，至少一条商品具备价格、淘宝 HTTP(S) 链接和图片。
- Gateway 最终仅有一个购物 `stream.chunk`、唯一 `<detail>`，并以 `run.end(reason=completed)` 结束。
