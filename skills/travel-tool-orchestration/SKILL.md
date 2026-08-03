---
name: travel-tool-orchestration
description: 用于处理涉及住宿、酒店价格或库存、地点、景点、周边、路线、通勤、定位或旅行天气的旅行与出行请求，包括单工具查询和多目标任务。
metadata:
  manifest-version: "2"
---

# 旅行问题解决与工具编排

把请求拆成独立的证据目标，为每个目标选择一个终点工具，并只补齐该工具真正缺少的输入。所有证据目标都完成或明确受阻后再回答。

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

- 旅行或出行场景中的住宿、酒店报价、地点、景点、周边 POI、路线、通勤、定位或天气请求。
- 带入住日期、预算、房型、价格、库存、可订语义或 OTA 候选的住宿请求。
- 单一旅行查询，以及住宿、路线、天气等多个证据目标组合的行程任务。

## When Not to Use

- 与旅行或出行无关的请求，即使本轮目录中存在受治理工具。
- 与地点、住宿、路线、定位或天气无关的纯写作、翻译或闲聊。
- 不把高德酒店 POI 当作带日期、价格或库存的住宿报价。
- 不把地点解析或周边搜索当作 lodging_search 的固定前置步骤。
- 不调用本轮 ToolSpec 中没有暴露的工具。

## Decision Rules

- 先列出用户要求的全部证据目标；每个目标各有一个终点工具，一个请求可以有多个终点工具。
- 日期价格、预算、房型、库存、可订候选或 OTA 使用 `lodging_search`；它可直接接收目的地和 `nearby_poi`，不要固定先查地图。
- 城市、行政区或普通关键词范围内的地点使用 `mcp.amap_maps.maps_text_search`；明确锚点附近或指定半径周边使用 `mcp.amap_maps.maps_around_search`。
- 步行、骑行、驾车或公共交通时间使用对应路线工具；缺起终点经纬度时才使用 `mcp.amap_maps.maps_geo`。
- 旅行日期天气使用 `mcp.amap_maps.maps_weather`，并按返回项的明确日期选择预报；缺城市且上下文没有可靠默认地点时先澄清。
- 用户明确要求“当前位置”或“附近”时，只有可信结构化上下文已经提供 ToolSpec 必需的 IP，或本轮 ToolSpec 明确允许省略 IP 由服务端识别，才使用 `mcp.amap_maps.maps_ip_location`；否则询问城市、区域或附近地标，不要求用户额外暴露原始 IP。IP 定位是粗粒度位置证据，不能证明步行范围。
- `nearby_poi` 只表达住宿搜索偏好。用户要求严格步行时间、交通时间或半径时，住宿结果之后仍需地图或路线证据核验。

## Procedure

- 提取全部证据目标、用户硬约束、已有事实和允许采用的明确假设。
- 为每个目标确定终点工具及其必填输入，形成依赖顺序；天气等无依赖目标可以独立推进。
- 只在缺失信息会阻止工具调用或实质改变结果时澄清。用户已给出的地点、日期、预算和偏好不重复询问。
- 按依赖顺序调用工具，每次结果返回后更新哪些目标已经完成、仍缺什么证据。
- 多个住宿候选需要路线核验时，先用住宿约束缩小候选，再按排序逐批核验，直到获得用户要求数量的合格候选、候选耗尽或触及本轮工具预算。
- 辅助查询为空时最多进行一次有实质差异的修正；其他目标不依赖该结果时继续执行。
- 只有全部目标已完成，或无法完成的目标及原因已经明确时才回答。
- 遇到区域落点、多候选路线、相对日期、定位边界、空结果或部分失败时，按需读取 `recovery-and-edge-cases` reference。

## Pitfalls

- 不在第一个终点工具成功后遗漏用户的其他证据目标。
- 不连续更换近义关键词消耗工具预算。
- 不把高德空结果解释为住宿 Provider 没有候选。
- 不用高德酒店 POI 支持指定日期的价格、库存或可订结论。
- 不用住宿报价或 `nearby_poi` 替代步行、驾车或公共交通路线证据。
- 不把 `maps_direction_transit_integrated` 的公交、地铁或混合方案无条件表述为纯公交。
- 不把 IP 定位表述为精确当前位置，不为了定位要求用户额外暴露原始 IP。

## Verification

- 用户要求的每个证据目标都有对应结果，或明确说明受阻原因。
- 每个终点工具与其证据类型一致，前置工具只为补齐真实依赖。
- 所有地点、价格、距离、库存、路线和天气事实都能对应本轮工具结果及正确日期、对象和范围。
- 严格距离或通勤约束已经由地图或路线证据核验；未核验的候选不表述为满足约束。
- 采用区域落点、相对日期或默认地点时明确说明会影响结果的假设。
- lodging_search 返回 booking_url 时提供对应可点击 OTA 链接，并说明跳转不代表锁价、预订成功或最终成交。
- booking_url 为空时明确当前没有跳转链接，不生成“点击链接”等悬空指代。

## Example

用户要求“找下周五到周日、每晚 800 元以内、离杭州东站步行 15 分钟内的可订酒店，比较去西湖的公共交通时间，并查看入住日天气”时：

1. 解析并确认入住、退房日期，调用 `lodging_search` 获取杭州东站附近的日期报价与库存候选。
2. 对实际准备推荐的候选和杭州东站补齐坐标，调用步行路线核验 15 分钟硬约束。
3. 为“西湖”选择明确落点并说明该落点，对通过步行筛选的候选调用公共交通路线。
4. 调用天气工具并选择入住日期对应的杭州预报。
5. 酒店、步行、公共交通和天气四个目标都完成或明确受阻后再回答。

## References

- recovery-and-edge-cases: references/recovery-and-edge-cases.md

## Visibility

- tags: travel, lodging, maps, route, weather
- enabled-by-default: true
- skill-only: false
