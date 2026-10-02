/* LyricFestival ambient renderer: palette-driven volumetric backdrop. */
(()=>{
'use strict';
const VS=`attribute vec2 aPos;void main(){gl_Position=vec4(aPos,0.,1.);}`;
const FS=`precision mediump float;
uniform vec2 uRes;uniform float uTime;uniform vec3 uA;uniform vec3 uB;
uniform float uEnergy;uniform float uFlash;uniform float uDetail;uniform float uDrift;
float hash(vec2 p){p=fract(p*vec2(123.34,456.21));p+=dot(p,p+45.32);return fract(p.x*p.y);}
float noise(vec2 p){vec2 i=floor(p),f=fract(p);vec2 u=f*f*(3.-2.*f);
 return mix(mix(hash(i),hash(i+vec2(1.,0.)),u.x),mix(hash(i+vec2(0.,1.)),hash(i+vec2(1.,1.)),u.x),u.y);}
float fbm(vec2 p){float v=0.,a=.5;mat2 m=mat2(1.6,1.2,-1.2,1.6);
 for(int i=0;i<5;i++){v+=a*noise(p);p=m*p;a*=.5;}return v;}
vec3 bokeh(vec2 p,float scale,float speed,float seed){
 vec2 g=p*scale+vec2(sin(uTime*.05+seed)*.6,uTime*speed);
 vec2 id=floor(g);vec2 f=fract(g)-.5;float h=hash(id+seed);
 vec2 off=vec2(hash(id+seed+1.3),hash(id+seed+7.1))-.5;
 float r=.025+.06*h;float d=length(f-off*.55);
 float disk=smoothstep(r,r*.7,d);
 float ring=smoothstep(r,r*.92,d)-smoothstep(r*.92,r*.78,d);
 float on=step(.88,h);float tw=.55+.45*sin(uTime*.9+h*40.);
 return mix(uA,uB,hash(id+seed+3.))*(disk*.22+ring*.26)*on*tw;
}
void main(){
 vec2 p=(gl_FragCoord.xy-.5*uRes)/uRes.y;
 float t=uTime*.04*uDrift;
 vec2 q=vec2(fbm(p*1.35+vec2(0.,t)),fbm(p*1.35+vec2(5.2,1.3)-t));
 vec2 r=vec2(fbm(p*1.6+3.*q+vec2(1.7,9.2)+t*1.3),fbm(p*1.6+3.*q+vec2(8.3,2.8)-t));
 float n=fbm(p*1.15+2.4*r+uEnergy*.08);
 float neb=smoothstep(.36,1.,n);neb*=neb;
 vec3 col=mix(vec3(.006,.006,.012),uB*.5,neb);
 col=mix(col,uA*.7,smoothstep(.62,1.12,n+length(r)*.3)*.55);
 vec2 src=vec2(0.,.72);vec2 dv=p-src;
 float ang=atan(dv.x,-dv.y);
 float shafts=pow(max(0.,sin(ang*11.+t*5.)*.5+.5),7.);
 shafts+=pow(max(0.,sin(ang*5.-t*3.3+1.7)*.5+.5),9.)*.7;
 shafts*=smoothstep(1.5,-.1,length(dv))*(.35+.65*fbm(vec2(ang*3.,t*2.)));
 col+=mix(uA,vec3(1.),.15)*shafts*(.03+.13*uEnergy);
 float d=length(p*vec2(.78,1.));
 col+=mix(uA,uB,.45)*exp(-d*d*4.2)*(.04+.22*uEnergy);
 if(uDetail>.5){
  col+=bokeh(p,3.4,.03,1.7)*.8;
  col+=bokeh(p,6.2,.055,9.1)*.55;
 }
 float streak=exp(-abs(p.y)*44.)*exp(-abs(p.x)*1.1);
 float streak2=exp(-abs(p.y-.018)*90.)*exp(-abs(p.x)*2.2);
 col+=(vec3(.35,.62,1.)*.7+uA*.3)*(streak+streak2*.5)*uFlash*.55;
 col+=uA*exp(-d*d*1.2)*uFlash*.08;
 col*=smoothstep(1.42,.22,length(p*vec2(.82,1.06)));
 col=pow(max(col,0.),vec3(1.18))*1.08;
 col+=(hash(gl_FragCoord.xy+fract(uTime*7.)*97.)-.5)*.03;
 gl_FragColor=vec4(max(col,0.),1.);
}`;

function hexToRgb(h){
  h=String(h||'').trim();
  if(h[0]==='#'){
    if(h.length===4)h='#'+h[1]+h[1]+h[2]+h[2]+h[3]+h[3];
    const v=parseInt(h.slice(1,7),16);
    if(Number.isFinite(v))return[(v>>16&255)/255,(v>>8&255)/255,(v&255)/255];
  }
  const m=h.match(/rgba?\(([^)]+)\)/);
  if(m){const a=m[1].split(',').map(Number);return[a[0]/255,a[1]/255,a[2]/255]}
  return[.84,1,.34];
}
function grade(c){
  const mx=Math.max(c[0],c[1],c[2]),mn=Math.min(c[0],c[1],c[2]);
  const k=.3+.7*Math.min(1,(mx-mn)*1.6);
  return c.map(v=>v*k);
}

const S={
  gl:null,canvas:null,prog:null,u:{},enabled:true,running:false,
  a:[.84,1,.34],b:[.5,.36,1],ta:[.84,1,.34],tb:[.5,.36,1],
  energy:0,flash:0,scale:.5,detail:1,fpsCap:60,last:0,t0:performance.now(),drift:1,
  perfAcc:0,perfN:0,perfT:0,minScale:.18
};

function compile(gl,type,src){
  const s=gl.createShader(type);gl.shaderSource(s,src);gl.compileShader(s);
  if(!gl.getShaderParameter(s,gl.COMPILE_STATUS)){console.warn(gl.getShaderInfoLog(s));return null}
  return s;
}
function init(canvas,opts={}){
  S.canvas=canvas;
  Object.assign(S,opts);
  let gl=null;
  try{gl=canvas.getContext('webgl',{antialias:false,alpha:false,depth:false,stencil:false,preserveDrawingBuffer:false,powerPreference:'high-performance'})}catch(e){}
  if(!gl){canvas.style.display='none';document.documentElement.classList.add('no-ambient');return false}
  const vs=compile(gl,gl.VERTEX_SHADER,VS),fs=compile(gl,gl.FRAGMENT_SHADER,FS);
  if(!vs||!fs){canvas.style.display='none';document.documentElement.classList.add('no-ambient');return false}
  const prog=gl.createProgram();gl.attachShader(prog,vs);gl.attachShader(prog,fs);gl.linkProgram(prog);
  if(!gl.getProgramParameter(prog,gl.LINK_STATUS)){canvas.style.display='none';document.documentElement.classList.add('no-ambient');return false}
  gl.useProgram(prog);
  const buf=gl.createBuffer();gl.bindBuffer(gl.ARRAY_BUFFER,buf);
  gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,3,-1,-1,3]),gl.STATIC_DRAW);
  const loc=gl.getAttribLocation(prog,'aPos');gl.enableVertexAttribArray(loc);gl.vertexAttribPointer(loc,2,gl.FLOAT,false,0,0);
  ['uRes','uTime','uA','uB','uEnergy','uFlash','uDetail','uDrift'].forEach(k=>S.u[k]=gl.getUniformLocation(prog,k));
  S.gl=gl;S.prog=prog;
  canvas.addEventListener('webglcontextlost',e=>{e.preventDefault();S.gl=null});
  resize();addEventListener('resize',resize);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)start()});
  start();
  return true;
}
function resize(){
  const c=S.canvas;if(!c)return;
  const dpr=Math.min(2,window.devicePixelRatio||1);
  let w=Math.max(1,Math.round(innerWidth*dpr*S.scale)),h=Math.max(1,Math.round(innerHeight*dpr*S.scale));
  const cap=1280;if(w>cap){h=Math.round(h*cap/w);w=cap}
  if(c.width!==w||c.height!==h){c.width=w;c.height=h}
}
function start(){if(!S.running&&S.gl&&S.enabled){S.running=true;requestAnimationFrame(frame)}}
function adapt(raw,now){
  if(raw<=0||raw>1000)return;
  S.perfAcc+=raw;S.perfN++;
  if(!S.perfT)S.perfT=now;
  if(now-S.perfT<1500)return;
  const avg=S.perfAcc/S.perfN;S.perfAcc=0;S.perfN=0;S.perfT=now;
  const budget=1000/Math.min(S.fpsCap,60)*1.9;
  if(avg>budget){
    if(S.detail>0){S.detail=0;return}
    if(S.scale>S.minScale){S.scale=Math.max(S.minScale,S.scale*.72);resize();return}
    S.enabled=false;S.canvas.style.display='none';
    document.documentElement.classList.remove('ambient-on');
  }
}
function lerp3(o,t,k){for(let i=0;i<3;i++)o[i]+=(t[i]-o[i])*k}
function frame(now){
  if(!S.gl||!S.enabled||document.hidden){S.running=false;return}
  requestAnimationFrame(frame);
  const minDt=1000/S.fpsCap-1;
  if(now-S.last<minDt)return;
  const raw=now-(S.last||now);const dt=Math.min(100,raw);S.last=now;
  adapt(raw,now);if(!S.gl||!S.enabled)return;
  const k=1-Math.pow(.004,dt/1000);
  lerp3(S.a,S.ta,k);lerp3(S.b,S.tb,k*.8);
  S.energy*=Math.pow(.18,dt/1000);S.flash*=Math.pow(.004,dt/1000);
  const gl=S.gl,u=S.u;
  gl.viewport(0,0,S.canvas.width,S.canvas.height);
  gl.uniform2f(u.uRes,S.canvas.width,S.canvas.height);
  gl.uniform1f(u.uTime,(now-S.t0)/1000);
  gl.uniform3fv(u.uA,S.a);gl.uniform3fv(u.uB,S.b);
  gl.uniform1f(u.uEnergy,Math.min(1.25,S.energy));
  gl.uniform1f(u.uFlash,Math.min(1,S.flash));
  gl.uniform1f(u.uDetail,S.detail);gl.uniform1f(u.uDrift,S.drift);
  gl.drawArrays(gl.TRIANGLES,0,3);
}

window.LFAmbient={
  init,
  palette(a,b){S.ta=grade(hexToRgb(a));S.tb=grade(hexToRgb(b))},
  beat(power=.5,down=false){S.energy=Math.min(1.3,S.energy*.6+power*(down?.9:.5))},
  flash(v=1){S.flash=Math.max(S.flash,v)},
  configure(o={}){Object.assign(S,o);resize();if(S.enabled)start()},
  enable(on){S.enabled=!!on;if(S.canvas)S.canvas.style.display=on?'':'none';if(on)start()},
  get active(){return!!S.gl&&S.enabled}
};
})();
