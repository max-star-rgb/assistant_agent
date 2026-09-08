# 客户端执行边界补全

目标：绑定设备时，Git、浏览器、cwd Skills、项目指令与用户媒体文件全部落在客户端；Studio 无设备绑定时继续调试服务机器。保持原生 Agent/ToolNode/HITL，不添加 Studio 设备选择。

浏览器决策：客户端安装阶段准备固定版本官方 Playwright MCP 与其匹配 Chromium；通过本机 PATH 查找 Node/npm，不硬编码开发机路径，不默认复用个人 Chrome。独立的 thread profile 和输出在 cwd/.agent/browser。保留服务端已有显式 MCP 配置。Node/npm 是当前 Python 发行的安装前提，尚非包含全部 OS 运行时的桌面安装器。

- [x] 二进制分块传输：复用 backend upload/download 原生协议；单块小于 WebSocket 帧限制，上传临时文件原子发布，大小/路径/offset 校验，断连清理。Git 原生工具通过同一连接在客户端执行。
- [x] 本地浏览器运行时：安装固定 MCP/Chromium；官方 MCP session 在客户端持有，服务端只转发受信 allowlist 的 browser tool 调用；客户端 session 跟随连接清理。
- [x] cwd 与上下文：设备 cwd 不按服务端 Home 校验；cwd Skills 和项目指令从设备读取，历史落盘也经设备 backend；没有设备仍使用既有服务器路径。
- [x] 媒体：生成调用留在服务端，结果二进制传到客户端 outputs；用户媒体本地路径先从设备获取，服务端临时目录仅供 Provider/解码消费。
- [x] 离线验证客户端与服务端目录不同且无路径泄漏、真实本地 Git 与 Chromium、断连及审批；更新安装副本与 owner 文档。

认证与发布边界：本轮不新增帐号系统、不仅靠解除回环校验开放公网；本地开发入口仍按现有身份治理。执行能力就绪不等于已验证跨 OS 安装、统一帐号正式登录或桌面安装器。
