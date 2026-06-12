# 37 API Key 与环境变量安全规范

## 核心原则

API Key 只能通过环境变量或本地未提交的 `.env.local` 提供。

禁止：

```text
把 API Key 写进源码
把 API Key 写进测试
把 API Key 写进文档
把 API Key 提交到 Git
把 API Key 发给 Codex 聊天窗口
```

## 推荐文件

```text
.env.example      # 可以提交，只放变量名和说明，不放真实值
.env.local        # 本地使用，必须被 .gitignore 忽略
```

## .gitignore 要求

确保包含：

```gitignore
.env
.env.*
!.env.example
.local/
```

## 推荐环境变量

```bash
MULTIMODAL_AGENT_VISION_PROVIDER=mock

# OpenAI Vision Provider
OPENAI_API_KEY=
OPENAI_VISION_BASE_URL=
OPENAI_VISION_MODEL=

# Qwen Vision Provider
QWEN_API_KEY=
QWEN_VISION_BASE_URL=
QWEN_VISION_MODEL=

# Integration Gate
RUN_INTEGRATION_TESTS=0
```

## 运行边界

默认：

```bash
python -m pytest
```

不得调用真实 Provider。

显式集成测试：

```bash
RUN_INTEGRATION_TESTS=1 python -m pytest tests/integration
```

缺少配置时应 skip 或返回 `provider_unconfigured`，不得因为缺 key 让默认测试失败。

## Codex 执行规则

Codex 可以创建或更新 `.env.example`，但不得创建包含真实密钥的 `.env` 或 `.env.local`。

如果 Codex 认为需要 API Key，应停止并提示用户在本地 shell 中设置环境变量。
