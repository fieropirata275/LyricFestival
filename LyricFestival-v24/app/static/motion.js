/* LyricFestival motion kit: path morphing, icon states, semantic sigils. */
(()=>{
'use strict';
const NS='http://www.w3.org/2000/svg';
const reduced=()=>matchMedia('(prefers-reduced-motion: reduce)').matches;

/* ---------- easing ---------- */
const Ease={
  linear:t=>t,
  outExpo:t=>t>=1?1:1-Math.pow(2,-10*t),
  inOutQuint:t=>t<.5?16*t*t*t*t*t:1-Math.pow(-2*t+2,5)/2,
  outBack:t=>{const c1=1.45,c3=c1+1;return 1+c3*Math.pow(t-1,3)+c1*Math.pow(t-1,2)},
  outQuart:t=>1-Math.pow(1-t,4),
  inOutCubic:t=>t<.5?4*t*t*t:1-Math.pow(-2*t+2,3)/2
};

/* ---------- shared ticker ---------- */
const tweens=new Set();let raf=0;
function tick(now){
  raf=0;
  for(const tw of tweens){
    const t=Math.min(1,(now-tw.start)/tw.dur);
    tw.step(tw.ease(t),t);
    if(t>=1){tweens.delete(tw);tw.done&&tw.done()}
  }
  if(tweens.size)raf=requestAnimationFrame(tick);
}
function tween(dur,step,ease=Ease.inOutQuint,done){
  const tw={start:performance.now(),dur:Math.max(1,dur),step,ease,done};
  tweens.add(tw);if(!raf)raf=requestAnimationFrame(tick);
  return ()=>tweens.delete(tw);
}

/* ---------- geometry ---------- */
let probe=null;
function probePath(){
  if(probe&&probe.isConnected)return probe;
  const svg=document.createElementNS(NS,'svg');
  svg.setAttribute('aria-hidden','true');
  svg.style.cssText='position:absolute;width:0;height:0;overflow:hidden;visibility:hidden;pointer-events:none';
  probe=document.createElementNS(NS,'path');svg.appendChild(probe);
  (document.body||document.documentElement).appendChild(svg);
  return probe;
}
const sampleCache=new Map();
function sample(d,n){
  const key=n+'|'+d;
  const hit=sampleCache.get(key);if(hit)return hit;
  const p=probePath();p.setAttribute('d',d);
  let L=0;try{L=p.getTotalLength()}catch(e){}
  const closed=/z\s*$/i.test(d.trim());
  const pts=new Float32Array(n*2);
  const div=closed?n:Math.max(1,n-1);
  for(let i=0;i<n;i++){
    const q=L?p.getPointAtLength(L*i/div):{x:0,y:0};
    pts[i*2]=q.x;pts[i*2+1]=q.y;
  }
  const r={pts,closed};
  if(sampleCache.size>600)sampleCache.clear();
  sampleCache.set(key,r);
  return r;
}
function centroid(pts){
  let x=0,y=0;const n=pts.length/2;
  for(let i=0;i<n;i++){x+=pts[i*2];y+=pts[i*2+1]}
  return[x/n,y/n];
}
function collapsed(pts){
  const[c0,c1]=centroid(pts);const o=new Float32Array(pts.length);
  for(let i=0;i<o.length;i+=2){o[i]=c0;o[i+1]=c1}
  return o;
}
function dist2(a,b,shift,rev){
  const n=a.length/2;let s=0;
  for(let i=0;i<n;i+=2){
    let j=rev?(shift-i+n*2)%n:(i+shift)%n;
    const dx=a[i*2]-b[j*2],dy=a[i*2+1]-b[j*2+1];s+=dx*dx+dy*dy;
  }
  return s;
}
function reorder(b,shift,rev){
  const n=b.length/2,o=new Float32Array(b.length);
  for(let i=0;i<n;i++){
    const j=rev?(shift-i+n*2)%n:(i+shift)%n;
    o[i*2]=b[j*2];o[i*2+1]=b[j*2+1];
  }
  return o;
}
function align(a,aClosed,b,bClosed){
  const n=b.length/2;
  if(aClosed&&bClosed){
    let best=Infinity,bs=0,br=false;
    for(let s=0;s<n;s+=2){
      const f=dist2(a,b,s,false);if(f<best){best=f;bs=s;br=false}
      const r=dist2(a,b,s,true);if(r<best){best=r;bs=s;br=true}
    }
    return reorder(b,bs,br);
  }
  const f=dist2(a,b,0,false),r=dist2(a,b,n-1,true);
  return r<f?reorder(b,n-1,true):b;
}
function toD(pts,closed){
  let s='M'+pts[0].toFixed(2)+' '+pts[1].toFixed(2);
  for(let i=2;i<pts.length;i+=2)s+='L'+pts[i].toFixed(2)+' '+pts[i+1].toFixed(2);
  return closed?s+'Z':s;
}

/* ---------- morphing path slot ---------- */
function morphSlot(el,targetD,{n=48,dur=520,ease=Ease.inOutQuint,twin=null}={}){
  el._stop&&el._stop();
  const cur=el._pts?{pts:el._pts,closed:el._closed}:(el._d?sample(el._d,n):null);
  if(!cur&&!targetD){el.setAttribute('d','');twin&&twin.setAttribute('d','');return}
  let tgt=targetD?sample(targetD,n):null;
  const from=cur?cur.pts:collapsed(tgt.pts);
  const fromClosed=cur?cur.closed:tgt.closed;
  let to=tgt?align(from,fromClosed,tgt.pts,tgt.closed):collapsed(from);
  const toClosed=tgt?tgt.closed:fromClosed;
  const o0=cur?1:0,o1=tgt?1:0;
  const closedMid=fromClosed&&toClosed;
  const buf=new Float32Array(from.length);
  const set=d=>{el.setAttribute('d',d);twin&&twin.setAttribute('d',d)};
  if(reduced()||dur<=1){
    el._pts=tgt?tgt.pts:null;el._closed=toClosed;el._d=targetD;
    set(targetD||'');el.style.opacity=o1;return;
  }
  el._stop=tween(dur,e=>{
    for(let i=0;i<buf.length;i++)buf[i]=from[i]+(to[i]-from[i])*e;
    el._pts=buf.slice();el._closed=e>.5?toClosed:fromClosed;
    set(toD(buf,closedMid));
    el.style.opacity=(o0+(o1-o0)*Math.min(1,e*1.4)).toFixed(3);
  },ease,()=>{
    el._pts=tgt?to:null;el._closed=toClosed;el._d=targetD;
    set(targetD||'');el.style.opacity=o1;el._stop=null;
  });
}

/* ---------- shape helpers ---------- */
const f=v=>+v.toFixed(2);
function circle(cx,cy,r){return`M${f(cx-r)} ${f(cy)}A${r} ${r} 0 1 0 ${f(cx+r)} ${f(cy)}A${r} ${r} 0 1 0 ${f(cx-r)} ${f(cy)}Z`}
function ellipse(cx,cy,rx,ry){return`M${f(cx-rx)} ${f(cy)}A${rx} ${ry} 0 1 0 ${f(cx+rx)} ${f(cy)}A${rx} ${ry} 0 1 0 ${f(cx-rx)} ${f(cy)}Z`}
function star(cx,cy,ro,ri,k,rot=-90){
  let s='';
  for(let i=0;i<k*2;i++){
    const r=i%2?ri:ro,a=(rot+i*180/k)*Math.PI/180;
    s+=(i?'L':'M')+f(cx+Math.cos(a)*r)+' '+f(cy+Math.sin(a)*r);
  }
  return s+'Z';
}
function poly(cx,cy,r,k,rot=-90){return star(cx,cy,r,r,k,rot)}
function line(x1,y1,x2,y2){return`M${f(x1)} ${f(y1)}L${f(x2)} ${f(y2)}`}
const HEART='M100 168C44 126 24 98 24 70C24 48 41 32 62 32C79 32 92 42 100 56C108 42 121 32 138 32C159 32 176 48 176 70C176 98 156 126 100 168Z';

/* ---------- icons (24 grid, stroke) ---------- */
const ICONS={
  bolt:['M13.2 2.5L4.5 13.6H11.2L10.4 21.5L19.5 10.1H12.8L13.2 2.5Z'],
  target:[circle(12,12,8.6),circle(12,12,4.6),circle(12,12,1)],
  wave:['M2.5 12H5.5L7.6 7L10.4 17L13.2 4.5L16 19L18.4 12H21.5'],
  scan:['M12 3.2A8.8 8.8 0 1 1 3.2 12',line(12,12,18.2,5.8),circle(12,12,1.1)],
  check:['M4.6 12.6L9.6 17.4L19.6 6.8'],
  cross:[line(6.4,6.4,17.6,17.6),line(17.6,6.4,6.4,17.6)],
  headphones:['M4 15.5V12.5A8 8 0 0 1 20 12.5V15.5','M4 14.2H6.4A1.6 1.6 0 0 1 8 15.8V18.6A1.6 1.6 0 0 1 6.4 20.2H5.2A1.2 1.2 0 0 1 4 19V14.2Z','M20 14.2H17.6A1.6 1.6 0 0 0 16 15.8V18.6A1.6 1.6 0 0 0 17.6 20.2H18.8A1.2 1.2 0 0 0 20 19V14.2Z'],
  speaker:['M3.8 9.4H7.4L12 5.4V18.6L7.4 14.6H3.8Z','M15.4 9A4.2 4.2 0 0 1 15.4 15','M17.9 6.4A7.8 7.8 0 0 1 17.9 17.6'],
  muted:['M3.8 9.4H7.4L12 5.4V18.6L7.4 14.6H3.8Z',line(15.6,9.6,20.4,14.4),line(20.4,9.6,15.6,14.4)],
  rave:['M12 2.6C12.7 8.4 15.6 11.3 21.4 12C15.6 12.7 12.7 15.6 12 21.4C11.3 15.6 8.4 12.7 2.6 12C8.4 11.3 11.3 8.4 12 2.6Z',line(19.2,2.4,19.2,6.4),line(17.2,4.4,21.2,4.4)],
  mic:['M9 5.6A3 3 0 0 1 15 5.6V11.4A3 3 0 0 1 9 11.4Z','M5.6 11.2A6.4 6.4 0 0 0 18.4 11.2',line(12,17.6,12,21)],
  expand:['M4 9V4H9','M15 4H20V9','M20 15V20H15','M9 20H4V15'],
  collapse:['M9 4V9H4','M20 9H15V4','M15 20V15H20','M4 15H9V20'],
  play:['M7.5 4.8L19 12L7.5 19.2Z'],
  pause:['M7 5H10V19H7Z','M14 5H17V19H14Z'],
  loader:['M12 3.2A8.8 8.8 0 1 1 3.2 12'],
  eye:['M2.5 12C5 7.2 8.3 5 12 5C15.7 5 19 7.2 21.5 12C19 16.8 15.7 19 12 19C8.3 19 5 16.8 2.5 12Z',circle(12,12,3)],
  eyeoff:['M2.5 12C5 7.2 8.3 5 12 5C15.7 5 19 7.2 21.5 12C19 16.8 15.7 19 12 19C8.3 19 5 16.8 2.5 12Z',line(4,4,20,20)],
  note:['M9 18.2V5.4L19 3.4V16.2',circle(6.6,18.2,2.4),circle(16.6,16.2,2.4)],
  aperture:[circle(12,12,8.6),poly(12,12,4.4,6),'M12 3.4L15.9 10.1'],
  potato:['M6.4 8.4C8.1 4.6 15 3.9 17.7 7.3C20.4 10.9 19.3 17.1 14.5 18.7C9.9 20.2 4.5 17.4 4.7 12.9C4.8 11.2 5.5 9.8 6.4 8.4Z',circle(10,10.5,.7),circle(14.2,14.4,.7)],
  high:['M3.5 17L8.6 11.4L12.4 14.6L20.5 6.4','M15.2 6.2H20.6V11.6'],
  max:['M12 2.6C12.7 8.4 15.6 11.3 21.4 12C15.6 12.7 12.7 15.6 12 21.4C11.3 15.6 8.4 12.7 2.6 12C8.4 11.3 11.3 8.4 12 2.6Z',circle(12,12,10),line(19.2,2.4,19.2,6.4)],
  link:['M10 14L14 10','M8.6 11.4L6.5 13.5A3.5 3.5 0 0 0 11.5 18.5L13.6 16.4','M15.4 12.6L17.5 10.5A3.5 3.5 0 0 0 12.5 5.5L10.4 7.6']
};

class MorphIcon{
  constructor(host,name,opts={}){
    this.n=opts.n||40;this.dur=opts.dur||560;this.slots=[];
    const svg=document.createElementNS(NS,'svg');
    svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');
    svg.classList.add('mi');
    for(let i=0;i<4;i++){const p=document.createElementNS(NS,'path');svg.appendChild(p);this.slots.push(p)}
    this.svg=svg;host.innerHTML='';host.appendChild(svg);
    this.name=null;this.set(name,{instant:true});
  }
  set(name,{instant=false,dur}={}){
    if(name===this.name||!ICONS[name])return this;
    const def=ICONS[name];this.name=name;this.svg.dataset.icon=name;
    this.slots.forEach((p,i)=>morphSlot(p,def[i]||null,{n:this.n,dur:instant?0:(dur||this.dur),ease:Ease.inOutQuint}));
    return this;
  }
}

/* ---------- semantic sigils (200 grid) ---------- */
const SIGILS={
  default:[circle(100,100,62),'M100 56L144 100L100 144L56 100Z',circle(100,100,5)],
  fire:['M100 18C112 54 152 74 150 124C148 160 124 184 100 184C74 184 50 162 50 126C50 94 72 80 78 50C88 70 96 76 100 18Z','M100 96C108 116 124 124 122 146C120 162 110 172 100 172C88 172 78 162 78 146C78 130 92 122 100 96Z'],
  ice:[line(100,20,100,180),line(30.7,60,169.3,140),line(169.3,60,30.7,140)],
  water:['M100 20C100 20 152 86 152 124A52 52 0 0 1 48 124C48 86 100 20 100 20Z','M68 128C80 118 90 138 100 128C110 118 120 138 132 128'],
  love:[HEART,circle(100,98,10)],
  money:['M136 62C126 44 74 42 68 70C62 100 136 98 132 130C128 158 74 160 64 138',line(100,26,100,174)],
  luxury:['M52 72L78 40H122L148 72L100 166Z',line(52,72,148,72),'M78 40L100 72L122 40'],
  royal:['M40 150L28 62L70 96L100 42L130 96L172 62L160 150Z',line(40,172,160,172),circle(100,124,7)],
  speed:['M44 44L100 100L44 156','M100 44L156 100L100 156',line(18,100,52,100)],
  up:[line(100,176,100,34),'M48 86L100 34L152 86',line(64,176,136,176)],
  down:[line(100,24,100,166),'M48 114L100 166L152 114',line(64,24,136,24)],
  impact:[star(100,100,86,36,8),circle(100,100,16)],
  weapon:[circle(100,100,58),line(100,22,100,178),line(22,100,178,100)],
  tech:[poly(100,100,76,6),poly(100,100,42,6,0),circle(100,100,7)],
  glitch:['M44 54H136V124H44Z','M64 76H156V146H64Z',line(26,100,174,100)],
  cosmic:[circle(100,100,40),'M22 116C10 94 190 58 178 84C170 102 34 138 22 116Z',circle(158,44,8)],
  rave:['M100 16C106 72 128 94 184 100C128 106 106 128 100 184C94 128 72 106 16 100C72 94 94 72 100 16Z',circle(100,100,82),circle(100,100,24)],
  dark:['M128 30A72 72 0 1 0 170 128A56 56 0 1 1 128 30Z',star(150,64,12,4,4)],
  light:[star(100,100,84,60,12),circle(100,100,38)],
  angel:[ellipse(100,40,54,14),'M94 112C72 80 36 84 22 122C48 114 70 126 94 146','M106 112C128 80 164 84 178 122C152 114 130 126 106 146'],
  devil:[line(100,182,100,30),'M54 30V66C54 98 146 98 146 66V30',line(100,30,100,82)],
  pain:[HEART,'M100 56L88 86L110 108L92 136L100 168'],
  rage:['M114 14L46 110H96L84 186L156 86H106Z',line(34,40,56,60),line(166,146,144,126)],
  body:[circle(78,100,46),circle(122,100,46)],
  smoke:['M36 140C56 118 76 162 100 140C124 118 144 162 164 140','M36 104C56 82 76 126 100 104C124 82 144 126 164 104','M36 68C56 46 76 90 100 68C124 46 144 90 164 68'],
  toxic:['M100 26L176 160H24Z',line(100,76,100,118),circle(100,138,5)],
  street:[line(86,28,38,176),line(114,28,162,176),line(100,48,100,168)],
  alone:[circle(100,100,70),circle(148,58,7)],
  stop:[poly(100,100,74,8,-112.5),line(62,100,138,100)],
  repeat:['M100 100C72 58 24 60 24 100C24 140 72 142 100 100C128 58 176 60 176 100C176 140 128 142 100 100Z',circle(100,100,4)],
  logo:['M100 16C106 72 128 94 184 100C128 106 106 128 100 184C94 128 72 106 16 100C72 94 94 72 100 16Z',circle(100,100,86),circle(100,100,30)]
};
SIGILS.shortword=SIGILS.impact;

class Sigil{
  constructor(host,opts={}){
    this.n=opts.n||96;this.dur=opts.dur||720;this.slots=[];
    const svg=document.createElementNS(NS,'svg');
    svg.setAttribute('viewBox','0 0 200 200');svg.setAttribute('aria-hidden','true');
    svg.classList.add('sigil');
    const uid='sg'+Math.random().toString(36).slice(2,8);
    svg.innerHTML=`<defs><linearGradient id="${uid}" x1="0" y1="0" x2="1" y2="1"><stop offset="0" class="s-a"/><stop offset="1" class="s-b"/></linearGradient></defs>`;
    const g=document.createElementNS(NS,'g');g.classList.add('sigil-shape');
    g.setAttribute('stroke',`url(#${uid})`);
    const gt=document.createElementNS(NS,'g');gt.classList.add('sigil-trace');
    for(let i=0;i<3;i++){
      const p=document.createElementNS(NS,'path'),t=document.createElementNS(NS,'path');
      t.setAttribute('pathLength','1');t.style.animationDelay=`${-i*.9}s`;if(i>0)t.style.display='none';
      g.appendChild(p);gt.appendChild(t);p._twin=t;this.slots.push(p);
    }
    svg.appendChild(g);svg.appendChild(gt);
    this.svg=svg;host.appendChild(svg);this.key=null;
    this.set(opts.initial||'default',{instant:true});
  }
  set(key,{instant=false}={}){
    if(!SIGILS[key])key='default';
    if(key===this.key)return this;
    this.key=key;this.svg.dataset.sigil=key;
    const def=SIGILS[key];
    this.slots.forEach((p,i)=>morphSlot(p,def[i]||null,{n:this.n,dur:instant?0:this.dur,ease:Ease.inOutQuint,twin:p._twin}));
    if(!instant&&!reduced())this.svg.animate(
      [{transform:'scale(.9) rotate(-8deg)',opacity:.55},{transform:'scale(1.04) rotate(2deg)',opacity:1,offset:.55},{transform:'none',opacity:1}],
      {duration:this.dur+120,easing:'cubic-bezier(.16,1,.3,1)'}
    );
    return this;
  }
}

/* ---------- glyph confetti (small sigil stamps) ---------- */
function glyph(key,size=40){
  const def=SIGILS[key]||SIGILS.default;
  const svg=document.createElementNS(NS,'svg');
  svg.setAttribute('viewBox','0 0 200 200');svg.setAttribute('width',size);svg.setAttribute('height',size);
  svg.setAttribute('aria-hidden','true');
  svg.innerHTML=def.map(d=>`<path d="${d}"/>`).join('');
  return svg;
}

/* ---------- text decode ---------- */
const GLYPHS='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#%&*+/<>=';
function decode(el,text,{dur=720}={}){
  text=String(text||'');
  el._decode&&el._decode();
  if(reduced()||!text){el.textContent=text;return}
  const chars=[...text];
  el._decode=tween(dur,(_,t)=>{
    let s='';
    for(let i=0;i<chars.length;i++){
      const reveal=i/chars.length*.72+.22;
      if(t>=reveal||chars[i]===' ')s+=chars[i];
      else s+=GLYPHS[(Math.random()*GLYPHS.length)|0];
    }
    el.textContent=s;
  },Ease.linear,()=>{el.textContent=text;el._decode=null});
}

/* warm geometry cache while idle so first morphs never stall */
function warm(){
  const jobs=[];
  for(const k in SIGILS)SIGILS[k].forEach(d=>jobs.push([d,96]));
  for(const k in ICONS)ICONS[k].forEach(d=>jobs.push([d,40]));
  const ric=window.requestIdleCallback||(cb=>setTimeout(()=>cb({timeRemaining:()=>8}),60));
  const run=dl=>{
    while(jobs.length&&dl.timeRemaining()>2){const[d,n]=jobs.shift();sample(d,n)}
    if(jobs.length)ric(run);
  };
  ric(run);
}
if(document.readyState==='complete')warm();else addEventListener('load',warm,{once:true});

window.LFMotion={Ease,tween,MorphIcon,Sigil,glyph,decode,ICONS,SIGILS,circle,star};
})();
