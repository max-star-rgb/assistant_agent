# 临时 TDD 开发区

`tests/tdd/<feature>/` 只用于功能开发期间的 RED/GREEN 临时 pytest。每个功能必须使用自己的
`<feature>` 子目录；禁止把 `test_*.py` 或 `*_test.py` 直接放在 `tests/tdd/` 根目录。

显式运行某个功能的临时测试：

```bash
python -m pytest -q tests/tdd/<feature>
```

- 临时测试只能使用 mock、local、offline 能力，不得访问真实 Provider、真实外部服务或付费 API。
- `tests/tdd/conftest.py` 会在显式运行时强制
  `MULTIMODAL_AGENT_PROVIDER_MODE=mock`。
- 裸 `pytest` 默认只收集 `tests/core`，不会收集这里的临时测试。
- TDD 测试不要求 `core_invariant` marker，也不会自动晋升为核心测试。只有稳定核心不变量发生变化并
  通过核心准入审查时，才可另行改写并登记到 `tests/core`。
- Codex 不得擅自删除 `tests/tdd/<feature>/`。功能完成后，用户可以手动删除整个 feature 目录；只有
  用户明确要求时 Codex 才可代为删除。
