
// ---- 训练包（charpack-v1）上传 → 解包校验 → 开训 ----
function fmtS(s){if(s==null)return '—';s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?h+'小时'+m+'分':m?m+'分'+(s%60)+'秒':s+'秒'}
let TRB=false;const TRB_IDS=['generate','pkStd','pkAdv','pkResume'];
function trBusy(b){TRB=b;TRB_IDS.forEach(i=>{const e=$(i);if(e){if(b)e.disabled=true;e.title=b?'训练中，先别出图':''}})}
document.addEventListener('click',e=>{const b=e.target.closest&&e.target.closest('button');if(TRB&&b&&TRB_IDS.includes(b.id)){e.preventDefault();e.stopImmediatePropagation();alert('正在训练人物小模型，训练期间不能出图。等训练完成或先点「停止训练」。')}},true);
const CPN={angle:'角度',framing:'景别',pitch:'俯仰',expression:'表情',front:'正面',three_quarter:'侧前',side:'侧面',back:'背面',head:'头肩',half:'半身',full:'全身',level:'平视',up:'抬头',down:'低头',neutral:'平静',smile:'微笑',laugh:'大笑',angry:'生气',sad:'难过',surprised:'惊讶',shout:'喊叫',unknown:'未知'};
const cpN=x=>String(x).split(':').map(y=>CPN[y]||y).join('·');
let cpT=null,cpAsked=false;
function cpShow(d){const m=$('cpMsg');$('cpTrain').hidden=d.state!=='ready';if(d.state==='idle'){m.textContent='';return}
let t='训练包 '+(d.zip||'')+'：'+(d.phase||d.state);
if(d.images)t+='\n有效图片 '+d.images+' 张，触发词 '+d.trigger+'，计划 '+d.steps_total+' 步'+(d.captions_made?'；自动补标注 '+d.captions_made+' 张':'')+(d.bad?'；损坏 '+d.bad+' 张已跳过':'');
if(d.coverage)t+='\n'+Object.entries(d.coverage).map(([k,v])=>CPN[k]+'：'+Object.entries(v).filter(([a,n])=>n).map(([a,n])=>CPN[a]+n).join(' ')).join('\n');
if(d.missing&&d.missing.length)t+='\n⚠ 缺：'+d.missing.map(cpN).join('、');
if(d.warnings&&d.warnings.length)t+='\n提示：'+d.warnings.join('；');
if(d.state==='error')t+='\n出错：'+d.error;m.textContent=t;
if(d.state==='running'){clearTimeout(cpT);cpT=setTimeout(cpPoll,1500)}
if(d.state==='ready'&&$('cpAuto').checked&&!cpAsked){cpAsked=true;setTimeout(()=>{if(confirm(t+'\n\n确定开始训练？（点「确定」开始；训练约 1 小时以上，期间不能出图）'))cpGo()},50)}}
async function cpPoll(){try{cpShow(await api('/api/charpack'))}catch(e){$('cpMsg').textContent=e.message}}
async function cpGo(){try{const r=await api('/api/charpack/train',json({}));$('cpMsg').textContent+='\n已开训。'+(r.freed||'');trPoll()}catch(e){$('cpMsg').textContent+='\n'+e.message}}
$('cpTrain').onclick=()=>{if(confirm('用这个训练包开始训练？训练期间不能出图。'))cpGo()};
$('cpUpBtn').onclick=()=>$('cpFile').click();
$('cpFile').onchange=async()=>{const f=$('cpFile').files[0];$('cpFile').value='';if(!f)return;const M=$('cpMsg'),P=$('cpProg');cpAsked=false;
try{const init=await api('/api/charpack/chunk',json({name:f.name,size:f.size,lastModified:f.lastModified}));let off=init.received||0;const CH=8*1048576;P.style.display='block';let fails=0;
while(off<f.size){const end=Math.min(f.size,off+CH);try{const r=await fetch('/api/charpack/chunk/'+init.id+'?offset='+off,{method:'PUT',body:f.slice(off,end),headers:{'X-Studio-Token':token,'Content-Type':'application/octet-stream'}});let d={};try{d=await r.json()}catch{}
if(r.ok){off=d.received;fails=0}else if(r.status===409&&d.received!=null){off=d.received}else throw Error(d.error||('HTTP '+r.status))}catch(e){if(++fails>8)throw e;M.textContent='网络断了，'+fails+' 次重试中…（断点续传）';await new Promise(s=>setTimeout(s,3000));continue}
P.value=Math.floor(off*100/f.size);M.textContent='上传训练包 '+(off/1048576).toFixed(1)+' / '+(f.size/1048576).toFixed(1)+' MB（'+P.value+'%）'}
await api('/api/charpack/chunk/'+init.id+'/finish',json({trigger:$('trTrig').value}));P.style.display='none';M.textContent='上传完成，正在解包校验…';cpPoll()}catch(e){M.textContent='训练包上传失败：'+e.message+'（重新选同一个文件会从断点接着传）'}};
cpPoll();
