# AI Coding Stage 4B1：受控依赖下载与 Egress 设计

日期：2026-08-20

## 1. 目标与范围

Stage 4B1 在不放宽 Stage 4A 验证容器隔离边界的前提下，为受信 repository 增加显式审批的 Python 依赖下载能力。
联网仅发生在短生命周期 downloader 中；test、lint、format、build 仍在 network none 的验证容器中执行。

首版只支持 Python binary wheel、repository 静态 dependency profile、hash-pinned 精确版本 lockfile、
HTTPS 443 exact-host allowlist、独立 HITL、有界 wheelhouse 和结构化 provenance。

首版不支持 secret、私有 registry、其他 package manager、sdist、安装脚本、VCS、editable、本地路径、任意 URL、
HTTP、wildcard domain、literal IP、CIDR、验证容器联网、通用 artifact、远程 sandbox、push、PR 或部署。
私有 registry 与 credential broker 属于 4B2；通用 artifact 治理属于 4B3。

## 2. 方案选择

拒绝直接给验证容器联网：依赖安装与测试共享网络，无法证明验证阶段断网。

拒绝只注入 HTTP_PROXY/HTTPS_PROXY：环境变量不是网络边界，命令可以直接建立 socket 绕过 proxy。

采用 internal network + 双网卡 allowlist proxy + 离线 wheelhouse：

    downloader -- internal network --> egress proxy -- external network --> allowlisted registry
         |
         +-- bounded wheelhouse --> managed temporary bundle
                                          |
                                          +-- docker cp --> network-none validation

downloader 只加入 Docker internal network，没有默认外部路由。proxy 同时加入 internal network 与独立 external
network，是唯一出口。验证容器不加入任何网络。

## 3. 配置契约

repository 可选声明一个 dependency_profile，字段包括：

- profile_id
- ecosystem，首版只能是 python-pip-wheel
- lockfile_path
- trigger，首版只能是 lockfile_changed
- allowed_hosts
- allowed_ports，首版必须精确为 443
- downloader_image 与 proxy_image，均为完整 RepoDigest
- timeout_seconds、max_download_bytes、max_files

约束：

- 两个 image 都必须带对应 protocol label；
- lockfile_path 是 repository-relative 普通 UTF-8 文件，不能位于 .git，不能 symlink；
- host 是小写 IDNA-normalized exact FQDN，不允许 wildcard、末尾点、scheme、path、userinfo 或 IP literal；
- profile、host、port、image、budget 只来自服务端配置；
- dependency profile 开启时必须同时开启 Stage 4A sandbox。

## 4. Lockfile 契约

首版使用严格 requirements lock 子集：

- 每项必须是规范化 package name 加 == 精确版本；
- 每项至少一个 sha256 hash；
- 允许空行、注释和 hash continuation；
- 禁止 marker、extra、constraint/include、index/trusted-host/find-links、URL、VCS、editable、本地路径和其他 pip option；
- downloader 固定执行 pip download、require-hashes、only-binary、no-deps；
- lockfile 原文、package 列表和 digest 都有界，完整 lockfile 不进入 interrupt。

## 5. 确定性 intent 与 HITL

dependency intent 不由 LLM 或关键词推断。Patch apply 后：

1. repository 未配置 profile：直接进入既有验证。
2. approved changed paths 不含 lockfile：直接进入既有验证。
3. lockfile 变化：重新读取 worktree lockfile，校验并构造 CodingDependencyPlan。
4. Graph 发出独立 coding_dependency_install interrupt。

interrupt 只包含 profile ID、ecosystem、lockfile path/digest、package count、exact hosts/ports、download budgets、
egress policy digest 和 plan digest。approve 必须回传 plan digest；reject 产生结构化 rejected terminal。resume 后
重新解析 lockfile并比对 digest，不能信任 checkpoint 中的宿主路径或 downloader 对象。

## 6. Egress proxy

proxy image 必须声明 org.assistant-agent.coding-egress-proxy-protocol=1。宿主生成 policy JSON，并在启动前通过
docker cp 注入只读 rootfs。proxy：

- 只接受 internal network 的 CONNECT；
- 只允许 exact host + 443；
- 每次连接自行解析 DNS；
- 拒绝 loopback、private、link-local、multicast、unspecified 和 reserved IP；
- 每次 CONNECT/redirect 重新授权；
- 限制连接数、请求头、字节和 wall time；
- 不记录 URL query/header/wheel 内容，不持有 credential。

## 7. Downloader 与 wheelhouse

downloader image 必须声明 org.assistant-agent.coding-dependency-fetch-protocol=1，并以 non-root、只读 rootfs、
cap-drop、no-new-privileges 和资源限额运行。它只获得 lockfile、internal proxy 固定服务名、服务端固定 pip argv
和 wheelhouse tmpfs。

结束后宿主从 stopped container 导出 wheelhouse，并验证：

- 只含根目录普通 .whl 文件，无 symlink、device、FIFO、socket 或子目录；
- 文件数、单文件与总字节受限；
- wheel filename 的 package/version 与 lockfile 一致；
- 每个文件 SHA-256 命中 lockfile hash；
- manifest digest 绑定 profile、lockfile、policy 和全部 wheel digest。

任一 container、network、proxy、copy、scan 或 cleanup 状态不确定均 fail closed。

## 8. 离线验证消费

CodingValidationService.run 在一次节点调用内：

1. 重新校验已批准 plan 和 lockfile；
2. fetch wheelhouse；
3. 每个 Stage 4A container 启动前把 wheelhouse 复制到 /dependencies；
4. trusted runner 固定执行 pip install 的 no-index、no-deps、only-binary、target、find-links 组合；
5. 服务端固定 PYTHONPATH 后执行原 validation argv；
6. 全部完成后删除 host bundle。

离线 install 与 command 使用同一验证容器、同一 tmpfs 和同一资源预算，但容器始终 network none。dependency
install evidence 与 command evidence 分开保存，不包含 proxy URL、Docker ID 或宿主路径。

## 9. Graph 与状态

拓扑：

    apply_patch
      -> plan_dependencies
           no intent -> run_validation
           intent    -> dependency_approval
                           approve -> run_validation
                           reject  -> summarize

CodingState 只新增 JSON-safe plan、approval status 和 dependency evidence；不保存 bundle path、network、container、
proxy client 或文件句柄。formatter patch 重新进入同一判断。

## 10. 稳定错误

- dependency_lockfile_invalid
- dependency_approval_required
- dependency_approval_mismatch
- dependency_install_rejected
- dependency_egress_unconfigured
- dependency_proxy_failed
- dependency_fetch_failed
- dependency_artifact_invalid
- dependency_cleanup_failed
- dependency_offline_install_failed

任何 dependency failure 都阻止 validation passed、controlled commit 和 merge；禁止回退宿主 pip 或普通网络容器。

## 11. 验收

1. 配置默认关闭且严格拒绝额外字段。
2. 只有 lockfile changed 的结构化事实触发独立 HITL。
3. digest 不匹配 resume fail closed。
4. downloader 无直接外网路由，validation container 始终 network none。
5. 非法 lockfile 全部拒绝。
6. proxy 只允许 exact FQDN + 443，并拒绝特殊地址。
7. wheelhouse 类型、数量、大小、名称和 hash 全部校验。
8. dependency evidence 与 cleanup facts 进入 terminal result。
9. Stage 4A、Stage 2/3 最小回归和 authority validator 通过。
10. 不调用真实 Provider；真实 registry smoke 必须 operator 显式确认并且无 secret。

