"""Shared demo example prompts for local CLI and API demo flows."""

from __future__ import annotations


DEMO_EXAMPLES = (
    "你好，简单介绍一下你能做什么。",
    "帮我购买乐事薯片，先搜索商品并比较价格，给出购买建议。",
    "帮我找一款Cinnamoroll的玉桂狗，并比较一下价格，最后给出推荐理由。",
    "生成一张白色运动鞋的电商主图，干净背景，真实摄影风格。",
    "帮我写一段适合小红书的商品介绍。",
)


def get_demo_examples() -> list[str]:
    return list(DEMO_EXAMPLES)
