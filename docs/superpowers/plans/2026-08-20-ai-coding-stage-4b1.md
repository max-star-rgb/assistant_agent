# AI Coding Stage 4B1 实施计划

日期：2026-08-20

## 约束

- 设计权威：docs/superpowers/specs/2026-08-20-ai-coding-stage-4b1-dependency-egress-design.md
- 分支：feat/ai-coding-stage-4b1；worktree：.worktrees/ai-coding-stage-4b1
- 临时测试：tests/tdd/ai-coding-dependency-egress，不提交、不自动删除
- 默认 mock/offline，不访问 registry、不调用真实 Provider
- 不启动、停止或重启 8089；合并后只检查 /ok
- spec 与 plan 默认不提交
- Core invariant: unchanged；不修改 tests/core

## Task 1：配置、lockfile parser 与 plan digest

修改 config.py、models.py；新增 dependencies.py 和临时 contracts 测试。

RED：

- 非 digest image、wildcard/IP host、非 443、sandbox disabled、非法 lockfile 全部拒绝
- 合法 lockfile 生成稳定 package set、lockfile/policy/plan digest
- plan schema 不含 argv、proxy URL、network、host path 或 Docker flags

GREEN：

- 增加严格 CodingDependencyProfile
- 实现有限 requirements lock parser
- 实现 CodingDependencyPlan 与 CodingDependencyApprovalDecision

提交：feat: define governed dependency plans

## Task 2：Graph 独立审批

修改 state.py、coding_graph.py；新增 dependency approval 临时测试。

RED：

- 非 lockfile patch 不触发 interrupt
- lockfile patch 在 apply 后、validation 前触发 coding_dependency_install
- approve 必须匹配 plan digest
- reject 与 resume drift 阻止 validation 和 integration
- formatter lockfile 变化重新审批

GREEN：

- 增加 plan_dependencies 与 dependency_approval 节点
- state 只保存 plan、approval status、结构化 evidence
- resume 重新解析当前 lockfile并校验 digest

提交：feat: add dependency installation approval

## Task 3：有界 wheelhouse 与 provenance

扩展 dependencies.py；新增 wheelhouse 临时测试。

RED：

- symlink、nested path、非 wheel、数量/大小超限、hash 不匹配和额外 package/version 均拒绝
- manifest digest 对 profile/lockfile/policy/wheels 任一变化敏感
- temporary bundle 无条件清理

GREEN：

- 实现 CodingDependencyBundle、manifest 和 managed temporary lifecycle
- 使用 wheel filename parser 与 lockfile hash 校验

提交：feat: validate dependency wheelhouse provenance

## Task 4：Docker egress proxy 与 downloader backend

新增 dependency_egress.py；修改 services.py；新增 Docker lifecycle 临时测试。

RED：

- proxy/downloader protocol label 缺失时 fail closed
- downloader 只加入 internal network，proxy 是唯一双网卡成员
- policy 通过 docker cp 注入，无 host mount、Docker socket、secret 或任意 env
- lifecycle 任一不确定状态均失败
- owner cleanup 只清理自己的 containers/networks

GREEN：

- 定义 CodingDependencyFetcher protocol 和 disabled backend
- 实现 Docker lifecycle、预生成 name、固定 hostname/labels、bounded pipe
- 导出 wheelhouse 后交给 Task 3 validator
- 仅在存在 dependency profile 时构造 backend

提交：feat: add governed dependency egress backend

## Task 5：Stage 4A 离线消费

修改 sandbox.py、sandbox_runner.py、validation.py、models.py；新增 offline validation 临时测试。

RED：

- wheelhouse 只在 start 前复制到 /dependencies
- validation create 始终 network none
- runner 固定 offline pip argv，request 不能覆盖
- install failure与command failure evidence 分离
- bundle cleanup failure阻止commit/merge

GREEN：

- sandbox request只接受受信bundle contract，不接受任意dependency argv
- runner先离线安装到tmpfs target，再执行原command
- validation service在单节点内fetch、verify、consume、cleanup

提交：feat: consume approved dependencies offline

## Task 6：Authority、配置示例与复核

修改 .env.example、三个 authority 文档和 docs/authority.toml。

1. 同步 Graph approval、proxy/downloader lifecycle、provenance 和离线验证边界。
2. 运行 Stage 4B1 TDD。
3. 运行存在的 Stage 4A 与 Stage 2/3 最小回归。
4. 运行 agent-server/runtime/tool-calling 定向 core。
5. 运行 authority validator。
6. 请求独立安全 review；Critical/Important 清零后才提供 merge。
7. 没有 operator 协议镜像时，真实 smoke 报 unconfigured，不自动 pull/build。

提交：docs: document governed dependency egress

## 验证

- MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q tests/tdd/ai-coding-dependency-egress
- MULTIMODAL_AGENT_PROVIDER_MODE=mock python -m pytest -q agent-server/runtime/tool-calling 定向 core
- python scripts/check_documentation_authority.py --repo-root .

## 完成汇报

Core invariant: unchanged.
Tests: added/updated tests/tdd/ai-coding-dependency-egress for temporary RED/GREEN; user may delete it manually.
Provider: 未调用真实 Provider。
Docker smoke: 协议合规 image 的结果，或 unconfigured。
Limitations: 仅 public HTTPS Python wheels；无 secret、私有 registry、其他 ecosystem、通用 artifact、push/PR。

