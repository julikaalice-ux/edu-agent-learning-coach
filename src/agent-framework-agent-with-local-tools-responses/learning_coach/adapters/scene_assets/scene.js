(() => {
  'use strict';
  const {spec,layout}=JSON.parse(document.getElementById('scene-data').textContent);
  const byId=id=>document.getElementById(id);
  const colors={neutral:'#3c5662',water:'#295e76',light:'#f5d785',accent:'#7acdb0',particle:'#cadfe2',warning:'#f2ab86',ink:'#e6f0ec'};
  const textColors={...colors,neutral:'#c6d9dc',water:'#9bcde1'};
  const draw=SVG().addTo('#scene').size('100%','100%').viewbox(0,0,860,440);
  draw.attr({role:'img','aria-label':spec.title});
  const art=draw.group().attr({'data-layer':'art'}), labels=draw.group().attr({'data-layer':'labels'});
  let index=0,timer=null;
  byId('goal').textContent=spec.learning_goal;
  byId('interaction-hint').textContent=spec.interaction_hint;
  byId('check-question').textContent=spec.check_question;
  byId('limitations').textContent=spec.limitations;
  byId('timeline').max=String(spec.frames.length-1);
  const stop=()=>{if(timer!==null)clearInterval(timer);timer=null;byId('play').textContent='自动播放';byId('play').setAttribute('aria-pressed','false');};
  const go=i=>{stop();index=Math.max(0,Math.min(spec.frames.length-1,i));render();};
  const buttons=spec.frames.map((frame,i)=>{
    const button=document.createElement('button');button.type='button';button.textContent=`${String(i+1).padStart(2,'0')}  ${frame.title}`;
    button.addEventListener('click',()=>go(i));byId('steps').append(button);return button;
  });
  const arrow=(x1,y1,x2,y2,color,opacity)=>{
    art.line(x1,y1,x2,y2).stroke({color,width:2.5,opacity});
    const angle=Math.atan2(y2-y1,x2-x1),length=9;
    art.polyline([[x2-length*Math.cos(angle-.4),y2-length*Math.sin(angle-.4)],[x2,y2],
      [x2-length*Math.cos(angle+.4),y2-length*Math.sin(angle+.4)]]).fill('none').stroke({color,width:2.5,opacity});
  };
  function render(){
    art.clear();labels.clear();const frame=spec.frames[index];
    for(const item of frame.elements){
      const color=colors[item.color],opacity=item.opacity===undefined?1:item.opacity;
      let shape;
      if(item.shape==='rect')shape=art.rect(item.width*8.6,item.height*4.4).move(item.x*8.6,item.y*4.4).radius(7).fill(color).opacity(opacity);
      else if(item.shape==='ellipse')shape=art.ellipse(item.width*8.6,item.height*4.4).move(item.x*8.6,item.y*4.4).fill(color).opacity(opacity);
      else if(item.shape==='line')shape=art.line(item.x1*8.6,item.y1*4.4,item.x2*8.6,item.y2*4.4).stroke({color,width:2.5,opacity});
      else if(item.shape==='arrow')arrow(item.x1*8.6,item.y1*4.4,item.x2*8.6,item.y2*4.4,color,opacity);
      else if(item.shape==='polyline')shape=art.polyline(item.points.map(p=>[p[0]*8.6,p[1]*4.4])).fill('none').stroke({color,width:2.5,opacity});
      if(shape)shape.attr({'data-element':item.id});
      const box=layout[index][item.id];
      if(box){
        const text=labels.text(add=>box.lines.forEach((line,i)=>{const span=add.tspan(line);if(i>0)span.newLine();}));
        const contrast=['light','accent','particle','warning'].includes(item.color)?'#10252e':'#e5f0ed';
        text.font({family:'system-ui, sans-serif',size:box.font_size,anchor:'middle',leading:1.35})
          .fill(item.shape==='text'?textColors[item.color]:(item.shape==='rect'||item.shape==='ellipse'?contrast:'#e5f0ed'))
          .center(box.cx,box.cy).attr({'data-label':item.id});
      }
    }
    draw.attr({'data-frame':String(index),'aria-label':frame.title+'。'+frame.explanation});
    byId('position').textContent=`第 ${index+1} / ${spec.frames.length} 步`;
    byId('frame-title').textContent=frame.title;byId('frame-explanation').textContent=frame.explanation;
    byId('previous').disabled=index===0;byId('next').disabled=index===spec.frames.length-1;
    byId('timeline').value=String(index);
    buttons.forEach((b,i)=>b.setAttribute('aria-pressed',String(i===index)));
  }
  byId('previous').addEventListener('click',()=>go(index-1));
  byId('next').addEventListener('click',()=>go(index+1));
  byId('timeline').addEventListener('input',e=>go(Number(e.target.value)));
  byId('play').addEventListener('click',()=>{
    if(timer!==null){stop();return;}
    if(index===spec.frames.length-1){index=0;render();}
    byId('play').textContent='暂停播放';byId('play').setAttribute('aria-pressed','true');
    timer=setInterval(()=>{if(document.hidden)return;index++;render();if(index===spec.frames.length-1)stop();},2500);
  });
  render();
})();
