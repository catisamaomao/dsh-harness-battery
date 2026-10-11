(() => {
  'use strict';
  const root = document.getElementById('whale-profile');
  const q = selector => root.querySelector(selector);
  const core = window.HarnessReport;
  const schema = JSON.parse(q('#profile-schema-data').textContent);
  const source = q('#profile-source-data').textContent;
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
  const themes = {
    explorer: {name:'逐浪', seal:'行', subtitle:'乘流而进',
      poem:['前路横潮未系舟','且将可渡付中流','一帆先送星河近','留待东风再上楼'],
      alt:'逐浪：鲸鱼娘跨步前进，握着罗盘指向远方。'},
    observer: {name:'观潮', seal:'察', subtitle:'照水知微',
      poem:['浪底微光入眼明','俯身重辨水中星','莫凭一叶言沧海','几度观澜几度听'],
      alt:'观潮：鲸鱼娘单膝俯身，用放大镜检视证据板。'},
    collaborator: {name:'合流', seal:'应', subtitle:'随约成章',
      poem:['一纸相期共展图','收帆扬棹各知途','潮来有约同舟渡','落笔成章不越隅'],
      alt:'合流：鲸鱼娘坐在共享绘图桌旁，向观看者递出画笔。'},
    pending: {name:'未定之相', seal:'待', subtitle:'潮声未定',
      poem:['鲸影初临雾未开','半笺潮迹待风来','且留六角星光白','莫把微澜作性裁']}
  };
  let state;
  try { state = core.createState(source, schema); }
  catch (error) { q('[data-error]').hidden = false; q('[data-error]').textContent = '内置报告校验失败：' + error.message; q('[data-report-source]').open = true; return; }
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
  function renderArt() {
    const key = core.portrait(state.report);
    const theme = themes[key || 'pending'];
    q('[data-profile-name]').textContent = theme.name;
    q('[data-subtitle]').textContent = theme.subtitle;
    q('[data-seal]').textContent = theme.seal;
    q('[data-poem]').replaceChildren(...theme.poem.map(line => make('p', line)));
    q('[data-art]').hidden = !key;
    q('[data-empty-art]').toggleAttribute('hidden', Boolean(key));
    if (key) { q('[data-art]').src = artSources[key]; q('[data-art]').alt = theme.alt; }
    else { q('[data-art]').removeAttribute('src'); q('[data-art]').alt = ''; }
    q('[data-stage]').dataset.theme = key || 'pending';
    const names = ['授权与范围','核验行动','表达形态','证据取用','异议回应','组织与汇报'];
    const statuses = {observed:'有事实观察',partial:'部分证据或待复核',unknown:'暂无证据'};
    const points = core.constellation(state.report);
    points.forEach((point,i) => {
      const vertex = q('[data-vertex="'+i+'"]');
      vertex.dataset.status = point.status;
      vertex.querySelector('title').textContent = names[i]+'：'+statuses[point.status];
    });
    q('#star-description').textContent = points.map((point,i) => names[i]+'：'+statuses[point.status]).join('；')+'。六个顶点固定位置，仅显示证据状态，不表示性格强弱或分数。';
    q('[data-theme-rule]').textContent = key ? '本次立绘依据已绑定的行为记录自动呈现。逐浪对应受阻任务中的部分交付；合流对应通知任务两种授权下的交付适配；观潮对应任务中的实际核验。一次具备完整证据的场景即可触发逐浪或观潮；合流须具备授权配对。多种表现同时满足时依此顺序展示，评诗是文学题辞，不是心理测量结论。' : '本次证据尚不足以呈现特定立绘。旧版报告、模拟记录或未达到展示条件的结果均保留未定之相，不使用演示参数补齐。';
  }
  q('[data-import]').addEventListener('change',async event=>{
    const file=event.target.files[0];if(!file)return;
    try {
      if(file.size>core.maxBytes)throw new Error('报告文件不能超过 2 MB');
      const text=await file.text();core.importReport(state,text,schema,file.name);
      renderReport();renderArt();showError('');announce('已导入 '+file.name+'。原报告不会被写回。');
    } catch(error) {showError('导入失败，当前报告保持不变：'+error.message);}
    finally {event.target.value='';}
  });
  q('[data-export]').addEventListener('click',()=>{
    const url=URL.createObjectURL(new Blob([state.sourceText],{type:'application/json;charset=utf-8'}));
    const link=make('a');link.href=url;link.download='harness-profile-source.json';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
    announce('已导出当前报告的来源 JSON。');
  });
  renderReport();renderArt();
})();
