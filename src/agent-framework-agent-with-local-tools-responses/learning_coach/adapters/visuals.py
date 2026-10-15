"""Declarative teaching visuals with a fixed renderer and expiring local artifacts."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import math

from jsonschema import Draft202012Validator


from learning_coach.domain.visual_contracts import VISUAL_SCHEMA

_VALIDATOR = Draft202012Validator(VISUAL_SCHEMA)
_CSS = """
:root{font-family:system-ui,-apple-system,sans-serif;color:#18334b;background:#edf3f6;color-scheme:light}
*{box-sizing:border-box}body{margin:0;padding:24px 14px}main{max-width:900px;margin:auto;background:white;padding:24px;border-radius:18px}
h1{font-size:clamp(1.4rem,4vw,2rem);line-height:1.3;margin:0 0 18px;overflow-wrap:anywhere}
h2{font-size:1.2rem}p{line-height:1.65}button{font:inherit;padding:10px 16px;min-height:44px;border:1px solid #8da6b8;border-radius:9px;background:#f4f8fa;color:#18334b;cursor:pointer}
button:hover,button:focus-visible{background:#dceaf2}button:disabled{opacity:.45;cursor:default}button[aria-pressed=true]{background:#1e5d83;color:white}
.controls{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}.card{background:#f3f7fa;padding:20px;border-radius:12px;min-height:120px;white-space:pre-wrap;overflow-wrap:anywhere}
.status{font-size:.95rem;color:#425d70;min-height:1.5em}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;table-layout:fixed}th,td{text-align:left;vertical-align:top;padding:12px;border:1px solid #c7d8e3;white-space:pre-wrap;overflow-wrap:anywhere}
th{background:#e8f1f6}caption{text-align:left;margin:8px 0}label{display:block;margin:16px 0;font-weight:600}input[type=range]{display:block;width:100%;margin-top:12px;min-height:32px}
canvas{display:block;width:100%;height:auto;border:1px solid #c7d8e3;border-radius:10px}progress{width:100%;height:12px}.muted{font-size:.9rem;color:#587082}[hidden]{display:none!important}
@media(max-width:480px){main{padding:16px}th,td{padding:8px;font-size:.9rem}.controls button{flex:1}}
"""

# This script is entirely application-owned. Model strings enter through JSON and
# textContent only; no innerHTML, eval, dynamic source code, or network requests.
_SCRIPT = """
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('visual-data').textContent);
  const root = document.getElementById('visual');
  const el = (tag, text, cls) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (cls) node.className = cls;
    return node;
  };
  const button = (text, action) => {
    const node = el('button', text);
    node.type = 'button'; node.addEventListener('click', action); return node;
  };
  const status = el('p', '', 'status'); status.setAttribute('role', 'status');
  if (data.kind === 'comparison') {
    const controls = el('div', undefined, 'controls');
    const table = el('table');
    const caption = el('caption', '选择一侧观察，再对照差异。'); table.append(caption);
    const head = el('thead'); const header = el('tr');
    ['比较项', data.left_label, data.right_label].forEach(t => {const c=el('th',t); c.scope='col'; header.append(c);});
    head.append(header); table.append(head); const body = el('tbody');
    for (const row of data.rows) {
      const tr=el('tr'); const first=el('th',row.aspect); first.scope='row'; tr.append(first,el('td',row.left),el('td',row.right)); body.append(tr);
    }
    table.append(body); const wrap=el('div',undefined,'table-wrap'); wrap.append(table);
    const buttons=[];
    const show = side => {
      for (const row of [...head.rows,...body.rows]) {row.cells[1].hidden=side==='right'; row.cells[2].hidden=side==='left';}
      buttons.forEach(([b,key])=>b.setAttribute('aria-pressed',String(key===side)));
      status.textContent=side==='both'?'正在同时显示两侧。':('正在显示：'+(side==='left'?data.left_label:data.right_label));
    };
    for (const [key,label] of [['both','同时显示'],['left',data.left_label],['right',data.right_label]]) {
      const b=button(label,()=>show(key)); buttons.push([b,key]); controls.append(b);
    }
    root.append(controls,wrap,status); show('both');
  } else if (data.kind === 'process') {
    let index=0;
    const progress=el('progress'); progress.max=data.steps.length; progress.setAttribute('aria-label','步骤进度');
    const card=el('section',undefined,'card'); const heading=el('h2'); const text=el('p'); card.append(heading,text);
    const controls=el('div',undefined,'controls');
    const prev=button('上一步',()=>{index--;update();}); const next=button('下一步',()=>{index++;update();}); controls.append(prev,next);
    const update=()=>{const step=data.steps[index]; heading.textContent=step.title;text.textContent=step.description;progress.value=index+1;status.textContent=`第 ${index+1} / ${data.steps.length} 步`;prev.disabled=index===0;next.disabled=index===data.steps.length-1;};
    root.append(progress,card,controls,status); update();
  } else if (data.kind === 'flashcards') {
    let index=0, flipped=false;
    const side=el('p',undefined,'muted'); const card=el('section',undefined,'card');
    const controls=el('div',undefined,'controls');
    const prev=button('上一张',()=>{index--;flipped=false;update();});
    const flip=button('查看答案',()=>{flipped=!flipped;update();});
    const next=button('下一张',()=>{index++;flipped=false;update();}); controls.append(prev,flip,next);
    const update=()=>{const item=data.cards[index];side.textContent=flipped?'答案':'问题';card.textContent=flipped?item.back:item.front;flip.textContent=flipped?'返回问题':'查看答案';flip.setAttribute('aria-pressed',String(flipped));prev.disabled=index===0;next.disabled=index===data.cards.length-1;status.textContent=`第 ${index+1} / ${data.cards.length} 张`;};
    root.append(side,card,controls,status); update();
  } else if (data.kind === 'linear_function') {
    const equation=el('p'); equation.setAttribute('role','status');
    const canvas=el('canvas'); canvas.width=720;canvas.height=360;canvas.setAttribute('role','img');
    const slider=(name,value,min,max)=>{const label=el('label');const text=el('span',name);const input=el('input');input.type='range';input.min=String(min);input.max=String(max);input.step='0.1';input.value=String(value);input.setAttribute('aria-label',name);label.append(text,input);root.append(label);return input;};
    root.append(equation,canvas);
    const slope=slider('斜率 m',data.slope,-10,10);const intercept=slider('截距 b',data.intercept,-20,20);
    const xmin=data.x_min,xmax=data.x_max;
    const fitExtent=()=>Math.max(1,Math.abs(xmin),Math.abs(xmax),Math.abs(Number(slope.value)*xmin+Number(intercept.value)),Math.abs(Number(slope.value)*xmax+Number(intercept.value)))*1.15;
    // Keep the scale fixed while a parameter changes: otherwise m cancels out
    // against the y-axis extent and different slopes appear identical.
    const initialExtent=fitExtent();let extent=initialExtent;
    const controls=el('div',undefined,'controls');
    controls.append(button('适应当前直线',()=>{extent=fitExtent();draw();}),button('重置演示',()=>{slope.value=String(data.slope);intercept.value=String(data.intercept);extent=initialExtent;draw();}));
    root.append(controls);
    const draw=()=>{
      const m=Number(slope.value),b=Number(intercept.value);
      equation.textContent=`y = ${m.toFixed(1)}x ${b<0?'−':'+'} ${Math.abs(b).toFixed(1)}`;
      canvas.setAttribute('aria-label',equation.textContent+`，横轴从 ${xmin} 到 ${xmax}`);
      const c=canvas.getContext('2d');if(!c){status.textContent='当前浏览器无法绘图，仍可通过上方公式观察数值变化。';return;}
      const W=canvas.width,H=canvas.height,p=38;
      const X=x=>p+(x-xmin)/(xmax-xmin)*(W-2*p),Y=y=>H-p-(y+extent)/(2*extent)*(H-2*p);
      c.clearRect(0,0,W,H);c.font='13px system-ui';c.lineWidth=1;c.strokeStyle='#dbe5ec';
      for(let i=0;i<=4;i++){const x=xmin+(xmax-xmin)*i/4,px=p+(W-2*p)*i/4;c.beginPath();c.moveTo(px,p);c.lineTo(px,H-p);c.stroke();c.fillStyle='#425d70';c.fillText(x.toFixed(1),px-12,H-12);}
      c.strokeStyle='#7694a9';c.beginPath();c.moveTo(p,Y(0));c.lineTo(W-p,Y(0));if(xmin<=0&&xmax>=0){c.moveTo(X(0),p);c.lineTo(X(0),H-p);}c.stroke();
      c.save();c.beginPath();c.rect(p,p,W-2*p,H-2*p);c.clip();
      c.strokeStyle='#176d9c';c.lineWidth=3;c.beginPath();c.moveTo(p,Y(m*xmin+b));c.lineTo(W-p,Y(m*xmax+b));c.stroke();c.restore();
      c.fillStyle='#425d70';c.fillText(extent.toFixed(1),3,p+4);c.fillText((-extent).toFixed(1),3,H-p);c.fillText('y',7,18);c.fillText('x',W-14,H-12);
      const clipped=Math.abs(m*xmin+b)>extent||Math.abs(m*xmax+b)>extent;
      status.textContent='拖动滑块，观察斜率与截距改变后的直线；坐标轴保持固定。'+(clipped?'部分直线超出视窗，可点“适应当前直线”调整视窗。':'');
    };
    slope.addEventListener('input',draw);intercept.addEventListener('input',draw);root.append(status);draw();
  }
})();
"""


def _diagram_svg(spec: dict) -> str:
    """Render a node/edge diagram as static, escaped SVG (no scripts, no links)."""
    nodes = {}
    for node in spec["nodes"]:
        if node["id"] in nodes:
            raise ValueError("duplicate_node_id")
        nodes[node["id"]] = node
    for edge in spec["edges"]:
        if edge["from"] not in nodes or edge["to"] not in nodes:
            raise ValueError("diagram_dangling_edge")

    width, height, pad = 900, 560, 80

    def position(node):
        return (pad + node["x"] / 100 * (width - 2 * pad),
                pad + node["y"] / 100 * (height - 2 * pad))

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(spec["title"], quote=True)}" '
        'xmlns="http://www.w3.org/2000/svg">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#7694a9"/></marker></defs>',
    ]
    for edge in spec["edges"]:
        x1, y1 = position(nodes[edge["from"]])
        x2, y2 = position(nodes[edge["to"]])
        parts.append(
            f'<line x1="{x1:.0f}" y1="{y1:.0f}" x2="{x2:.0f}" y2="{y2:.0f}" '
            'stroke="#7694a9" stroke-width="2" marker-end="url(#arrow)"/>')
        if edge.get("label"):
            mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
            parts.append(
                f'<text x="{mid_x:.0f}" y="{mid_y - 8:.0f}" text-anchor="middle" '
                'font-size="13" fill="#425d70">'
                f'{html.escape(edge["label"])}</text>')
    for node in spec["nodes"]:
        x, y = position(node)
        label = html.escape(node["label"])
        box_w = min(240, max(96, len(node["label"]) * 15 + 28))
        parts.append(
            f'<g><rect x="{x - box_w / 2:.0f}" y="{y - 24:.0f}" width="{box_w:.0f}" height="48" rx="12" '
            'fill="#e8f1f6" stroke="#8da6b8" stroke-width="1.5"/>'
            f'<text x="{x:.0f}" y="{y + 5:.0f}" text-anchor="middle" font-size="15" '
            f'fill="#18334b">{label}</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def render_visual(spec: dict, image: bytes | None = None) -> str:
    """Validate a bounded declarative spec and render a self-contained page."""
    if not isinstance(spec, dict) or not _VALIDATOR.is_valid(spec):
        raise ValueError("invalid_visual_spec")
    if spec["kind"] == "scene":
        from learning_coach.adapters.scenes import render_scene

        return render_scene(spec)
    if spec["kind"] == "illustration":
        if image is None:
            raise ValueError("illustration_requires_image")
        from learning_coach.adapters.illustrate import image_page_html

        return image_page_html(spec, image)
    if spec["kind"] == "diagram":
        # Static SVG: no scripts, no dynamic data, so a simpler page suffices.
        title = html.escape(spec["title"], quote=True)
        return (
            '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'style-src \'unsafe-inline\'">'
            '<meta name="referrer" content="no-referrer"><title>' + title + '</title>'
            '<style>' + _CSS + '</style></head><body><main><h1>' + title + '</h1>'
            + _diagram_svg(spec) +
            '</main></body></html>'
        )
    if spec["kind"] == "linear_function":
        if not all(math.isfinite(spec[key]) for key in ("slope", "intercept", "x_min", "x_max")):
            raise ValueError("invalid_visual_number")
        if spec["x_min"] >= spec["x_max"]:
            raise ValueError("invalid_visual_range")
    data = json.dumps(spec, ensure_ascii=True, allow_nan=False)
    # Escape HTML raw-text delimiters even inside application/json script tags.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    title = html.escape(spec["title"], quote=True)
    script_hash = base64.b64encode(hashlib.sha256(_SCRIPT.encode("utf-8")).digest()).decode("ascii")
    policy = (
        "default-src 'none'; script-src 'sha256-" + script_hash
        + "'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
    )
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">'
        '<meta name="referrer" content="no-referrer"><title>' + title + '</title>'
        '<style>' + _CSS + '</style></head><body><main><h1>' + title + '</h1>'
        '<div id="visual"></div><noscript>请启用浏览器的 JavaScript 以使用互动内容。</noscript></main>'
        '<script type="application/json" id="visual-data">' + data + '</script>'
        '<script>' + _SCRIPT + '</script></body></html>'
    )


