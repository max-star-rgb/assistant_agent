# SigLIP2 + SSIM 实时关键帧设计

日期：2026-08-03

## 1. 目标与范围

实时视频观察器使用 SSIM 与本地 SigLIP2 image tower 综合选择关键帧，最大静态关键帧间隔为
10 秒。当前版本只执行图像 embedding，不加载 tokenizer 或 text tower，不新增视觉关注、文本检索
或主动通知行为。

选用完整 SigLIP2 模型族是为了让后续版本能够在不替换历史图像向量空间的前提下，增加文本到画面
的检索与关注能力。本轮只预留接口、模型标识和 embedding 元数据，不提前实现未批准的产品功能。

不变边界：

- Runtime 根据可信 `entry_profile + video_ids` 暴露 `live_view_inspect`；LLM 自主决定是否调用。
- 后台关键帧选择和视觉文本预热不把帧、embedding 或视觉文本被动写入主 LLM prompt。
- `live_view_inspect` 的 A 时刻目标冻结、最多等待 10 秒及不得消费 A 之后结果的语义保持不变。
- SigLIP2 只负责选帧 embedding，不生成视觉描述；关键帧文本仍由受治理的视觉理解链路生成。

## 2. 模型与部署

采用 `google/siglip2-base-patch16-224` 的 vision tower。运行时模型导出为本地 ONNX 图，使用
ONNX Runtime CUDA FP16 推理；输出取模型的 pooled image embedding，并在比较前做 L2
归一化。预处理参数必须随模型资产清单固定，不能由调用方自行猜测。

模型权重、ONNX 文件和缓存不进入 Git。Runtime 只读取显式配置的本地模型目录，不在服务启动或
请求过程中联网下载。模型目录包含：

- vision ONNX 文件；
- 预处理配置；
- 原始 Hugging Face model id/revision；
- 输出维度和资产 checksum；
- embedding schema version。

本轮运行时可选依赖限定为 `numpy`、`Pillow` 和 `onnxruntime-gpu`。模型下载/导出所需的
`torch`、`transformers`、`optimum` 不作为线上 Runtime 强制依赖。进程内共享一个惰性初始化的
CUDA session，避免每个连接重复占用显存。

默认 mock/offline 测试不加载真实模型。真实本地模型必须通过显式 provider 配置和完整本地模型路径
启用；缺少依赖、模型文件、checksum 不匹配或 CUDA session 初始化失败时 fail closed 为
`SSIM-only`，返回可观测错误，不能再用灰度直方图冒充 semantic embedding。

## 3. 选帧策略

每个输入帧先计算廉价 SSIM 结构变化。SigLIP2 对以下帧执行 image embedding：首帧、SSIM 明显变化
候选、固定语义探测节奏到期的帧，以及 10 秒最大间隔到期的帧。固定语义探测使语义变化能够在 SSIM
未越阈值时独立触发；具体探测频率作为配置项，初始值 2 FPS，并受输入帧率上限约束。

相对上一关键帧计算：

```text
structural_change = 1 - SSIM(current, last_keyframe)
semantic_change   = 1 - cosine(siglip_image(current), siglip_image(last_keyframe))
combined_change   = 0.4 * structural_change + 0.6 * semantic_change
```

满足任一条件即选中：

1. `structural_change >= 0.35`；
2. `semantic_change >= 0.18`；
3. `combined_change >= 0.25`；
4. 距离上一关键帧达到 10 秒。

这些阈值是初始工程默认值，必须可配置，并通过录制视频的离线评测校准。像素差可继续作为采样调度的
廉价诊断信号，但不再计入最终关键帧得分，也不称为 semantic change。首帧必选；最大静态间隔默认
且固定为 10 秒配置值。

选中关键帧时同时缓存其归一化 image embedding，后续候选只需计算当前帧 embedding。缓存随
observer/video 生命周期释放，不跨用户共享。

## 4. 后续 text tower 预留

provider 的当前最小契约仍为 `embed_image(frame)`。结构化结果额外稳定记录：

- `model_family=siglip2`；
- 完整 model id/revision；
- `embedding_space_id`；
- dimension、normalization 和 schema version。

后续可以在同一 provider family 增加独立的 `embed_text(text)` 能力，但不能让当前调用者依赖尚未
实现的方法。只有 image/text 的 `embedding_space_id` 完全一致，才允许执行跨模态相似度。

该预留可支持后续单独评审的能力：

- 用户显式创建视觉关注目标；
- 通话内按自然语言检索历史关键帧；
- 用当前任务文本调整候选关键帧相关性；
- 开放词表候选分类。

这些能力必须通过新的受治理 Tool 或结构化显式 opt-in 启用。Runtime 不得从普通用户文本关键词
自动创建关注任务，也不得因为 text tower 存在就向 LLM 暴露后台视觉内容。

## 5. 配置与兼容

新增本地 `siglip2` vision embedding provider 选项以及本地模型目录、CUDA device、语义探测 FPS、
SSIM/semantic/combined 阈值配置。默认 provider mode 仍是 mock；现有 DashScope adapter 保持兼容，
但 realtime observer 显式选择本地 SigLIP2 后不得静默调用远程 embedding API。

现有 `VisionEmbeddingResult` 继续作为结构化边界，并扩展可选 embedding-space 元数据。模型推理错误
进入 `FrameProcessingResult.errors` 和安全日志；不记录图片内容、完整本地路径或 embedding 数值。

## 6. 验证

本功能属于具体视频选帧实现，不修改 `tests/core/INVARIANTS.md` 中的 core invariant。临时 TDD
验证覆盖：

- SSIM 与 SigLIP2 语义信号可独立触发；
- 加权组合可触发；
- 静态画面恰在 10 秒强制选帧；
- image embedding 缓存和 L2/cosine 计算正确；
- mock/offline 不加载模型或访问网络；
- 本地模型不可用时明确退化为 SSIM-only，直方图不冒充 semantic；
- observer 使用 10 秒配置且保留 A 时刻目标语义。

真实 GPU 时延和阈值质量不由 pytest 证明。模型资产准备完成后，使用显式 system eval 记录预处理、
模型 revision、GPU、预热后 P50/P95 推理时延、选帧率和代表性视频误触发/漏触发结果；不调用真实
视觉文本 Provider 来证明本地 embedding。

## 7. 验收标准

- 实时 observer 的最终选帧信号为 SSIM + SigLIP2 image embedding，二者可独立触发。
- 最大静态关键帧间隔为 10 秒。
- 当前运行路径不加载或调用 text tower。
- 配置和结果元数据足以让后续 text tower 与既有 image embedding 使用同一向量空间。
- 缺少真实模型时行为可解释且保持离线安全，不产生伪 semantic 分数。
- `live_view_inspect` 的工具暴露、A 时刻冻结和等待语义不发生回归。
