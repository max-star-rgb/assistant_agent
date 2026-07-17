---
name: realtime_web_search
description: Look up current, latest, today, news, or web-backed information through the governed web_search tool.
enabled: true
disable-model-invocation: false
---
## Governed Tools
- web_search

## Permissions
- tool:web_search

## Required Inputs
- web_search: query

## When To Use
- User asks for latest, current, today, news, or web-backed information.
- User explicitly asks to search the web.
- 用户询问今天、最新、当前、新闻、消息、资讯或需要联网搜索的信息。
- 用户明确要求搜索网页、查一下网上信息或验证最新情况。

## When Not To Use
- User asks for stored personal memory; use memory tools instead.
- User asks to buy, compare, or inspect products; use product tools instead.
- User asks for image, video, or generated visual output.

## Safe Examples
- latest AI industry news
- check today's market headlines
- 今天 AI 行业最新消息
- 联网搜索某个产品的最新公告

## Runtime Constraints
- Selection context only; execute governed tools only through ToolExecutor.
- Read-only external lookup; do not execute raw HTTP from this descriptor.
- Governed tool execution may retry retryable transient failures once under ToolExecutor policy; this descriptor does not grant retry permission.
