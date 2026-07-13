# H.264 视频数据传输专项指导

本文档详细说明媒体服务与Agent之间H.264视频数据的完整处理流程。

---

## 1. 数据流概述

```
┌─────────────┐      YUV原始帧       ┌──────────────────┐
│   摄像头    │ ──────────────────> │ YuvToH264Converter│
│ (I420格式)  │                      │ ( FFmpeg/libx264) │
└─────────────┘                      └────────┬─────────┘
                                              │
                                              ▼
                                    H.264 NALU (包含起始码)
                                    00 00 00 01 67... (SPS)
                                    00 00 00 01 68... (PPS)
                                    00 00 00 01 65... (I-Frame)
                                              │
                                              ▼
                               videoData.toString('hex')
                               十六进制字符串 (小写，无0x前缀)
                                              │
                                              ▼
┌─────────────┐                      ┌──────────────────┐
│    Agent    │ <─────────────────── │  WebSocket传输   │
│ws_llm_client│                       └──────────────────┘
└──────┬──────┘
       │
       ▼
bytes.fromhex(hex_string)  ───────► 还原为原始H264字节
       │
       ▼
av.CodecContext.create('h264', 'r')
       │
       ▼
codec_ctx.decode(packet)  ───────► H264 → 原始视频帧(YUV)
       │
       ▼
frame.reformat(format='bgr24')  ──► YUV → BGR24
       │
       ▼
cv2.imencode('.jpg', bgr)  ───────► BGR24 → JPEG
       │
       ▼
base64.b64encode(jpg_data)  ─────► JPEG → Base64字符串
       │
       ▼
message["video"] = base64_string  ─► 发送给MLLM服务
```

---

## 2. 媒体服务端编码详解

### 2.1 编码器配置

**文件**: `media-service/src/utils/YuvToH264Converter.js`

```javascript
// FFmpeg编码参数
inputStream:  // 原始YUV数据
  -f rawvideo
  -video_size {width}x{height}
  -r {frameRate}              // 帧率
  -pix_fmt yuv420p            // 像素格式

outputOptions:
  -f h264                     // 输出H264格式
  -preset ultrafast           // 速度优先
  -tune zerolatency           // 零延迟优化
  -b:v {bitrate}              // 视频码率
  -an                         // 无音频
  -frames 1                   // 每帧单独编码（关键！）
```

### 2.2 输出特性

1. **每帧单独编码**: `-frames 1` 意味着每一帧都是完整的关键帧(I-Frame)，不依赖前后帧
2. **包含起始码**: 输出数据以 `00 00 00 01` NAL单元起始码开头
3. **包含SPS/PPS**: 每帧前有序列参数集(SPS)和图像参数集(PPS)

### 2.3 典型输出结构

```
Frame 1:
  00 00 00 01 67 42 00 1e fa 28 02 18 10 ...  (SPS)
  00 00 00 01 68 ce 38 80 00 00 00 01 65 ...  (PPS + I-Frame)

Frame 2:
  00 00 00 01 67 42 00 1e fa 28 02 18 10 ...  (SPS)
  00 00 00 01 68 ce 38 80 00 00 00 01 65 ...  (PPS + I-Frame)
  ...（每帧重复SPS/PPS）
```

### 2.4 发送代码

**文件**: `media-service/src/services/AgentClient.js:1046-1085`

```javascript
sendVideoToAgent(type, timestamps, length, videoData, timestampStr, isH264) {
    // videoData 已经是 Buffer (H264字节数据)
    // 转换为十六进制字符串发送
    const videoPackage = {
        userNumber: this.userNumber,
        videoIndex: this.videoIndexCounter.toString(),
        contents: [{
            speakerNumber: this.speakerNumber,
            videoContent: videoData.toString('hex'),  // 关键：Buffer → hex string
            time: timestampStr
        }],
        videoConfig: {
            codec: "H264",
            resolution: `${this.videoWidth}x${this.videoHeight}`,
            frameRate: this.getActualVideoFrameRate(),
            // ...
        }
    };
}
```

### 2.5 WebSocket消息格式

```json
{
  "message": "video",
  "body": "{
    \"userNumber\": \"13800138000\",
    \"videoIndex\": \"1\",
    \"contents\": [{
      \"speakerNumber\": \"13800138000\",
      \"videoContent\": \"00000001674d001e96a8028000030001ac56c18000003ac56c18000000...",
      \"time\": \"2026-07-13T08:30:00Z\"
    }],
    \"videoConfig\": {
      \"codec\": \"H264\",
      \"resolution\": \"1280x720\",
      \"frameRate\": 25,
      \"width\": 1280,
      \"height\": 720
    }
  }"
}
```

---

## 3. Agent端接收详解

### 3.1 入口函数

**文件**: `calling-agent-service/src/websocket_server.py:347-388`

```python
async def handle_video_stream(websocket, body_str: str):
    body = json.loads(body_str)
    session_id, agent, phone_number, _, _ = _get_agent_and_session(websocket, body)

    # 解析videoContent
    video_frames = _parse_contents(body)  # 返回 contents 列表

    # 保存视频配置
    video_config = body.get('videoConfig')
    if video_config:
        save_video_config(session_id, video_config)

    # 发送给MLLM
    await agent.handle_video_stream(video_frames)

    # 缓存（可选）
    if VIDEO_CACHE_ENABLED:
        await cache_video_slice(session_id, video_frames, float(VIDEO_CACHE_DURATION))
```

### 3.2 关键解析函数

**文件**: `calling-agent-service/src/websocket_server.py:636-658`

```python
def _parse_contents(body):
    """提取 contents 中的视频数据"""
    contents = body.get("contents")
    frames = []
    if isinstance(contents, str):
        frames = [contents]  # 直接是字符串
    elif isinstance(contents, list):
        frames = contents    # 列表
    return frames
```

### 3.3 发送到MLLM

**文件**: `calling-agent-service/src/calling_agent.py:285-320`

```python
async def handle_video_stream(self, video_frames: list):
    # 第一帧建立连接
    is_first = not agent_sessions.get(self.task_id, {}).get(f"{self.task_id}_stream_started", False)
    if is_first:
        await self._init_mllm_client()
        agent_sessions.setdefault(self.task_id, {})[f"{self.task_id}_stream_started"] = True

    # 发送视频帧
    await self.mllm_client.send_media_stream(video=video_frames)
```

### 3.4 MLLM客户端处理（核心解码）

**文件**: `calling-agent-service/src/ws_llm_client.py:239-327`

```python
async def send_media_frame(self, frame_data: str, media_type: str = "video") -> bool:
    message = {
        "type": "media.stream",
        "session_id": self.session_id,
        "timestamp": int(time.time() * 1000)
    }

    if media_type == "video":
        video_data = frame_data
        if isinstance(video_data, str):  # 十六进制字符串格式
            video_bytes = bytes.fromhex(video_data.replace('0x', '').replace(' ', '').replace('\n', ''))
        else:
            video_bytes = bytes(video_data)

        # ====== 关键：H264解码 ======
        codec_ctx = av.CodecContext.create('h264', 'r')
        packet = av.Packet(video_bytes)
        frames = codec_ctx.decode(packet)
        if not frames:
            logger.warning("Failed to decode H.264")
            return False

        # ====== 转JPEG ======
        frame = frames[0]
        bgr = frame.reformat(format='bgr24').to_ndarray()
        success, jpg_data = cv2.imencode('.jpg', bgr)
        if not success:
            logger.error("Failed to encode JPEG")
            return False

        # ====== Base64编码 ======
        jpeg_base64 = base64.b64encode(jpg_data).decode('utf-8')
        message["video"] = jpeg_base64

    await self.ws.send(json.dumps(message, ensure_ascii=False))
    return True
```

### 3.5 最终发送的消息格式

```json
{
  "type": "media.stream",
  "session_id": "abc123",
  "timestamp": 1720849800000,
  "video": "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgMC..."
}
```

注意：这里的 `video` 字段是 **Base64编码的JPEG图片**，不是原始H264数据。

---

## 4. 数据格式对比

| 阶段 | 格式 | 示例 |
|------|------|------|
| 摄像头原始 | YUV (I420) | `yuvData.length = 1280 * 720 * 1.5 = 1,382,400` |
| FFmpeg输出 | H264 NALU (Buffer) | `00 00 00 01 67 42 00 1e ...` |
| WebSocket传输 | hex字符串 (String) | `"000000016742001e..."` |
| MLLM接收后 | 原始H264字节 (bytes) | `b'\x00\x00\x00\x01\x67\x42...' |
| 解码后 | YUV/视频帧 | `av.VideoFrame` |
| 转换后 | BGR24 ndarray | `numpy.ndarray (H, W, 3)` |
| MLLM发送 | JPEG Base64 | `"/9j/4AAQSkZJRgABAQAAAQ..."` |

---

## 5. 常见问题排查

### 5.1 解码失败常见原因

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| `Failed to decode H.264` | H264数据缺少起始码 | 确保FFmpeg输出包含 `00 00 00 01` |
| `frames`为空 | 数据不完整 | 检查hex字符串是否完整传输 |
| 花屏/马赛克 | 非关键帧 | 媒体服务确保每帧都是I-Frame |

### 5.2 调试技巧

**媒体服务端落盘H264数据**:
```javascript
// AgentClient.js 中启用
if (this.h264DumpStream) {
    this.h264DumpStream.write(h264Data);
}
// 输出文件: ./out/{userNumber}_{timestamp}.h264
```

**验证H264数据**:
```bash
# 使用ffplay播放
ffplay -f h264 -i {filename}.h264

# 使用ffprobe查看信息
ffprobe -show_streams -select_streams v {filename}.h264
```

### 5.3 性能考量

1. **每帧I-Frame**: 虽然保证了独立性，但码率较高；可根据场景调整
2. **YUV→H264→JPEG**: 经历了编码+解码+再编码，建议评估是否可以直接传YUV
3. **锁机制**: `h264_decoder.py` 使用线程锁，多线程安全

---

## 6. 参考代码位置

| 功能 | 文件 | 行号 |
|------|------|------|
| H264编码器 | `media-service/src/utils/YuvToH264Converter.js` | 52-119 |
| 发送video消息 | `media-service/src/services/AgentClient.js` | 1046-1085 |
| 接收video消息 | `calling-agent-service/src/websocket_server.py` | 347-388 |
| MLLM发送 | `calling-agent-service/src/ws_llm_client.py` | 239-327 |
| H264解码器 | `calling-agent-service/src/common/h264_decoder.py` | 24-77 |

---

## 7. 建议：新Agent实现注意点

如果新Agent需要实现类似功能：

1. **数据解析**: 从`videoContent`字段获取十六进制字符串
2. **Hex→Bytes**: 使用 `bytes.fromhex(hex_string)`
3. **解码**: 推荐使用FFmpeg的`av`库，或使用`h264_decoder.py`中的`H264Decoder`类
4. **NALU处理**: 数据可能包含多个NAL单元，需要按起始码切分
5. **格式转换**: 解码后的YUV帧可通过OpenCV转为BGR再编码为JPEG
