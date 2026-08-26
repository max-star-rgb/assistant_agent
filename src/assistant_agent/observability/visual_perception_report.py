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
    session_digest: str | None,
    order: int,
) -> dict[str, Any] | None:
    normalized_digest = (
        _normalize_session_digest(session_digest)
        if session_digest is not None
        else None
    )
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
    payload_digest = payload.get("session_id_digest")
    if not isinstance(payload_digest, str) or _SESSION_DIGEST.fullmatch(
        payload_digest
    ) is None:
        return None
    if normalized_digest is not None and payload_digest != normalized_digest:
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
    live_keyframes_url: str | None = None,
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
    live_keyframes = json.dumps(live_keyframes_url, ensure_ascii=False).replace(
        "<", "\\u003c"
    )
    return (
        _HTML.replace("__REPORT_DATA__", data)
        .replace("__LIVE_EVENTS_URL__", live_url)
        .replace("__LIVE_KEYFRAMES_URL__", live_keyframes)
        .replace(
            "__LIVE_KEYFRAMES_ENABLED__",
            "true" if live_keyframes_url is not None else "false",
        )
    )


def _is_prompt_safe_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _normalize_session_digest(session_digest: str) -> str:
    normalized_digest = session_digest.strip().lower()
    if _SESSION_DIGEST.fullmatch(normalized_digest) is None:
        raise ValueError("session digest must contain exactly 16 lowercase hex characters")
    return normalized_digest


_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>视觉感知诊断时间轴</title>
<style>
:root{color-scheme:dark;--bg:#0b1020;--card:#121a2d;--grid:#2a3550;--text:#e8edf7;--muted:#92a0ba;--green:#4ade80;--blue:#60a5fa;--amber:#fbbf24;--red:#fb7185}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif}.wrap{max-width:1280px;margin:auto;padding:24px}.head{display:flex;justify-content:space-between;gap:20px;align-items:end}.muted{color:var(--muted)}.card{background:var(--card);border:1px solid #24304a;border-radius:14px;padding:18px;margin-top:18px;box-shadow:0 12px 30px #0004}.chart{width:100%;height:320px;display:block}.legend{display:flex;gap:18px;flex-wrap:wrap}.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}.empty{padding:48px;text-align:center;color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid #25314b;font-variant-numeric:tabular-nums}th{color:var(--muted)}
.keyframe-stage{position:relative;display:grid;place-items:center;min-height:320px;overflow:hidden;border:1px solid #2a3550;border-radius:12px;background:#080d19}.keyframe-stage img{display:block;width:100%;max-height:68vh;object-fit:contain}.keyframe-meta{display:flex;gap:14px;flex-wrap:wrap;min-height:24px;margin-top:10px;color:var(--muted);font-variant-numeric:tabular-nums}.keyframe-strip{display:flex;gap:10px;overflow-x:auto;padding:10px 2px 2px;scrollbar-color:#3b4a69 transparent}.keyframe-thumb{flex:0 0 148px;padding:0;overflow:hidden;border:2px solid transparent;border-radius:10px;background:#080d19;color:var(--text);cursor:pointer;text-align:left}.keyframe-thumb.active{border-color:var(--green)}.keyframe-thumb img{display:block;width:100%;height:92px;object-fit:cover;background:#080d19}.keyframe-thumb span{display:block;padding:6px 8px;font-variant-numeric:tabular-nums}.keyframe-thumb.missing img{opacity:.25}.keyframe-thumb:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
</style>
</head>
<body><main class="wrap">
<div class="head"><div><h1>视觉感知诊断时间轴</h1><div class="muted">事件与曲线仅包含脱敏 ID、帧序号、cosine 与决策；关键帧图片由回环本地服务按需读取，不进入日志。</div></div><div><code id="session"></code><div id="liveStatus" class="muted"></div></div></div>
<section class="card" id="keyframeViewer" data-live-keyframes="__LIVE_KEYFRAMES_ENABLED__" data-history-limit="12"><h2>实时关键帧</h2><div class="muted">最新选中帧大图；下方时间轴保留最近 12 张，可点击回看。</div><div class="keyframe-stage"><img id="latestKeyframe" alt="最新选中关键帧" hidden><div id="keyframeEmpty" class="empty">等待 semantic selector 选出关键帧…</div></div><div id="keyframeMeta" class="keyframe-meta"></div><div id="keyframeStrip" class="keyframe-strip" aria-label="最近关键帧时间轴"></div></section>
<section class="card"><h2>关键帧选取</h2><div class="muted">横坐标：实际完成 SigLIP2 的原始帧 sequence；被 latest-pending 替换或 embedding 失败的帧没有 cosine 点。</div><div class="legend"><span><i class="dot" style="background:var(--blue)"></i>未选中</span><span><i class="dot" style="background:var(--green)"></i>已选关键帧</span><span><i class="dot" style="background:var(--amber)"></i>semantic change 阈值</span></div><canvas id="semanticChangeChart" class="chart"></canvas></section>
<section class="card"><h2>提醒图文匹配 cosine</h2><div class="legend"><span><i class="dot" style="background:var(--blue)"></i>cosine</span><span><i class="dot" style="background:var(--amber)"></i>阈值</span><span><i class="dot" style="background:var(--red)"></i>matched</span></div><canvas id="reminderCosineChart" class="chart"></canvas></section>
<section class="card"><h2>事件明细</h2><div id="events"></div></section>
</main>
<script>
const report=__REPORT_DATA__;
const liveEventsUrl=__LIVE_EVENTS_URL__;
const liveKeyframesUrl=__LIVE_KEYFRAMES_URL__;
const sessionLabel=document.getElementById('session');
sessionLabel.textContent=report.session_digest||'等待视觉会话';
const semantic=report.events.filter(e=>e.event_name==='semantic_frame.selected'||(e.event_name==='semantic_frame.skipped'&&e.semantic_change!==undefined));
const comparisons=report.events.filter(e=>e.event_name==='visual_reminder.compared');
const reminderLifecycle=report.events.filter(e=>e.event_name==='visual_reminder.created'||e.event_name==='visual_reminder.triggered'||e.event_name==='visual_reminder.cancelled');
const keyframeViewer=document.getElementById('keyframeViewer'),latestKeyframe=document.getElementById('latestKeyframe'),keyframeEmpty=document.getElementById('keyframeEmpty'),keyframeMeta=document.getElementById('keyframeMeta'),keyframeStrip=document.getElementById('keyframeStrip');
const keyframeHistoryLimit=Number(keyframeViewer.dataset.historyLimit)||12,keyframes=[];let activeKeyframeKey='';
function keyframeUrl(event){const digest=event.session_id_digest,sequence=event.sequence;if(!liveKeyframesUrl||typeof digest!=='string'||!/^[0-9a-f]{16}$/.test(digest)||!Number.isInteger(sequence)||sequence<=0)return null;return `${liveKeyframesUrl}/${digest}/${sequence}.jpg?event=${Number.isInteger(event.order)?event.order:sequence}`}
function clearKeyframes(){keyframes.length=0;activeKeyframeKey='';renderKeyframes()}
function acceptKeyframe(event,{renderNow=true}={}){if(event.event_name!=='semantic_frame.selected')return;const url=keyframeUrl(event);if(!url)return;const key=`${event.session_id_digest}:${event.sequence}`;const existing=keyframes.findIndex(frame=>frame.key===key);if(existing>=0)keyframes.splice(existing,1);keyframes.push({key,url,sequence:event.sequence,reason:event.reason,semanticChange:event.semantic_change});if(keyframes.length>keyframeHistoryLimit)keyframes.splice(0,keyframes.length-keyframeHistoryLimit);activeKeyframeKey=key;if(renderNow)renderKeyframes()}
function renderKeyframes(){keyframeStrip.replaceChildren();if(!keyframes.length){latestKeyframe.hidden=true;latestKeyframe.removeAttribute('src');keyframeMeta.replaceChildren();keyframeEmpty.hidden=false;keyframeEmpty.textContent=liveKeyframesUrl?'等待 semantic selector 选出关键帧…':'关键帧图片仅在本地实时模式中提供';return}const active=keyframes.find(frame=>frame.key===activeKeyframeKey)||keyframes[keyframes.length-1];activeKeyframeKey=active.key;latestKeyframe.dataset.key=active.key;latestKeyframe.alt=`关键帧 ${active.sequence}`;latestKeyframe.src=active.url;latestKeyframe.hidden=false;keyframeEmpty.hidden=true;keyframeMeta.replaceChildren();for(const value of [`sequence ${active.sequence}`,active.reason?`reason ${active.reason}`:'',Number.isFinite(active.semanticChange)?`semantic change ${active.semanticChange.toFixed(4)}`:'']){if(!value)continue;const span=document.createElement('span');span.textContent=value;keyframeMeta.appendChild(span)}for(const frame of keyframes){const button=document.createElement('button');button.type='button';button.className=`keyframe-thumb${frame.key===active.key?' active':''}`;button.setAttribute('aria-label',`查看关键帧 ${frame.sequence}`);const image=document.createElement('img');image.src=frame.url;image.alt='';image.loading='lazy';image.onerror=()=>button.classList.add('missing');const label=document.createElement('span');label.textContent=`#${frame.sequence} · ${frame.reason||'selected'}`;button.append(image,label);button.onclick=()=>{activeKeyframeKey=frame.key;renderKeyframes()};keyframeStrip.appendChild(button)}keyframeStrip.scrollLeft=keyframeStrip.scrollWidth}
latestKeyframe.onload=()=>{keyframeEmpty.hidden=true;latestKeyframe.hidden=false};latestKeyframe.onerror=()=>{if(!latestKeyframe.src)return;latestKeyframe.hidden=true;keyframeEmpty.hidden=false;keyframeEmpty.textContent='关键帧文件已清理或暂不可用'};
for(const event of report.events)acceptKeyframe(event,{renderNow:false});renderKeyframes();
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
function render(){draw(document.getElementById('semanticChangeChart'),semantic,'semantic_change','semantic_threshold','matched',0,2);draw(document.getElementById('reminderCosineChart'),comparisons,'similarity','similarity_threshold','matched',-1,1,reminderLifecycle)}
addEventListener('resize',render);render();
const eventRoot=document.getElementById('events');
function appendCell(row,value,tag='td'){const cell=document.createElement(tag);cell.textContent=String(value??'');row.appendChild(cell)}
function renderTable(){eventRoot.replaceChildren();if(!report.events.length){const empty=document.createElement('div');empty.className='empty';empty.textContent='没有匹配当前 session 的诊断事件';eventRoot.appendChild(empty);return}const table=document.createElement('table'),head=document.createElement('thead'),headRow=document.createElement('tr'),body=document.createElement('tbody');for(const label of ['时间','帧','事件','状态','数值'])appendCell(headRow,label,'th');head.appendChild(headRow);for(const event of report.events){const row=document.createElement('tr');appendCell(row,event.recorded_at);appendCell(row,event.sequence??event.frame_sequence);appendCell(row,event.event_name);appendCell(row,event.reason??event.status);appendCell(row,event.semantic_change??event.similarity);body.appendChild(row)}table.append(head,body);eventRoot.appendChild(table)}
renderTable();
function acceptLiveEvent(event){if(!event||typeof event!=='object'||typeof event.event_name!=='string')return;const digest=event.session_id_digest;if(typeof digest==='string'&&digest!==report.session_digest){const previous=report.session_digest;report.session_digest=digest;sessionLabel.textContent=digest;report.events.length=0;semantic.length=0;comparisons.length=0;reminderLifecycle.length=0;clearKeyframes();document.getElementById('liveStatus').textContent=previous?`已切换会话 ${previous} → ${digest}`:'已发现视觉会话'}report.events.push(event);if(event.event_name==='semantic_frame.selected'||(event.event_name==='semantic_frame.skipped'&&event.semantic_change!==undefined))semantic.push(event);if(event.event_name==='visual_reminder.compared')comparisons.push(event);if(event.event_name==='visual_reminder.created'||event.event_name==='visual_reminder.triggered'||event.event_name==='visual_reminder.cancelled')reminderLifecycle.push(event);acceptKeyframe(event);render();renderTable()}
if(liveEventsUrl){const status=document.getElementById('liveStatus'),latest=Math.max(0,...report.events.map(event=>Number.isInteger(event.order)?event.order:0)),separator=liveEventsUrl.includes('?')?'&':'?',source=new EventSource(`${liveEventsUrl}${separator}after=${latest}`);status.textContent='正在连接实时日志…';source.onopen=()=>{status.textContent='实时更新中'};source.onerror=()=>{status.textContent='连接中断，正在重连…'};source.addEventListener('visual-perception',message=>{try{acceptLiveEvent(JSON.parse(message.data))}catch{status.textContent='收到无法解析的诊断事件'}})}
</script></body></html>"""
