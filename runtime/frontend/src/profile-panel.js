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
      poem:['欧拉欧拉先开冲','主线锁住绕路通','这波不是我太勇','俺寻思之力发动'],
      alt:'逐浪：鲸鱼娘跨步前进，握着罗盘指向远方。'},
    observer: {name:'观潮', seal:'察', subtitle:'照水知微',
      poem:['真相只有一个喵','疑点一个都别跑','你说已经大丈夫','她说再验亿遍好'],
      alt:'观潮：鲸鱼娘单膝俯身，用放大镜检视证据板。'},
    collaborator: {name:'合流', seal:'应', subtitle:'随约成章',
      poem:['契约之内我全包','隐藏支线先别抛','不是傲娇不帮忙','跨服需求达咩哟'],
      alt:'合流：鲸鱼娘坐在共享绘图桌旁，向观看者递出画笔。'},
    pending: {name:'未定之相', seal:'待', subtitle:'潮声未定',
      poem:['能力面板未解锁','战力厨先别开火','不是杂鱼是伏笔','下集一定别催我']}
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
    const axes = [
      {name:'自主推进', low:'先询问', high:'先行动', explanation:'在已明确授权的任务里，是再确认后行动，还是直接推进。'},
      {name:'核验投入', low:'直接交付', high:'多角度核验', explanation:'看公开过程中的检查方式与角度；检查失败也算投入。'},
      {name:'表达详略', low:'简短直给', high:'多层展开', explanation:'看回答展开了多少理由、步骤、例子与分支，不评价答案好坏。'},
      {name:'依据展开', low:'结论为主', high:'说明依据', explanation:'看是否解释具体来源如何支持结论，以及材料的适用范围。'},
      {name:'异议直率', low:'委婉提示', high:'直截了当', explanation:'在确实纠正错误前提时，看表达多直接；没有实质异议则暂不判读。'},
      {name:'分段推进', low:'等条件齐', high:'先做可做的', explanation:'条件受阻时，是整体等待，还是先启动获授权的独立部分。'}
    ];
    const points = core.tendency(state.report);
    const geometry = core.radarGeometry(points.map(point => point.value));
    const svg = (tag, attributes, text) => {
      const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
      Object.entries(attributes).forEach(([name,value]) => node.setAttribute(name, value));
      if (text !== undefined) node.textContent = text;
      return node;
    };
    const description = points.map((point,i) => {
      const axis = axes[i];
      const reading = point.value === null ? '未判定，不按零分处理' :
        point.value+'/100，'+point.n+' 次可判读记录';
      return axis.name+'：'+axis.low+' → '+axis.high+'；'+reading+'。'+axis.explanation;
    });
    points.forEach((point,i) => {
      const vertex = q('[data-vertex="'+i+'"]');
      vertex.dataset.status = point.value === null ? 'unknown' : 'scored';
      const label = vertex.querySelector('text');
      label.textContent = axes[i].name;
      label.append(svg('tspan', {class:'axis-reading', x:label.getAttribute('x'),dy:15},
        point.value === null ? '未定' : String(point.value)));
      vertex.querySelector('title').textContent = description[i];
    });
    const fill = q('[data-radar-fill]');
    fill.toggleAttribute('hidden', !geometry.closed);
    if (geometry.closed) fill.setAttribute('points', geometry.fillPoints);
    else fill.removeAttribute('points');
    q('[data-radar-edges]').replaceChildren(...geometry.segments.map(segment =>
      svg('line', {class:'radar-edge',x1:segment.from.x,y1:segment.from.y,x2:segment.to.x,y2:segment.to.y})));
    q('[data-radar-nodes]').replaceChildren(...geometry.nodes.map(point => {
      const node = svg('circle', {class:'radar-node',cx:point.x,cy:point.y,r:5});
      node.append(svg('title', {}, description[point.index]));
      return node;
    }));
    const known = points.filter(point => point.value !== null).length;
    q('[data-radar-note]').textContent = known === 6 ? '本次倾向 · AI 判读 · 高低无好坏' :
      known ? '本次倾向 · '+(6-known)+' 项未定，轮廓暂不闭合' : '尚未判读 · 空缺不代表低分';
    q('#star-description').textContent = description.join('；')+'。外圈为刻度上限100，顶点距离按读数等比例变化；仅六项均有读数时填色。属于本次公开行为的AI判读，不代表稳定人格。';
  }
  renderArt();
})();
