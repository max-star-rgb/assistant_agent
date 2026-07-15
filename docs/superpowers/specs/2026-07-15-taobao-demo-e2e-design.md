# 淘宝真实商品演示闭环设计

## 目标

在不要求佣金结算和淘宝 PID 的前提下，将当前真实 Provider 演示限定为淘宝，完成 `DeepSeek -> product_search -> price_compare -> ShoppingDetailPresenter -> Gateway stream.chunk -> run.end` 的端到端闭环。用户必须能看到真实淘宝商品标题、价格、图片和可点击链接。

## 范围

- 本阶段只启用 `taobao`，不调用京东、拼多多或唯品会 Provider。
- 保留现有多平台适配代码，以后获得权限后通过配置重新开启。
- 不接入订单拉取、佣金结算或返利。
- 不使用 mock 商品伪装成真实商品。

## 配置与适配边界

新增好单库已启用平台配置，使用逗号分隔的平台名，当前演示环境设为 `taobao`。适配器将模型请求的平台与已启用集合取交集；模型未指定平台时，直接使用已启用集合。因此即使 DeepSeek 没有显式填写 `platforms`，也只会调用淘宝 `supersearch`。

默认 mock/local/offline 行为不改变；真实好单库仍只能在 `provider_smoke` 或 `pilot` profile 下启用。

## 搜索、比价与链接

淘宝 `supersearch` 返回真实商品候选。现有链接安全校验保留：只接受 HTTP(S)、淘宝/天猫允许域名和非空路径。

- 配置 PID 和授权昵称时，调用 `ratesurl`，成功链接标记为 `verified`。
- 未配置 PID 时，使用搜索结果中通过安全校验的直链，标记为 `unverified`；它用于演示购物跳转，不承诺佣金归因。
- 没有合法链接、图片或价格的商品不进入 `<detail>`。

`price_compare` 继续复用现有结构化排序和每平台三条配额；由于只启用淘宝，最终最多输出三条淘宝商品。

## Gateway 输出

仅启用 `supports_shopping_detail_v1` 的 App/Gateway 入口生成固定协议。Gateway 在成功的 `price_compare` 后抑制 LLM 自行生成的商品卡片，由 `ShoppingDetailPresenter` 一次性输出一个完整 `stream.chunk`，然后发送 `run.end(reason=completed)`。

期望协议仅含淘宝条目：

```text
{自然语言摘要}
<detail>
1. 淘宝 - {商品标题} {价格}元 <link>{商品链接}</link> <pic>{图片链接}</pic>
</detail>
```

## 失败处理

- 淘宝搜索超时、权限或响应错误时，返回结构化 Provider 错误，不回退到伪造真实商品的 mock 结果。
- 无合格商品时只返回自然语言说明，不输出空 `<detail>`。
- 未启用平台不发起请求，也不作为部分失败暴露给演示用户。

## 验证

- 离线单元测试覆盖已启用平台默认值、请求交集和中文别名归一化。
- 伪造 HTTP 响应验证淘宝请求是唯一外部商品请求。
- Gateway 集成测试断言只有一个完整购物 `stream.chunk`、仅含淘宝条目，并以 `run.end completed` 结束。
- 最后在 `provider_smoke` 下使用真实 DeepSeek 和好单库执行一次有界闭环，只记录平台、条数、链接状态和 Gateway 终态，不保存 Provider 原始响应。
