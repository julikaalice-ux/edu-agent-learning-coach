"""Declarative teaching visuals with a fixed renderer and expiring local artifacts."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator


def _text(limit: int) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": limit, "pattern": "\\S"}


def _object(properties: dict) -> dict:
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


def _variant(kind: str, properties: dict) -> dict:
    return _object({"kind": {"const": kind}, "title": _text(120), **properties})


VISUAL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TeachingVisual",
    "oneOf": [
        _variant("comparison", {
            "left_label": _text(80), "right_label": _text(80),
            "rows": {"type": "array", "minItems": 1, "maxItems": 8,
                     "items": _object({"aspect": _text(80), "left": _text(600), "right": _text(600)})},
        }),
        _variant("process", {
            "steps": {"type": "array", "minItems": 2, "maxItems": 8,
                      "items": _object({"title": _text(80), "description": _text(600)})},
        }),
        _variant("flashcards", {
            "cards": {"type": "array", "minItems": 1, "maxItems": 12,
                      "items": _object({"front": _text(300), "back": _text(800)})},
        }),
        _variant("linear_function", {
            "slope": {"type": "number", "minimum": -10, "maximum": 10},
            "intercept": {"type": "number", "minimum": -20, "maximum": 20},
            "x_min": {"type": "number", "minimum": -20, "maximum": 20},
            "x_max": {"type": "number", "minimum": -20, "maximum": 20},
        }),
    ],
}

_VALIDATOR = Draft202012Validator(VISUAL_SCHEMA)
_TOKEN = re.compile(r"[A-Za-z0-9_-]{32}\Z")
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
    const draw=()=>{
      const m=Number(slope.value),b=Number(intercept.value),xmin=data.x_min,xmax=data.x_max;
      equation.textContent=`y = ${m.toFixed(1)}x ${b<0?'−':'+'} ${Math.abs(b).toFixed(1)}`;
      canvas.setAttribute('aria-label',equation.textContent+`，横轴从 ${xmin} 到 ${xmax}`);
      const c=canvas.getContext('2d');if(!c){status.textContent='当前浏览器无法绘图，仍可通过上方公式观察数值变化。';return;}
      const W=canvas.width,H=canvas.height,p=38,extent=Math.max(1,Math.abs(m*xmin+b),Math.abs(m*xmax+b))*1.15;
      const X=x=>p+(x-xmin)/(xmax-xmin)*(W-2*p),Y=y=>H-p-(y+extent)/(2*extent)*(H-2*p);
      c.clearRect(0,0,W,H);c.font='13px system-ui';c.lineWidth=1;c.strokeStyle='#dbe5ec';
      for(let i=0;i<=4;i++){const x=xmin+(xmax-xmin)*i/4,px=p+(W-2*p)*i/4;c.beginPath();c.moveTo(px,p);c.lineTo(px,H-p);c.stroke();c.fillStyle='#425d70';c.fillText(x.toFixed(1),px-12,H-12);}
      c.strokeStyle='#7694a9';c.beginPath();c.moveTo(p,Y(0));c.lineTo(W-p,Y(0));if(xmin<=0&&xmax>=0){c.moveTo(X(0),p);c.lineTo(X(0),H-p);}c.stroke();
      c.strokeStyle='#176d9c';c.lineWidth=3;c.beginPath();c.moveTo(p,Y(m*xmin+b));c.lineTo(W-p,Y(m*xmax+b));c.stroke();
      c.fillStyle='#425d70';c.fillText(extent.toFixed(1),3,p+4);c.fillText((-extent).toFixed(1),3,H-p);c.fillText('y',7,18);c.fillText('x',W-14,H-12);
      status.textContent='拖动滑块，观察斜率与截距改变后的直线；纵轴会自动缩放。';
    };
    slope.addEventListener('input',draw);intercept.addEventListener('input',draw);root.append(status);draw();
  }
})();
"""


def render_visual(spec: dict) -> str:
    """Validate a bounded declarative spec and render a self-contained page."""
    if not isinstance(spec, dict) or not _VALIDATOR.is_valid(spec):
        raise ValueError("invalid_visual_spec")
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


class ArtifactStore:
    """Store prepared visuals after acceptance; tokens expire 24h after commit.

    A token is a bearer capability, not an authenticated user identity. Owners
    are hashed session identifiers supplied by application code, never the model.
    SQLite indexes survive restarts; only indexed files are readable. Prepared
    objects are accepted only by the instance that created them.
    """

    TTL_SECONDS = 24 * 60 * 60

    def __init__(self, root, public_base_url: str = "http://localhost:8088"):
        parsed = urlsplit(public_base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or any(c.isspace() for c in public_base_url)):
            raise ValueError("invalid_public_base_url")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        self._lock = threading.RLock()
        self._prepared = {}
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS artifacts "
                       "(token TEXT PRIMARY KEY, session_hash TEXT NOT NULL, expires_at REAL NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS artifacts_session ON artifacts(session_hash)")
        self._cleanup_expired()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.root / "index.sqlite3", timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _owner(session_id: str) -> str:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("invalid_session_id")
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def _cleanup_expired(self):
        with self._lock:
            now = time.time()
            with self._connection() as db:
                expired = db.execute("SELECT token FROM artifacts WHERE expires_at <= ?", (now,)).fetchall()
                # Keep the index if removal fails so a later cleanup can retry.
                for (token,) in expired:
                    if _TOKEN.fullmatch(token):
                        (self.root / (token + ".html")).unlink(missing_ok=True)
                db.execute("DELETE FROM artifacts WHERE expires_at <= ?", (now,))
            self._prepared = {token: item for token, item in self._prepared.items() if item[4] > now}

    def prepare(self, session_id: str, spec: dict) -> dict:
        owner = self._owner(session_id)
        document = render_visual(spec)
        with self._lock:
            for _ in range(10):
                token = secrets.token_urlsafe(24)
                if not _TOKEN.fullmatch(token):
                    raise ValueError("invalid_generated_token")
                with self._connection() as db:
                    exists = db.execute("SELECT 1 FROM artifacts WHERE token = ?", (token,)).fetchone()
                if token not in self._prepared and exists is None:
                    break
            else:
                raise RuntimeError("artifact_token_collision")
            url = self.public_base_url + "/artifacts/" + token
            self._prepared[token] = (
                owner, hashlib.sha256(document.encode("utf-8")).hexdigest(),
                spec["title"], url, time.time() + self.TTL_SECONDS,
            )
        return {"token": token, "url": url, "title": spec["title"], "html": document}

    def commit(self, session_id: str, artifact: dict) -> None:
        owner = self._owner(session_id)
        if (not isinstance(artifact, dict) or set(artifact) != {"token", "url", "title", "html"}
                or not all(isinstance(value, str) for value in artifact.values())
                or not _TOKEN.fullmatch(artifact["token"])):
            raise ValueError("invalid_prepared_artifact")
        token = artifact["token"]
        with self._lock:
            self._cleanup_expired()
            prepared = self._prepared.get(token)
            if (prepared is None or prepared[0] != owner
                    or prepared[1] != hashlib.sha256(artifact["html"].encode("utf-8")).hexdigest()
                    or prepared[2] != artifact["title"] or prepared[3] != artifact["url"]):
                raise ValueError("artifact_not_prepared_for_session")
            path = self.root / (token + ".html")
            temporary = None
            try:
                with self._connection() as db:
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT 1 FROM artifacts WHERE token = ?", (token,)).fetchone():
                        raise ValueError("artifact_already_committed")
                    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root,
                                                     prefix=".artifact-", suffix=".tmp", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(artifact["html"])
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                    temporary = None
                    db.execute("INSERT INTO artifacts(token, session_hash, expires_at) VALUES (?, ?, ?)",
                               (token, owner, time.time() + self.TTL_SECONDS))
                self._prepared.pop(token, None)
            except BaseException:
                # A failed commit must not leave a readable, partially accepted visual.
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
                with self._connection() as db:
                    accepted = db.execute("SELECT 1 FROM artifacts WHERE token = ?", (token,)).fetchone()
                if accepted is None:
                    path.unlink(missing_ok=True)
                raise

    def get(self, token: str) -> str | None:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            return None
        with self._lock:
            self._cleanup_expired()
            with self._connection() as db:
                found = db.execute("SELECT 1 FROM artifacts WHERE token = ? AND expires_at > ?",
                                   (token, time.time())).fetchone()
            if found is None:
                return None
            path = self.root / (token + ".html")
            if path.is_symlink():
                return None
            try:
                return path.read_text(encoding="utf-8")
            except (FileNotFoundError, UnicodeDecodeError):
                return None

    def delete_session(self, session_id: str) -> None:
        owner = self._owner(session_id)
        with self._lock:
            # Pending artifacts are revoked even when disk cleanup needs a retry.
            self._prepared = {token: item for token, item in self._prepared.items() if item[0] != owner}
            with self._connection() as db:
                rows = db.execute("SELECT token FROM artifacts WHERE session_hash = ?", (owner,)).fetchall()
                for (token,) in rows:
                    if _TOKEN.fullmatch(token):
                        (self.root / (token + ".html")).unlink(missing_ok=True)
                db.execute("DELETE FROM artifacts WHERE session_hash = ?", (owner,))
