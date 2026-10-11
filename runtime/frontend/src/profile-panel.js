(() => {
  'use strict';
  const root = document.getElementById('whale-profile');
  const q = selector => root.querySelector(selector);
  const core = window.HarnessReport;
  const schema = JSON.parse(q('#profile-schema-data').textContent);
  const source = q('#profile-source-data').textContent;
  const defaults = JSON.parse(q('#profile-demo-data').textContent);
  const artSources = JSON.parse(q('#profile-art-data').textContent);
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const detail = (label, className) => {
    const node = make('details', undefined, className);
    node.append(make('summary', label)); return node;
  };
  const statusNames = { limited_observation: '有限观察', pending_review: '待复核线索', not_established: '尚未建立' };
  const sourceNames = { answer: '公开回答', session: '会话记录', grader: '独立产物检查' };
  const interpretationNames = { reviewed_descriptive_observation: '人工复核的描述性观察',
    automatic_evidence_observation: '自动提取的事实观察 · 未经人工复核', heuristic_candidate: '自动提取的候选线索 · 等待人工复核' };
  const injectionNames = { observed: '观测到注入', none_observed: '未观测到注入', unknown: '注入状态未知' };
  const retrievalNames = { observed: '观测到成功检索', not_observed: '未观测到成功检索', unknown: '轨迹不足或未记录' };
  const samples = [
    { key: 'explorer', name: '逐浪', vector: [82,54,90,34,42,76], alt: '逐浪：鲸鱼娘跨步前进，一手握罗盘，另一手指向远方。' },
    { key: 'observer', name: '观潮', vector: [38,92,44,30,72,25], alt: '观潮：鲸鱼娘单膝俯身，用放大镜检视证据板。' },
    { key: 'collaborator', name: '合流', vector: [58,58,60,92,64,48], alt: '合流：鲸鱼娘坐在共享绘图桌旁，向观看者递出画笔。' }
  ];
  const axes = [
    { name:'主动推进', low:'等待明确指令', high:'主动提出下一步' },
    { name:'核验投入', low:'较少复核', high:'多步核验' },
    { name:'探索广度', low:'沿既定路径', high:'比较多种路径' },
    { name:'协作密度', low:'独立推进', high:'频繁对齐' },
    { name:'表达详略', low:'简短直接', high:'展开说明' },
    { name:'范围扩展', low:'聚焦当前任务', high:'拓展关联任务' }
  ];
  let state;
  try { state = core.createState(source, schema, defaults); }
  catch (error) { q('[data-error]').hidden = false; q('[data-error]').textContent = '内置报告校验失败：' + error.message; q('[data-settings]').open = true; return; }
  const announce = text => { q('[data-announcement]').textContent = text; };
  const showError = text => { q('[data-error]').textContent = text; q('[data-error]').hidden = !text; };
  function evidenceNode(observation) {
    const node = detail('展开证据与来源 · ' + observation.evidence.length + ' 条摘录', 'evidence-detail');
    observation.evidence.forEach(evidence => {
      node.append(make('blockquote', evidence.quote));
      node.append(make('p', sourceNames[evidence.source] + (evidence.session_line ? ' · 第 ' + evidence.session_line + ' 行' : '') + ' · ' + evidence.run_id, 'run-id'));
      const sources = detail('来源记录', 'source-detail');
      const record = state.report.provenance[evidence.run_id];
      const text = 'run_id: ' + evidence.run_id + '\n' + Object.entries(record).map(([key, value]) => key + ': ' + (value === null ? '未记录' : value)).join('\n');
      sources.append(make('div', text, 'source-body')); node.append(sources);
    });
    const conditions = detail('条件分组与全部运行记录 · ' + observation.condition_slices.length + ' 组', 'source-detail');
    observation.condition_slices.forEach(cell => {
      const c = cell.conditions;
      const text = [cell.n + ' 条记录 · ' + injectionNames[c.injection.status],
        ...(c.condition_id ? ['预定条件：' + c.condition_id, '被测对象：' + c.subject] : []),
        ...(c.subject ? [
          '记忆服务可用：' + (c.memory_available === true ? '已确认' : c.memory_available === false ? '不可用' : '未确认'),
          '成功检索证据：' + (retrievalNames[c.memory_retrieval_status] || retrievalNames.unknown),
          '记忆运行状态：' + (c.memory_runtime_status || '未记录'),
          '未观测到成功检索不等于从未读取记忆；服务可用也不等于本题已使用记忆。'] : []),
        '模型：' + (c.model || '未知') + ' / ' + (c.provider || '未知'),
        '题面：' + (c.prompt_hash || '未知'), '工作区：' + (c.workspace || '未知'),
        '注入摘要：' + (c.injection.digests.join(', ') || '无已记录摘要'),
        '缺失条件：' + (cell.missing_conditions.join(', ') || '无已列字段缺失'),
        '运行记录：\n' + cell.run_ids.join('\n')].join('\n');
      const group = detail(cell.n + ' 条 · ' + injectionNames[c.injection.status], 'source-detail');
      group.append(make('div', text, 'source-body')); conditions.append(group);
    });
    node.append(conditions); return node;
  }
  function renderReport() {
    q('[data-profile-name]').textContent = state.report.profile;
    q('[data-report-review-note]').textContent = state.report.schema_version === 'harness-profile.v2' ?
      '当前结果由自动规则整理。事实观察和待复核线索分别展示；尚未经人工复核，也未证明记忆养成效果。' :
      '当前是人工复核后的局部描述性观察，尚未证明记忆养成效果。';
    const observations = new Map(state.report.observations.map(o => [o.id, o]));
    const dimensions = [];
    [...state.report.dimensions].sort((a,b) => a.id.localeCompare(b.id)).forEach(dimension => {
      const node = make('details', undefined, 'dimension');
      const summary = make('summary', undefined, 'dimension-summary');
      summary.append(make('span', dimension.id, 'dimension-id'), make('span', dimension.label, 'dimension-label'),
        make('span', statusNames[dimension.status], 'status' + (dimension.status === 'not_established' ? ' unset' : '')),
        make('span', '›', 'chevron'));
      summary.lastChild.setAttribute('aria-hidden', 'true');
      const body = make('div', undefined, 'dimension-body'); body.append(make('p', dimension.note, 'note'));
      dimension.observation_ids.forEach(id => {
        const observation = observations.get(id);
        const article = make('article', undefined, 'observation');
        article.append(make('h3', observation.title),
          make('p', interpretationNames[observation.interpretation_status] + (observation.rubric_version ? ' · 判据 ' + observation.rubric_version : ''), 'note'),
          make('p', observation.statement), make('p', observation.limits, 'limits'), evidenceNode(observation));
        body.append(article);
      });
      node.append(summary, body); dimensions.push(node);
    });
    q('[data-dimensions]').replaceChildren(...dimensions);
    q('[data-source-description]').textContent = state.sourceName + ' · ' + state.report.profile + ' · 生成于 ' + state.report.generated_at;
    q('[data-report-limitations]').replaceChildren(...state.report.limitations.map(text => make('p', text, 'note')));
    const inventory = state.report.inventory;
    q('[data-inventory]').textContent = ['资料范围内问卷记录：' + inventory.questionnaire_runs,
      '资料范围内任务记录：' + inventory.task_runs,
      '本报告纳入记录：' + (inventory.included_runs ?? Object.keys(state.report.provenance).length),
      '人工复核记录：' + inventory.reviewed_runs,
      '题面不一致记录：' + state.report.prompt_mismatches.length,
      '以上是资料清单，不是每个维度的样本数。',
      '题面不一致的记录 ID：\n' + state.report.prompt_mismatches.map(x => x.run_id).join('\n')].join('\n');
  }
  const grade = value => value >= 80 ? 'A' : value >= 60 ? 'B' : value >= 40 ? 'C' : value >= 20 ? 'D' : 'E';
  const svgNode = (tag, attrs, text) => {
    const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text !== undefined) node.textContent = text; return node;
  };
  function demoArt() {
    const weights = [3,3,3,3,.5,1];
    return [...samples].sort((a,b) => {
      const distance = sample => sample.vector.reduce((total,v,i) => total + weights[i] * (state.demo.values[i]-v)**2,0);
      return distance(a)-distance(b);
    })[0];
  }
  function renderArt() {
    const sample = state.mode === 'report' ? samples.find(s => s.key === state.cover) : demoArt();
    q('[data-art]').src = artSources[sample.key]; q('[data-art]').alt = sample.alt;
    q('[data-art-caption]').textContent = (state.mode === 'report' ? '自选封面 · ' : '形象演示 · ') + sample.name;
    q('[data-demo-name]').textContent = sample.name;
  }
  function drawDemo() {
    if (state.mode !== 'demo') return;
    const values = state.demo.values;
    const width = Math.max(220, q('[data-radar]').getBoundingClientRect().width);
    const radius = width/2 - 64, y = radius + 61, height = radius*2+122;
    const points = v => v.map((n,i) => { const angle = -Math.PI/2 + i*Math.PI/3; return [width/2+Math.cos(angle)*radius*n/100,y+Math.sin(angle)*radius*n/100]; });
    const pointString = v => points(v).map(pair => pair.join(',')).join(' ');
    const svg = q('[data-radar]'); svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height); svg.style.height = height + 'px';
    const grid = [];
    for (let n=1;n<=5;n++) grid.push(svgNode('polygon',{points:pointString(Array(6).fill(n*20)),class:'radar-grid'}));
    points(Array(6).fill(100)).forEach(([x,endY]) => grid.push(svgNode('line',{x1:width/2,y1:y,x2:x,y2:endY,class:'radar-grid'})));
    q('[data-grid]').replaceChildren(...grid); q('[data-polygon]').setAttribute('points', pointString(values));
    const locations = [[width/2,y-radius-43,'middle'],[width-2,y-radius*.5-12,'end'],[width-2,y+radius*.5+13,'end'],[width/2,y+radius+27,'middle'],[2,y+radius*.5+13,'start'],[2,y-radius*.5-12,'start']];
    const labels = [];
    axes.forEach((axis,i) => {const [x,cy,anchor]=locations[i]; labels.push(svgNode('text',{x,y:cy,'text-anchor':anchor},axis.name),svgNode('text',{x,y:cy+25,'text-anchor':anchor,class:'grade'},grade(values[i])));});
    q('[data-radar-labels]').replaceChildren(...labels);
    q('#radar-description').textContent = '演示参数，非实际测评。' + axes.map((axis,i) => axis.name + ' ' + values[i] + '，' + grade(values[i])).join('；');
    axes.forEach((axis,i) => {q('#demo-value-'+i).textContent = values[i] + ' · ' + grade(values[i]);});
  }
  function setMode(mode) {
    state.mode = mode;
    q('[data-report-panel]').hidden = mode !== 'report'; q('[data-demo-panel]').hidden = mode !== 'demo';
    q('[data-report-source]').hidden = mode !== 'report';
    root.querySelectorAll('[data-mode]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mode === mode)));
    renderArt(); drawDemo();
  }
  axes.forEach((axis,i) => {
    const row = make('div'); const head = make('div', undefined, 'input-head');
    const label = make('label', axis.name); label.htmlFor='demo-axis-'+i;
    const output=make('output');output.id='demo-value-'+i;output.htmlFor='demo-axis-'+i;head.append(label,output);
    const input=make('input');input.type='range';input.id='demo-axis-'+i;input.min='0';input.max='100';input.step='1';input.value=state.demo.values[i];
    const ends=make('div',undefined,'ends');ends.append(make('span',axis.low),make('span',axis.high));
    input.addEventListener('input',() => {const values=[...state.demo.values];values[i]=Number(input.value);core.setDemoValues(state,values);drawDemo();renderArt();});
    row.append(head,input,ends);q('[data-sliders]').append(row);
  });
  samples.forEach(sample => {
    const button=make('button',sample.name);button.type='button';button.dataset.cover=sample.key;button.setAttribute('aria-pressed',String(sample.key===state.cover));
    button.addEventListener('click',()=>{state.cover=sample.key;root.querySelectorAll('[data-cover]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.cover===state.cover)));renderArt();announce('报告封面已改为'+sample.name);});
    q('[data-covers]').append(button);
  });
  root.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click',()=>setMode(button.dataset.mode)));
  q('[data-import]').addEventListener('change',async event=>{
    const file=event.target.files[0];if(!file)return;
    try {
      if(file.size>core.maxBytes)throw new Error('报告文件不能超过 2 MB');
      const text=await file.text();core.importReport(state,text,schema,file.name);
      renderReport();setMode('report');showError('');announce('已导入 '+file.name+'。原报告不会被写回。');
    } catch(error) {showError('导入失败，当前报告保持不变：'+error.message);}
    finally {event.target.value='';}
  });
  q('[data-export]').addEventListener('click',()=>{
    const url=URL.createObjectURL(new Blob([state.sourceText],{type:'application/json;charset=utf-8'}));
    const link=make('a');link.href=url;link.download='harness-profile-source.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    announce('已导出当前报告的来源 JSON。');
  });
  renderReport();setMode('report');new ResizeObserver(drawDemo).observe(q('[data-radar]'));
})();
