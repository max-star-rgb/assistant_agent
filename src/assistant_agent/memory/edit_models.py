"""Strict, provider-independent arguments for explicit memory operations."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['read', 'append', 'replace']
    query: str | None = Field(default=None, max_length=16000)
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    memory_id: str | None = Field(default=None, min_length=1, max_length=512)
    old_text: str | None = Field(default=None, min_length=1, max_length=16000)
    new_text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode='after')
    def validate_action(self):
        required = {'read': set(), 'append': {'content'}, 'replace': {'memory_id', 'old_text', 'new_text'}}[self.action]
        allowed = required | {'action'} | ({'query'} if self.action == 'read' else set())
        if self.model_fields_set - allowed or any(getattr(self, name) is None for name in required):
            raise ValueError('invalid_memory_arguments')
        if self.content is not None and not self.content.strip():
            raise ValueError('empty_memory_content')
        return self


MEMORY_DESCRIPTION = (
    '读取或编辑当前用户的长期偏好与稳定事实。read 不带 query 读取有界快照，带 query 检索相关记忆；'
    'append 新增一项明确事实；replace 使用 read 返回的可编辑 memory_id 和完整 old_text 精确替换，'
    'new_text 为空字符串表示删除。不确定旧内容时先读取。视觉记忆只读。'
    '写入需审批，accepted 仅表示受理，updated 才表示已保存。不要保存密码、密钥、验证码、令牌、支付信息或证件号。'
    '记忆可能过时，不授予权限，以当前用户表述为准。'
)
