"""Prompt-safe local visual-perception diagnostic reports."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import json
import re
from typing import Any


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SESSION_DIGEST = re.compile(r"^[0-9a-f]{16}$")
_MARKER = "multimodal_observation "
_EVENT_FIELDS = {
    "semantic_frame.admitted": {
        "session_id_digest",
        "sequence",
        "reason",
        "replaced_sequence",
    },
    "semantic_frame.replaced": {
        "session_id_digest",
        "sequence",
        "reason",
        "replaced_sequence",
    },
    "semantic_frame.skipped": {
        "session_id_digest",
        "sequence",
        "reason",
        "reference_sequence",
        "semantic_similarity",
        "semantic_change",
        "semantic_threshold",
        "selected",
    },
    "semantic_frame.selected": {
        "session_id_digest",
        "sequence",
        "reason",
        "reference_sequence",
        "semantic_similarity",
        "semantic_change",
        "semantic_threshold",
        "selected",
    },
    "visual_reminder.created": {
        "session_id_digest",
        "reminder_id",
        "similarity_threshold",
        "status",
    },
    "visual_reminder.compared": {
        "session_id_digest",
        "reminder_id",
        "frame_sequence",
        "similarity",
        "similarity_threshold",
        "matched",
        "status",
    },
    "visual_reminder.triggered": {
        "session_id_digest",
        "reminder_id",
        "frame_sequence",
        "similarity_threshold",
        "status",
    },
    "visual_reminder.cancelled": {
        "session_id_digest",
        "reminder_id",
        "frame_sequence",
        "similarity_threshold",
        "status",
    },
}


@dataclass(frozen=True)
class VisualPerceptionReport:
    session_digest: str
    events: tuple[dict[str, Any], ...]
    frame_sequences: tuple[int, ...]
    reminder_ids: tuple[str, ...]


def parse_visual_perception_log(
    lines: Iterable[str],
    *,
    session_digest: str,
) -> VisualPerceptionReport:
    normalized_digest = session_digest.strip().lower()
    if _SESSION_DIGEST.fullmatch(normalized_digest) is None:
        raise ValueError("session digest must contain exactly 16 lowercase hex characters")
    events: list[dict[str, Any]] = []
    for index, raw_line in enumerate(lines):
        event = parse_visual_perception_line(
            raw_line,
            session_digest=normalized_digest,
            order=index,
        )
        if event is not None:
            events.append(event)
    return build_visual_perception_report(events, session_digest=normalized_digest)


def parse_visual_perception_line(
    raw_line: str,
    *,
    session_digest: str,
    order: int,
) -> dict[str, Any] | None:
    normalized_digest = session_digest.strip().lower()
    if _SESSION_DIGEST.fullmatch(normalized_digest) is None:
        raise ValueError("session digest must contain exactly 16 lowercase hex characters")
    line = _ANSI_ESCAPE.sub("", raw_line)
    marker = line.find(_MARKER)
    if marker < 0:
        return None
    encoded = line[marker + len(_MARKER) :]
    try:
        value, _end = json.JSONDecoder().raw_decode(encoded)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    event_name = value.get("event_name")
    payload = value.get("payload")
    allowed = _EVENT_FIELDS.get(event_name)
    if allowed is None or not isinstance(payload, dict):
        return None
    if payload.get("session_id_digest") != normalized_digest:
        return None
    projected = {
        key: payload[key]
        for key in allowed
        if key in payload and _is_prompt_safe_scalar(payload[key])
    }
    return {
        "event_name": event_name,
        "recorded_at": line.split(" ", 1)[0],
        "order": order,
        **projected,
    }


def build_visual_perception_report(
    events: Iterable[dict[str, Any]],
    *,
    session_digest: str,
) -> VisualPerceptionReport:
    normalized_events = tuple(events)
    frame_sequences = sorted(
        {
            sequence
            for event in normalized_events
            for sequence in (event.get("sequence"), event.get("frame_sequence"))
            if isinstance(sequence, int) and not isinstance(sequence, bool)
        }
    )
    reminder_ids = sorted(
        {
            reminder_id
            for event in normalized_events
            if isinstance((reminder_id := event.get("reminder_id")), str)
        }
    )
    return VisualPerceptionReport(
        session_digest=session_digest,
        events=normalized_events,
        frame_sequences=tuple(frame_sequences),
        reminder_ids=tuple(reminder_ids),
    )


def render_visual_perception_html(
    report: VisualPerceptionReport,
    *,
    live_events_url: str | None = None,
) -> str:
    data = json.dumps(
        {
            "session_digest": report.session_digest,
            "events": report.events,
            "frame_sequences": report.frame_sequences,
            "reminder_ids": report.reminder_ids,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    live_url = json.dumps(live_events_url, ensure_ascii=False).replace("<", "\\u003c")
    return _HTML.replace("__REPORT_DATA__", data).replace(
        "__LIVE_EVENTS_URL__",
        live_url,
    )


def _is_prompt_safe_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>视觉感知诊断时间轴</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#121a2d;--grid:#2a3550;--text:#e8edf7;--muted:#92a0ba;--green:#4ade80;--blue:#60a5fa;--amber:#fbbf24;--red:#fb7185}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif}.wrap{max-width:1280px;margin:auto;padding:24px}.head{display:flex;justify-content:space-between;gap:20px;align-items:end}.muted{color:var(--muted)}.card{background:var(--card);border:1px solid #24304a;border-radius:14px;padding:18px;margin-top:18px;box-shadow:0 12px 30px #0004}.chart{width:100%;height:320px;display:block}.legend{display:flex;gap:18px;flex-wrap:wrap}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}.empty{padding:48px;text-align:center;color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid #25314b;font-variant-numeric:tabular-nums}th{color:var(--muted)}
</style>
</head>
<body><main class="wrap">
<div class="head"><div><h1>视觉感知诊断时间轴</h1><div class="muted">仅包含脱敏 ID、帧序号、cosine 与决策，不包含媒体内容或 embedding 向量。</div></div><div><code id="session"></code><div id="liveStatus" class="muted"></div></div></div>
<section class="card"><h2>关键帧选取</h2><div class="legend"><span><i class="dot" style="background:var(--blue)"></i>semantic change</span><span><i class="dot" style="background:var(--amber)"></i>阈值</span><span><i class="dot" style="background:var(--green)"></i>selected</span></div><canvas id="semanticChangeChart" class="chart"></canvas></section>
<section class="card"><h2>提醒图文匹配 cosine</h2><div class="legend"><span><i class="dot" style="background:var(--blue)"></i>cosine</span><span><i class="dot" style="background:var(--amber)"></i>阈值</span><span><i class="dot" style="background:var(--red)"></i>matched</span></div><canvas id="reminderCosineChart" class="chart"></canvas></section>
<section class="card"><h2>事件明细</h2><div id="events"></div></section>
</main>
<script>
const report=__REPORT_DATA__;
const liveEventsUrl=__LIVE_EVENTS_URL__;
document.getElementById('session').textContent=report.session_digest;
const semantic=report.events.filter(e=>e.event_name==='semantic_frame.selected'||(e.event_name==='semantic_frame.skipped'&&e.semantic_change!==undefined));
const comparisons=report.events.filter(e=>e.event_name==='visual_reminder.compared');
const reminderLifecycle=report.events.filter(e=>e.event_name==='visual_reminder.created'||e.event_name==='visual_reminder.triggered'||e.event_name==='visual_reminder.cancelled');
function lifecycleFrame(event){if(Number.isInteger(event.frame_sequence))return event.frame_sequence;const first=comparisons.find(row=>row.reminder_id===event.reminder_id);return first?.frame_sequence}
function draw(canvas, rows, valueKey, thresholdKey, selectedKey, yMin, yMax, markers=[]){
 const ratio=devicePixelRatio||1,box=canvas.getBoundingClientRect();canvas.width=box.width*ratio;canvas.height=box.height*ratio;const c=canvas.getContext('2d');c.scale(ratio,ratio);const w=box.width,h=box.height,p={l:48,r:18,t:22,b:38};c.clearRect(0,0,w,h);c.font='12px system-ui';c.strokeStyle='#2a3550';c.fillStyle='#92a0ba';
 for(let i=0;i<=5;i++){const y=p.t+(h-p.t-p.b)*i/5,value=yMax-(yMax-yMin)*i/5;c.beginPath();c.moveTo(p.l,y);c.lineTo(w-p.r,y);c.stroke();c.fillText(value.toFixed(2),6,y+4)}
 if(!rows.length){c.fillText('没有可绘制的数据',p.l+20,h/2);return}
 const seqs=rows.map(r=>r.sequence??r.frame_sequence),min=Math.min(...seqs),max=Math.max(...seqs),x=s=>p.l+(w-p.l-p.r)*(max===min?.5:(s-min)/(max-min)),y=v=>p.t+(h-p.t-p.b)*(yMax-v)/(yMax-yMin);
 for(const marker of markers){const seq=lifecycleFrame(marker);if(!Number.isInteger(seq)||seq<min||seq>max)continue;const symbol=marker.event_name.endsWith('.created')?'C':marker.event_name.endsWith('.triggered')?'T':'X';c.strokeStyle=symbol==='T'?'#4ade80':symbol==='X'?'#fb7185':'#92a0ba';c.lineWidth=1;c.setLineDash([3,4]);c.beginPath();c.moveTo(x(seq),p.t);c.lineTo(x(seq),h-p.b);c.stroke();c.setLineDash([]);c.fillStyle=c.strokeStyle;c.fillText(symbol,x(seq)+4,p.t+12)}
 const threshold=rows.find(r=>Number.isFinite(r[thresholdKey]))?.[thresholdKey];if(Number.isFinite(threshold)){c.strokeStyle='#fbbf24';c.setLineDash([6,5]);c.beginPath();c.moveTo(p.l,y(threshold));c.lineTo(w-p.r,y(threshold));c.stroke();c.setLineDash([])}
 c.strokeStyle='#60a5fa';c.lineWidth=2;c.beginPath();let started=false;for(const row of rows){const value=row[valueKey],seq=row.sequence??row.frame_sequence;if(!Number.isFinite(value))continue;if(!started){c.moveTo(x(seq),y(value));started=true}else c.lineTo(x(seq),y(value))}c.stroke();
 for(const row of rows){const value=row[valueKey],seq=row.sequence??row.frame_sequence;if(!Number.isFinite(value))continue;c.fillStyle=row[selectedKey]?'#fb7185':row.selected?'#4ade80':'#60a5fa';c.beginPath();c.arc(x(seq),y(value),4,0,Math.PI*2);c.fill()}
 c.fillStyle='#92a0ba';c.fillText(String(min),p.l,h-12);c.fillText(String(max),w-p.r-24,h-12)
}
function render(){draw(document.getElementById('semanticChangeChart'),semantic,'semantic_change','semantic_threshold','matched',0,1);draw(document.getElementById('reminderCosineChart'),comparisons,'similarity','similarity_threshold','matched',-1,1,reminderLifecycle)}
addEventListener('resize',render);render();
const eventRoot=document.getElementById('events');
function appendCell(row,value,tag='td'){const cell=document.createElement(tag);cell.textContent=String(value??'');row.appendChild(cell)}
function renderTable(){eventRoot.replaceChildren();if(!report.events.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='没有匹配当前 session 的诊断事件';eventRoot.appendChild(empty);return}const table=document.createElement('table'),head=document.createElement('thead'),headRow=document.createElement('tr'),body=document.createElement('tbody');for(const label of ['时间','帧','事件','状态','数值'])appendCell(headRow,label,'th');head.appendChild(headRow);for(const event of report.events){const row=document.createElement('tr');appendCell(row,event.recorded_at);appendCell(row,event.sequence??event.frame_sequence);appendCell(row,event.event_name);appendCell(row,event.reason??event.status);appendCell(row,event.semantic_change??event.similarity);body.appendChild(row)}table.append(head,body);eventRoot.appendChild(table)}
renderTable();
function acceptLiveEvent(event){if(!event||typeof event!=='object'||typeof event.event_name!=='string')return;report.events.push(event);if(event.event_name==='semantic_frame.selected'||(event.event_name==='semantic_frame.skipped'&&event.semantic_change!==undefined))semantic.push(event);if(event.event_name==='visual_reminder.compared')comparisons.push(event);if(event.event_name==='visual_reminder.created'||event.event_name==='visual_reminder.triggered'||event.event_name==='visual_reminder.cancelled')reminderLifecycle.push(event);render();renderTable()}
if(liveEventsUrl){const status=document.getElementById('liveStatus'),latest=Math.max(0,...report.events.map(event=>Number.isInteger(event.order)?event.order:0)),separator=liveEventsUrl.includes('?')?'&':'?',source=new EventSource(`${liveEventsUrl}${separator}after=${latest}`);status.textContent='正在连接实时日志…';source.onopen=()=>{status.textContent='实时更新中'};source.onerror=()=>{status.textContent='连接中断，正在重连…'};source.addEventListener('visual-perception',message=>{try{acceptLiveEvent(JSON.parse(message.data))}catch{status.textContent='收到无法解析的诊断事件'}})}
</script></body></html>"""
