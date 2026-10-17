'use strict';
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = x => String(x ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const number = x => typeof x === 'number' && Number.isFinite(x);
const fmt = x => number(x) ? new Intl.NumberFormat('en', {notation: x >= 1e6 ? 'compact' : 'standard', maximumFractionDigits:1}).format(x) : '—';
const money = x => number(x) ? '$' + x.toLocaleString('en', {maximumFractionDigits:x < 10 ? 2 : 0}) : '—';
const percent = x => number(x) ? (x * 100).toFixed(1) + '%' : '—';
const time = x => number(x) ? x >= 3600 ? (x / 3600).toFixed(1) + 'h' : Math.round(x / 60) + 'm' : '—';
let data, models, tasks, runsByGroup, selectedModels = [], selectedTask, selectedRuns = new Map();
let renderVersion = 0, currentView = 'dataset', syncing = false;
const trajectoryCache = new Map();

function group(model, task) { return runsByGroup.get(model + '\n' + task) || []; }
function score(runs) {
  const scored = runs.filter(r => r.scored !== false && number(r.reward));
  return {n:scored.length, success:scored.filter(r => r.reward === 1).length, rate:scored.length ? scored.filter(r => r.reward === 1).length / scored.length : null,
    mean:scored.length ? scored.reduce((s,r) => s+r.reward,0) / scored.length : null, complete:scored.length > 0 && scored.length === runs.length};
}
function taskScore(task) { return score(data.runs.filter(r => r.task_name === task)); }
function saveDownload(text, filename, type = 'application/x-ndjson') {
  const url = URL.createObjectURL(new Blob([text], {type}));
  const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 3000);
}
function notice(text) { $('#load-error').hidden = false; $('#load-error').textContent = text; }
async function pollDownload() {
  try {
    const response=await fetch('data/crawl-status.json', {cache:'no-store'});
    if(!response.ok) return;
    const status=await response.json(), box=$('#download-progress');
    const fresh=Date.now()-Date.parse(status.updated_at)<20000;
    const active=['running','building'].includes(status.state) && fresh;
    const archived=(status.counts.cached || 0)+(status.counts.downloaded || 0);
    const total=status.selected_trials || data.expected_trials;
    const label=active ? (status.state==='building' ? '正在导出 JSONL / 更新网页' : '正在下载 Harbor 轨迹') : status.state==='complete' ? '本轮下载已结束' : '下载已停止';
    box.hidden=false;
    box.innerHTML=`<div class="download-heading"><strong>${active ? '<span class="loading-dot"></span> ' : ''}${label}</strong><span>${fmt(archived)} / ${fmt(total)} 条已归档</span><button class="button refresh-index">刷新网页索引 ↻</button></div><div class="download-track"><i style="width:${Math.min(100,archived/Math.max(1,total)*100)}%"></i></div><div class="download-details">${active ? `${fmt(status.downloaded_per_minute)} 条/分钟 · ${fmt(status.network_mbps)} MB/s · ${status.active_requests || 0} 个活跃请求 · 剩余 ${status.remaining ?? '—'} 条待处理${status.transfer_total_bytes ? ' · '+(status.transfer_received_bytes/1e9).toFixed(2)+' / '+(status.transfer_total_bytes/1e9).toFixed(2)+' GB' : ''}` : `本轮错误 ${status.counts.error || 0} · 源站未提供 ${status.counts.unavailable || 0}`}<span>网页索引：${fmt(data.counts.downloaded || 0)} 条${archived>(data.counts.downloaded || 0) ? ' · 新轨迹将在导出后可读' : ''}</span></div>`;
    $('.refresh-index',box).onclick=()=>location.reload();
  } catch { /* A static archive may intentionally have no live crawl status. */ }
}
function modelLabel(id) { const m=models.get(id); return m ? `${m.name} · ${m.agent}${m.effort ? ' / '+m.effort : ''}` : id; }
function downloadName(id,task) { return `${models.get(id).name}-${tasks.get(task).name}${data.web_projection==='text_only' ? '.text' : ''}.jsonl`; }
function options(selected) { return data.models.map(m => `<option value="${esc(m.id)}" ${m.id === selected ? 'selected' : ''}>${esc(modelLabel(m.id))}</option>`).join(''); }
function setRoute(view = 'compare') {
  const p = new URLSearchParams();
  if (view === 'compare') {
    p.set('task', selectedTask); p.set('models', selectedModels.join(','));
    const ids = selectedModels.map(m => selectedRuns.get(m) || '');
    if (ids.some(Boolean)) p.set('runs', ids.join(','));
  }
  const hash = view + (p.size ? '?' + p.toString() : '');
  if (location.hash.slice(1) === hash) route(); else location.hash = hash;
}
function route() {
  if (!data) return;
  const [view, query = ''] = location.hash.slice(1).split('?');
  currentView = ['dataset','compare','analysis','about'].includes(view) ? view : 'dataset';
  const p = new URLSearchParams(query);
  if (currentView === 'compare') {
    if (tasks.has(p.get('task'))) selectedTask = p.get('task');
    if (p.has('models')) {
      const ids = [...new Set(p.get('models').split(',').filter(id => models.has(id)))].slice(0,4);
      if (ids.length) selectedModels = ids;
    }
    if (p.has('runs')) p.get('runs').split(',').forEach((id,i) => { if (selectedModels[i] && group(selectedModels[i], selectedTask).some(r => r.trial_id === id)) selectedRuns.set(selectedModels[i], id); });
    renderCompare();
  }
  $$('.view').forEach(el => el.hidden = el.id !== currentView);
  $$('nav a').forEach(el => el.classList.toggle('active', el.dataset.tab === currentView));
  if (currentView === 'analysis') renderMatrix();
}
function renderOverview() {
  const downloaded = data.counts.downloaded || 0;
  $('#stats').innerHTML = [
    ['SCIENTIFIC TASKS', data.tasks.length, '独立科学任务'],
    ['MODEL CONFIGURATIONS', data.models.length, '模型 / Agent / 推理强度'],
    ['EVALUATION RUNS', fmt(data.expected_trials), `可枚举运行 / ${fmt(data.official_trials)} 榜单记录`],
    ['ARCHIVED TRAJECTORIES', fmt(downloaded), `${percent(downloaded / (data.expected_trials || 1))} 下载覆盖率`],
  ].map(([label,value,desc],i) => `<div class="stat"><small>${label}</small><strong class="${i === 3 ? 'accent' : ''}">${value}</strong><span>${desc}</span></div>`).join('');
  $('#model-table').innerHTML = data.models.map((m,i) => {
    const official=m.official_metrics, runs=data.runs.filter(r=>r.model_id === m.id), n=runs.filter(r=>r.download_status === 'downloaded').length;
    return `<tr><td class="rank">${String(i+1).padStart(2,'0')}</td><td><div class="model-name">${esc(m.name)}</div><span class="agent">${esc(m.agent)}</span></td><td><span class="pill">${esc(m.effort || 'default')}</span></td><td><div class="rate">${number(official.accuracy) ? official.accuracy.toFixed(1)+'%' : '—'}<span class="rate-track"><i style="width:${number(official.accuracy) ? Math.max(0,Math.min(100,official.accuracy)) : 0}%"></i></span></div></td><td class="number">${fmt(official.total_tokens)}</td><td class="number">${money(official.total_cost_usd)}</td><td class="download-status">${runs.length ? n+' / '+runs.length : '仅榜单分数'}</td><td><button class="compare-link" style="border:0;background:none" data-compare="${esc(m.id)}" aria-label="对比 ${esc(m.name)}">↗</button></td></tr>`;
  }).join('');
  $$('[data-compare]').forEach(button => button.onclick = () => {
    selectedModels = [...new Set([button.dataset.compare, data.models.find(m=>m.id !== button.dataset.compare)?.id].filter(Boolean))]; setRoute();
  });
  $('#coverage-note').textContent = `本快照：${data.models.length} 个模型配置、${data.tasks.length} 道题；榜单记录 ${data.official_trials} 次运行，其中 ${data.expected_trials} 次提供公开运行列表，${data.unlisted_trials} 次未公开 trial。已归档 ${downloaded} 条轨迹；不可用 ${data.counts.unavailable || 0}，错误 ${data.counts.error || 0}，等待 ${data.counts.pending || 0}。榜单发现${data.discovery_complete ? '完整' : '未完成'}。`;
  if(data.web_projection==='text_only') $('#coverage-note').textContent+=' 此网页快照为文本阅读版，省略内嵌图片与二进制；网页下载的 JSONL 带 text 标记。完整无损 ATIF / JSONL 保存在运行下载器的本地 dataset 目录。';
}
function renderTasks() {
  const q = $('#task-search').value.trim().toLowerCase();
  let list = data.tasks.filter(t => t.name.toLowerCase().includes(q));
  const sort=$('#task-sort').value;
  if (sort === 'hard') list.sort((a,b)=>(taskScore(a.id).rate ?? 2)-(taskScore(b.id).rate ?? 2));
  if (sort === 'disagree') {
    const variance = t => { const rates=data.models.map(m=>score(group(m.id,t.id)).rate).filter(number); const mean=rates.reduce((a,b)=>a+b,0)/(rates.length||1); return rates.reduce((s,r)=>s+(r-mean)**2,0)/(rates.length||1); };
    list.sort((a,b)=>variance(b)-variance(a));
  }
  $('#task-count').textContent = `${list.length} / ${data.tasks.length}`;
  $('#task-list').innerHTML = list.length ? list.map(t => { const s=taskScore(t.id); return `<button class="task-item ${t.id === selectedTask ? 'active' : ''}" data-task="${esc(t.id)}"><b>${esc(t.name)}</b><span>${s.success}/${s.n} passed · ${percent(s.rate)}</span></button>`; }).join('') : '<div class="empty">没有匹配题目</div>';
  $$('.task-item').forEach(button => button.onclick = () => { selectedTask=button.dataset.task; selectedRuns.clear(); setRoute(); });
}
function renderPickers() {
  $('#model-picker').innerHTML = selectedModels.map((id,i)=>`<div class="picker-slot"><select aria-label="第 ${i+1} 列模型" data-slot="${i}">${options(id)}</select><button class="remove-column" data-remove="${i}" aria-label="移除第 ${i+1} 列" ${selectedModels.length === 1 ? 'disabled' : ''}>×</button></div>`).join('') + `<button class="add-column" ${selectedModels.length >= Math.min(4,data.models.length) ? 'disabled' : ''}>＋ 添加模型</button>`;
  $$('[data-slot]').forEach(select => select.onchange = () => {
    const i=Number(select.dataset.slot), other=selectedModels.indexOf(select.value);
    if (other !== -1 && other !== i) selectedModels[other]=selectedModels[i];
    selectedModels[i]=select.value; setRoute();
  });
  $$('[data-remove]').forEach(button => button.onclick=()=>{ selectedModels.splice(Number(button.dataset.remove),1); setRoute(); });
  $('.add-column').onclick=()=>{ const model=data.models.find(m=>!selectedModels.includes(m.id)); if(model){selectedModels.push(model.id);setRoute();} };
}
async function loadTask(path) {
  if (trajectoryCache.has(path)) return trajectoryCache.get(path);
  const promise=(async()=>{
    const descriptor=data.files?.[path];
    const parts=descriptor?.parts || [path+'.gz'];
    const buffers=[];
    for(const part of parts){
      const response=await fetch('data/'+part);
      if(!response.ok) throw new Error('JSONL 加载失败：HTTP '+response.status);
      buffers.push(new Uint8Array(await response.arrayBuffer()));
    }
    const bytes=new Uint8Array(buffers.reduce((n,b)=>n+b.length,0));let offset=0;
    for(const buffer of buffers){bytes.set(buffer,offset);offset+=buffer.length;}
    let text;
    if(bytes[0] === 0x1f && bytes[1] === 0x8b){
      if (!('DecompressionStream' in window)) throw new Error('当前浏览器不支持 gzip 解压，请使用最新版 Chrome、Safari 或 Firefox。');
      text=await new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream('gzip'))).text();
    } else text=new TextDecoder().decode(bytes);
    return {text, records:text.split('\n').filter(Boolean).map(line=>JSON.parse(line))};
  })();
  trajectoryCache.set(path,promise);
  if(trajectoryCache.size>8) trajectoryCache.delete(trajectoryCache.keys().next().value);
  try {return await promise;} catch(error){trajectoryCache.delete(path);throw error;}
}
function content(value) {
  if(value == null) return '';
  if(typeof value === 'string') return value;
  if(Array.isArray(value)) return value.map(part=>part.type === 'image' ? `[image: ${part.source?.path || part.image_url?.url || 'see raw ATIF'}]` : part.text || content(part.content) || JSON.stringify(part,null,2)).join('\n');
  return JSON.stringify(value,null,2);
}
function details(title, value, lazy, structured=false, markdown=false) {
  const id=lazy.length;lazy.push(value);
  return `<details data-lazy="${id}" ${structured ? 'data-structured="1"' : ''} ${markdown ? 'data-markdown="1"' : ''}><summary>${esc(title)}</summary></details>`;
}
function messageHTML(text) {
  return $('#message-format').value==='source' ? `<pre class="message-source">${esc(text)}</pre>` : `<div class="markdown-body">${ScienceMarkdown.html(text)}</div>`;
}
function observationBlocks(value, blocks=[], depth=0) {
  if(depth>40 || value==null) return blocks;
  if(typeof value==='string'){if(value) blocks.push({type:'text',text:value});return blocks;}
  if(Array.isArray(value)){for(const item of value) observationBlocks(item,blocks,depth+1);return blocks;}
  if(typeof value!=='object') return blocks;
  if(value.type==='image'){
    const mime=value.source?.media_type || value.file?.type;
    const base64=value.source?.data || value.file?.base64;
    if(/^image\/(png|jpeg|webp|gif)$/.test(mime || '') && typeof base64==='string') blocks.push({type:'image',mime,base64});
    else blocks.push({type:'text',text:'[Image reference: '+(value.source?.path || 'see original Harbor trial')+']'});
  }else if(value.type==='text' && typeof value.text==='string') blocks.push({type:'text',text:value.text});
  else for(const key of ['results','content','text','stdout','stderr','output','return']) if(key in value) observationBlocks(value[key],blocks,depth+1);
  return blocks;
}
function appendLongText(parent,text) {
  const frame=document.createElement('div');frame.className='code-frame';
  frame.innerHTML='<div class="code-toolbar"><span>原始字段 / 工具输出</span><button type="button" class="code-copy">复制代码</button></div><pre class="code"><code></code></pre>';
  const code=$('code',frame);code.textContent=text.slice(0,60000);parent.append(frame);
  ScienceMarkdown.attachCopy($('.code-copy',frame),()=>text);
  if(text.length>60000){const button=document.createElement('button');button.className='button';button.textContent=`显示完整内容 · ${fmt(text.length)} characters`;button.onclick=()=>{code.textContent=text;button.remove();};parent.append(button);}
}
function stepHTML(step,index,lazy) {
  const tools=step.tool_calls || [], observation=step.observation;
  let html=`<article class="step"><div class="step-title"><strong>${esc((step.source || 'step').toUpperCase())}</strong><span>#${esc(step.step_id ?? index+1)} ${esc(step.timestamp ? new Date(step.timestamp).toLocaleTimeString('en-GB',{timeZone:'UTC'})+' UTC' : '')}</span></div>`;
  if(step.message){
    const message=content(step.message);
    html+=messageHTML(message.slice(0,20000));
    if(message.length>20000) html+=details('展开完整消息 · '+fmt(message.length)+' characters',step.message,lazy,false,true);
  }
  const reasoning=step.reasoning_content || step.reasoning || step.extra?.reasoning;
  if(reasoning) html+=details('REASONING',reasoning,lazy,false,true);
  for(const tool of tools) html+=details('↳ '+(tool.function_name || tool.function?.name || 'Tool call'),tool.arguments ?? tool.function?.arguments ?? tool,lazy);
  if(observation) html+=details('↳ OBSERVATION / 工具返回', observation,lazy,true);
  html+=details(data.web_projection==='text_only' ? 'RAW STEP · 文本视图字段（已省略二进制）' : 'RAW STEP · 完整原始字段',step,lazy);
  return html+'</article>';
}
function renderSteps(container, record) {
  let steps=(record.trajectory || record.reading_trajectory)?.steps || [];
  const mode=$('#reading-mode').value, query=$('#step-search').value.toLowerCase();
  if(!$('#show-system').checked) steps=steps.filter(s=>s.source !== 'system' && !content(s.message).startsWith('<environment_context>'));
  if(mode === 'answer') {
    const answers=steps.filter(s=>['agent','assistant'].includes(s.source) && content(s.message).trim() && !(s.tool_calls?.length));
    steps=answers.length ? answers.slice(-1) : [];
  } else if(mode === 'tools') steps=steps.filter(s=>s.tool_calls?.length || s.observation || s.source === 'tool');
  else if(mode === 'reasoning') steps=steps.filter(s=>['agent','assistant','reasoning'].includes(s.source) && (content(s.message).trim() || s.reasoning || s.reasoning_content));
  if(query) steps=steps.filter(s=>JSON.stringify(s).toLowerCase().includes(query));
  container.innerHTML='';
  if(!steps.length){container.innerHTML='<div class="empty">此模式下没有可显示的步骤。</div>';return;}
  let offset=0;const lazy=[];
  function append(){
    $('.load-more',container)?.remove();
    const chunk=steps.slice(offset,offset+80); offset+=chunk.length;
    container.insertAdjacentHTML('beforeend',chunk.map((s,i)=>stepHTML(s,offset-chunk.length+i,lazy)).join(''));
    ScienceMarkdown.bind(container);
    $$('details[data-lazy]',container).forEach(detail=>{
      if(detail.dataset.bound) return;detail.dataset.bound='1';
      detail.ontoggle=()=>{
        if(!detail.open || detail.dataset.loaded) return;detail.dataset.loaded='1';
        const value=lazy[Number(detail.dataset.lazy)],blocks=detail.dataset.structured ? observationBlocks(value) : [];
        if(detail.dataset.markdown && $('#message-format').value!=='source'){
          ScienceMarkdown.append(detail,content(value));return;
        }
        if(blocks.length){for(const block of blocks){
          if(block.type==='text') appendLongText(detail,block.text);
          else {const img=document.createElement('img');img.alt='Image from original trajectory';img.loading='lazy';img.src=`data:${block.mime};base64,${block.base64}`;detail.append(img);}
        }}else appendLongText(detail,content(value));
      };
    });
    if(offset<steps.length){const button=document.createElement('button');button.className='button load-more';button.textContent=`继续显示 · ${offset} / ${steps.length} 步`;button.onclick=append;container.append(button);}
  }
  append();
}
async function renderCompare() {
  const version=++renderVersion;
  renderTasks(); renderPickers();
  $('#selected-task').textContent=tasks.get(selectedTask)?.name || selectedTask;
  const aggregate=taskScore(selectedTask);
  $('#task-description').textContent=`${aggregate.n} scored runs · ${percent(aggregate.rate)} overall pass rate · 独立运行完整保留`;
  $('#compare-columns').innerHTML=selectedModels.map(id=>{
    const list=group(id,selectedTask), m=models.get(id);
    let run=list.find(r=>r.trial_id === selectedRuns.get(id));
    if(!run) run=list.find(r=>r.download_status === 'downloaded') || list[0];
    if(run) selectedRuns.set(id,run.trial_id);
    return `<article class="trajectory-column" data-model="${esc(id)}"><div class="column-head"><h3>${esc(m.name)}</h3><div class="config">${esc(m.agent)} · ${esc(m.effort || 'default')}</div><select class="run-select" aria-label="${esc(m.name)} 运行选择" ${!list.length ? 'disabled' : ''}>${list.length ? list.map((r,i)=>`<option value="${esc(r.trial_id)}" ${r === run ? 'selected' : ''}>Run ${i+1} · ${number(r.reward) ? 'reward '+r.reward : '未评分'} · ${r.download_status} · ${esc((r.job_name || r.job_id).slice(-50))}</option>`).join('') : '<option>此题没有运行记录</option>'}</select><div class="run-metrics">${run ? `<span class="${run.reward === 1 ? 'pass' : 'fail'}">${number(run.reward) ? 'Reward '+run.reward : '未评分'}</span><span>${money(run.cost_usd)}</span><span>${fmt(number(run.input_tokens) && number(run.output_tokens) ? run.input_tokens+run.output_tokens : null)} tok</span><span>${time(run.duration_seconds)}</span>` : ''}</div><div class="column-links">${run ? `<a href="${esc(run.source_url)}" target="_blank" rel="noopener">原始轨迹 ↗</a><button class="download-jsonl" style="border:0;background:none;padding:0;color:inherit;font:inherit">↓ 此题 JSONL</button>` : ''}</div></div><div class="column-body"><div class="empty"><span class="loading-dot"></span> 正在载入完整轨迹…</div></div></article>`;
  }).join('');
  if(data.web_projection==='text_only') $$('.download-jsonl').forEach(button=>button.textContent='↓ 此题文本 JSONL');
  $$('.run-select').forEach(select=>select.onchange=()=>{selectedRuns.set(select.closest('[data-model]').dataset.model,select.value);setRoute();});
  $$('.column-body').forEach(body=>body.onscroll=()=>{
    if(!$('#sync-scroll').checked || syncing) return;
    const ratio=body.scrollTop / Math.max(1,body.scrollHeight-body.clientHeight);
    syncing=true;
    $$('.column-body').forEach(other=>{if(other!==body) other.scrollTop=ratio*Math.max(0,other.scrollHeight-other.clientHeight);});
    requestAnimationFrame(()=>{syncing=false;});
  });
  await Promise.all(selectedModels.map(async id=>{
    const column=$$('[data-model]').find(c=>c.dataset.model===id), body=$('.column-body',column);
    const run=group(id,selectedTask).find(r=>r.trial_id===selectedRuns.get(id));
    if(!run){body.innerHTML='<div class="empty"><b>源站未公开运行记录</b>此配置仅有榜单分数，Harbor 未提供可访问的 job / trial 列表。</div>';return;}
    if(run.download_status !== 'downloaded'){
      body.innerHTML=`<div class="empty"><b>${esc({pending:'轨迹等待下载',unavailable:'源站未提供轨迹',error:'轨迹下载失败'}[run.download_status] || run.download_status)}</b>${esc(run.download_error || '运行指标已经收录，下载器完成后可查看解题过程。')}</div>`;
    }
    const unavailable=body.innerHTML;
    $('.download-jsonl',column).onclick=async()=>{
      try{const file=await loadTask(run.path);saveDownload(file.text,downloadName(id,selectedTask));}
      catch(error){notice(error.message);}
    };
    const load=async()=>{try{
      body.innerHTML='<div class="empty"><span class="loading-dot"></span> 正在解压轨迹…</div>';
      const file=await loadTask(run.path);
      if(version!==renderVersion) return;
      $('.download-jsonl',column).onclick=()=>saveDownload(file.text,downloadName(id,selectedTask));
      const record=file.records.find(r=>r.trial_id===run.trial_id);
      const readable=record?.trajectory || record?.reading_trajectory;
      if(!readable){if(run.download_status === 'downloaded') throw new Error('轨迹文件中没有此 trial。');body.innerHTML=unavailable;return;}
      renderSteps(body,record);
      if(record.source_format==='nonstandard_json') body.insertAdjacentHTML('afterbegin','<p class="projection-note">源文件含未加引号的脱敏标记。此处显示阅读副本，脱敏数值留空；原始文本完整保存在本地 JSONL 的 raw_trajectory 中。</p>');
      if(!$('.task-prompt')){
        const prompts=readable.steps.filter(s=>s.source==='user').map(s=>content(s.message)).filter(s=>!s.startsWith('<environment_context>'));
        const prompt=prompts.sort((a,b)=>b.length-a.length)[0];
        if(prompt){
          const details=document.createElement('details');details.className='task-prompt';
          details.innerHTML='<summary>查看原始任务说明</summary>';$('#task-description').append(details);
          ScienceMarkdown.append(details,prompt);
        }
      }
    } catch(error){if(version===renderVersion) body.innerHTML=`<div class="empty"><b>读取失败</b>${esc(error.message)}</div>`;}};
    if((data.files?.[run.path]?.raw_bytes || 0)>50*1024*1024){
      body.innerHTML=`<div class="empty"><b>大型轨迹文件 · ${(data.files[run.path].raw_bytes/1024/1024).toFixed(0)} MB</b>该题包含大量工具输出，按需加载完整内容。<br><br><button class="button load-large">加载完整轨迹</button></div>`;
      $('.load-large',body).onclick=load;
    } else await load();
  }));
}
function renderCostChart() {
  const list=data.models.filter(m=>number(m.official_metrics.total_cost_usd) && m.official_metrics.tasks>0 && number(m.official_metrics.accuracy));
  const W=620,H=290,L=45,R=20,T=28,B=40;
  const maxCost=Math.max(1,...list.map(m=>m.official_metrics.total_cost_usd/m.official_metrics.tasks))*1.15;
  let svg=`<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="模型成功率与平均每次运行成本散点图">`;
  for(let i=0;i<=4;i++){
    const y=T+(H-T-B)*i/4;
    svg+=`<path d="M${L} ${y}H${W-R}" stroke="#e6ede3"/><text x="${L-10}" y="${y+3}" text-anchor="end" font-size="9" fill="#8da38e">${100-i*25}%</text>`;
    const x=L+(W-L-R)*i/4;
    svg+=`<text x="${x}" y="${H-B+18}" text-anchor="middle" font-size="9" fill="#8da38e">$${(maxCost*i/4).toFixed(0)}</text>`;
  }
  for(const [i,m] of list.entries()){
    const x=L+(W-L-R)*(m.official_metrics.total_cost_usd/m.official_metrics.tasks)/maxCost, y=T+(H-T-B)*(1-m.official_metrics.accuracy/100);
    svg+=`<g><circle cx="${x}" cy="${y}" r="5" fill="${i<3 ? '#2d8657' : '#96b99c'}" stroke="white" stroke-width="1.5"><title>${esc(modelLabel(m.id))}: ${m.official_metrics.accuracy.toFixed(1)}%, ${money(m.official_metrics.total_cost_usd/m.official_metrics.tasks)} / run</title></circle>${i<4 ? `<text x="${Math.min(x+9,W-95)}" y="${y-9-(i%2)*5}" font-size="9" fill="#517f55">${esc(m.name)}</text>` : ''}</g>`;
  }
  svg+=`<text x="${W/2}" y="${H-3}" text-anchor="middle" font-size="9" fill="#8da38e">AVERAGE COST PER RUN (USD) · hover for details</text></svg>`;
  $('#cost-chart').innerHTML=svg;
}
function renderPairPickers() {
  $('#pair-picker').innerHTML=`<select id="pair-a" aria-label="配对模型 A">${options(data.models[0]?.id)}</select><select id="pair-b" aria-label="配对模型 B">${options(data.models[1]?.id || data.models[0]?.id)}</select>`;
  $('#pair-a').onchange=renderPair;$('#pair-b').onchange=renderPair;renderPair();
}
function renderPair() {
  const a=$('#pair-a').value,b=$('#pair-b').value;
  if(a===b){$('#pair-result').innerHTML='<p class="subtle">请选择两个不同配置。</p>';return;}
  let wins=0,ties=0,losses=0;
  for(const t of data.tasks){
    const x=score(group(a,t.id)),y=score(group(b,t.id));
    const expected=data.expected_trials_per_task || 3;
    if(!x.complete || !y.complete || x.n<expected || y.n<expected) continue;
    if(Math.abs(x.mean-y.mean)<1e-12) ties++;else if(x.mean>y.mean) wins++;else losses++;
  }
  $('#pair-result').innerHTML=`<div class="pair-stats"><div><strong>${wins}</strong><span>A 胜出</span></div><div><strong>${ties}</strong><span>相同平均 REWARD</span></div><div><strong>${losses}</strong><span>B 胜出</span></div></div><p class="subtle">${wins+ties+losses} 道共同完整评分题目 · 按每题平均 reward 比较</p><button class="button" id="compare-pair">阅读两个模型的轨迹 ↗</button>`;
  $('#compare-pair').onclick=()=>{selectedModels=[a,b];setRoute();};
}
function renderMatrix() {
  const q=$('#matrix-search').value.toLowerCase();
  const colors=['#edf1ef','#b8e4cf','#64bc98','#187653'];
  $('#matrix').innerHTML='<table><thead><tr><th>SCIENTIFIC TASK</th>'+data.models.map(m=>`<th title="${esc(modelLabel(m.id))}">${esc(m.name)}<br><span>${esc(m.agent)}</span></th>`).join('')+'</tr></thead><tbody>'+data.tasks.filter(t=>t.name.toLowerCase().includes(q)).map(t=>`<tr><td><button data-task-open="${esc(t.id)}">${esc(t.name)}</button></td>${data.models.map(m=>{
    const s=score(group(m.id,t.id)),r=s.rate;
    return `<td><button data-cell-task="${esc(t.id)}" data-cell-model="${esc(m.id)}" style="background:${r===null ? '#fafbf9' : colors[Math.round(r*3)]};color:${r>.8 ? 'white' : '#537a5d'}" title="${esc(modelLabel(m.id))}: ${s.success}/${s.n} scored runs">${r===null ? '—' : Math.round(r*100)+'%'}</button></td>`;
  }).join('')}</tr>`).join('')+'</tbody></table>';
  $$('[data-task-open]').forEach(button=>button.onclick=()=>{selectedTask=button.dataset.taskOpen;setRoute();});
  $$('[data-cell-task]').forEach(button=>button.onclick=()=>{selectedTask=button.dataset.cellTask;selectedModels=[...new Set([button.dataset.cellModel,...selectedModels])].slice(0,2);setRoute();});
}
async function init() {
  try{
    const response=await fetch('data/manifest.json', {cache:'no-store'});
    if(!response.ok) throw new Error('尚未生成数据索引，请先运行 python3 crawl.py crawl 或 build，再通过 HTTP 服务访问页面。');
    data=await response.json();
    models=new Map(data.models.map(m=>[m.id,m]));tasks=new Map(data.tasks.map(t=>[t.id,t]));runsByGroup=new Map();
    for(const run of data.runs){const key=run.model_id+'\n'+run.task_name;if(!runsByGroup.has(key)) runsByGroup.set(key,[]);runsByGroup.get(key).push(run);}
    selectedModels=data.models.slice(0,2).map(m=>m.id);
    selectedTask=data.tasks.find(t=>t.name==='traffic-flux-inversion')?.id || data.tasks[0]?.id;
    $('#source-link').href=data.source_url;$('#updated').textContent='SNAPSHOT '+new Date(data.generated_at).toISOString().slice(0,10);
    if(!data.discovery_complete || data.counts.error || data.counts.pending || data.counts.unavailable || data.unlisted_trials || data.web_projection==='text_only') notice(`${data.web_projection==='text_only' ? '网页文本阅读版：省略内嵌图片与二进制，完整 JSONL 在本地 dataset 目录。' : ''}数据覆盖：${data.counts.downloaded || 0} / ${data.expected_trials} 条可枚举轨迹。${!data.discovery_complete ? '榜单发现不完整；' : ''}${data.unlisted_trials || 0} 次榜单运行未公开 trial；${data.counts.pending || 0} 等待下载，${data.counts.error || 0} 下载错误，${data.counts.unavailable || 0} 源站未提供轨迹。`);
    renderOverview();renderCostChart();renderPairPickers();route();
    $('#task-search').oninput=renderTasks;$('#task-sort').onchange=renderTasks;$('#matrix-search').oninput=renderMatrix;
    $('#reading-mode').onchange=renderCompare;$('#show-system').onchange=renderCompare;
    $('#message-format').onchange=renderCompare;
    let timer;$('#step-search').oninput=()=>{clearTimeout(timer);timer=setTimeout(renderCompare,250);};
    $('#share').onclick=async()=>{try{await navigator.clipboard.writeText(location.href);$('#share').textContent='链接已复制 ✓';}catch{ $('#share').textContent='请复制地址栏中的完整链接'; } };
    window.addEventListener('hashchange',route);
    pollDownload();setInterval(pollDownload,5000);
  }catch(error){notice(error.message);$('#stats').innerHTML='<div class="empty">数据尚未载入</div>';}
}
init();
