"""Environment resolution for the isolated Mem0 sidecar."""

from __future__ import annotations

import os
from collections.abc import Mapping


DEFAULT_QWEN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_EMBEDDING_MODEL = "text-embedding-v4"
LONG_TERM_MEMORY_CUSTOM_INSTRUCTIONS = """\
你负责从对话中提取对未来跨会话协助具有持续价值的长期记忆。

只提取以下内容：
1. 用户明确表达且可能长期稳定的偏好、习惯和沟通方式。
2. 用户的长期目标、持续项目、固定约束和重要计划。
3. 用户明确提供、未来可能反复使用的个人背景、关系或设备配置。
4. 用户明确要求记住的非敏感信息。
5. 对未来任务确有帮助，并且能够直接从对话得到支持的事实。

必须忽略以下内容：
1. 单次画面或某一时刻的环境状态，例如桌面上暂时出现的物品。
2. 临时位置、当前动作、短暂情绪、一次性天气和随手闲聊。
3. 仅在当前任务中有用、任务结束后不再有复用价值的信息。
4. 助手的猜测、推断、建议或未经用户确认的结论。
5. 从照片、视频或对话间接推断出的身份、性格、财务、健康等属性。
6. 密码、token、API key、身份证号、银行卡号等凭据或高度敏感信息。
7. 已有记忆的同义重复。

判断原则：
- 不确定是否具有跨会话价值时，不提取。
- 不要把一次观察改写成用户长期拥有、偏好或经常使用某物。
- 用户明确要求记住时可以提高保留优先级，但安全限制仍然有效。
- 事实必须忠于原始信息，不补充推断。
- 对确实值得保留且具有时效性的事件、计划、里程碑或阶段性状态，必须根据提取
  prompt 中的 Observation Date 或用户明确提供的日期，在记忆正文开头使用
  `YYYY-MM-DD：` 标明日期；用户明确提供的事件日期优先于 Observation Date。
- 不得把记忆创建时间伪装成事件发生时间；无法可靠确定日期时不编造日期。
- 长期稳定的偏好、习惯、身份背景和固定配置不机械添加日期。
- 如果没有符合条件的事实，返回空的事实列表。
- 所有提取、合并或更新后的记忆文本使用自然、准确的简体中文；英文输入中的
  可翻译事实也要翻译为简体中文，日期、金额、数字、URL、型号和必要的专有名词或
  缩写保持准确。

示例：
- 输入：画面中白色桌面上放着玻璃杯、手机和显示代码的显示器。
  结果：不提取任何长期记忆。
- 输入：我今天有点累。
  结果：不提取任何长期记忆。
- 输入：我平时使用 Python 开发，希望代码示例优先使用 Python。
  结果：提取“用户平时使用 Python 开发，并希望代码示例优先使用 Python”。
- Observation Date：2026-08-03；输入：我今天开始迁移 Gateway，计划本月底完成。
  结果：提取“2026-08-03：用户开始迁移 Gateway，计划在 2026-08-31 前完成”。
- 输入：请记住，我的项目生产环境固定使用 Python 3.12。
  结果：提取“用户的项目生产环境固定使用 Python 3.12”。"""


def resolve_mem0_provider_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve sidecar settings from explicit overrides or repo dotenv names."""

    env = source if source is not None else os.environ
    qwen_api_key = _required_first(
        env,
        "OPENAI_API_KEY",
        "QWEN_API_KEY",
        "DASHSCOPE_API_KEY",
    )
    qwen_base_url = _first(
        env,
        "OPENAI_BASE_URL",
        "QWEN_CHAT_BASE_URL",
    ) or DEFAULT_QWEN_BASE_URL
    return {
        "chat_model": _required_first(
            env,
            "OPENAI_MODEL",
            "QWEN_CHAT_MODEL",
        ),
        "chat_api_key": qwen_api_key,
        "chat_base_url": qwen_base_url,
        "embedding_model": _first(
            env,
            "EMBEDDING_MODEL",
        ) or DEFAULT_EMBEDDING_MODEL,
        "embedding_api_key": _first(
            env,
            "EMBEDDING_API_KEY",
        ) or qwen_api_key,
        "embedding_base_url": _first(
            env,
            "EMBEDDING_BASE_URL",
        ) or qwen_base_url,
    }


def _required_first(source: Mapping[str, str], *names: str) -> str:
    value = _first(source, *names)
    if value:
        return value
    raise RuntimeError(
        "required sidecar environment variable is missing: " + " or ".join(names)
    )


def _first(source: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = source.get(name)
        if value and value.strip():
            return value.strip()
    return None
