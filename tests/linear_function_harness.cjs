// Run the production browser script against a small DOM/canvas test double.
// Record its actual drawing and events; do not duplicate coordinate formulas.
const fs = require('node:fs');
const vm = require('node:vm');
const {script, spec} = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = [];
const drawing = {paths: [], texts: [], path: [], clipped: false,
  clearRect() {this.paths = []; this.texts = [];},
  beginPath() {this.path = [];},
  moveTo(x,y) {this.path.push([x,y]);},
  lineTo(x,y) {this.path.push([x,y]);},
  stroke() {this.paths.push({color: this.strokeStyle, points: this.path, clipped: this.clipped});},
  fillText(text,x,y) {this.texts.push([text,x,y]);},
  save() {}, restore() {this.clipped = false;}, rect() {}, clip() {this.clipped = true;},
};
class Element {
  constructor(tag) {this.tag = tag; this.textContent = ''; this.children = []; this.attributes = {}; this.events = {}; elements.push(this);}
  append(...children) {this.children.push(...children);}
  setAttribute(key,value) {this.attributes[key] = value;}
  addEventListener(event,handler) {this.events[event] = handler;}
  getContext() {return drawing;}
}
const root = new Element('div');
const document = {
  getElementById(id) {return id === 'visual' ? root : {textContent: JSON.stringify(spec)};},
  createElement(tag) {return new Element(tag);},
};
vm.runInNewContext(script, {document});
const slope = elements.find(e => e.attributes['aria-label'] === '斜率 m');
const intercept = elements.find(e => e.attributes['aria-label'] === '截距 b');
const snapshot = () => JSON.parse(JSON.stringify({
  curve: drawing.paths.find(path => path.color === '#176d9c'),
  ticks: drawing.texts,
  slope: Number(slope.value), intercept: Number(intercept.value),
}));
const initial = snapshot();
slope.value = '2'; slope.events.input();
const changed = snapshot();
elements.find(e => e.tag === 'button' && e.textContent === '适应当前直线').events.click();
const fitted = snapshot();
intercept.value = '3'; intercept.events.input();
elements.find(e => e.tag === 'button' && e.textContent === '重置演示').events.click();
const reset = snapshot();
process.stdout.write(JSON.stringify({initial, changed, fitted, reset}));
