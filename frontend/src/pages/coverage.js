import {domain} from '../constants/domain.js';import {research} from '../stores/research.js';import {session} from '../stores/session.js';import {coverageApi} from '../api/research.js';import {table} from '../components/common/table.js';import {esc,localTime} from '../utils/format.js';import {action,toast} from '../hooks/actions.js';

const batchStates={queued:'排队中',running:'核对中',verified:'已核对',failed:'未通过'};
const itemStates={pending:'待判',running:'判定中',done:'已出证明',failed:'判定失败'};
const sampleSheet='HX-CHK-001, 35.10, 4.20, 75.00, 3.10\nHX-CHK-002, 72.40, 9.50, 96.00, 4.90';
const batchStatus=s=>'<span class="status '+esc(s)+'">'+esc(batchStates[s]||s)+'</span>';
const itemStatus=s=>'<span class="status '+esc(s)+'">'+esc(itemStates[s]||s)+'</span>';

function requestKey(){const bytes=new Uint8Array(16);window.crypto.getRandomValues(bytes);return Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('')}

function proofRow(item){
  if(!item.proof)return '<p class="fine">'+esc(item.error||'整批尚未核对齐，证明暂不可查看。')+'</p>';
  const p=item.proof;
  if(p.kind==='covered'){
    return '<div class="proof"><b>可加权覆盖</b> · 至多五件，权重和为 1，边界算覆盖'
      +table(p.weights.map(w=>({reference:w.reference,weight:w.fraction,decimal:w.decimal,index:w.index})),[{key:'reference',label:'训练试件'},{key:'index',label:'训练内序号'},{key:'weight',label:'非负权重（分数）'},{key:'decimal',label:'权重（小数近似）'}])
      +'<p class="fine">权重精确和：'+esc(p.weight_sum)+' · 参与 '+p.reference_count+' 件 · 按 '+p.scale_digits+' 位小数十进制整数化</p></div>';
  }
  return '<div class="proof"><b>线性分离</b><p><code>'+esc(p.bound)+'</code></p>'
    +'<p class="fine">系数：'+esc(p.coefficients.join(' / '))+' · 常数 '+esc(String(p.constant))+' · 待判点超出 '+esc(p.excess)+' · '+esc(p.note)+'</p></div>';
}

function batchDetail(batch){
  if(!batch)return '';
  return '<section class="coverage-detail"><h2>批次 #'+batch.id+' · '+batchStatus(batch.state)+'</h2>'
    +'<p class="fine">请求编号 <code>'+esc(batch.request_key)+'</code> · 依据 '+batch.basis_count+' 件冻结训练试件 · 摘要 '+esc(String(batch.basis_digest||'').slice(0,16))+'… · 受理于 '+esc(localTime(batch.created_at))+'</p>'
    +(batch.state==='verified'?'<p class="fine">整批 '+batch.item_count+' 条证明已复核对齐于 '+esc(localTime(batch.verified_at||batch.completed_at))+'，可以查看。</p>':'<p class="fine">批次尚未核对齐（已出 '+(batch.progress.done+batch.progress.failed)+' / '+batch.item_count+'），证明在整批通过后才展示。</p>')
    +(batch.error?'<p class="failure">'+esc(batch.error)+'</p>':'')
    +'<ol class="coverage-items">'+batch.items.map(item=>'<li><header><strong>'+esc(item.label)+'</strong>'+itemStatus(item.state)+'</header>'
      +'<p class="fine">'+domain.fields.filter(f=>f[0]!=='minutes').map(f=>esc(f[1])+' '+esc(item.values[f[0]])).join(' · ')+'</p>'
      +proofRow(item)+'</li>').join('')+'</ol></section>';
}

export function coverageSection(run,batches,detail){
  if(!run||run.state!=='completed')return '';
  if(!research.coverageKey)research.coverageKey=requestKey();
  const ledger=batches.length?batches.map(b=>'<a href="#results" data-coverage="'+b.id+'" class="'+(detail&&detail.id===b.id?'active':'')+'"><small>#'+b.id+'</small><strong>'+esc(b.request_key.slice(0,10))+'… · '+b.item_count+' 条</strong>'+batchStatus(b.state)+'<time>'+esc(localTime(b.created_at))+'</time></a>').join(''):'<p class="fine">本任务还没有覆盖检查批次。</p>';
  const draft=research.coverageDraft??sampleSheet;
  const form=session.writable?'<form id="coverage-form"><label>批次请求编号<input name="request_key" required value="'+esc(research.coverageKey)+'" readonly></label><p class="fine">每行一条工况：名称与四项输入（初始湿度 %、厚度 mm、设定温度 ℃、风量 m³/min），十进制照录、不回写科学计数。</p><label>工况清单<textarea name="items" rows="7" required spellcheck="false">'+esc(draft)+'</textarea></label><p class="fine">依据为任务创建时冻结的训练试件；受理后补录的样本不进入本次判定。重复提交同一请求编号不会重复入账。</p><button type="submit">提交批量检查 →</button></form>':'<p>阅读者可查看已核对齐的批次与证明；提交由分析师或负责人执行。</p>';
  return '<section class="coverage-block"><h2>新工况覆盖检查 <small>精确十进制 · 凸组合或线性分离</small></h2>'
    +'<p class="fine">每条工况若能由该研究的训练试件以非负权重加权凑出，给出至多五件与和为 1 的权重；否则给出能把它与全部训练试件分开的整数系数界限。边界算覆盖，不穷举组合。</p>'
    +'<div class="split coverage-split"><section><h3>本任务的批次</h3><div class="ledger">'+ledger+'</div>'+form+'</section><aside>'+batchDetail(detail)+'</aside></div></section>';
}

export function parseConditions(text){
  const items=[];
  text.split(/\n+/).forEach(line=>{
    const trimmed=line.trim();if(!trimmed)return;
    const parts=trimmed.split(/[,，\t]+/).map(x=>x.trim());
    if(parts.length!==5)throw new Error('每行需五个值：名称, 初始湿度, 厚度, 温度, 风量');
    const [label,moisture,thickness,temperature,airflow]=parts;
    if(!/^-?\d+(\.\d+)?$/.test(moisture)||!/^-?\d+(\.\d+)?$/.test(thickness)||!/^-?\d+(\.\d+)?$/.test(temperature)||!/^-?\d+(\.\d+)?$/.test(airflow))throw new Error(label+'：四项输入须为十进制数值');
    items.push({label,moisture,thickness,temperature,airflow});
  });
  if(!items.length)throw new Error('至少填写一条工况');
  if(items.length>500)throw new Error('单批最多 500 条工况');
  return items;
}

export function bindCoverage(run,refresh){
  for(const link of document.querySelectorAll('[data-coverage]'))link.addEventListener('click',()=>{research.coverageId=Number(link.dataset.coverage);refresh()});
  const sheet=document.querySelector('#coverage-form textarea[name=items]');
  if(sheet){
    // 自动刷新重绘整页时保住草稿与光标，避免把正在录入的工况清掉。
    sheet.addEventListener('input',()=>{research.coverageDraft=sheet.value;research.coverageSelection=[sheet.selectionStart,sheet.selectionEnd]});
    sheet.addEventListener('focus',()=>{research.coverageFocus=true});
    sheet.addEventListener('blur',()=>{research.coverageFocus=false});
    if(research.coverageSelection&&research.coverageDraft!==null){
      const [start,end]=research.coverageSelection;
      if(document.activeElement===sheet||research.coverageFocus){sheet.setSelectionRange(start,end)}
    }
  }
  action('#coverage-form',async form=>{
    const data=Object.fromEntries(new FormData(form));
    const items=parseConditions(data.items);
    const batch=await coverageApi.create(run.id,{request_key:data.request_key.trim(),items});
    research.coverageId=batch.id;research.coverageDraft=null;research.coverageKey=null;research.coverageSelection=null;
    await refresh();toast('批次已受理；worker 逐条判定，整批核对齐后可查看证明');
  });
}
