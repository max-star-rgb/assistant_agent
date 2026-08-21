# AI Coding Stage 4B2 实施计划

日期：2026-08-20

## 约束

- 分支 feat/ai-coding-stage-4b2，仓库内隔离 worktree。
- 临时 TDD：tests/tdd/ai-coding-credential-broker，不提交、不自动删除。
- spec/plan 默认不提交。
- mock/offline，不使用真实 credential、registry 或 Provider。
- Core invariant: unchanged。

## Task 1：配置与 credential request 契约

修改 config.py、models.py；新增 credentials.py 和 contracts TDD。实现严格 profile、专用 env 前缀、policy/request digest、
窄 interrupt payload 与 approval decision。

提交：feat: define coding credential lease contracts

## Task 2：独立 Graph HITL

修改 state.py、coding_graph.py；新增 approval TDD。在 dependency approval 后插入 plan_credentials 与
credential_approval，resume 重建 dependency/credential request并校验 digest。

提交：feat: add coding credential lease approval

## Task 3：Broker 与零化 lease

实现 CredentialBroker protocol、EnvironmentCredentialBroker、mutable secret lease、expiry/scope检查、finally zeroize，
新增 broker lifecycle TDD。

提交：feat: add coding credential broker

## Task 4：Gateway stdin 注入与私有 fetch

扩展 dependency_egress.py 与 services.py。严格gateway image协议/entrypoint，通过bounded docker exec stdin注入，
ready后才启动downloader；任何资源/lease cleanup不确定状态fail closed。新增Docker TDD。

提交：feat: inject private registry credentials safely

## Task 5：Validation evidence、authority与复核

扩展 validation/models/Graph terminal evidence，更新 .env.example、三个authority与manifest。运行4B2、4B1、4A、3、2、
完整core、authority和独立安全review；Critical/Important清零后才提供合并。

提交：docs: document coding credential broker

