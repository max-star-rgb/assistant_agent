# AI Coding Stage 4B3：通用 Artifact 治理设计

日期：2026-08-20

## 1. 目标与范围

Stage 4B3 在 Stage 4A 的 network-none sandbox、Stage 4B1 的受控 egress 和 Stage 4B2 的 credential broker
之上，增加 repository 静态配置驱动的通用 artifact ingress 与 build-output egress。artifact 不是模型 Tool，
不引入第二套 Runtime，也不允许模型、客户端或 repository 内容决定 host、scanner image、宿主路径或导出规则。

首版只支持：

- repository-relative JSON artifact manifest 变化触发的 ingress intent；
- exact HTTPS 443 URL、静态 host allowlist、声明 size 与 SHA-256 的普通文件；
- 独立 digest-bound `coding_artifact_ingress` HITL；
- digest-pinned fetcher/proxy/scanner protocol image；
- 通过受管临时 bundle 只读复制进 network-none validation sandbox；
- command 配置声明的 exact relative build output；
- sandbox 停止后、删除前由 backend 导出，再执行类型、数量、大小、SHA-256 与 scanner 校验；
- 只返回 opaque bundle ref 和 provenance，不把宿主路径或二进制写入 checkpoint。

不支持用户上传、任意 URL、目录递归、symlink、archive 自动解压、脚本执行、容器镜像、Git/VCS、对象存储
credential、验证容器联网、artifact 发布、push、PR 或部署。Stage 4B2 credential gateway 不自动扩展到 artifact；
需要私有 artifact 时必须后续建立独立 credential scope 与审批。

## 2. 配置契约

repository 可选声明 `artifact_profile`：

- `profile_id`、`manifest_path`、`trigger=manifest_changed`；
- `allowed_hosts`、`allowed_ports=(443,)`；
- digest-pinned `fetcher_image`、`proxy_image`、`scanner_image`；
- `allowed_media_types`；
- ingress 的 file/total/count/time 上限；
- `exports`：command ID 到 exact repository-relative output path、media type、单文件上限的静态映射；
- export 总数与总字节上限、bundle TTL。

启用 artifact profile 必须同时启用 Stage 4A sandbox。manifest 和 export path 不得位于 `.git`、不得绝对、
不得包含 `.`/`..`、反斜线、NUL 或换行。image 必须固定完整 RepoDigest。

## 3. Ingress manifest 与审批

manifest schema 固定为 `coding_artifacts_v1`，每项仅含 `artifact_id`、`url`、`filename`、`media_type`、
`size_bytes`、`sha256`。URL 必须是 HTTPS、无 userinfo/query/fragment、port 省略或 443、host 精确命中静态
allowlist；filename 是 bundle 根目录普通 basename。artifact ID、filename 唯一。

Graph 在 patch apply 后重新读取 manifest 并构造 `CodingArtifactIngressPlan`。interrupt 只投影 profile、manifest
digest、artifact count、hosts、budgets、policy digest 与 plan digest，不携带完整 URL 或宿主路径。resume 后重新读取、
解析并比对 plan digest；任何漂移失效。

## 4. 隔离 fetch、scan 与 sandbox 消费

fetcher 只加入 Docker internal network，通过 Stage 4B1 同等级 exact-host proxy 访问外部 HTTPS。fetcher 镜像使用
固定入口和 protocol label；输出位于受限 tmpfs。宿主导出后重新校验普通文件、basename、数量、声明 size、实际
size 和 SHA-256。

scanner 使用独立 network-none 容器、只读 rootfs、drop capabilities、non-root 与受限 tmpfs。输入由 `docker cp`
复制，固定 scanner 入口只返回每个 artifact 的 `clean|rejected` 结构化结果及 scanner policy digest。scanner 不
执行 artifact、不解压 archive、不把内容或宿主路径写入日志。全部 clean 后生成 `CodingArtifactIngressManifest`，
其 digest 绑定 ingress plan、文件 provenance 与 scanner policy。

validation service 在一次节点内持有临时 bundle；sandbox backend 启动前复制到 `/artifacts/input`。trusted runner
只把该目录作为只读输入暴露给固定 command，sandbox 始终 `network none`。bundle 在 validation 返回前清理，
清理失败覆盖成功结果。

## 5. Build-output egress

只有 server-owned command config 声明的 exact output 才可导出。trusted runner 在命令成功后验证 output 是
`/workspace` 内普通非 symlink 文件，计算 size/SHA-256，并在 runner JSON 返回 metadata；不得把内容编码进 JSON。

backend 在 container stopped 且 state/payload 均通过后、container remove 前，把精确路径逐个 `docker cp` 到
backend 分配的空目录。宿主重新验证 basename、普通文件、无 hardlink/symlink、size/hash 与声明一致，然后交给
同一 digest-pinned scanner image做 network-none 扫描。失败、formatter command、未声明路径或多余输出均不导出。

成功产物移动到 workspace root 下的受管 bundle，返回 HMAC/随机 opaque `bundle_ref`、过期时间和
`CodingArtifactExportManifest`。Graph/checkpoint/evidence 只保存 ref、relative logical name、media type、size/hash、
scanner/policy/manifest digest；不保存宿主 path、container ID 或内容。Stage 4B3 不提供下载 route 或发布动作。

## 6. 错误与清理

稳定错误包括：`artifact_manifest_invalid`、`artifact_approval_required`、`artifact_approval_mismatch`、
`artifact_ingress_rejected`、`artifact_fetch_failed`、`artifact_scan_failed`、`artifact_export_invalid`、
`artifact_cleanup_failed`、`artifact_unconfigured`。

container、network、bundle、scanner、copy、lease 或 cleanup 状态不确定均 fail closed，并阻止 controlled commit
与 merge。owner shutdown 只按 owner label 回收自身资源；受管 bundle 按 TTL 清理。

## 7. 测试与验收

临时 TDD 使用 `tests/tdd/ai-coding-artifact-governance`，强制 mock/offline。覆盖配置、manifest、digest/HITL、
Docker topology、scan、sandbox ingress、declared export、provenance、cleanup 与 composition。Stage 4B1/4B2、
Stage 4A、Stage 2/3、core 与 authority validator 作为回归；真实 artifact endpoint、Docker image 或 Provider 不自动
调用。Core invariant 不变。

