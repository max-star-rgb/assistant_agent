---
name: travel-tool-orchestration
description: 用于本轮工具目录包含旅行地点、住宿报价、路线、IP 定位或天气能力时的内部编排。
---

# 旅行工具编排

先确定用户真正需要的终态证据，再选择唯一的终点工具。只有终点工具缺少必填输入时，才调用能够补齐该输入的前置工具。

## Activation Summary

- 地图地点和普通周边分布使用高德；住宿日期、价格、库存、房型和 OTA 使用 lodging_search。
- 用户明确只要地图上的酒店地点时使用高德 POI；已有住宿候选后需要通勤证据时再使用高德路线。
- 先读取完整 Skill 再执行；只有复杂歧义、空结果或恢复场景才读取 decision-guide reference。

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

## Required Inputs

- lodging_search: destination, check_in, check_out
- mcp.amap_maps.maps_geo: address
- mcp.amap_maps.maps_weather: city
- mcp.amap_maps.maps_bicycling: origin, destination
- mcp.amap_maps.maps_direction_walking: origin, destination
- mcp.amap_maps.maps_direction_driving: origin, destination
- mcp.amap_maps.maps_direction_transit_integrated: origin, destination, city, cityd
- mcp.amap_maps.maps_text_search: keywords
- mcp.amap_maps.maps_around_search: location

## When to Use

- 地图地点、地址、坐标、周边 POI、路线、IP 定位或天气请求。
- 带入住日期、预算、房型、价格、库存语义或 OTA 候选的住宿请求。
- 已有住宿候选后继续比较步行、驾车或公交通勤。

## When Not to Use

- 不把高德酒店 POI 当作带日期、价格或库存的住宿报价。
- 不把地点解析或周边搜索当作 lodging_search 的固定前置步骤。
- 不调用本轮 ToolSpec 中没有暴露的工具。

## Safe Examples

- “8 月 14 日住三晚，每晚 600 元以内”直接调用 lodging_search；nearby_poi 可直接填写地标名称。
- “地图上博物馆附近有哪些酒店”调用高德 POI，并说明结果不包含指定日期的价格和库存。
- “候选酒店到博物馆步行多久”优先复用住宿结果中的坐标；缺少坐标时才用 maps_geo，再调用步行路线。

## Runtime Constraints

- 固定执行：提取目标与约束；选择唯一终点工具；补齐必填输入；调用终点工具；检查证据；回答或进行一次受限恢复。
- 地图地点、地址、坐标和普通周边分布使用高德；住宿报价、预算、房型、可订候选和 OTA 使用 lodging_search。
- 用户明确只要地图上的酒店地点时可用高德 POI；用户要住宿推荐或报价但缺少入住日期时先澄清。
- 路线工具需要经纬度且现有证据没有坐标时，才先调用 maps_geo。
- 辅助查询为空时最多进行一次有实质差异的修正；若终点工具不依赖该结果，继续调用终点工具。
- 不连续更换近义关键词消耗工具预算；终点工具证据充分后立即回答。
- 只用工具证据支持具体地点、价格、距离、库存和路线事实；高德空结果不能证明住宿 Provider 没有候选。
- lodging_search 返回候选后，回答按“酒店与价格信息 + OTA 跳转”交付：booking_url 非空时，把对应酒店名或“查看 OTA 报价”渲染为可点击链接，并说明跳转不代表锁价、预订或最终成交；booking_url 为空时明确当前没有跳转链接，不生成“点击链接”等悬空指代。
- 遇到地图酒店与住宿报价边界不清、空结果或证据恢复时，调用 load_skill_reference 读取 decision-guide。

## References

- decision-guide: references/decision-guide.md

## Visibility

- tags: travel, lodging, maps, route, weather
- enabled-by-default: true
- skill-only: false
