---
name: travel-tool-orchestration
description: 用于需要组合两种或以上地点、住宿、路线、定位或天气能力的复杂旅行任务；单工具查询不使用。
metadata:
  manifest-version: 2
---

# 旅行工具编排

先确定用户真正需要的终态证据，再选择唯一的终点工具。只有终点工具缺少必填输入时，才调用能够补齐该输入的前置工具。

## Governed Tools

- lodging_search
- mcp.amap_maps.maps_geo
- mcp.amap_maps.maps_ip_location
- mcp.amap_maps.maps_weather
- mcp.amap_maps.maps_bicycling
- mcp.amap_maps.maps_direction_walking
- mcp.amap_maps.maps_direction_driving
- mcp.amap_maps.maps_direction_transit_integrated
- mcp.amap_maps.maps_text_search
- mcp.amap_maps.maps_around_search

## Permissions

- tool:lodging_search
- tool:mcp.amap_maps.maps_geo
- tool:mcp.amap_maps.maps_ip_location
- tool:mcp.amap_maps.maps_weather
- tool:mcp.amap_maps.maps_bicycling
- tool:mcp.amap_maps.maps_direction_walking
- tool:mcp.amap_maps.maps_direction_driving
- tool:mcp.amap_maps.maps_direction_transit_integrated
- tool:mcp.amap_maps.maps_text_search
- tool:mcp.amap_maps.maps_around_search

## When to Use

- 地图地点、地址、坐标、周边 POI、路线、IP 定位或天气请求。
- 带入住日期、预算、房型、价格、库存语义或 OTA 候选的住宿请求。
- 已有住宿候选后继续比较步行、驾车或公交通勤。

## When Not to Use

- 不把高德酒店 POI 当作带日期、价格或库存的住宿报价。
- 不把地点解析或周边搜索当作 lodging_search 的固定前置步骤。
- 不调用本轮 ToolSpec 中没有暴露的工具。

## Decision Rules

- 用户明确只要地图地点、地址、坐标或普通周边分布时，使用高德 POI。
- 城市、行政区或普通关键词范围内的地图地点使用 maps_text_search；明确要求某个锚点附近或指定半径周边时使用 maps_around_search。
- 用户需要入住日期对应的价格、预算、房型、库存、可订候选或 OTA 时，使用 lodging_search。
- 已有住宿候选后还需要步行、骑行、驾车或公交通勤证据时，使用对应高德路线工具。
- lodging_search 可直接接收目的地和 nearby_poi；不要固定先查高德。maps_around_search 缺中心经纬度或路线工具缺起终点经纬度时，才调用 maps_geo。

## Procedure

- 提取用户目标与约束，选择唯一终点工具。
- 地图地点终点为 maps_text_search 时直接按关键词与城市查询；终点为 maps_around_search 时复用已有中心坐标，没有坐标才先 maps_geo。
- 仅在终点工具缺少必填输入时，调用能够补齐该输入的前置工具。
- 调用终点工具后检查证据是否足以回答；充分时立即回答。
- 辅助查询为空时最多进行一次有实质差异的修正；终点工具不依赖该结果时继续执行。
- 遇到地图酒店与住宿报价边界不清、空结果或证据恢复时，按需读取 decision-guide reference。

## Pitfalls

- 不连续更换近义关键词消耗工具预算。
- 不把高德空结果解释为住宿 Provider 没有候选。
- 不用高德酒店 POI 支持指定日期的价格、库存或可订结论。
- 不用住宿报价替代步行、驾车或公交路线证据。

## Verification

- 最终工具与用户真正需要的证据类型一致。
- 所有地点、价格、距离、库存和路线事实都能对应本轮工具结果。
- lodging_search 返回 booking_url 时提供对应可点击 OTA 链接，并说明跳转不代表锁价、预订成功或最终成交。
- booking_url 为空时明确当前没有跳转链接，不生成“点击链接”等悬空指代。

## References

- decision-guide: references/decision-guide.md

## Visibility

- tags: travel, lodging, maps, route, weather
- enabled-by-default: true
- skill-only: false
