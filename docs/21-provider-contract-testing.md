# 21 Provider Adapter 契约测试设计

## 目标

真实 Provider 接入前，先建立统一契约测试，确保每个 Adapter 不管背后接什么服务，都返回项目统一 Schema。

## 原则

单元测试默认只使用 MockAdapter。

真实 Provider 测试必须显式开启：

```bash
RUN_INTEGRATION_TESTS=1 python -m pytest tests/integration
```

## 契约测试覆盖

每个 Adapter 至少覆盖：

- 输入 Schema 校验。
- 输出 Schema 校验。
- 错误处理。
- 超时/失败时不抛出未捕获异常。
- Tool 层不感知具体 Provider。

## 推荐目录

```text
tests/contracts/
├── test_vision_adapter_contract.py
├── test_product_adapter_contract.py
├── test_image_generation_adapter_contract.py
└── test_render_adapter_contract.py

tests/integration/
├── test_real_vision_provider.py
├── test_real_image_provider.py
└── conftest.py
```

## Mock 与真实 Provider 区别

Contract tests 必须默认运行，使用 MockAdapter 验证统一接口。

Integration tests 默认 skip，需要环境变量，可调用真实服务。

## 验收标准

- Contract tests 默认运行。
- Integration tests 默认 skip。
- 无 API Key 泄漏。
- Provider 配置缺失时给出明确 skip reason。
