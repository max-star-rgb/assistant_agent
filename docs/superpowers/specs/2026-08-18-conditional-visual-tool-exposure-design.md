# 视觉 Tool 条件渐进暴露设计

## 目标

将用户主动上传的媒体分析与 WebSocket 实时视频能力拆成不同产品语义，并为三个视觉 Tool 建立独立于 Skill 的条件渐进暴露机制：

- `uploaded_media_inspect` 只分析用户主动上传的图片或视频；
- `live_view_inspect` 只在当前连接完成 VIDEO 握手后暴露；
- `visual_memory_search` 只在 VIDEO 握手完成且当前视觉会话已有可检索文本观察后暴露。

所有已知 Tool 仍静态注册给 `create_agent` / `ToolNode`。运行时只在每次模型调用前缩小 `ModelRequest.tools`，不动态重建 Agent，不根据用户文字推断意图。

## 核心决策

1. 将 `media_inspect` 重命名为 `uploaded_media_inspect`，同时保留主动上传图片和主动上传视频的分析能力；生产模型不再看到旧名称，也不注册兼容别名。
2. `uploaded_media_inspect` 改为 LangChain 原生函数 Tool 工厂，通过 `@tool` 和 `ToolRuntime[AssistantRunContext]` 构造；VLM client 由进程级视觉模块注入并复用。
3. 新增独立的条件暴露层。三个视觉 Tool 不读取 `active_skill_ids`，不依赖 `load_skill`，也不由 Skill 渐进加载解锁。
4. Tool 只声明受控的 availability 类型；核心 middleware 统一读取可信运行事实并裁决。Tool 不携带可执行的任意 availability 回调。
5. `visual_memory_search` 的可见性由后台实时 VLM 管线是否已经写入可检索文本观察决定，不依赖模型是否调用过 `live_view_inspect`。
6. 视觉观察历史是 VIDEO 会话内的派生时间线，不是 Agent 长期 Memory，也不等同于 LangGraph Store。

## 概念边界

### 用户主动上传媒体

用户通过受信入口主动附加的图片或视频。入口归一化后必须保留机器可读来源，例如 `source=uploaded`。仅凭 `type=image|video` 无法区分上传视频与摄像头实时视频，因此不能作为唯一判断依据。

`uploaded_media_inspect` 只消费此类附件。它不读取实时视频 session、实时帧索引或视觉观察历史。

### 实时视频

实时视频只指 `/agent-service/v1` WebSocket 连接完成 `callType=VIDEO` 握手后，由 `video` 消息持续提交的摄像头帧。入口归一化时标记为 `source=live_camera`，不能让它满足上传媒体条件。

### 视觉观察历史

后台实时 VLM 管线根据可信摄像头帧产生的 session-scoped 文本观察及其派生检索索引。建议在接口和变量中使用 `visual_observation_history`、`has_searchable_observations()` 等名称，避免继续以泛化的 `memory_store` 表达该概念。

该历史与两类持久化边界不同：

```text
Agent Store / 长期 Memory
  └── 跨 run 的用户事实、偏好和长期信息

Visual Observation History
  └── 当前 VIDEO 会话产生的 VLM 文本时间线和派生索引
```

## VIDEO 握手完成语义

连接必须按以下顺序完成状态迁移：

```text
WebSocket connected
  -> 收到 assistantControl / assistantControlStart
  -> number / userInfo.number 有效
  -> callType 明确等于 VIDEO
  -> authenticated identity 匹配
  -> 当前连接未重复绑定
  -> Agent Server thread 创建成功
  -> MediaConnectionSession 绑定成功
  -> 实时视觉 session 创建成功
  -> 服务端成功发送 assistantControl 成功 ACK
  -> VIDEO_HANDSHAKE_COMPLETED
```

从客户端协议看，收到成功 ACK 才表示握手完成。从服务端后续 chat run 看，可以读取已经绑定的连接状态；同一 WebSocket 顺序处理消息，因此合法 chat 进入 Agent 时，前述 ACK 已经发送。

只完成 WebSocket 建连、入口支持 video、声明 `media_capabilities` 或收到第一帧，都不是本设计中的握手完成定义。第一帧不是 `live_view_inspect` 的暴露前提。

## Tool availability 契约

availability 使用核心维护的封闭枚举：

```python
class ToolAvailability(str, Enum):
    ALWAYS = "always"
    UPLOADED_MEDIA_PRESENT = "uploaded_media_present"
    VIDEO_HANDSHAKE_COMPLETED = "video_handshake_completed"
    VISUAL_HISTORY_AVAILABLE = "visual_history_available"
```

三个 Tool 的声明和判定如下：

| Tool | availability | 可信条件 |
| --- | --- | --- |
| `uploaded_media_inspect` | `uploaded_media_present` | 当前对话上下文存在至少一个 `source=uploaded` 的图片或视频附件 |
| `live_view_inspect` | `video_handshake_completed` | 当前 WebSocket 连接已经完成 VIDEO 握手 |
| `visual_memory_search` | `visual_history_available` | VIDEO 握手完成，且当前 user/session/as-of 范围已有至少一条可检索视觉文本观察 |

availability metadata 是静态声明，不接受模型输入。身份、session 和 as-of 仍从受信 Runtime/ServerInfo 获得。

## 中央条件暴露层

新增 `ConditionalToolExposureMiddleware`，在每次同步或异步 model call 前执行：

1. 从 `request.tools` 开始，不从完整 inventory 重新装配，避免重新加入已被其他 middleware 隐藏的 Tool；
2. 读取 Tool 的受控 availability metadata；
3. 从标准消息、`AssistantRunContext`、连接状态和窄视觉历史探针读取可信事实；
4. 只保留当前条件成立的 Tool；
5. 通过 `request.override(tools=visible_tools)` 交给下游模型。

最终可见集为多个独立过滤层的交集：

```text
静态注册的完整 Tool inventory
          ∩
Skill progressive exposure
          ∩
Conditional tool exposure
          =
本次 ModelRequest.tools
```

Skill 层回答“模型已加载哪些专业流程”，条件层回答“当前结构化运行条件允许哪些能力”。本设计的三个 Tool 只受条件层控制；条件层不读取 `active_skill_ids` 或 `skill_reference_grants`。

对于视觉历史条件，middleware 依赖窄接口 `VisualObservationHistoryProbe`，例如：

```python
class VisualObservationHistoryProbe(Protocol):
    def has_searchable_observations(
        self,
        *,
        user_id: str,
        session_id: str,
        as_of_sequence: int | None,
    ) -> bool: ...
```

该接口只回答 availability，不向 middleware 暴露向量、文本内容或底层数据库。每次模型调用重新探测，因此后台管线在 Agent loop 期间首次写入记录后，下一次模型调用即可看到 `visual_memory_search`。

## 上传媒体 Tool 与 VLM client 生命周期

`uploaded_media_inspect` 由工厂创建原生函数 Tool：

```python
def create_uploaded_media_inspect_tool(
    client: VisionUnderstandingClient,
) -> BaseTool:
    @tool(
        "uploaded_media_inspect",
        response_format="content_and_artifact",
        metadata={
            "effect": "read",
            "availability": "uploaded_media_present",
        },
    )
    def uploaded_media_inspect(
        question: str,
        runtime: ToolRuntime[AssistantRunContext],
    ) -> tuple[str, dict[str, object]]:
        ...

    return uploaded_media_inspect
```

模型只提交需要判断的问题；上传附件引用、authenticated identity、thread/run 和其他 runtime-owned 参数由 `ToolRuntime` 与标准消息提取，不能由模型伪造。Tool 继续返回给模型的简洁 observation 和机器 artifact，并沿用标准 Tool 错误语义。

进程级 `VisualPerceptionModule` 创建并长期持有按需理解使用的 `VisionUnderstandingClient`。Tool 工厂闭包只引用该 client，不在每次调用时创建或关闭它；服务 shutdown 时由模块统一关闭一次。实时观察管线如因 Provider 流协议需要独立 session client，其生命周期仍由实时视觉 session 管理，但不能与 Tool 是否可见耦合。

## 数据流

### 主动上传图片或视频

```text
受信入口收到附件
  -> 归一化标准 content block，并标记 source=uploaded
  -> ConditionalToolExposureMiddleware 检测到上传附件
  -> 模型看到 uploaded_media_inspect
  -> ToolRuntime 提取可信附件引用
  -> 持久 VisionUnderstandingClient 执行图片或显式视频理解
  -> ToolMessage content + artifact
```

没有上传附件时，Tool schema 不发送给模型。即使出现“看看这张图”之类文字，也不能据此暴露 Tool。

### 实时视频与视觉历史

```text
VIDEO 握手成功
  -> 后续 chat 的可信运行事实标记 video handshake completed
  -> 模型看到 live_view_inspect

摄像头帧 -> 后台 VLM -> Visual Observation History
  -> has_searchable_observations() 首次为 true
  -> 下一次 model call 同时看到 visual_memory_search
```

VIDEO 握手后尚无帧或尚无成功文本观察时，`live_view_inspect` 可见但可以结构化返回当前无可用观察；`visual_memory_search` 保持隐藏。

## 执行期保护与竞态

schema 隐藏是模型上下文治理，不替代执行授权。三个 Tool 在执行时必须重新验证自身前置条件：

- 上传附件已不存在或来源不可信时，返回可解释的缺少附件错误；
- VIDEO session 已关闭或不匹配时，实时 Tool 拒绝读取；
- 视觉历史在调用前被清理时，搜索 Tool 返回结构化 `empty` 或 `unavailable`，不得查询其他 session；
- middleware 探测失败时 fail closed，隐藏依赖该事实的 Tool，并记录不含视觉内容的诊断事件。

连接断开后，新 run 不再满足 VIDEO 握手条件。即使底层视觉观察历史仍在 retention 窗口内，普通入口也不暴露 `visual_memory_search`。

## 迁移范围

- 将常量、Tool 名称、Plugin 装配、Skill 说明、eval 入口和当前 authority 文档中的 `media_inspect` 更新为 `uploaded_media_inspect`；不保留生产 alias。
- 为上传附件补充可信来源投影，确保实时摄像头 video block 不会误触发上传 Tool。
- 为 WebSocket 连接状态增加明确的 VIDEO 握手事实，不再让 `media_capabilities` 同时承担协议状态和产品能力含义。
- 为视觉观察历史提供窄 availability probe；不让 middleware 直接理解 Qdrant、SQLite 或 LangGraph Store。
- 新增条件 middleware 并接入共享 fast agent；planning 分支复用同一个 fast Agent，因此获得相同可见性规则。
- 更新视觉架构、WebSocket 协议和 Tool 调用 authority，删除“断线后普通入口仍可搜索视觉历史”的旧规则。

## 验证要求

- 无上传附件时隐藏 `uploaded_media_inspect`；上传图片或上传视频时暴露；实时摄像头 block 不触发它。
- AUDIO 握手和普通 Agent Server 入口隐藏两个实时视觉 Tool。
- VIDEO 成功 ACK 后、第一帧前暴露 `live_view_inspect`，但隐藏 `visual_memory_search`。
- 后台首次写入可检索文本观察后，下一次 model call 暴露 `visual_memory_search`。
- 连接断开后的新 run 隐藏两个实时视觉 Tool，即使视觉历史仍有 retention 数据。
- Skill activation 状态变化不影响这三个 Tool；条件 middleware 不重新加入 Skill 层已隐藏的其他 Tool。
- `uploaded_media_inspect` 复用同一进程级 VLM client；并发调用不发生逐次创建或提前关闭，shutdown 只关闭一次。
- Tool 执行期条件变化时 fail closed，身份、session、as-of 和附件来源不可由模型伪造。
- 默认测试使用 mock/local/offline，不调用真实 Provider。

## 非目标

- 不根据关键词、正则、用户话术或额外 LLM 分类决定 Tool 可见性。
- 不动态重建 `create_agent`、ToolNode 或完整 Tool inventory。
- 不把视觉观察历史写入长期 Memory，也不把 LangGraph Store 改造成视觉时间线。
- 不要求调用 `live_view_inspect` 才开始生产视觉历史。
- 不在本次改动中调整 `visual_reminder_manage` 的暴露策略。
- 不保留 `media_inspect` 的生产兼容别名。
