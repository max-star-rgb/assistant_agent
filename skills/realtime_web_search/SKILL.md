---
name: realtime_web_search
description: Look up current or web-backed information through the governed web_search tool.
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

## When Not To Use
- User asks for stored personal memory; use memory tools instead.
- User asks to buy or compare products; use product tools instead.

## Safe Examples
- latest AI industry news
- check today's market headlines

## Runtime Constraints
- Selection context only; execute governed tools only through ToolExecutor.
- Read-only external lookup; do not execute raw HTTP from this descriptor.
