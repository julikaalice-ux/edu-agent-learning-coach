// Execute production frame controls and inspect the drawing output. SVG.js's
// real browser integration is covered separately, not imitated by these stubs.
const fs = require('node:fs');
const vm = require('node:vm');
const {script, markup, data} = JSON.parse(fs.readFileSync(0, 'utf8'));
class Element {
  constructor() {this.attributes={};this.events={};this.children=[];this.textContent='';}
  setAttribute(key,value) {this.attributes[key]=value;}
  addEventListener(name,handler) {this.events[name]=handler;}
  append(child) {this.children.push(child);}
  fire(name) {this.events[name]({target:this});}
}
const elements=Object.fromEntries([...markup.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
for(const tag of markup.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
  for(const attribute of tag[0].matchAll(/([\w-]+)="([^"]*)"/g)) {
    elements[tag[1]].setAttribute(attribute[1],attribute[2]);
  }
}
elements['scene-data']={textContent:JSON.stringify(data)};
class Shape {
  constructor(kind,args=[]) {this.kind=kind;this.args=args;this.children=[];this.attributes={};}
  add(kind,args=[]) {const child=new Shape(kind,args);this.children.push(child);return child;}
  group() {return this.add('group');}
  rect(...args) {return this.add('rect',args);}
  ellipse(...args) {return this.add('ellipse',args);}
  line(...args) {return this.add('line',args);}
  polyline(...args) {return this.add('polyline',args);}
  text(callback) {const node=this.add('text');callback({tspan:line=>node.add('tspan',[line])});return node;}
  attr(values) {Object.assign(this.attributes,values);return this;}
  clear() {this.children=[];return this;}
}
for(const method of ['addTo','size','viewbox','move','radius','fill','stroke','font','center','opacity','newLine']) {
  Shape.prototype[method]=function(...args){this.attributes[method]=args.length===1?args[0]:args;return this;};
}
const drawing=new Shape('svg'),timers=new Map();let nextTimer=0;
vm.runInNewContext(script,{
  SVG:()=>drawing,
  document:{getElementById:id=>{
    if(!elements[id])throw Error('missing_markup_id:'+id);return elements[id];
  },createElement:()=>new Element(),hidden:false},
  setInterval:callback=>{timers.set(++nextTimer,callback);return nextTimer;},
  clearInterval:id=>timers.delete(id),
});
const layer=name=>drawing.children.find(node=>node.attributes['data-layer']===name);
const snapshot=()=>JSON.parse(JSON.stringify({
  frame:drawing.attributes['data-frame'],
  art:layer('art').children,
  title:elements['frame-title'].textContent,
  explanation:elements['frame-explanation'].textContent,
  previousDisabled:elements.previous.disabled,nextDisabled:elements.next.disabled,
  slider:elements.timeline.value,playing:elements.play.attributes['aria-pressed'],
  selectedSteps:elements.steps.children.map(button=>button.attributes['aria-pressed']),
  activeTimers:timers.size,
}));
const tick=()=>{for(const callback of [...timers.values()])callback();};
const initial=snapshot();
elements.next.fire('click');const next=snapshot();
elements.previous.fire('click');const previous=snapshot();
elements.timeline.value='2';elements.timeline.fire('input');const slider=snapshot();
elements.play.fire('click');const restart=snapshot();
tick();const advanced=snapshot();
elements.play.fire('click');const paused=snapshot();
tick();const stillPaused=snapshot();
elements.play.fire('click');tick();const finished=snapshot();
elements.steps.children[0].fire('click');const firstStep=snapshot();
process.stdout.write(JSON.stringify({initial,next,previous,slider,restart,advanced,paused,stillPaused,finished,firstStep}));
