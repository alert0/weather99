(() => {
'use strict';
const $ = id => document.getElementById(id);
const number = (v,d=1) => v == null ? '—' : Number(v).toFixed(d);
const esc = v => String(v??'—').replace(/[&<>"']/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const time = v => v ? new Date(v).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false}) : '暂无';
function renderSettlement(scans){
  const statuses={not_collected:'尚未采集（需要新一轮扫描）',unsupported_rules:'结算规则尚未支持',awaiting_target_day:'目标日尚未开始',source_error:'结算源请求失败',no_observations:'结算源尚无有效常规报文',provisional:'日内暂定',day_closed_evidence:'已见次日首份报文，等待市场最终裁定'};
  $('settlement-summary').replaceChildren();
  scans.forEach(s=>{
    const e=s.settlement||{status:'not_collected'}, p=document.createElement('div');
    p.textContent=`${s.date} · ${statuses[e.status]||'状态未知'} · 已观测最高 ${number(e.observed_max_c,1)} ℃ · 取整 ${number(e.rounded_max_c,0)} ℃ · ${e.observation_count??0} 份有效报文 · 采集 ${time(e.collected_at)}`;
    if(e.source_url){
      try {const url=new URL(e.source_url);if(url.protocol==='https:'&&['aviationweather.gov','www.wunderground.com'].includes(url.hostname)){
        const link=document.createElement('a');link.href=url.href;link.target='_blank';link.rel='noreferrer';link.textContent=' 查看市场指定结算源';p.append(link);
      }} catch {}
    }
    if(e.error)p.append(document.createTextNode(` · ${e.error}`));
    const details=document.createElement('details'), summary=document.createElement('summary'), rules=document.createElement('p');
    summary.textContent='市场原始结算规则';rules.textContent=e.rules||'尚无规则记录';details.append(summary,rules);p.append(details);
    $('settlement-summary').append(p);
  });
  const rows=scans.flatMap(s=>(s.settlement?.observations||[]).slice().reverse().map(p=>`<tr><th scope="row">${esc(s.date)}</th><td>${esc(time(p.observed_at))}</td><td>${number(p.temperature_c,1)}</td><td>${esc(p.raw_metar)}</td></tr>`));
  $('settlement-rows').innerHTML=rows.join('')||'<tr><td colspan="4" class="empty">尚无结算源有效报文，请查看上方采集状态。</td></tr>';
}

let busy=false;
async function refreshEvidence(){
 if(busy||!$('settlement-summary'))return;busy=true;
 try{const r=await fetch('/api/dashboard',{cache:'no-store',signal:AbortSignal.timeout(12000)});if(!r.ok)throw new Error(`HTTP ${r.status}`);const d=await r.json();renderSettlement(d.scans||[]);}
 catch(e){$('settlement-summary').textContent=`结算依据暂时无法更新（${e.message}），下方如有报文属于上次读取记录。`;}
 finally{busy=false;}
}
refreshEvidence();setInterval(refreshEvidence,10000);$('refresh')?.addEventListener('click',refreshEvidence);
})();
