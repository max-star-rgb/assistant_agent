请审计当前 Phase 5G 完成情况，不要先改代码。

请回答：

1. video_understanding 是否作为 Assistant capability 接入？
2. VideoUnderstandingRequest / VideoUnderstandingResult contract 是否稳定？
3. VideoUnderstandingAdapter contract 是否明确？
4. 默认 provider 是否为 mock？
5. 缺 video_ref 是否进入 ask_followup？
6. 有 video 但普通聊天是否不会强制 video_understanding？
7. video_understanding → product_search / price_compare / image_generation / render_3d 是否可运行？
8. smoke / eval / API / WebSocket / demo runner 是否有视频覆盖？
9. 默认 pytest / eval / demo 是否不调用真实 Video Provider？
10. 是否避免了自研视频模型、复杂抽帧、WebRTC、视频数据库？
11. 下一步应该执行哪个 task？
