# AI Coding Stage 4B3 实施计划

日期：2026-08-20

## Task 1：配置、manifest 与 digest 契约

- 在 `coding/config.py` 增加 disabled-by-default artifact profile 与 export allowlist。
- 在 `coding/models.py` 增加 ingress plan/approval/manifest、scan 与 export provenance model。
- 新增 `coding/artifacts.py`，严格解析 manifest、生成 plan/interrupt、校验 approval 与本地 bundle。
- 临时 TDD 固定 URL、path、hash、size、image、budget、schema 与 deterministic digest 边界。
- 提交：`feat: define coding artifact governance contracts`。

## Task 2：原生 Graph ingress HITL

- 在 dependency/credential gate 后增加 `plan_artifacts` 与 `artifact_approval`。
- resume 重新解析 manifest 并比较 plan digest。
- no-intent 保持既有 validation；reject/mismatch 结构化终止。
- 提交：`feat: gate coding artifact ingress with HITL`。

## Task 3：隔离 fetch、scan 与 sandbox ingress

- 新增 process-owned Docker artifact backend，internal proxy fetch、host revalidation、network-none scanner。
- validation service 使用临时受管 ingress bundle并传给 sandbox request。
- sandbox backend 只复制到固定 `/artifacts/input`，runner 接收 digest-bound manifest metadata。
- 全部 Docker/bundle cleanup fail closed；composition 只构造并关闭一份 backend。
- 提交：`feat: scan coding artifact ingress`。

## Task 4：声明式 build artifact egress

- runner 只返回 declared output metadata，不返回二进制。
- sandbox backend 在 stop/remove 窗口精确导出并重新校验。
- network-none scanner 扫描后生成受管 bundle 与 opaque ref；TTL cleanup。
- evidence 绑定 command、policy、file hash、scanner 与 manifest digest。
- 提交：`feat: govern coding build artifacts`。

## Task 5：文档、全量验证与安全审查

- 同步 `.env.example`、Agent Server/tool-calling authority 和 `docs/authority.toml`。
- 运行 4B3、4B2、4B1、4A、Stage 2/3、core 与 authority validator。
- 独立复核 URL/SSRF、archive/path、scanner、binary transport、cleanup、checkpoint 与 provenance。
- 修复全部 Critical/Important 并复核。
- 规格、计划和 `tests/tdd/**` 保持未提交。

