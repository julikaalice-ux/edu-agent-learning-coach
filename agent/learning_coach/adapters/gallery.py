"""Render a gallery spec (web pictures + labels + guided steps + optional drawn companion) as one page.

The markup is built here from validated data with every model string
escaped; the small script only switches what is shown (panels, steps,
timeline playback, zoom). Pictures are embedded as data URLs, so the page
makes no network requests. A companion visual is the normal rendered page
of that kind, embedded in a sandboxed frame.
"""

from __future__ import annotations

import base64
import hashlib
import html
import re

from learning_coach.domain.teaching_visuals import gallery_arrangement

_DATA_URL = re.compile(r"data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+\Z")
_WEB_URL = re.compile(r"https?://[^\s\"'<>]+\Z")
_INLINE_SCRIPT = re.compile(r"<script>(.*?)</script>", re.S)
SOURCE_NAMES = {"commons": "Wikimedia Commons", "bocha": "网页搜索"}

_CSS = """
:root{font-family:system-ui,-apple-system,sans-serif;color:#18334b;background:#edf3f6;color-scheme:light}
*{box-sizing:border-box}body{margin:0;padding:20px 12px}main{max-width:980px;margin:auto;background:#fff;padding:22px;border-radius:18px}
h1{font-size:clamp(1.3rem,4vw,1.9rem);line-height:1.3;margin:0 0 16px;overflow-wrap:anywhere}
h2{font-size:1.1rem;margin:10px 0 6px}ul{margin:6px 0 0;padding-left:1.2em;line-height:1.6}
button{font:inherit;padding:8px 14px;min-height:40px;border:1px solid #8da6b8;border-radius:9px;background:#f4f8fa;color:#18334b;cursor:pointer}
button:hover,button:focus-visible{background:#dceaf2}button[aria-selected=true],button[aria-pressed=true]{background:#1e5d83;color:#fff;border-color:#1e5d83}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 14px}
.timeline .pick{position:relative;flex-wrap:nowrap;overflow-x:auto;padding:6px 2px 10px}
.timeline .pick::before{content:"";position:absolute;left:8px;right:8px;top:50%;border-top:2px solid #c7d8e3;z-index:0}
.timeline .pick button{position:relative;z-index:1;white-space:nowrap}
.panels{display:grid;gap:16px}
.side_by_side .panels{grid-template-columns:repeat(auto-fit,minmax(240px,1fr))}
.panel{margin:0;padding:12px;border:2px solid #e3edf3;border-radius:14px;background:#fbfdfe;transition:border-color .3s,box-shadow .3s}
.panel.spot{border-color:#1e5d83;box-shadow:0 0 0 4px #dceaf2}
.one .panel{display:none}.one .panel.shown{display:block;animation:enter .45s ease-out}
.photo-wrap{display:flex;align-items:center;justify-content:center;background:#f3f7fa;border-radius:10px;overflow:hidden;min-height:160px}
.photo{display:block;max-width:100%;max-height:380px;object-fit:contain;cursor:zoom-in}
.playing .panel.shown .photo{animation:drift 4s ease-in-out}
.no-photo{padding:24px;color:#587082;text-align:center}
.tour{margin-top:16px;padding:14px;border-radius:12px;background:#f3f7fa}
.tour ol{margin:0;padding-left:1.3em;line-height:1.65}.tour.js ol{padding-left:0;list-style:none}.tour.js ol li{display:none}.tour.js ol li.now{display:block;animation:enter .35s ease-out}
.tour .count{font-size:.9rem;color:#425d70;margin:0 0 6px}.tour .bar{margin:10px 0 0}
.companion{margin-top:20px}.companion iframe{width:100%;height:540px;border:1px solid #c7d8e3;border-radius:12px;background:#fff}
.credits{margin-top:18px;font-size:.82rem;color:#587082;line-height:1.55;overflow-wrap:anywhere}.credits a{color:#1e5d83}
.zoom{position:fixed;inset:0;background:rgba(10,25,40,.86);display:flex;flex-direction:column;align-items:center;justify-content:center;padding:16px;z-index:10;cursor:zoom-out}
.zoom img{max-width:100%;max-height:85vh;object-fit:contain;border-radius:8px;background:#fff}.zoom p{color:#fff;margin:10px 0 0}
[hidden]{display:none!important}
@keyframes enter{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@keyframes drift{from{transform:scale(1)}to{transform:scale(1.06)}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media(max-width:480px){main{padding:14px}.photo{max-height:300px}}
"""

# Application-owned; reads only DOM built above, never model text as code.
_SCRIPT = """
(() => {
  'use strict';
  const page = document.getElementById('gallery');
  const panels = Array.from(page.querySelectorAll('.panel'));
  const tabs = Array.from(page.querySelectorAll('.bar.pick button'));
  const single = page.classList.contains('one');
  let current = 0, timer = null;
  const show = (index) => {
    current = (index + panels.length) % panels.length;
    panels.forEach((panel, i) => panel.classList.toggle('shown', i === current));
    tabs.forEach((tab, i) => tab.setAttribute('aria-selected', String(i === current)));
  };
  const play = document.getElementById('play');
  const stop = () => {
    if (timer) { clearInterval(timer); timer = null; }
    page.classList.remove('playing');
    if (play) { play.textContent = '播放'; play.setAttribute('aria-pressed', 'false'); }
  };
  if (single) {
    tabs.forEach((tab, i) => tab.addEventListener('click', () => {
      stop(); panels.forEach(panel => panel.classList.remove('spot')); show(i);
    }));
    show(0);
  }
  if (play) {
    play.addEventListener('click', () => {
      if (timer) { stop(); return; }
      page.classList.add('playing');
      play.textContent = '暂停'; play.setAttribute('aria-pressed', 'true');
      timer = setInterval(() => show(current + 1), 4000);
    });
  }
  const tour = page.querySelector('.tour');
  if (tour) {
    tour.classList.add('js');
    const steps = Array.from(tour.querySelectorAll('ol li'));
    const count = tour.querySelector('.count');
    const prev = document.getElementById('prev'), next = document.getElementById('next');
    let step = 0;
    const go = (index) => {
      step = Math.max(0, Math.min(steps.length - 1, index));
      steps.forEach((item, i) => item.classList.toggle('now', i === step));
      count.textContent = '第 ' + (step + 1) + ' / ' + steps.length + ' 步';
      prev.disabled = step === 0; next.disabled = step === steps.length - 1;
      const target = steps[step].dataset.panel;
      panels.forEach(panel => panel.classList.remove('spot'));
      if (target !== undefined && panels[Number(target)]) {
        const panel = panels[Number(target)];
        if (single) { stop(); show(Number(target)); }
        panel.classList.add('spot');
        panel.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      }
    };
    prev.addEventListener('click', () => go(step - 1));
    next.addEventListener('click', () => go(step + 1));
    go(0);
  }
  const zoom = document.getElementById('zoom');
  const zoomed = zoom.querySelector('img');
  page.querySelectorAll('.photo').forEach(img => img.addEventListener('click', () => {
    zoomed.src = img.src; zoomed.alt = img.alt; zoom.hidden = false;
  }));
  const close = () => { zoom.hidden = true; zoomed.removeAttribute('src'); };
  zoom.addEventListener('click', close);
  document.addEventListener('keydown', event => { if (event.key === 'Escape') close(); });
})();
"""


def _hash(script: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii") + "'"


def _credit(panel: dict, photo: dict) -> str:
    source = SOURCE_NAMES.get(photo.get("source"), "网页")
    parts = [f"{html.escape(panel['label'])}：{html.escape(photo.get('title') or '未命名图片')}"]
    if photo.get("author"):
        parts.append("作者 " + html.escape(photo["author"]))
    parts.append("许可证 " + html.escape(photo.get("license") or "未知（版权归原作者，仅作学习展示）"))
    url = photo.get("page_url") or ""
    if _WEB_URL.match(url):
        escaped = html.escape(url, quote=True)
        parts.append(f'来源 {source}：<a href="{escaped}" target="_blank" rel="noopener noreferrer">{escaped}</a>')
    else:
        parts.append("来源 " + source)
    return "<li>" + "，".join(parts) + "</li>"


def render_gallery(spec: dict, photos: list | None, render_companion) -> str:
    """photos: per panel {data_url, title, author, license, page_url, source} or None."""
    panels = spec["panels"]
    photos = list(photos or [None] * len(panels))
    if len(photos) != len(panels):
        raise ValueError("gallery_photo_count_mismatch")
    for step in spec["steps"]:
        if step["panel"] is not None and step["panel"] >= len(panels):
            raise ValueError("gallery_step_panel_out_of_range")
    arrange = gallery_arrangement(spec)
    single = arrange in ("timeline", "tabs")
    parts = [f'<div id="gallery" class="{arrange}{" one" if single else ""}">']
    if single:
        labels = [(panel["time"] or panel["label"]) if arrange == "timeline" else panel["label"] for panel in panels]
        parts.append('<div class="bar pick" role="tablist">' + "".join(
            f'<button type="button" role="tab" aria-selected="false">{html.escape(label)}</button>'
            for label in labels) + "</div>")
        if arrange == "timeline":
            parts.append('<div class="bar"><button type="button" id="play" aria-pressed="false">播放</button></div>')
    parts.append('<div class="panels">')
    credits = []
    for index, (panel, photo) in enumerate(zip(panels, photos)):
        heading = html.escape(((panel["time"] + " · ") if panel["time"] else "") + panel["label"])
        if photo is not None:
            if not _DATA_URL.match(photo.get("data_url") or ""):
                raise ValueError("gallery_invalid_photo")
            picture = (f'<img class="photo" src="{photo["data_url"]}" alt="{html.escape(panel["label"], quote=True)}"'
                       ' title="点击放大">')
            credits.append(_credit(panel, photo))
        else:
            picture = '<div class="no-photo">这一格没有找到合适的网络图片，先看下面的要点。</div>'
        points = "".join(f"<li>{html.escape(point)}</li>" for point in panel["points"])
        parts.append(f'<figure class="panel" data-index="{index}"><div class="photo-wrap">{picture}</div>'
                     f"<figcaption><h2>{heading}</h2><ul>{points}</ul></figcaption></figure>")
    parts.append("</div>")
    if spec["steps"]:
        items = "".join(
            (f'<li data-panel="{step["panel"]}">' if step["panel"] is not None else "<li>")
            + html.escape(step["text"]) + "</li>" for step in spec["steps"])
        parts.append('<section class="tour" aria-live="polite"><p class="count">分步讲解</p>'
                     f"<ol>{items}</ol>"
                     '<div class="bar"><button type="button" id="prev">上一步</button>'
                     '<button type="button" id="next">下一步</button></div></section>')
    parts.append("</div>")
    hashes = [_hash(_SCRIPT)]
    if spec["companion"] is not None:
        companion = render_companion(spec["companion"])
        hashes += [_hash(script) for script in _INLINE_SCRIPT.findall(companion)]
        parts.append('<section class="companion"><h2>配合图解</h2>'
                     f'<iframe title="{html.escape(spec["companion"]["title"], quote=True)}" '
                     f'sandbox="allow-scripts" srcdoc="{html.escape(companion, quote=True)}"></iframe></section>')
    if credits:
        parts.append('<footer class="credits"><p>图片来源（只说明外观，不作原理证据）：</p><ul>'
                     + "".join(credits) + "</ul></footer>")
    parts.append('<div class="zoom" id="zoom" hidden><img alt=""><p>点击任意处关闭</p></div>')
    # A srcdoc frame inherits this policy, so the companion's own scripts are
    # allowed here by hash as well.
    policy = ("default-src 'none'; script-src " + " ".join(dict.fromkeys(hashes))
              + "; style-src 'unsafe-inline'; img-src data:; frame-src 'self'; base-uri 'none'; form-action 'none'")
    title = html.escape(spec["title"], quote=True)
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="' + html.escape(policy, quote=True) + '">'
            '<meta name="referrer" content="no-referrer"><title>' + title + '</title>'
            '<style>' + _CSS + '</style></head><body><main><h1>' + title + '</h1>'
            + "".join(parts) + '</main><script>' + _SCRIPT + '</script></body></html>')
