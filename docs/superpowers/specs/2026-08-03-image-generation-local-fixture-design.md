# 图片生成本地 Fixture 设计

日期：2026-08-03

## 目标

真实通话联调整体图片与 3D 链路时，暂时禁止 `image_generation` 调用付费生图 API，固定返回
`.local/generated/349cc6c272f4ec7a88800f0f.png`。主 LLM、Agent Runtime、Tool 治理、媒体
WebSocket、图片 Base64 投递、`image_to_3d`、3D 服务请求和回调链路保持原有运行模式。

## 配置与启用边界

新增可选配置：

```text
IMAGE_GENERATION_FIXTURE_ID=349cc6c272f4ec7a88800f0f.png
```

只有该配置非空时启用本地 fixture。值只能是单个文件名，不接受目录、绝对路径、URL、查询参数或
Data URL。删除配置后恢复既有图片 Provider 选择与 readiness 规则。

fixture 是显式联调模式，不是 Provider 失败后的 fallback。配置存在但文件缺失、越界、超限或不是
受支持图片时，`image_generation` 返回结构化失败，不调用真实生图 API。

## 实现边界

在图片生成 Plugin 装配处优先检查显式 fixture 配置。启用时注册本地 fixture adapter；未启用时继续
调用现有 `create_image_generation_adapter`，不改变正常生产路径。

本地 adapter 使用现有 `generated_artifact_payload` 校验
`/artifacts/generated/{fixture_id}`，成功后返回现有 `ImageGenerationResult`：

- `status=succeeded`
- `image_url`、`download_url`、`output_ref` 均指向受管 artifact URL
- `image_id` 使用文件名 stem
- `provider=local_fixture`

`ImageGenerationTool` 后续仍从同一受管 artifact 产生 output ref 和模型 observation。Agent-Service
成功终包继续通过 `_generated_image_details` 从 `.local/generated` 读取图片、转为纯 Base64，并发送
`IMAGE` detail。`image_to_3d(src_image=...)` 同样从该目录读取，因此不会复制图片或写第二份镜像。

## 错误处理与安全

- fixture 配置非法：Tool 构建或执行返回可解释错误，不泄露绝对路径。
- fixture 文件不存在或不是受支持图片：返回 `local_fixture_unavailable`，不调用真实 Provider。
- 不在日志、trace 或模型 observation 中记录 Base64。
- 不接受 `.local/generated` 之外的文件。

## 验证

临时 TDD 覆盖：

1. real 主 LLM 模式下显式 fixture 优先于真实图片 Provider；
2. adapter 返回固定受管 output ref 和 image ID；
3. fixture 非法或不存在时 fail closed，且真实 Provider adapter 未被调用；
4. Agent-Service 从本地文件构造 `IMAGE.image` Base64；
5. `image_to_3d` 可使用同一 fixture 的 stem 读取本地图片。

不调用真实生图 Provider、3D 服务或渲染服务。
