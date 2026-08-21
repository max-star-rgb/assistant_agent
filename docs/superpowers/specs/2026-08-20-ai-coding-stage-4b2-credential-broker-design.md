# AI Coding Stage 4B2：Credential Broker 与私有 Registry 设计

日期：2026-08-20

## 1. 范围

Stage 4B2 在 Stage 4B1 wheel-only 下载链上增加 operator-owned 私有 Python registry 凭据。当前 Agent Server
使用 tokenless developer identity，因此首版禁止按客户端 identity 选择凭据；repository 只能引用服务端静态
credential profile。

首版仍只支持 hash-pinned binary wheels、HTTPS exact host 和 443。它不支持用户上传 secret、云身份、SSH agent、
Git credential、任意 HTTP header、其他 ecosystem、push/PR 或部署凭据。

## 2. 信任拓扑

普通 CONNECT proxy 无法在 TLS 隧道内安全注入 upstream Authorization。Stage 4B2 使用 digest-pinned trusted
registry gateway：

    downloader -- internal HTTP --> registry gateway -- HTTPS + upstream credential --> private registry

gateway 终止内部请求、校验固定 path/method/host、向单一 HTTPS upstream 添加 Authorization，并重写受信下载链接。
downloader 只能访问 internal network，不获得 upstream token。gateway 是唯一双网卡成员。

## 3. 配置

dependency profile 可选引用 credential_profile_id。独立 operator credential 配置声明：

- credential_profile_id
- registry_host 与 registry_base_path
- auth_scheme，首版只能 bearer
- secret_env，必须使用 MULTIMODAL_AGENT_CODING_CREDENTIAL_ 前缀
- lease_ttl_seconds，30 到 900
- gateway_image，完整 RepoDigest并带 credential gateway protocol label

repository JSON 与 Graph state 只保存 credential profile ID，不保存 secret_env 的值。mock/offline 测试只使用假 broker。

## 4. 独立审批

dependency approval 通过后，如 profile 引用 credential，再发出 coding_credential_lease interrupt。payload 只包含：

- credential profile ID
- registry host/base path
- auth scheme
- lease TTL
- dependency plan digest
- credential policy digest
- credential request digest

approve 必须绑定 request digest。resume 后重新解析 dependency plan 与 credential policy；漂移即失败。reject 阻止 fetch、
validation、commit 和 merge。

## 5. Broker 与 lease

CredentialBroker 返回进程内不可序列化 lease：

- opaque lease ID
- issued/expires monotonic deadline
- exact registry scope
- mutable secret bytearray

lease 不进入 Pydantic Graph model、checkpoint、日志或 exception。使用结束、失败、cancel 或 timeout 都覆盖 secret buffer
并关闭 lease。过期 lease 禁止注入。

首版 EnvironmentCredentialBroker 只读取专用前缀 env；它提供进程内最小暴露生命周期，不声称把静态 upstream token
转换为真正可撤销的 provider token。后续 provider exchange adapter 可实现真实短期 token。

## 6. Secret 注入

secret 不进入 docker create/exec argv、environment、policy JSON、bind mount、docker cp、inspect 或日志。gateway 启动后，
宿主通过 docker exec -i 调用固定 credential loader，并从 stdin 发送有界 binary envelope。loader 将 secret 写入
gateway tmpfs 的 mode 0600 文件，确认 digest 后 gateway 才 ready；downloader 之后才启动。

所有 Docker CLI stdout/stderr 继续有界；secret bytes 不能进入错误消息。注入完成后宿主立即零化临时 buffer。

## 7. Evidence 与清理

evidence 只包含 credential profile ID、policy/request digest、lease ID digest、issued/expires time、inject status 和 cleanup
status，不包含 secret、secret hash、env name、Authorization 或 gateway内部路径。gateway/container/network/lease 任一
cleanup 不确定状态 fail closed，并保留已产生 command evidence。

## 8. 验收

1. 配置默认关闭；credential profile 必须与 dependency profile、registry host和gateway image一致。
2. 客户端 identity、messages、Tool 或 resume payload不能选择 credential ID或提交 secret。
3. 独立 HITL digest mismatch与workspace/policy drift fail closed。
4. downloader create/inspect/env/files均不含 upstream secret。
5. secret只经 bounded stdin进入gateway tmpfs，过期前检查，finally零化。
6. gateway未 ready 时不得启动downloader。
7. 失败 evidence保留profile/request/lease事实但不含secret材料。
8. Stage 2/3/4A/4B1、core、authority与独立安全review通过。

