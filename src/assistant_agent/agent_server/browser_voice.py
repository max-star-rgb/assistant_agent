"""Browser PCM ingress; speech cancellation never cancels business runs."""

import asyncio
import json
import logging
import math
import struct
from time import monotonic, time
from uuid import uuid4

import anyio
from fastapi import WebSocket, WebSocketDisconnect
from langgraph_sdk import get_client

from assistant_agent.agent_server.config import agent_server_url
from assistant_agent.agent_server.voice_tasks import VoiceTasks
from assistant_agent.providers.realtime_session import RealtimeSession

_HEADER = struct.Struct('<BId')
_log = logging.getLogger(__name__)
_connections = set()


def voice_config():
    from assistant_agent.agent_server.graph import current_native_execution_owner
    config = current_native_execution_owner().voice_config
    if config is None:
        raise ValueError('Realtime requires configured real provider')
    return config


async def serve_voice(socket: WebSocket):
    owner = socket.scope['user'].identity
    client = get_client(url=agent_server_url(), headers={'X-Assistant-User': owner})
    lock = asyncio.Lock()
    live = None
    session_id = None
    output_sequence = samples = received = 0
    last_sequence = -1
    window = monotonic()
    error_sent = False

    async def send(event):
        nonlocal output_sequence, error_sent
        async with lock:
            if event.get('type') == 'error':
                if error_sent:
                    return
                error_sent = True
            if event.get('type') == 'voice_audio':
                await asyncio.wait_for(socket.send_bytes(_HEADER.pack(3, output_sequence, time() * 1000) + event['pcm']), 10)
                output_sequence += 1
            else:
                await asyncio.wait_for(socket.send_json(event), 10)
            if event.get('type') == 'error':
                await socket.close(code=1011)

    try:
        session_id = str(uuid4())
        tasks = VoiceTasks(client, session_id)
        await send({'type': 'session.ready', 'mode': 'voice', 'thread_id': session_id,
                    'audio_sample_rate': 16000, 'output_sample_rate': 24000})
        while True:
            packet = await asyncio.wait_for(socket.receive(), 120)
            if packet['type'] == 'websocket.disconnect':
                break
            now = monotonic()
            if now - window >= 1:
                window, received = now, 0
            received += 1
            if received > 80:
                raise ValueError('input_rate_limit')
            raw = packet.get('bytes')
            if raw is not None:
                if live is None or not 13 < len(raw) <= 6413:
                    raise ValueError('invalid_audio_frame')
                kind, seq, captured_at = _HEADER.unpack_from(raw)
                if kind != 1 or seq <= last_sequence or not math.isfinite(captured_at) or len(raw[13:]) % 2:
                    raise ValueError('invalid_audio_header')
                last_sequence = seq
                await live.append(raw[13:])
                samples += len(raw[13:]) // 2
                await send({'type': 'media.received', 'audio_samples': samples})
                continue
            text = packet.get('text', '')
            if len(text) > 4096:
                raise ValueError('control_message_limit')
            event = json.loads(text)
            if not isinstance(event, dict):
                raise ValueError('invalid_control_message')
            kind = event.get('type')
            if kind == 'ping':
                await send({'type': 'pong', 'time': event.get('time')})
            elif kind == 'audio.start':
                if live is not None or (event.get('sample_rate'), event.get('channels'), event.get('format')) != (16000, 1, 'pcm_s16le'):
                    raise ValueError('invalid_audio_start')
                if len(_connections) >= 32:
                    raise ValueError('voice_connection_limit')
                _connections.add(session_id)
                from assistant_agent.agent_server.graph import current_native_execution_owner
                tasks.search_tool = current_native_execution_owner().voice_search_tool
                tasks.memory_backend = current_native_execution_owner().memory_backend
                tasks.memory_store = current_native_execution_owner().memory_store
                tasks.identity = owner
                if getattr(tasks.memory_backend, 'supports_edits', False):
                    await tasks.memory_backend.prepare_extraction(identity=owner, thread_id=session_id, messages=[])
                live = RealtimeSession(voice_config(), session_id, send, tasks)
                await live.open()
                await send({'type': 'audio.started', 'vad': 'server_vad'})
            elif kind == 'turn.cancel' and live is not None:
                await live.interrupt()
            elif kind in {'audio.discard', 'audio.stop'}:
                break
            else:
                raise ValueError('unknown_control')
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        _log.warning('Browser voice failed session_id=%s error_type=%s', session_id, type(exc).__name__)
        try:
            await send({'type': 'error', 'message': '实时语音连接失败，请重连。任务状态请按任务编号查询。',
                        'session_id': session_id, 'error_type': type(exc).__name__})
        except Exception:
            pass
    finally:
        with anyio.CancelScope(shield=True):
            if live:
                await live.close()
            _connections.discard(session_id)
            await client.aclose()
            try:
                await socket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass
