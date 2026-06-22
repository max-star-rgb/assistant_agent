# Task 009：3D 渲染适配器

## Goal

实现 3D/场景渲染工具接口和 mock 输出，为 Blender/Unity/Three.js 等后端预留适配层。

## Read first

- `docs/05-tool-contracts.md`
- `docs/07-service-api.md`

## Scope

新增/修改：

```text
src/multimodal_agent/services/render_adapter.py
src/multimodal_agent/tools/render_tool.py
tests/unit/test_render_adapter.py
```

## Steps

1. 定义 `RenderAdapter`。
2. 实现 `MockRenderAdapter`。
3. 输入商品、目标场景、材质、光照、镜头参数。
4. 返回 render_id、task_status、preview_url。

## Acceptance

```bash
pytest tests/unit/test_render_adapter.py
```

必须验证：

- “放到客厅看看”可以创建 mock render task。
- 返回 preview_url 和 task_status。

## Out of scope

- 不调用 Blender/Unity。
- 不创建真实 3D 模型。
