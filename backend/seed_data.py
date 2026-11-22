"""冷启动种子应用模板库（数据库启动时注入公共广场，user_id=0, is_public=1）。

全部为完整、无外部阻断依赖的单文件 HTML 应用，满足笔试红线：
「评委打开链接不能空白」——未登录游客首屏即可点击预览与真实交互。
模板资产唯一归属本模块与 database.py，agents 层零冗余存储（L4 兜底经 template_lookup 回调回到这里）。
"""

from __future__ import annotations

SEED_MORTGAGE = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>智能房贷计算器</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
body{min-height:100vh;background:linear-gradient(135deg,#0f2027,#203a43,#2c5364);color:#eaf2f8;padding:24px 12px}
.wrap{max-width:760px;margin:0 auto}
h1{text-align:center;font-size:24px;margin-bottom:6px}
.sub{text-align:center;color:#9fb3c8;font-size:13px;margin-bottom:20px}
.card{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:14px;padding:18px;margin-bottom:16px;backdrop-filter:blur(6px)}
.card h2{font-size:16px;margin-bottom:14px;color:#ffd166}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
label{font-size:12px;color:#9fb3c8;display:block;margin-bottom:4px}
input,select{width:100%;padding:9px 10px;border-radius:8px;border:1px solid rgba(255,255,255,.2);background:rgba(0,0,0,.3);color:#fff;font-size:14px;outline:none}
input:focus,select:focus{border-color:#ffd166}
select option{color:#000}
.btn{display:block;width:100%;margin-top:14px;padding:11px;border:none;border-radius:9px;background:linear-gradient(90deg,#ffd166,#f4a261);color:#1d2d44;font-size:15px;font-weight:700;cursor:pointer;transition:.15s}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(255,209,102,.35)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.result{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:14px}
.kpi{background:rgba(0,0,0,.28);border-radius:10px;padding:12px;text-align:center}
.kpi .v{font-size:19px;font-weight:800;color:#06d6a0}
.kpi .k{font-size:12px;color:#9fb3c8;margin-top:3px}
.msg{margin-top:10px;font-size:13px;color:#ef476f;min-height:16px}
.chart{margin-top:14px}
.bar-row{display:flex;align-items:center;gap:8px;margin-bottom:5px;font-size:11px;color:#9fb3c8}
.bar{flex:1;height:14px;background:rgba(255,255,255,.08);border-radius:4px;overflow:hidden;display:flex}
.bar i{display:block;height:100%}
.bp{background:#06d6a0}.bi{background:#ef476f}
.tag{white-space:nowrap}
.hl{color:#06d6a0;font-weight:700}
</style>
</head>
<body>
<div class="wrap">
  <h1>智能房贷计算器</h1>
  <p class="sub">等额本息 / 等额本金 · 支持提前还款对比</p>
  <div class="card">
    <h2>贷款信息</h2>
    <div class="grid">
      <div><label>贷款总额（万元）</label><input id="principal" type="number" value="100" min="1"></div>
      <div><label>年利率（%）</label><input id="rate" type="number" value="3.6" step="0.01" min="0.1"></div>
      <div><label>贷款年限（年）</label><input id="years" type="number" value="30" min="1" max="30"></div>
      <div><label>还款方式</label>
        <select id="method">
          <option value="ep">等额本息（月供固定）</option>
          <option value="ec">等额本金（逐月递减）</option>
        </select>
      </div>
    </div>
    <button class="btn" id="calcBtn">计算还款方案</button>
    <div class="msg" id="msg1"></div>
    <div class="result" id="r1" style="display:none">
      <div class="kpi"><div class="v" id="k1"></div><div class="k" id="k1t">月供</div></div>
      <div class="kpi"><div class="v" id="k2"></div><div class="k">总利息</div></div>
      <div class="kpi"><div class="v" id="k3"></div><div class="k">还款总额</div></div>
    </div>
    <div class="chart" id="chart"></div>
  </div>
  <div class="card">
    <h2>提前还款对比</h2>
    <div class="grid">
      <div><label>已还期数（月）</label><input id="paid" type="number" value="12" min="0"></div>
      <div><label>提前还款金额（万元）</label><input id="lump" type="number" value="20" min="1"></div>
      <div><label>还款后选择</label>
        <select id="opt">
          <option value="cutTerm">缩短期限（月供基本不变）</option>
          <option value="cutPay">减少月供（期限不变）</option>
        </select>
      </div>
    </div>
    <button class="btn" id="prepayBtn">计算提前还款</button>
    <div class="msg" id="msg2"></div>
    <div class="result" id="r2" style="display:none">
      <div class="kpi"><div class="v" id="p1"></div><div class="k" id="p1t"></div></div>
      <div class="kpi"><div class="v" id="p2"></div><div class="k">剩余利息（新方案）</div></div>
      <div class="kpi"><div class="v hl" id="p3"></div><div class="k">节省利息</div></div>
    </div>
    <p id="pnote" style="margin-top:10px;font-size:12px;color:#9fb3c8"></p>
  </div>
</div>
<script>
var fmt=function(n){return n.toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});};
var wan=function(n){return fmt(n/10000);};
function pmt(P,r,n){return r===0?P/n:P*r*Math.pow(1+r,n)/(Math.pow(1+r,n)-1);}
function buildPlan(P,r,n,method){
  var rows=[],balance=P,totalInterest=0;
  var mp=method==='ep'?pmt(P,r,n):null,pp=P/n;
  for(var i=1;i<=n;i++){
    var interest=balance*r,pay;
    if(method==='ep'){pay=mp;}else{pay=pp+interest;}
    var princ=pay-interest;
    if(princ>balance){princ=balance;pay=princ+interest;}
    totalInterest+=interest;
    rows.push({month:i,pay:pay,interest:interest,principal:princ,balance:balance-princ});
    balance-=princ;
    if(balance<=0.005)break;
  }
  return {rows:rows,totalInterest:totalInterest};
}
function $(id){return document.getElementById(id);}
$('calcBtn').onclick=function(){
  var P=parseFloat($('principal').value)*10000,r=parseFloat($('rate').value)/100/12,n=Math.round(parseFloat($('years').value)*12),m=$('method').value;
  $('msg1').textContent='';
  if(!(P>0&&r>0&&n>0)){$('msg1').textContent='请填写有效的贷款金额、利率与年限。';return;}
  var plan=buildPlan(P,r,n,m),rows=plan.rows;
  $('r1').style.display='grid';
  if(m==='ep'){$('k1').textContent=fmt(rows[0].pay)+' 元';$('k1t').textContent='月供（固定）';}
  else{$('k1').textContent=fmt(rows[0].pay)+' 元';$('k1t').textContent='首月月供（逐月递减 '+fmt(rows[0].pay-rows[1].pay)+' 元）';}
  $('k2').textContent=wan(plan.totalInterest)+' 万元';
  $('k3').textContent=wan(plan.totalInterest+P)+' 万元';
  var html='',max=rows[0].pay;
  for(var i=0;i<Math.min(12,rows.length);i++){
    var row=rows[i];
    html+='<div class="bar-row"><span class="tag">'+row.month+'月</span><div class="bar"><i class="bp" style="width:'+(row.principal/max*100)+'%"></i><i class="bi" style="width:'+(row.interest/max*100)+'%"></i></div><span class="tag">'+fmt(row.pay)+'</span></div>';
  }
  html+='<div class="bar-row"><span style="color:#06d6a0">■ 本金</span><span style="color:#ef476f">■ 利息</span><span>（前 '+Math.min(12,rows.length)+' 期）</span></div>';
  $('chart').innerHTML=html;
  window.__plan={P:P,r:r,n:n,m:m,plan:plan};
};
$('prepayBtn').onclick=function(){
  var st=window.__plan;
  $('msg2').textContent='';
  if(!st){$('msg2').textContent='请先计算还款方案。';return;}
  var k=Math.round(parseFloat($('paid').value)),lump=parseFloat($('lump').value)*10000,opt=$('opt').value;
  if(!(k>=0&&k<st.plan.rows.length)){$('msg2').textContent='已还期数需在 0 ~ '+st.plan.rows.length+' 之间。';return;}
  if(!(lump>0)){$('msg2').textContent='请输入有效的提前还款金额。';return;}
  var balance=k===0?st.P:st.plan.rows[k-1].balance;
  var A=Math.min(lump,balance),newP=balance-A,rem=st.n-k;
  var oldRemInt=0,i;
  for(i=k;i<st.plan.rows.length;i++){oldRemInt+=st.plan.rows[i].interest;}
  var newPlan,newDesc;
  if(opt==='cutPay'){
    var nm=pmt(newP,st.r,rem);
    newPlan=buildPlan(newP,st.r,rem,st.m);
    newDesc='新月供 '+fmt(nm)+' 元';
  }else{
    var oldPay=st.plan.rows[Math.min(k,st.plan.rows.length-1)].pay,target=Math.max(oldPay*0.999,1),nn=1;
    while(pmt(newP,st.r,nn)>target&&nn<600){nn++;}
    newPlan=buildPlan(newP,st.r,nn,st.m);
    newDesc='期限缩短至 '+Math.ceil(nn/12)+' 年 '+nn+' 个月';
  }
  var newRemInt=newPlan.totalInterest,saved=oldRemInt-newRemInt;
  $('r2').style.display='grid';
  $('p1').textContent=newDesc.split(' ')[0]==='新月供'?fmt(newPlan.rows[0].pay)+' 元':newDesc;
  $('p1t').textContent=opt==='cutPay'?'新月供':'新期限';
  $('p2').textContent=wan(newRemInt)+' 万元';
  $('p3').textContent=wan(saved)+' 万元';
  $('pnote').textContent='说明：提前还款 '+wan(A)+' 万元后，按「'+($('opt').selectedOptions[0].textContent)+'」重新规划，'
    +(saved>0?'预计节省利息 ':'较原方案多支出 ')+wan(Math.abs(saved))+' 万元。实际金额以银行结清通知为准。';
};
window.onload=function(){$('calcBtn').click();};
</script>
</body>
</html>'''

SEED_TODO = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>项目管理待办看板</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
body{min-height:100vh;background:#f0f2f5;padding:20px 12px}
h1{text-align:center;font-size:22px;color:#1f2933}
.sub{text-align:center;color:#7b8794;font-size:13px;margin:4px 0 16px}
.addbox{max-width:900px;margin:0 auto 14px;display:flex;gap:8px;flex-wrap:wrap}
.addbox input,.addbox select{padding:10px;border:1px solid #d9e2ec;border-radius:8px;font-size:14px;outline:none}
.addbox input[type=text]{flex:2;min-width:180px}
.addbox select{flex:1;min-width:90px}
.addbox button{padding:10px 18px;border:none;border-radius:8px;background:#2680c2;color:#fff;font-size:14px;font-weight:700;cursor:pointer}
.filters{max-width:900px;margin:0 auto 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.chip{padding:5px 14px;border-radius:999px;border:1px solid #cbd2d9;background:#fff;font-size:13px;cursor:pointer}
.chip.on{background:#2680c2;color:#fff;border-color:#2680c2}
.board{max-width:900px;margin:0 auto;display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.col{background:#e4e7eb;border-radius:12px;padding:10px;min-height:300px}
.col h3{font-size:14px;padding:4px 6px 10px;display:flex;justify-content:space-between;color:#323f4b}
.count{background:#cbd2d9;border-radius:999px;padding:0 9px;font-size:12px}
.task{background:#fff;border-radius:9px;padding:10px;margin-bottom:8px;box-shadow:0 1px 3px rgba(0,0,0,.08);cursor:grab}
.task:active{cursor:grabbing}
.task .t{font-size:14px;color:#1f2933;word-break:break-all}
.task .meta{display:flex;justify-content:space-between;align-items:center;margin-top:8px}
.prio{font-size:11px;padding:2px 8px;border-radius:999px;color:#fff}
.prio.high{background:#e12d39}.prio.mid{background:#f0b429}.prio.low{background:#3ebd63}
.ops button{border:none;background:none;cursor:pointer;font-size:13px;margin-left:6px;color:#7b8794}
.ops button:hover{color:#2680c2}
.empty{text-align:center;color:#9aa5b1;font-size:12px;padding:26px 0}
.col.dragover{outline:2px dashed #2680c2;outline-offset:-6px}
.tip{max-width:900px;margin:12px auto 0;font-size:12px;color:#9aa5b1;text-align:center}
</style>
</head>
<body>
<h1>项目管理待办看板</h1>
<p class="sub">拖拽卡片或使用 → 按钮移动状态，数据自动保存在本地</p>
<div class="addbox">
  <input type="text" id="title" placeholder="输入任务内容，回车快速添加" maxlength="60">
  <select id="prio">
    <option value="high">高优先级</option>
    <option value="mid" selected>中优先级</option>
    <option value="low">低优先级</option>
  </select>
  <button id="addBtn">＋ 添加任务</button>
</div>
<div class="filters">
  <span style="font-size:13px;color:#7b8794">筛选：</span>
  <button class="chip on" data-f="all">全部</button>
  <button class="chip" data-f="high">高</button>
  <button class="chip" data-f="mid">中</button>
  <button class="chip" data-f="low">低</button>
  <button class="chip" id="clearDone">清除已完成</button>
</div>
<div class="board" id="board"></div>
<p class="tip">提示：任务保存在浏览器 localStorage，刷新不丢失。</p>
<script>
var KEY='p2a_todo_board_v1',STATES=['todo','doing','done'],SN={todo:'待办',doing:'进行中',done:'已完成'};
var tasks=JSON.parse(localStorage.getItem(KEY)||'null')||[
  {id:1,title:'演示任务：拖拽我到「进行中」',prio:'high',status:'todo'},
  {id:2,title:'点击 → 推进状态',prio:'mid',status:'doing'}
];
var filter='all',dragId=null;
function save(){localStorage.setItem(KEY,JSON.stringify(tasks));}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function render(){
  var board=document.getElementById('board');board.innerHTML='';
  STATES.forEach(function(st){
    var col=document.createElement('div');col.className='col';col.dataset.state=st;
    var list=tasks.filter(function(t){return t.status===st&&(filter==='all'||t.prio===filter);});
    col.innerHTML='<h3>'+SN[st]+'<span class="count">'+list.length+'</span></h3>';
    if(!list.length){col.innerHTML+='<div class="empty">暂无任务</div>';}
    list.forEach(function(t){
      var el=document.createElement('div');el.className='task';el.draggable=true;el.dataset.id=t.id;
      el.innerHTML='<div class="t">'+esc(t.title)+'</div><div class="meta"><span class="prio '+t.prio+'">'+
        ({high:'高',mid:'中',low:'低'}[t.prio])+'</span><span class="ops">'+
        (st!=='done'?'<button data-act="next" title="推进">→</button>':'')+
        '<button data-act="del" title="删除">✕</button></span></div>';
      el.addEventListener('dragstart',function(e){dragId=t.id;e.dataTransfer.effectAllowed='move';});
      col.appendChild(el);
    });
    col.addEventListener('dragover',function(e){e.preventDefault();col.classList.add('dragover');});
    col.addEventListener('dragleave',function(){col.classList.remove('dragover');});
    col.addEventListener('drop',function(e){
      e.preventDefault();col.classList.remove('dragover');
      var task=tasks.find(function(x){return x.id===dragId;});
      if(task&&task.status!==st){task.status=st;save();render();}
    });
    board.appendChild(col);
  });
}
document.getElementById('addBtn').onclick=function(){
  var input=document.getElementById('title'),v=input.value.trim();
  if(!v){input.focus();return;}
  tasks.push({id:Date.now(),title:v,prio:document.getElementById('prio').value,status:'todo'});
  input.value='';save();render();
};
document.getElementById('title').addEventListener('keydown',function(e){
  if(e.key==='Enter'){document.getElementById('addBtn').click();}
});
document.getElementById('board').addEventListener('click',function(e){
  var btn=e.target.closest('button');if(!btn)return;
  var id=+btn.closest('.task').dataset.id,task=tasks.find(function(x){return x.id===id;});
  if(!task)return;
  if(btn.dataset.act==='next'){task.status=STATES[Math.min(STATES.indexOf(task.status)+1,2)];}
  if(btn.dataset.act==='del'){tasks=tasks.filter(function(x){return x.id!==id;});}
  save();render();
});
document.querySelectorAll('.chip[data-f]').forEach(function(c){
  c.onclick=function(){
    filter=c.dataset.f;
    document.querySelectorAll('.chip[data-f]').forEach(function(x){x.classList.toggle('on',x===c);});
    render();
  };
});
document.getElementById('clearDone').onclick=function(){
  tasks=tasks.filter(function(t){return t.status!=='done';});save();render();
};
render();
</script>
</body>
</html>'''

SEED_HOME = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>个人主页生成器</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
body{min-height:100vh;background:#f6f7fb;color:#1e293b;padding:24px 14px}
.wrap{max-width:520px;margin:0 auto}
h1{text-align:center;font-size:24px}
.sub{text-align:center;color:#64748b;font-size:13px;margin:4px 0 18px}
.card{background:#fff;border-radius:16px;padding:18px;box-shadow:0 6px 24px rgba(30,41,59,.07);margin-bottom:16px}
.field{margin-bottom:12px}
label{font-size:12px;color:#64748b;display:block;margin-bottom:4px}
input,textarea{width:100%;padding:10px 12px;border:1px solid #e2e8f0;border-radius:10px;font-size:14px;outline:none;font-family:inherit;resize:vertical;background:#f8fafc}
input:focus,textarea:focus{border-color:#6366f1;background:#fff}
.btns{display:flex;gap:10px;margin-top:4px}
button{flex:1;padding:11px;border:none;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;transition:.15s}
#genBtn{background:linear-gradient(90deg,#6366f1,#8b5cf6);color:#fff}
#genBtn:hover{opacity:.9;transform:translateY(-1px)}
#dlBtn{background:#e2e8f0;color:#334155}
#dlBtn:hover{background:#cbd5e1}
.msg{margin-top:8px;font-size:12px;color:#ef4444;min-height:14px}
.tip{font-size:11px;color:#94a3b8;text-align:center;margin-top:10px}
</style>
</head>
<body>
<div class="wrap">
  <h1>个人主页生成器</h1>
  <p class="sub">输入你的信息，一键生成排版精良的个人主页</p>
  <div class="card">
    <div class="field"><label>姓名 *</label><input id="name" placeholder="你的姓名" maxlength="12"></div>
    <div class="field"><label>职业 / 头衔 *</label><input id="role" placeholder="例如：全栈开发工程师" maxlength="20"></div>
    <div class="field"><label>技能（用逗号分隔）</label><input id="skills" placeholder="例如：Python, TypeScript, Docker" maxlength="80"></div>
    <div class="field"><label>联系方式（邮箱 / 电话）</label><input id="contact" placeholder="例如：hi@example.com" maxlength="40"></div>
    <div class="field"><label>个人简介（可选）</label><textarea id="bio" rows="3" maxlength="200" placeholder="一两句话介绍你自己"></textarea></div>
    <div class="msg" id="msg"></div>
    <div class="btns">
      <button id="genBtn">✨ 生成主页</button>
      <button id="dlBtn">📥 下载 HTML</button>
    </div>
    <p class="tip">填写信息 → 点击生成，下方实时预览，可下载为单文件 HTML</p>
  </div>
  <div class="card" style="padding:8px">
    <iframe id="preview" style="width:100%;height:520px;border:0;border-radius:10px;background:#fff"></iframe>
  </div>
</div>
<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
var PALETTES=[['#6366f1','#8b5cf6'],['#0ea5e9','#6366f1'],['#10b981','#0ea5e9'],['#f59e0b','#ef4444'],['#ec4899','#8b5cf6']];
var pi=0;
function buildHtml(d){
  var pal=PALETTES[pi%PALETTES.length];pi++;
  var skills=d.skills.split(/[,，]/).map(function(s){return s.trim();}).filter(Boolean);
  var skillBars=skills.map(function(s,i){
    var w=95-i*(60/Math.max(skills.length,1));
    return '<div style="margin-bottom:16px"><div style="display:flex;justify-content:space-between;font-size:14px;margin-bottom:6px"><span>'+esc(s)+'</span><span style="color:#94a3b8">'+Math.round(w)+'%</span></div>'+
      '<div style="height:8px;background:rgba(148,163,184,.15);border-radius:99px;overflow:hidden"><i style="display:block;height:100%;width:0;border-radius:99px;background:linear-gradient(90deg,'+pal[0]+','+pal[1]+');transition:width 1s cubic-bezier(.2,.8,.2,1)" data-w="'+w+'"></i></div></div>';
  }).join('');
  return '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"><title>'+esc(d.name)+' 的主页</title>'+
  '<style>*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}body{background:#0b1020;color:#e2e8f0}'+
  '.hero{min-height:88vh;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;padding:0 6vw;background:radial-gradient(ellipse at 30% 20%,'+pal[0]+'33,transparent 55%),radial-gradient(ellipse at 70% 80%,'+pal[1]+'2e,transparent 55%)}'+
  '.hi{font-size:14px;letter-spacing:5px;color:'+pal[0]+';margin-bottom:14px}.hero h1{font-size:clamp(34px,7vw,60px)}.hero h1 em{font-style:normal;background:linear-gradient(90deg,'+pal[0]+','+pal[1]+');-webkit-background-clip:text;background-clip:text;color:transparent}'+
  '.role{font-size:clamp(16px,3vw,22px);color:#94a3b8;margin-top:14px}.bio{max-width:560px;margin-top:22px;color:#cbd5e1;line-height:1.9;font-size:15px}'+
  '.chip{display:inline-block;margin-top:26px;padding:11px 26px;border-radius:999px;border:1px solid #334155;color:#cbd5e1;font-size:14px;text-decoration:none}'+
  'section{max-width:820px;margin:0 auto;padding:80px 6vw 40px}h2{font-size:24px;margin-bottom:22px}h2::before{content:"#";color:'+pal[0]+';margin-right:6px}'+
  'footer{text-align:center;color:#475569;font-size:12px;padding:26px;border-top:1px solid rgba(255,255,255,.06)}</style></head><body>'+
  '<header class="hero"><div class="hi">HELLO WORLD · 你好</div><h1>我是 <em>'+esc(d.name)+'</em></h1><div class="role">'+esc(d.role)+'</div>'+
  '<p class="bio">'+esc(d.bio)+'</p><a class="chip" href="mailto:'+esc(d.contact)+'">联系我 · '+esc(d.contact)+'</a></header>'+
  (skills.length?'<section><h2>技能</h2>'+skillBars+'</section>':'')+
  '<footer>© 2025 '+esc(d.name)+' · 由 Prompt2App 生成</footer>'+
  '<script>window.addEventListener("load",function(){document.querySelectorAll("[data-w]").forEach(function(el){setTimeout(function(){el.style.width=el.dataset.w+"%";},150);});});<\/script></body></html>';
}
function collect(){
  return {name:document.getElementById('name').value.trim()||'无名氏',
    role:document.getElementById('role').value.trim()||'开发者',
    skills:document.getElementById('skills').value,
    contact:document.getElementById('contact').value.trim(),
    bio:document.getElementById('bio').value.trim()};
}
var lastHtml='';
function generate(){
  lastHtml=buildHtml(collect());
  document.getElementById('preview').srcdoc=lastHtml;
}
document.getElementById('genBtn').onclick=function(){
  var name=document.getElementById('name').value.trim(),
      role=document.getElementById('role').value.trim(),
      msg=document.getElementById('msg');
  if(!name||!role){msg.textContent='请先填写姓名和职业 / 头衔';return;}
  msg.textContent='';
  generate();
};
document.getElementById('dlBtn').onclick=function(){
  if(!lastHtml){document.getElementById('msg').textContent='请先生成主页';return;}
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([lastHtml],{type:'text/html'}));
  a.download='homepage.html';a.click();
};
document.getElementById('preview').srcdoc='<div style="font-family:-apple-system,sans-serif;color:#94a3b8;display:flex;height:100vh;align-items:center;justify-content:center;font-size:14px">← 填写上方信息，点击「生成主页」</div>';
</script>
</body>
</html>'''

SEED_POMODORO = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>专注番茄钟</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
body{min-height:100vh;display:flex;align-items:center;justify-content:center;background:linear-gradient(160deg,#1a1a2e,#16213e);color:#eee;padding:20px}
.app{width:min(430px,100%);background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:20px;padding:26px;text-align:center;backdrop-filter:blur(8px)}
.tabs{display:flex;gap:8px;margin-bottom:20px}
.tab{flex:1;padding:9px;border-radius:10px;border:1px solid rgba(255,255,255,.15);background:transparent;color:#aab;font-size:13px;cursor:pointer;transition:.2s}
.tab.on{background:#e94560;border-color:#e94560;color:#fff;font-weight:700}
.ringwrap{position:relative;width:250px;height:250px;margin:0 auto}
svg{transform:rotate(-90deg)}
.bg{fill:none;stroke:rgba(255,255,255,.08);stroke-width:11}
.fg{fill:none;stroke:#e94560;stroke-width:11;stroke-linecap:round;transition:stroke-dashoffset .35s linear,stroke .3s}
.time{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.time .t{font-size:52px;font-weight:800;letter-spacing:2px}
.time .m{font-size:13px;color:#8892b0;margin-top:4px}
.task{margin:18px 0 4px}
.task input{width:100%;padding:11px;border-radius:10px;border:1px solid rgba(255,255,255,.15);background:rgba(0,0,0,.25);color:#fff;text-align:center;font-size:14px;outline:none}
.task input:focus{border-color:#e94560}
.ctrl{display:flex;gap:10px;margin-top:16px}
.ctrl button{flex:1;padding:12px;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;transition:.15s}
#startBtn{background:#e94560;color:#fff}
#resetBtn{background:rgba(255,255,255,.12);color:#ddd}
.ctrl button:active{transform:scale(.97)}
.stats{display:flex;justify-content:space-around;margin-top:18px;padding-top:16px;border-top:1px solid rgba(255,255,255,.08);font-size:13px;color:#8892b0}
.stats b{display:block;font-size:20px;color:#fff;margin-bottom:2px}
.log{margin-top:14px;text-align:left;max-height:120px;overflow:auto;font-size:12px;color:#8892b0}
.log div{padding:5px 2px;border-bottom:1px dashed rgba(255,255,255,.06)}
.log span{color:#e94560}
</style>
</head>
<body>
<div class="app">
  <div class="tabs">
    <button class="tab on" data-mode="work">专注 25</button>
    <button class="tab" data-mode="short">短休 5</button>
    <button class="tab" data-mode="long">长休 15</button>
  </div>
  <div class="ringwrap">
    <svg width="250" height="250">
      <circle class="bg" cx="125" cy="125" r="112"></circle>
      <circle class="fg" id="ring" cx="125" cy="125" r="112"></circle>
    </svg>
    <div class="time"><div class="t" id="clock">25:00</div><div class="m" id="label">准备开始专注</div></div>
  </div>
  <div class="task"><input id="taskName" placeholder="本次要专注的任务（可选）" maxlength="40"></div>
  <div class="ctrl">
    <button id="startBtn">开始专注</button>
    <button id="resetBtn">重置</button>
  </div>
  <div class="stats">
    <div><b id="todayCount">0</b>今日番茄</div>
    <div><b id="totalMin">0</b>专注分钟</div>
    <div><b id="cycleCount">0</b>完成轮次</div>
  </div>
  <div class="log" id="log"></div>
</div>
<script>
var DUR={work:25*60,short:5*60,long:15*60},MODE_TXT={work:'专注中',short:'休息一下',long:'长休息'},
    COLORS={work:'#e94560',short:'#0fbf8f',long:'#4d80e4'};
var R=112,C=2*Math.PI*R;
var mode='work',remain=DUR.work,timer=null,workCycles=0,today=0,minutes=0;
var ring=document.getElementById('ring');
ring.style.strokeDasharray=C;ring.style.strokeDashoffset=0;ring.style.stroke=COLORS.work;
function load(){var d=JSON.parse(localStorage.getItem('p2a_pomo_v1')||'{}');today=d.today||0;minutes=d.minutes||0;
  if(d.date!==new Date().toDateString()){today=0;}updStats();}
function save(){localStorage.setItem('p2a_pomo_v1',JSON.stringify({today:today,minutes:minutes,date:new Date().toDateString()}));}
function fmt(s){var m=Math.floor(s/60),ss=s%60;return (m<10?'0':'')+m+':'+(ss<10?'0':'')+ss;}
function draw(){
  document.getElementById('clock').textContent=fmt(remain);
  ring.style.strokeDashoffset=C*(1-remain/DUR[mode]);
  ring.style.stroke=COLORS[mode];
  document.title=(timer?fmt(remain)+' ':'')+'番茄钟';
}
function updStats(){
  document.getElementById('todayCount').textContent=today;
  document.getElementById('totalMin').textContent=minutes;
  document.getElementById('cycleCount').textContent=Math.floor(workCycles);
}
function setMode(m,auto){
  mode=m;remain=DUR[m];clearInterval(timer);timer=null;
  document.querySelectorAll('.tab').forEach(function(t){t.classList.toggle('on',t.dataset.mode===m);});
  document.getElementById('startBtn').textContent='开始专注';
  document.getElementById('label').textContent=auto?MODE_TXT[m]:'准备开始'+(m==='work'?'专注':'休息');
  draw();
}
function beep(){
  try{
    var ctx=new (window.AudioContext||window.webkitAudioContext)();
    [0,.25].forEach(function(dt){
      var o=ctx.createOscillator(),g=ctx.createGain();
      o.connect(g);g.connect(ctx.destination);o.frequency.value=880;
      g.gain.setValueAtTime(0.0001,ctx.currentTime+dt);
      g.gain.exponentialRampToValueAtTime(0.25,ctx.currentTime+dt+0.03);
      g.gain.exponentialRampToValueAtTime(0.0001,ctx.currentTime+dt+0.35);
      o.start(ctx.currentTime+dt);o.stop(ctx.currentTime+dt+0.4);
    });
  }catch(e){}
}
function log(txt){
  var el=document.getElementById('log'),d=document.createElement('div');
  d.innerHTML='<span>'+new Date().toTimeString().slice(0,8)+'</span> '+txt;
  el.insertBefore(d,el.firstChild);
}
function finish(){
  clearInterval(timer);timer=null;beep();
  var task=document.getElementById('taskName').value.trim();
  if(mode==='work'){
    today++;workCycles++;minutes+=Math.round(DUR.work/60);save();updStats();
    log('完成一个番茄'+(task?'（'+task+'）':''));
    setMode(workCycles%4===0?'long':'short',true);
    document.getElementById('label').textContent='专注完成，休息一下吧';
  }else{
    log('休息结束');
    setMode('work',true);
    document.getElementById('label').textContent='休息结束，开始下一轮';
  }
}
document.getElementById('startBtn').onclick=function(){
  if(timer){clearInterval(timer);timer=null;this.textContent=mode==='work'?'继续专注':'继续休息';
    document.getElementById('label').textContent='已暂停';}
  else{
    var endAt=Date.now()+remain*1000;
    timer=setInterval(function(){
      remain=Math.max(0,Math.round((endAt-Date.now())/1000));draw();
      if(remain<=0)finish();
    },250);
    this.textContent='暂停';
    document.getElementById('label').textContent=MODE_TXT[mode];
  }
};
document.getElementById('resetBtn').onclick=function(){setMode(mode,false);};
document.querySelectorAll('.tab').forEach(function(t){t.onclick=function(){setMode(this.dataset.mode,false);};});
load();draw();updStats();
</script>
</body>
</html>'''

SEED_NOTES = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>灵感笔记本</title>
<style>
*{margin:0;padding:0;box-sizing:border-box;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
body{min-height:100vh;background:linear-gradient(135deg,#fdf6ec,#ffe8d6);padding:24px 12px}
.wrap{max-width:860px;margin:0 auto}
h1{text-align:center;color:#5f3dc4;font-size:24px}
.sub{text-align:center;color:#9775fa;font-size:13px;margin:4px 0 18px}
.editor{background:#fff;border-radius:14px;padding:16px;box-shadow:0 4px 18px rgba(95,61,196,.08);margin-bottom:16px}
.editor input,.editor textarea{width:100%;border:1px solid #e5dbff;border-radius:9px;padding:10px;font-size:14px;outline:none;font-family:inherit;resize:vertical}
.editor input:focus,.editor textarea:focus{border-color:#845ef7}
.editor textarea{margin-top:10px;min-height:76px}
.row{display:flex;gap:10px;margin-top:10px;align-items:center;flex-wrap:wrap}
select{padding:9px;border-radius:9px;border:1px solid #e5dbff;font-size:13px;outline:none;background:#fff}
button.add{padding:10px 22px;border:none;border-radius:9px;background:#845ef7;color:#fff;font-size:14px;font-weight:700;cursor:pointer;margin-left:auto}
button.add:hover{background:#7048e8}
.search{margin:0 0 14px}
.search input{width:100%;padding:11px 14px;border-radius:999px;border:1px solid #e5dbff;font-size:14px;outline:none;background:#fff}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.note{border-radius:12px;padding:14px;box-shadow:0 3px 12px rgba(0,0,0,.06);position:relative;animation:pop .18s ease}
@keyframes pop{from{transform:scale(.96);opacity:0}}
.note h3{font-size:15px;margin-bottom:6px;color:#343a40;word-break:break-all}
.note p{font-size:13px;color:#868e96;line-height:1.6;white-space:pre-wrap;word-break:break-all}
.note .meta{display:flex;justify-content:space-between;align-items:center;margin-top:10px;font-size:11px;color:#adb5bd}
.note .del{border:none;background:none;cursor:pointer;font-size:14px;color:#adb5bd}
.note .del:hover{color:#fa5252}
.empty{text-align:center;color:#b197fc;padding:40px 0;grid-column:1/-1}
.count{font-size:12px;color:#9775fa;margin-bottom:10px;text-align:right}
</style>
</head>
<body>
<div class="wrap">
  <h1>灵感笔记本</h1>
  <p class="sub">随手记录，自动保存在浏览器本地</p>
  <div class="editor">
    <input id="t" placeholder="标题" maxlength="30">
    <textarea id="c" placeholder="写点什么…（支持多行）" maxlength="500"></textarea>
    <div class="row">
      <select id="color">
        <option value="#fff9db">暖黄</option>
        <option value="#e7f5ff">天蓝</option>
        <option value="#ebfbee">薄荷</option>
        <option value="#fff0f6">樱花</option>
      </select>
      <button class="add" id="addBtn">保存笔记</button>
    </div>
  </div>
  <div class="search"><input id="q" placeholder="搜索笔记标题或内容…"></div>
  <div class="count" id="cnt"></div>
  <div class="grid" id="grid"></div>
</div>
<script>
var KEY='p2a_notes_v1';
var notes=JSON.parse(localStorage.getItem(KEY)||'null')||[
  {id:1,title:'欢迎使用',body:'点击右上角 ✕ 删除我。\n用顶部搜索框可以快速筛选。',color:'#fff9db',time:Date.now()}
];
function save(){localStorage.setItem(KEY,JSON.stringify(notes));}
function esc(s){var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function ago(t){
  var s=Math.floor((Date.now()-t)/1000);
  if(s<60)return '刚刚';if(s<3600)return Math.floor(s/60)+' 分钟前';
  if(s<86400)return Math.floor(s/3600)+' 小时前';
  return new Date(t).toLocaleDateString('zh-CN');
}
function render(){
  var q=document.getElementById('q').value.trim().toLowerCase();
  var list=notes.filter(function(n){return !q||n.title.toLowerCase().indexOf(q)>=0||n.body.toLowerCase().indexOf(q)>=0;})
    .sort(function(a,b){return b.time-a.time;});
  document.getElementById('cnt').textContent='共 '+notes.length+' 条'+(q?'，筛选出 '+list.length+' 条':'');
  var g=document.getElementById('grid');
  g.innerHTML=list.length?'':'<div class="empty">暂无笔记，写一条吧 ✍️</div>';
  list.forEach(function(n){
    var el=document.createElement('div');el.className='note';el.style.background=n.color;
    el.innerHTML='<h3>'+esc(n.title||'无标题')+'</h3><p>'+esc(n.body)+'</p><div class="meta"><span>'+ago(n.time)+
      ' · '+n.body.length+' 字</span><button class="del" title="删除">✕</button></div>';
    el.querySelector('.del').onclick=function(){
      notes=notes.filter(function(x){return x.id!==n.id;});save();render();
    };
    g.appendChild(el);
  });
}
document.getElementById('addBtn').onclick=function(){
  var t=document.getElementById('t'),c=document.getElementById('c');
  if(!t.value.trim()&&!c.value.trim()){t.focus();return;}
  notes.push({id:Date.now(),title:t.value.trim(),body:c.value.trim(),color:document.getElementById('color').value,time:Date.now()});
  t.value='';c.value='';t.focus();save();render();
};
['t','c'].forEach(function(id){
  document.getElementById(id).addEventListener('keydown',function(e){
    if(e.key==='Enter'&&(id==='t'||(e.ctrlKey||e.metaKey))){document.getElementById('addBtn').click();}
  });
});
document.getElementById('q').addEventListener('input',render);
render();
</script>
</body>
</html>'''

# 关键词 -> (模板名, 模板 HTML)，顺序即优先级；L4 兜底模板查询共用同一份映射
SEED_APPS: list[tuple[str, str]] = [
    ("智能房贷计算器", SEED_MORTGAGE),
    ("项目管理待办看板", SEED_TODO),
    ("个人主页生成器", SEED_HOME),
    ("专注番茄钟", SEED_POMODORO),
    ("灵感笔记本", SEED_NOTES),
]

# L4 离线兜底关键词匹配（agents 层经 template_lookup 回调使用）
SEED_RULES = [
    (("房贷", "贷款", "还款", "按揭", "月供", "利率", "买房", "mortgage"), SEED_MORTGAGE),
    (("待办", "todo", "看板", "任务", "日程", "清单", "项目管理"), SEED_TODO),
    (("主页", "首页", "个人网站", "简历", "portfolio", "个人介绍", "关于我"), SEED_HOME),
    (("番茄", "pomodoro", "专注", "计时", "倒计时", "闹钟", "自习"), SEED_POMODORO),
    (("笔记", "记事", "便签", "备忘录", "记录", "note", "memo", "日记"), SEED_NOTES),
]
SEED_GENERIC = SEED_NOTES


def match_seed_template(user_msg: str | None) -> str:
    """按用户消息关键词匹配种子模板，未命中返回通用模板（L4 离线兜底用）。"""
    text = (user_msg or "").lower()
    for keywords, template in SEED_RULES:
        if any(k in text for k in keywords):
            return template
    return SEED_GENERIC


def match_seed_name(user_msg: str | None) -> str:
    """与 match_seed_template 配套，返回匹配到的模板名（供状态展示）。"""
    text = (user_msg or "").lower()
    for keywords, template in SEED_RULES:
        if any(k in text for k in keywords):
            for name, tpl in SEED_APPS:
                if tpl is template:
                    return name
    return "灵感笔记本"
