请审计当前 Phase 5C 完成情况，不要先改代码。

请回答：

1. product_search 是否支持 text-only query？
2. product_search 是否可接收 visual_summary / video_summary？
3. price_compare 是否可接收 product_search 结果？
4. ProductSearchAdapter contract 是否明确？
5. PriceCompareAdapter 或 compare contract 是否明确？
6. ProductResult / PriceOffer schema 是否稳定？
7. 默认 pytest / eval 是否离线？
8. smoke 脚本是否只有用户显式运行才触发真实 Provider？
9. 是否存在 API Key、登录态、cookie、爬虫、购买/支付风险？
10. 下一步应该执行哪个 task？
