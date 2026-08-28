# Studio 演进拓扑展示

这张独立 Graph 只展示 `execute_route -> fast / plan / code`，所有节点都是 no-op；不会调用模型、Tool、Provider 或 Memory，也不参与生产 `assistant-native-v4`。

先停止当前 `8089` 开发服务，再从仓库根目录运行：

```bash
/home/lenovo1/miniconda3/envs/hello_agent/bin/python scripts/run_server.py \
  --backend dev \
  --config langgraph.showcase.json \
  --host 127.0.0.1 \
  --port 8089 \
  --no-env-file
```

Studio 打开后选择 `studio-evolution-showcase`。输入可用 `{}`（默认走 `fast`），或 `{"route":"plan"}`、`{"route":"code"}` 查看分支；不要与生产开发服务并行运行。
