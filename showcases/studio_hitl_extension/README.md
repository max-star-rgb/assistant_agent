# Studio HITL 表单扩展

这个 Chrome Manifest V3 扩展把当前 LangSmith Studio 页面里的标准 LangGraph HITL interrupt 渲染成表单，免去手写 `Command(resume=...)` JSON。它不修改 Studio 源码，也不保存 thread、消息或审批内容。

## 安装

1. 打开 `chrome://extensions`，启用右上角“开发者模式”。
2. 点击“加载已解压的扩展程序”，选择本目录：
   `/home/lenovo1/pycharm_project/assistant_agent/showcases/studio_hitl_extension/extension`
3. 刷新已经打开的 Studio thread 页面。

扩展只匹配 `https://smith.langchain.com/studio/*` 和当前的 `https://smith.langchain.com/o/*/studio/*` 路由，并且只在页面 URL 的 `baseUrl` 为 `http://127.0.0.1:8089` 时工作。遇到恰好一个标准原生 HITL interrupt 后，页面中央会出现“需要你的批准”表单；参数始终可见，每个 action 都要显式选择决定，也可点击“使用 Studio 原界面”回到 JSON 兜底。

## 离线演示

先停止 PyCharm 当前占用 8089 的 Agent Server，再从仓库根目录启动同一个端口的离线 showcase：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --backend dev \
  --host 127.0.0.1 \
  --port 8089 \
  --config langgraph.showcase.json \
  --no-env-file \
  --no-browser
```

在 Studio 选择 `studio-hitl-showcase`，新建 thread 后输入以下任一 state：

```json
{"scenario":"single"}
```

```json
{"scenario":"multi"}
```

`single` 用于检查 enum、数字、布尔、嵌套对象和数组编辑；`multi` 用于检查多个 action 的批准/编辑/拒绝顺序。showcase 只汇总决定，不会执行命令或写文件。

提交后扩展会显示执行状态；run 完成或进入下一个 interrupt 时页面刷新一次。checkpoint 已变化、网络失败或 Agent Server 拒绝请求时不会自动重试。

演示结束后停止该进程，再从 PyCharm 恢复原来的唯一 8089 Agent Server。

## 边界与卸载

- 同时出现多个原生 interrupt、非标准 interrupt 或不受支持的参数形态时，扩展保持静默，继续使用 Studio 原界面。
- 后台只访问 `http://127.0.0.1:8089`，使用本地 Agent Server 固定的 `langgraph-studio-user` 开发身份，提交前会重新核对 `checkpoint_id + interrupt_id`。
- 数组和对象按现有结构递归编辑；需要增删字段或数组项时使用 Studio 原始 JSON。
- 卸载时在 `chrome://extensions` 中移除或停用 **Assistant Agent Studio HITL** 即可。
