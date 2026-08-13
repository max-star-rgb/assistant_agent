# Agent Server 原生部署探针

该专项以 mock Provider 启动真实 `langgraph dev`，通过公开 `langgraph_sdk`
验证 deployment manifest、custom app、schema、thread/run/state、Store 和 cancel。
它不调用真实 Provider，不读取密钥；in-memory 持久化目录已从 Git 排除。

运行：

```bash
MULTIMODAL_AGENT_PROVIDER_MODE=mock LANGSMITH_TRACING=false \
/home/lenovo1/miniconda3/envs/hello_agent/bin/python \
  evals/system/incubating/agent_server_native_runtime/checks_deployment.py
```

每项结果以一行 JSON 输出；任一检查失败时进程返回非零，并始终关闭子服务。
