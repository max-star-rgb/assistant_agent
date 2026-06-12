# 13 真实 Provider 接入设计

## 目标

当前 Adapter 多数应为 Mock 实现。下一阶段开始逐步接入真实服务，但必须遵守 Adapter 隔离原则。

## 原则

Tool 永远不直接调用外部服务。

正确：

```text
Tool → Adapter Interface → Provider Adapter → External Service
```

错误：

```text
Tool → OpenAI / Qwen / ComfyUI / Blender / API
```

## 推荐接入顺序

1. Vision Provider
2. Product Search Provider
3. Image Generation Provider
4. Render Provider
5. Persistent Memory Provider

## Provider 配置

通过环境变量或配置文件注入，不要把 key 写进代码。

```text
OPENAI_API_KEY
QWEN_API_KEY
COMFYUI_BASE_URL
BLENDER_RENDER_URL
SEARCH_API_BASE_URL
```

## 测试要求

真实 Provider 测试必须和单元测试分离。

推荐：

```text
tests/unit/
tests/integration/
tests/e2e/
```

单元测试默认使用 MockAdapter。

真实服务测试必须显式打开：

```bash
RUN_INTEGRATION_TESTS=1 pytest tests/integration
```

## 验收标准

- 至少接入一个真实 Provider。
- MockAdapter 测试仍可离线运行。
- 无 API Key 泄露。
- Tool 层无需知道具体 Provider。
