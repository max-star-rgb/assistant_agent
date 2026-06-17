# 127-1 Routing Safety Hotfix: Scene Description

## Problem

Image and video understanding prompts often ask the assistant to describe the scene:

```text
图里是什么？请简要描述主要物体、颜色、材质和场景。
```

This is an understanding request. It should not trigger `render_3d` just because the word `场景` appears.

## Why Scene Description Is Not Render Intent

`场景` can mean the visible context in an image or video. In that usage, the user asks for perception:

- what objects are present
- what colors and materials are visible
- where the image or video appears to take place
- what is happening in the frame

That is different from asking the agent to create a new rendered scene, model, or 3D preview.

## Capability Boundary

`image_understanding` handles:

- describing an image
- explaining the image scene
- identifying objects, colors, materials, OCR, and visual context

`video_understanding` handles:

- describing a video
- summarizing what happens in the video scene
- identifying objects, actions, and temporal context

`render_3d` handles:

- creating a 3D preview
- rendering a product or scene
- modeling an object or scene
- placing a product into a target environment

## Negative Guard

Do not trigger `render_3d` for scene-description wording alone:

- `描述图片里的场景`
- `图里是什么场景`
- `分析画面场景`
- `这个视频里的场景发生了什么`
- `主要物体、颜色、材质和场景`

If media is attached, route to `image_understanding` or `video_understanding` only.

## Strong Render Triggers

Trigger `render_3d` only when the request clearly asks for creation or rendering:

- `3D`
- `三维`
- `渲染`
- `建模`
- `模型`
- `创建 3D 场景预览`
- `生成三维商品展示场景`
- `放到/放进/放入 ... 客厅/展厅/办公室/卧室/空间/场景`
- `用 3D 方式建模`

Media plus strong render intent can still produce multi-step flows such as:

```text
image_understanding -> render_3d
video_understanding -> render_3d
```

## Test Cases

Should not trigger `render_3d`:

1. `图里是什么？请简要描述主要物体、颜色、材质和场景。`
2. `请描述这张图片的场景。`
3. `这个视频里的场景发生了什么？`
4. `画面中的主要场景是什么？`
5. `分析一下图片中的物体和场景。`

Should trigger `render_3d`:

1. `根据这张图创建一个 3D 场景预览。`
2. `把这个商品放进一个客厅场景里渲染。`
3. `生成一个三维商品展示场景。`
4. `请用 3D 方式建模这个场景。`
5. `渲染一个包含这个商品的展示空间。`

## Safety Boundary

This hotfix changes only routing and planning rules.

It does not:

- modify Provider adapters
- call real APIs
- write API keys
- change runtime profile defaults
- change mock/local/offline defaults
