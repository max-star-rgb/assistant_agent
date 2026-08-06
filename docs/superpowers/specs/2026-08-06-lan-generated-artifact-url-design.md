# 局域网生图访问地址与最近图片复用设计

日期：2026-08-06

## 目标

Agent Server 生成图片后继续将原文件保存到 `.local/generated/`，并向所有局域网入口返回由服务端确定性构造的可访问绝对 URL。后续图片转 3D 默认复用当前连接最近一次成功生成的受管图片，不依赖模型猜测文件名或公网地址。

## 现状与问题

- 生图文件和 `/artifacts/generated/<filename>` 静态路由已经存在，但 Tool observation 会把内部相对路径暴露给主 LLM。
- 主 LLM 可能自行给相对路径补充未经配置的域名，产生不可访问的链接。
- Agent-Service 已能把受管图片投影为 WebSocket `IMAGE detail`，但文本入口缺少统一、可信的绝对访问地址。
- `image_to_3d` 当前优先采用模型提交的 `src_image`。即使运行时已经绑定最近图片，模型虚构的显式 ID 仍会覆盖正确值。

## 设计

### 1. 受管 artifact 与局域网地址

新增独立部署配置 `ARTIFACT_BASE_URL`，值为局域网客户端可访问的 Agent Server origin，例如：

```text
http://192.168.1.20:8089
```

服务端只接受规范化的 `http` 或 `https` origin，不允许 query、fragment、userinfo 或非空路径。配置存在时，受管相对引用：

```text
/artifacts/generated/<filename>
```

确定性投影为：

```text
<ARTIFACT_BASE_URL>/artifacts/generated/<filename>
```

内部 `output_ref` 和 `image_id` 仍保持现有受管格式，避免 3D、Media-Agent 和本地文件解析依赖网络地址。`ARTIFACT_BASE_URL` 不复用 `PUBLIC_IP` / `PUBLIC_PORT`，因为后者属于 3D callback 部署边界。

### 2. 响应与模型边界

- 生图 Tool 给主 LLM 的 prompt-safe observation 只提供成功状态和 `image_id`，不提供内部相对路径、Provider URL 或最终展示 URL。
- Runtime 保留完整 `ToolResult.output_ref`，供 Gateway 和入口投影使用。
- 对支持文本链接的响应，由服务端从受管 `output_ref` 构造权威绝对 URL，并确定性加入最终交付结果；不要求 LLM 拼接 Markdown 链接。
- Agent-Service 的 `IMAGE detail` 继续使用同一受管本地文件和 Base64，不改 wire schema。
- 未配置 `ARTIFACT_BASE_URL` 时不伪造绝对地址。内部 artifact、Media-Agent 图片投递和 3D 复用仍可工作，文本交付明确省略外部链接。

### 3. 最近图片转 3D

`image_to_3d` 的源图片解析顺序调整为：

1. 同一 run 最近一次成功 `image_generation` 的图片 ID；
2. 当前 Agent-Service 连接上一 turn 最近成功图片 ID；
3. 模型提交的 `src_image`，仅在没有 runtime-owned 最近图片时使用；
4. 均不存在时返回现有“请先生成图片”结构化失败。

这样，普通“生成 3D 模型”稳定复用最近产物，模型虚构的 `cake_001` 不会覆盖 runtime-owned 事实。显式转换历史图片仍可在没有当前最近图片绑定的入口使用；未来若需要在同一连接选择任意历史图片，应新增可信的结构化 artifact 选择字段，而不是从普通文本推断权限或文件身份。

## 安全与部署约束

- 静态路由继续只暴露 `.local/generated` 根目录内经过安全解析的受支持图片。
- URL 构造只消费可信进程配置，不读取普通用户 metadata、请求 `Host` 或转发头自动决定 origin。
- Server 需要监听局域网接口（当前联调为 `0.0.0.0:8089`），主机防火墙需允许目标局域网访问。
- 文件名仍使用受管 ID；本设计不把“不可猜测文件名”当作授权。当前局域网匿名访问边界保持不变。

## 验证

- 在 `tests/tdd/` 独立 feature 测试中先复现并证明：可信 base URL 能生成绝对 artifact URL、缺失配置不会伪造 URL、最近图片优先于模型传入的无效 ID。
- 运行现有 Agent-Service 图片投递和 image-to-3D 相关最小测试，确认 Base64 `IMAGE detail`、本地 artifact 解析和 3D job 行为不回归。
- 使用 mock/local fixture，不调用真实图片 Provider 或 3D 服务。
- 更新 `docs/media-agent-service-websocket.md` 和相关配置示例，使当前权威文档与实现一致。

## 非目标

- 不引入 OSS、S3、CDN、签名 URL 或公网反向代理。
- 不修改 Media-Agent wire schema。
- 不让 `run_client.py` 保存第二份图片。
- 不改变 3D callback 地址或任务完成投递机制。
