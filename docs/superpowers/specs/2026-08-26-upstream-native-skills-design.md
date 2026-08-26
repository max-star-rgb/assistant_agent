# 上游原生 Skills 体系迁移设计

## 背景

当前实现同时存在两套相互重叠的 Skill 生命周期：Deep Agents `SkillsMiddleware.before_agent` 负责发现
`SKILL.md` 元数据，项目又在构图阶段手动调用该 hook 生成另一份目录快照；正文和 reference 则通过项目自研
`load_skill`、`load_skill_reference`、`loaded_skill_ids` 和 `skill_reference_grants` 读取与授权。这造成目录来源重复、
Planner/worker state key 分裂，以及两个自研读取 Tool 同批执行时的先后竞态。

## 目标

- 直接采用当前锁定 Deep Agents 的原生 Skill 使用方式：`SkillsMiddleware + FilesystemMiddleware(read_file)`。
- Skill 目录只由 `SkillsMiddleware.before_agent` 发现，并由同一 middleware 注入模型 system message。
- `SKILL.md` 正文和 supporting files 都通过上游 `read_file` 读取。
- fast 与 planning coordinator 使用相同的 Skill 发现和读取机制。
- 保持 Skill 与 Tool Profile 分离；读取 Skill 不授予业务 Tool。
- 保持 Skill 文件系统只读且限制在仓库 `skills/` 虚拟根内，不重新暴露宿主任意文件。

## 非目标

- 不保留项目自研的 Skill ID-only 读取协议。
- 不保留 reference grant、已加载 Skill ID 或 Planner→worker 的 Skill state 映射。
- 不修改 Deep Agents 或 LangChain 上游包源码。
- 不让 Skill metadata、正文或路径成为业务 Tool 授权来源。

## 目标架构

每个 fast/planning `create_agent` 装配以下上游 middleware：

```text
SkillsMiddleware
  before_agent      扫描 skills/，写入私有 skills_metadata
  modify_request    从该 state 生成 Skill 目录和渐进披露说明

FilesystemMiddleware(tools=["read_file"])
  tools             注册上游标准 read_file
  backend           只指向仓库 skills/ 的 FilesystemBackend 虚拟根
```

不再在构图阶段调用 `SkillsMiddleware.before_agent({})`，也不再由 fast/planning 自定义 prompt 渲染 Skill
名称与简介。`SkillsMiddleware` 的默认 prompt 和运行时 `skills_metadata` 是唯一 L0 目录来源。

模型读取完整 Skill 时，直接调用目录中提示的路径：

```text
read_file(file_path="/travel-tool-orchestration/SKILL.md", limit=1000)
```

读取 reference 或其他 supporting file 时，使用 `SKILL.md` 中提供的相对资源位置转换成同一虚拟根内的完整路径，
再调用同一个 `read_file`。不再存在 `load_skill_reference` 专用阶段或授权表。

## Planner 与 worker

planning coordinator 和共享 fast worker 各自拥有原生 `SkillsMiddleware` 私有 state。Planner 是否读取 Skill 仍由
LLM 自主决定。Planner 创建 task 时，必须把已读取 Skill 中与子任务相关的规则写入完整 description；这是 Deep
Agents 原生的上下文传递方式。

worker 不继承 Planner 的 `skills_metadata`、文件读取 transcript 或“已加载”标记；它可以根据 task description
直接执行，也可以自主再次读取相关 Skill。项目只继续传递冻结的 Memory、TrustedRuntimeFacts 和 execution mode，
并继续阻止 worker 私有 transcript/state 回写 Planner。

## 安全边界

- `FilesystemBackend(root_dir=<repo>/skills, virtual_mode=True)` 是 `read_file` 的唯一 backend；模型看到的 `/` 只是
  Skill 虚拟根，不是宿主文件系统根。
- `FilesystemMiddleware` 只注册 `read_file`，不注册 `ls`、`glob`、`grep`、写入、删除或执行 Tool。
- 路径规范化、越界拒绝、分页和错误 `ToolMessage` 使用上游实现。
- Skill 内容仍是指引，不是 Tool allowlist；业务 Tool 只能通过受信静态 composition 和 Tool Profile 暴露。
- 原有自研 `file_read` 不恢复；新增的是受限 backend 上的上游标准 `read_file`。

## 调用限制与错误

上游 middleware 注入的 `read_file` 必须进入现有通用 per-Tool 参数限制：同一规范化参数每个 invocation 最多执行一次，
不同参数最多十二次。fast/planning 仍各自最多十二次 model call，不增加跨 Tool 的总上限。

`read_file` 自身把非法路径、权限拒绝和读取失败返回为标准 error `ToolMessage`，不会出现
`skill_reference_not_loaded`。因为 reference grant 协议被删除，也不存在 `load_skill` 与 reference 同批调用的竞态。

## 代码迁移

- `skills/native.py`：只保留创建 Skill backend 所需的薄函数；删除手动 metadata snapshot 和窄读取辅助函数。
- fast/planning composition：装配上游 `SkillsMiddleware` 与只含 `read_file` 的 `FilesystemMiddleware`；删除自定义
  Skill 目录 prompt、Skill state 映射和自研 Skill Tool 装配。
- Tool inventory/plugin：删除 `SkillLoadingPlugin` 及 `load_skill`、`load_skill_reference` 的生产注册和无引用实现。
- state：删除 `loaded_skill_ids`、`skill_reference_grants`、`planner_loaded_skill_ids`、
  `planner_skill_reference_grants`。
- prompt/Skill 文档：把“调用 `load_skill`/`load_skill_reference`”改为按上游目录使用 `read_file`，移除 reference
  grant 描述。
- authority 与测试：同步 Tool Calling、Runtime、Context authority 以及 CTX-001/EXT-001 的当前事实。

## 验证

- RED/GREEN 临时测试验证：目录仅来自 runtime `before_agent`；模型可使用上游 `read_file` 读取 `SKILL.md` 和 reference；
  不再注册两个自研 Tool；路径不能逃逸 Skill 虚拟根。
- 更新现有 CTX-001 测试，验证 fast/planning 都装配上游 middleware，并且 task 仍隔离父 conversation/state。
- 更新 EXT-001，验证生产 inventory 不再包含自研 Skill loader，也不暴露宿主文件系统读取能力。
- mock/offline 运行相关 core 与临时 TDD 测试，执行文档 authority validator，并验证唯一 8089 服务重载。

## 迁移结果

迁移后只有一个 Skill 概念链：

```text
SkillsMiddleware.before_agent
  → SkillsMiddleware system prompt 目录
  → FilesystemMiddleware.read_file(SKILL.md/supporting file)
  → 按 Skill 指引执行
```

项目不再解释或维护“Skill 已加载”“reference 已授权”等上游不存在的状态。
