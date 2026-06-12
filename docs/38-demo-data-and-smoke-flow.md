# 38 Demo 数据与 Smoke Flow 设计

## Demo 数据原则

首次真实 Provider 测试只使用低风险数据。

允许：

```text
鞋子图片
背包图片
杯子图片
桌面商品图
无人的室内场景图
公开样例图
```

避免：

```text
身份证
合同
票据
人脸
车牌
家庭视频
公司内部资料
客户数据
```

## 推荐目录

```text
demo_data/
├── README.md
├── images/
│   └── .gitkeep
└── videos/
    └── .gitkeep
```

`demo_data/README.md` 说明用户可以把自己的低风险样例图放进去，但默认仓库不需要提交大文件。

## Smoke 脚本

推荐新增：

```text
scripts/smoke_real_vision.py
```

行为：

1. 检查环境变量。
2. 检查 demo image 路径。
3. 构造 `UserRequest`。
4. 调用 `AgentGraphRuntime`。
5. 打印：provider、intent、response_text、tool_calls、errors、trace_id。
6. 缺少配置时清晰退出。

## 推荐命令

```bash
export MULTIMODAL_AGENT_VISION_PROVIDER=openai
export OPENAI_API_KEY="..."
export OPENAI_VISION_MODEL="..."
python scripts/smoke_real_vision.py --image demo_data/images/shoe.jpg
```

## 输出要求

脚本输出不应打印 API Key。

失败时输出：

```text
provider_unconfigured
missing OPENAI_API_KEY
how to set env vars
```

成功时输出：

```text
status: success
provider: openai
intent: ...
tool_calls: ...
trace_id: ...
```
