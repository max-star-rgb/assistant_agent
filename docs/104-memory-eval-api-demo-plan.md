# 104 Memory Eval / API / Demo Plan

## 目标

为 memory hardening 增加 eval、API、demo runner 覆盖，确保记忆能力不是只存在于 store 层。

## Eval 分类

建议新增 suite：

```text
memory
```

覆盖：

```text
preference_memory_retrieval
product_memory_retrieval
artifact_memory_retrieval
task_memory_retrieval
memory_to_image_generation
memory_to_render
memory_user_isolation
memory_missing_context_followup
memory_delete
```

## Eval 示例

### 偏好记忆

第一轮：

```text
我喜欢日系极简、浅色背景。
```

第二轮：

```text
按我喜欢的风格生成一张商品图。
```

期望：

```text
memory_retrieval → image_generation
```

### 商品记忆

第一轮：

```text
记住这款黑色双肩包。
```

第二轮：

```text
把上次那个包放到客厅里渲染。
```

期望：

```text
memory_retrieval → render_3d
```

### 用户隔离

用户 A 保存偏好，用户 B 查询“我喜欢的风格”，不应返回用户 A 的记忆。

## API 覆盖

建议支持或测试：

```text
POST /memory/save
POST /memory/search
DELETE /memory/{memory_id}
```

如果当前 API 不适合新增 endpoint，可以先测试 runtime-level memory behavior。

## Demo Runner 覆盖

新增 demo scenarios：

```text
memory_preference_to_image_generation
memory_product_to_render
memory_task_resume
memory_user_isolation
```

## 默认安全

- 默认本地 memory。
- 不调用外部 memory service。
- 不提交真实用户记忆。
- 不保存敏感数据。
- eval 使用 mock memory 数据。

## 验收标准

- memory eval suite 存在。
- demo runner 有 memory scenarios。
- API 或 runtime-level memory tests 存在。
- user isolation 有测试。
- default pytest / eval / demo 离线。
