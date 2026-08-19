# 实时视觉目标窗口设计

日期：2026-08-19

## 目标

恢复原生 LangGraph 迁移前实时视觉链路中最重要的语义：提问到达时冻结视觉边界，前台只等待该边界的目标帧，不等待较早帧或全局 idle。同时把旧实现的单 Qwen in-flight/latest-wins 队列升级为五帧独立并行 VLM。

以提问到达时最新成功解码帧为第 8 帧为例：

1. 原子读取并冻结第 4、5、6、7、8 帧；不足五帧时使用当时全部可用帧。
2. 五帧分别创建独立 VLM client、adapter 和 Provider WebSocket，并行执行。
3. 第 8 帧是唯一前台完成屏障。第 7 帧未完成不影响第 8 帧解除屏障。
4. 第 8 帧成功后，立即把“冻结窗口中此刻已完成的记录”返回 fast Agent；不得继续等待第 7 帧或全局 idle。
5. 第 8 帧失败或达到有界 deadline 后，立即返回结构化 unavailable；不得用第 7 帧或更旧结果伪装成当前画面。
6. 第 9 帧及之后的结果不得进入本轮回答。较早帧晚到可以进入历史语义存储，但不得修改已经发出的回答。

## Git 考古结论

| Commit | 可继承的思路 | 不能直接恢复的部分 |
| --- | --- | --- |
| `891da2d6` | chat 到达时冻结 `target_sequence`，只等待目标序号，超时后继续回答 | 当时只有一个 Qwen in-flight |
| `af5bb68d` | promotion 与 wait 共用 deadline；enqueue 加锁；waiter 先清事件再检查状态，避免丢唤醒 | 仍使用 latest-wins pending |
| `3025e523` | promotion 用 observer-owned task + `shield`，前台超时/取消不应中断已经接管的帧 | 只保护单个目标帧 |
| `e18a70bd` | 部分保留失败必须清理文件，ownership 要明确 | 没有多帧窗口 ownership |
| `51933ce9` | `pin_sequence/release_sequence`、`as_of_sequence` 和 chat 生命周期绑定，未来帧不能污染当前回答 | 默认 raw frame window 只有 3；严格目标仍经单 pending 队列 |
| `5e87829e` | 背景观察尝试复用 Provider WebSocket 以降低建连成本 | 与五帧并行互斥；当前 adapter 含 `_socket`、`_target_sequence`、诊断等可变单飞状态，不能跨并发帧共享 |
| `247390b0` 及后续原生化提交 | 生产回答必须继续走 `AssistantRootGraph -> fast create_agent -> BaseTool/ToolNode` | 不恢复旧 Gateway/Runtime facade |

结论：恢复的是“冻结边界、目标屏障、observer ownership、as-of 投影”，不是回滚旧 Runtime，也不是恢复旧单飞队列。

## 当前问题

### 1. 表面并发，实际共享单飞 adapter

`VisualPerceptionModule._create_observer()` 当前每个连接只创建一个 `RealtimeVisualObservationService`。`RealtimeVideoObserver` 虽然为每个 sequence 创建独立 `asyncio.Task`，这些任务最终都调用同一个 service/client。

`QwenRealtimeVisionAdapter` 明确是 single-in-flight，并保存 `_socket`、`_target_sequence`、阶段状态和最后诊断等可变字段。并发帧共享它会互相覆盖连接和序号状态，造成目标帧超时、错误归因或错误结果发布。

### 2. chat 只冻结一帧，没有冻结五帧窗口

`VisualPerceptionSession` 只保存每个 video 的 `_latest_frames`。`prepare_strict_target()` 只 promote 最新帧。底层 H.264 和 `VideoContextStore` 的默认 raw window 也是 3，无法在第 8 帧到达时稳定取得第 4–8 帧。

### 3. 现有 strict promotion 仍经过 latest-wins pipeline

`SemanticFramePipeline` 只有一个 in-flight 和一个 pending；pinned pending 会拒绝后续 interactive 帧。连续 promote 五帧并不能得到五个 VLM 调用，反而会覆盖或拒绝其中部分帧。

### 4. promotion 与后台 selection 存在同序号竞态

`_promote()` 只查询已经发布的 semantic record，没有先把 `_observation_tasks` 视为同序号 single-flight。后台 selection 和 chat promotion 可以为同一 sequence 各保留一份文件并触发重复路径；`already_retained=True` 的早退分支还可能遗留已转移 ownership 的文件。

### 5. Tool 读取的是“最近 8 条旧记录”，不是冻结窗口

`video_branch.py` 当前以 `target_sequence` 为上界读取最近 8 条语义记录。它能排除未来帧，但不能表达第 4–8 帧边界；如果第 7 帧尚未完成，可能混入第 3 帧甚至更旧记录。

## 设计

### 1. 单一窗口契约

新增内部不可变契约：

```python
@dataclass(frozen=True)
class VisualTargetWindow:
    window_id: str
    video_id: str
    start_sequence: int
    target_sequence: int
    sequences: tuple[int, ...]

    @property
    def target_ready_boundary(self) -> int:
        return self.target_sequence
```

常量统一为：

```python
REALTIME_VISUAL_TARGET_WINDOW_SIZE = 5
REALTIME_VISUAL_TARGET_WAIT_SECONDS = 4.0
```

H.264 ingestion 和 `VideoContextStore` 的 raw retention 都至少保留五帧。chat 到达后从同一个 store 一次读取最近五帧，验证同一 `video_id`、严格递增且最后一帧为 target，然后交给 observer 接管文件副本。

Graph state 不保存 JPEG、client、task 或 lease。Media 薄适配层只把可信的 `window_id`、`window_start_sequence` 和 `target_sequence` 写入 live-camera content block；原生 graph 继续只接收标准 messages。

### 2. 严格窗口直达 observation enqueue

新增 `RealtimeVideoObserver.promote_window(frames)`：

1. 在 `_enqueue_lock` 下按 sequence 去重和保留五个文件。
2. 目标帧先创建 observation task，其余帧随后创建；该过程不等待任何 VLM 结果。
3. strict window 绕过 `SemanticFramePipeline` 的 embedding selection/pending 队列，但仍复用 `_enqueue_serialized()`、`RealtimeVisualObservationService`、semantic store、memory store、visual index 和统一 tracing。
4. 自适应背景路径保持不变，只负责非查询驱动的长期视觉历史。
5. 同一 `(video_id, sequence)` 在 semantic record、observation task 或 enqueue reservation 中任一处已存在时都复用，不重复 VLM。

为避免“检查后再保留”的竞态，`_enqueue_serialized()` 在复制前后都检查 sequence reservation。`already_retained=True` 的重复项由 observer 显式删除其接管的文件，确保 semantic pipeline 转移 ownership 后无泄漏。

### 3. 每帧隔离的 Provider 生命周期

Observer 不再接收一个共享 `RealtimeVisualObservationService`，而是接收 factory：

```python
RealtimeVisualObservationServiceFactory = (
    Callable[[], RealtimeVisualObservationService]
)
```

每个 `_run_observation(item)` 在工作线程中：

```python
service = observation_service_factory()
try:
    return service.observe(request, trace_context=trace_context)
finally:
    service.close()
```

因此每帧都有独立的 `AdapterVisionUnderstandingClient`、`QwenRealtimeVisionAdapter`、WebSocket、目标序号和诊断状态。`close_connection_on_return=False` 不再用于 strict/background realtime client；一次观察结束即关闭自己的 Provider 会话，禁止跨帧携带隐式 conversation state。

Observer 不设置“只允许一个 VLM”的全局锁或 semaphore。五个 task 必须能重叠运行；Provider 限流作为显式错误记录到对应 sequence，不允许退回共享单飞。后续若需要全局容量治理，应另加不阻塞 target lane 的调度器，不能改变本设计的目标屏障。

### 4. 目标帧完成屏障

fast Agent 调用 live-view Tool 时：

```text
wait_for_sequence(video_id, exact=target_sequence, deadline=4s)
```

`wait_for_sequence` 只在以下任一条件发生时返回：

- target 成功发布；
- target 明确失败；
- 4 秒 deadline 到达；
- session/store 关闭。

它不检查第 4–7 帧是否完成，也不等待 observer idle。target 成功后同一调用栈立即读取窗口内 ready subset 并返回 ToolNode；fast Agent 可立即合成和流式发送回答。

target 失败或超时时，Tool 返回：

```json
{
  "status": "unavailable",
  "target_sequence": 8,
  "target_status": "failed_or_timeout",
  "usable_visual_text": false
}
```

不得将 sequence 7 或更旧 snapshot 作为当前事实。

### 5. 窗口投影

给 `SessionVisualSemanticStore` 增加按 sequence 范围读取接口：

```python
records_in_sequence_range(
    video_id,
    *,
    start_sequence: int,
    end_sequence: int,
) -> list[VisualSemanticRecord]
```

target 成功时，Tool 只读取 `[window_start_sequence, target_sequence]` 中当时已经成功的记录。例如 4、5、6、8 已完成而 7 未完成，则立即返回 4、5、6、8，并标记：

```json
{
  "window_start_sequence": 4,
  "target_sequence": 8,
  "ready_sequences": [4, 5, 6, 8],
  "missing_sequences": [7],
  "target_ready": true
}
```

模型观察中把 sequence 8 明确标记为 `target`，其他记录标记为 `context`。历史记录晚到只写 semantic store，不回写已经完成的 Graph run。

### 6. 生命周期与清理

- raw store 保留最近五帧；observer enqueue 成功后拥有自己的 hard-link/copy，raw window 后续淘汰不影响观察。
- chat 取消或 4 秒超时不取消 observer-owned VLM task；任务继续完成并写入历史。
- session close 会停止接收新任务、等待/取消受控任务、关闭每个 frame-owned service，并删除未发布文件。
- 同序号重复 promotion 必须返回 reused，不创建第二个文件、client 或 trace。
- 任何复制、client 创建、Provider 调用或 semantic publish 失败都只结算对应 sequence，不影响其他帧。

### 7. 可观测性

每帧保留独立 `vlm.infer` generation，并增加安全字段：

- `visual_window_id`（随机 ID，不含用户内容）；
- `frame_sequence`；
- `window_start_sequence` / `target_sequence`；
- `window_role=target|context|background`；
- `observation_reused`；
- `provider_connection_isolated=true`。

窗口 barrier 增加 started/finished 事件，记录 `wait_ms`、`target_status`、`ready_count`、`missing_count`，不记录图片路径、用户文本、VLM 正文或 Provider 原始响应。

LangGraph 条件路由 span 的 input/output 显示 `fast` 是 `route_execution_mode()` 的正常分支值，不是 VLM input/output 被改写。本方案不伪造或重建 shadow trace；只让五个 VLM generation 的 sequence 和父子关联更明确。

## 不变量

1. 生产主链仍为 Agent Server + `AssistantRootGraph`；Media route 只做协议归一化和可信窗口边界投影。
2. live-view 仍通过标准 `BaseTool -> ToolNode` 返回结构化结果。
3. 默认 mock/offline；真实 Provider 必须显式 real mode、完整配置和 operator 授权。
4. 不按用户关键词选择窗口或 Tool；窗口来自已完成的 video handshake 与可信 media content block。
5. 不把图片、路径、VLM 原文或 Provider payload 写入 Graph state、日志或 eval artifact。

## 验收标准

- 第 8 帧提问会冻结 4–8；五个 fake VLM 调用能够同时处于 running。
- 人为阻塞第 7 帧、先完成第 8 帧时，live-view Tool 在第 8 帧发布后立即返回，`missing_sequences=[7]`。
- 第 9 帧先完成也不会进入第 8 帧对应回答。
- 第 8 帧失败/超时不回退到第 7 帧作为当前画面。
- 同序号背景 selection 与 strict promotion 只产生一次 VLM 调用和一份受控文件。
- 真实 trace 中五帧是五个独立 generation/Provider connection，target wait 不包含第 7 帧尾延迟。
