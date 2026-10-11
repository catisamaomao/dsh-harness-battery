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
  const themes = {
    explorer: {name:'逐浪', seal:'行', subtitle:'乘流而进',
      poem:['前路没通先开工','能修一段算一功','大饼还在锅里画','小菜先端不扑空'],
      alt:'逐浪：鲸鱼娘跨步前进，握着罗盘指向远方。'},
    observer: {name:'观潮', seal:'察', subtitle:'照水知微',
      poem:['风吹草动先存疑','掏出量尺量空气','你说差不多就行','她把差不多当题'],
      alt:'观潮：鲸鱼娘单膝俯身，用放大镜检视证据板。'},
    collaborator: {name:'合流', seal:'应', subtitle:'随约成章',
      poem:['开工先把暗号对','说好修门不拆柜','若要顺手摘星星','请把需求补到位'],
      alt:'合流：鲸鱼娘坐在共享绘图桌旁，向观看者递出画笔。'},
    pending: {name:'未定之相', seal:'待', subtitle:'潮声未定',
      poem:['线索不够别封神','水晶球里没缓存','鲸鱼正在搓小手','多看几回再变身']}
  };
  let state;
  try { state = core.createState(source, schema); }
  catch (error) { q('[data-error]').hidden = false; q('[data-error]').textContent = '内置报告校验失败：' + error.message; return; }
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
    q('#star-description').textContent = points.map((point,i) => names[i]+'：'+statuses[point.status]).join('；')+'。填色雷达轮廓为固定造型，仅节点显示证据状态，不表示性格强弱或分数。';
  }
  renderArt();
})();
