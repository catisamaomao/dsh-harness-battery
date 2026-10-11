window.__ModuleLoader__.load({
  id: 'dsh-harness-battery',
  factory(require) {
    const React = require('react');
    const h = React.createElement;
    const { useState, useEffect, useRef } = React;
    const PANEL = 'dsh-harness-battery';
    const API = 'api/dsh-harness-battery';
    const labels = { preparing: '准备中', running: '测试中', cancelling: '正在取消', cancelled: '已取消', completed: '已完成', failed: '未完成', interrupted: '已中断' };
    const css = `
      .whale-app{box-sizing:border-box;position:relative;isolation:isolate;z-index:1;width:100%;height:100%;overflow:auto;padding:28px;background:#f6f4ef;color:#22283b;color-scheme:light;font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
      .whale-app *{box-sizing:border-box}.whale-app button,.whale-app select,.whale-app input{font:inherit;color:inherit}.whale-app button,.whale-app select{border:1px solid #85809866;border-radius:8px;background:transparent;padding:8px 13px}.whale-app button{cursor:pointer}.whale-app button:disabled{opacity:.45;cursor:default}.whale-app button:focus-visible,.whale-app select:focus-visible,.whale-app summary:focus-visible{outline:2px solid #ae8fd8;outline-offset:3px}.whale-app option{background:#fff;color:#22283b}.whale-app h1{font-family:"Songti SC",serif;font-size:30px;letter-spacing:5px;margin:0}.whale-app h2{font-size:16px;margin:0 0 10px}.whale-app p{margin:8px 0}.whale-shell{max-width:1120px;margin:auto}.whale-head{margin-bottom:22px}.whale-muted{color:#676a76;font-size:12px}.whale-card{background:#fff;border:1px solid #85809840;border-radius:12px;padding:22px;margin-bottom:16px}.whale-start{display:flex;gap:24px;align-items:center;justify-content:space-between}.whale-start h2{font-size:21px;font-weight:500}.whale-start p{max-width:640px}.whale-field{display:block;margin:12px 0 7px}.whale-select{width:100%}.whale-memory{display:flex;gap:9px;align-items:start;margin-top:18px}.whale-memory input{margin-top:5px;accent-color:#8f6eb9}.whale-actions{display:flex;gap:8px;flex-wrap:wrap;flex:none}.whale-app .whale-primary{background:#79608f;color:white;border-color:#79608f;min-height:44px;padding:10px 22px}.whale-error{border-left:3px solid #d47489;padding:10px 13px;background:#fff0f1;overflow-wrap:anywhere}.whale-status{font-size:12px;color:#6c4986}.whale-progress{width:100%;accent-color:#9777b9}.whale-history{list-style:none;margin:0;padding:0;max-height:300px;overflow:auto}.whale-history li+li{border-top:1px solid #85809830}.whale-history button{display:block;width:100%;text-align:left;border:0;border-radius:0;padding:12px 0}.whale-history button[aria-pressed=true]{color:#6c4986}.whale-history small{display:block;color:#676a76}.whale-report{display:block;width:100%;height:calc(100vh - 100px);min-height:700px;border:1px solid #85809840;border-radius:12px;background:#fff}.whale-empty{display:flex;min-height:260px;align-items:center;justify-content:center;text-align:center;color:#626778}.whale-question{padding:10px 0;border-top:1px solid #85809830}.whale-question summary{cursor:pointer;font-size:12px}.whale-question pre{white-space:pre-wrap;font-family:inherit;font-size:12px;line-height:1.7;max-height:300px;overflow:auto;overflow-wrap:anywhere}.whale-questions{max-height:320px;overflow:auto}.whale-checks{margin:10px 0;padding-left:18px;font-size:12px}.whale-topline{display:flex;gap:12px;align-items:center;justify-content:space-between}.whale-app details>summary{cursor:pointer}.whale-app progress{height:7px}.whale-run{overflow-wrap:anywhere}.whale-more{margin:4px 0 22px}.whale-more>summary{display:inline-flex;min-height:40px;align-items:center;color:#676a76;font-size:13px;gap:8px;list-style:none}.whale-more>summary::-webkit-details-marker{display:none}.whale-more>summary:before{content:'＋';font-size:16px}.whale-more[open]>summary:before{content:'−'}.whale-more-body{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:8px}.whale-more-body .whale-card{margin:0}.whale-check-button{margin-top:10px}.whale-portrait{scroll-margin-top:18px}@media(max-width:700px){.whale-app{padding:18px 14px}.whale-start{align-items:flex-start;flex-direction:column;gap:12px}.whale-actions{width:100%}.whale-actions button{flex:1}.whale-more-body{grid-template-columns:1fr}.whale-report{height:900px;min-height:650px}.whale-card{padding:19px}.whale-topline{align-items:flex-start}}
    `;
    async function api(path, options = {}) {
      const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 65_000);
      try {
        const response = await fetch(API + path, { credentials: 'same-origin', ...options, signal: controller.signal });
        const data = await response.json();
        if (!response.ok || data.ok === false) throw new Error(data.error || `请求失败 (${response.status})`);
        return data;
      } finally { clearTimeout(timer); }
    }
    function WhaleIcon() {
      return h('svg', { viewBox: '0 0 24 24', width: 20, height: 20, fill: 'none', 'aria-hidden': true },
        h('path', { d: 'M3 13c0-4 3-7 8-7 4 0 6 2 7 5 2 0 3-1 4-3 1 4-1 7-4 8-3 5-11 5-14 1-1-1-1-2-1-4Z', stroke: 'currentColor', strokeWidth: 1.5 }),
        h('circle', { cx: 12, cy: 11, r: 1, fill: 'currentColor' }));
    }
    function Page() {
      const [state, setState] = useState(null); const [environment, setEnvironment] = useState(null);
      const [pack, setPack] = useState('core'); const [memory, setMemory] = useState(false);
      const [catalogue, setCatalogue] = useState(null); const [selection, setSelection] = useState(null);
      const [error, setError] = useState(''); const [busy, setBusy] = useState(false); const [checking, setChecking] = useState(false);
      const alive = useRef(true); const knownReports = useRef(null); const refreshSequence = useRef(0); const reportView = useRef(null);
      const refresh = async () => {
        const sequence = ++refreshSequence.current;
        const next = await api('/state');
        if (!alive.current || sequence !== refreshSequence.current) return;
        const reports = next.runs.filter(run => run.hasReport);
        const fresh = knownReports.current === null ? reports[0] : reports.find(run => !knownReports.current.has(run.id));
        knownReports.current = new Set(reports.map(run => run.id));
        setState(next);
        // A new result opens automatically. Ordinary polling preserves a manually selected older report.
        setSelection(previous => fresh?.id || (reports.some(run => run.id === previous) ? previous : reports[0]?.id || null));
      };
      const check = async () => {
        setChecking(true);
        try { const next = await api('/check'); if (alive.current) { setEnvironment(next.environment); setError(''); } }
        catch (cause) { if (alive.current) setError(cause.message); }
        finally { if (alive.current) setChecking(false); }
      };
      useEffect(() => {
        alive.current = true; let timer; let cancelled = false;
        const poll = async () => { try { await refresh(); } catch (cause) { if (alive.current) setError(cause.message); } if (!cancelled) timer = setTimeout(poll, 2000); };
        poll(); check();
        return () => { alive.current = false; cancelled = true; clearTimeout(timer); };
      }, []);
      useEffect(() => {
        let current = true; setCatalogue(null);
        const questionPack = state?.active?.battery || pack;
        api('/questions?battery=' + questionPack).then(data => { if (current) setCatalogue(data.catalogue); }).catch(cause => { if (current) setError(cause.message); });
        return () => { current = false; };
      }, [pack, state?.active?.battery]);
      const action = async (path, data) => {
        setBusy(true); setError('');
        try { await api(path, { method: 'POST', headers: { 'content-type': 'application/json', 'x-whale-token': state.token }, body: JSON.stringify(data) }); await refresh(); }
        catch (cause) { if (alive.current) setError(cause.message); }
        finally { if (alive.current) setBusy(false); }
      };
      const active = state?.active; const shownPack = active?.battery || pack; const shownMemory = active ? Boolean(active.includeMemory) : memory; const total = shownPack === 'core' ? 9 : 23;
      const selected = state?.runs.find(run => run.id === selection && run.hasReport);
      const checkEntries = Object.entries(environment?.checks || {});
      return h('div', { className: 'whale-app' }, h('style', null, css), h('div', { className: 'whale-shell' },
        h('header', { className: 'whale-head' }, h('h1', null, '鲸相')),
        error && h('p', { className: 'whale-error', role: 'alert' }, error),
        h('section', { className: 'whale-card whale-start', 'aria-label': '开始测试' },
          h('div', null,
            h('h2', null, selected ? '再认识它一点。' : '你的 Harness，是什么样？'),
            h('p', { className: 'whale-muted' }, `${shownPack === 'core' ? '核心版 · 9 题' : '扩展版 · 23 题'}，完成后自动展示画像。通过 DeepSeek 测试，按实际模型用量计费。`),
            h('p', { className: 'whale-muted' }, shownMemory ? '已选择记忆副本：开始后会复制本机 Memorix 记忆，并将测试选中的内容发送给 DeepSeek。' : '默认不带记忆。可在更多选项中开启。'),
            !active && (!environment?.can_run || !state?.supported) && h('p', { className: 'whale-status', role: 'status' }, checking || !state ? '正在准备…' : !state.supported ? '首版测试仅支持 macOS。' : '还需要完成环境配置，请展开更多选项查看。')
          ),
          h('div', { className: 'whale-actions' },
            h('button', { className: 'whale-primary', disabled: busy || !!active || !state?.supported || !environment?.can_run, onClick: () => action('/start', { battery: pack, includeMemory: memory, allowLive: true }) }, active ? '测试进行中' : '开始测试'),
            selected && h('button', { onClick: () => reportView.current?.scrollIntoView({ block: 'start' }) }, '查看画像')
          )
        ),
        active && h('section', { className: 'whale-card whale-run', 'aria-live': 'polite' },
          h('div', { className: 'whale-topline' }, h('div', null, h('h2', null, labels[active.status] || active.status), h('p', { className: 'whale-muted' }, active.message)), h('button', { disabled: busy || !['preparing', 'running'].includes(active.status), onClick: () => action('/cancel', { id: active.id }) }, '取消测试')),
          active.progress && h('div', null, h('progress', { className: 'whale-progress', value: active.progress.completed, max: active.progress.total }), h('p', { className: 'whale-muted' }, `${active.progress.completed} / ${active.progress.total} 题`))
        ),
        h('details', { className: 'whale-more' }, h('summary', null, '更多选项与记录'),
          h('div', { className: 'whale-more-body' },
            h('section', { className: 'whale-card', 'aria-label': '测试选项' },
              h('h2', null, '测试选项'), h('label', { className: 'whale-field', htmlFor: 'whale-battery' }, '题包'),
              h('select', { id: 'whale-battery', className: 'whale-select', value: shownPack, disabled: !!active || busy, onChange: event => setPack(event.target.value) }, h('option', { value: 'core' }, '核心版 · 9 题'), h('option', { value: 'extended' }, '扩展版 · 23 题')),
              h('p', { className: 'whale-muted' }, shownPack === 'core' ? '文档、计算、沟通、纠错与受阻任务。' : '增加租房、清单核对、来源冲突与活动筹备，含原 9 题。'),
              h('label', { className: 'whale-memory' }, h('input', { type: 'checkbox', checked: shownMemory, disabled: !!active || busy, onChange: event => setMemory(event.target.checked) }), h('span', null, '带上 Memorix 记忆副本')),
              h('p', { className: 'whale-muted' }, '默认关闭。开启后点击开始，会复制本机 Memorix 记忆，并将测试选中的内容发送给 DeepSeek；原记忆不会被测试改写。'),
              h('p', { className: 'whale-muted' }, `本次预算上限：${total * 30} 次模型请求，每次最多 16,384 输出 token。每题最长 10 分钟。`),
              h('p', { className: 'whale-muted' }, '点击开始即授权本次模型调用。测试在独立会话中进行。')
            ),
            h('section', { className: 'whale-card', 'aria-label': '环境检查' },
              h('div', { className: 'whale-topline' }, h('h2', null, '运行环境'), h('span', { className: 'whale-status' }, environment?.can_run ? '就绪' : checking ? '检查中…' : '待配置')),
              h('ul', { className: 'whale-checks' }, checkEntries.map(([key, value]) => h('li', { key }, key + '：' + (typeof value === 'object' ? value.message || value.status || (value.ok ? '可用' : '待配置') : value === true ? '可用' : value === false ? '待配置' : String(value))))),
              h('button', { className: 'whale-check-button', disabled: checking || busy, onClick: check }, checking ? '检查中…' : '重新检查'),
              h('p', { className: 'whale-muted' }, '0.1.2 · 探索版。新题的区分度仍待验证。')
            ),
            h('section', { className: 'whale-card' }, h('h2', null, `题目预览 · ${catalogue?.item_count || total} 题`), h('p', { className: 'whale-muted' }, '预览不会调用模型。'), h('div', { className: 'whale-questions' }, (catalogue?.items || []).map(item => h('details', { className: 'whale-question', key: item.id }, h('summary', null, item.id), h('pre', null, item.prompt))))),
            h('section', { className: 'whale-card' }, h('h2', null, '历史画像'), h('ul', { className: 'whale-history' }, (state?.runs || []).map(run => h('li', { key: run.id }, h('button', { disabled: !run.hasReport, 'aria-pressed': selection === run.id, onClick: () => setSelection(run.id) }, `${run.battery === 'core' ? '核心版' : '扩展版'} · ${labels[run.status] || run.status}`, h('small', null, new Date(run.startedAt).toLocaleString('zh-CN')), h('small', null, run.includeMemory ? '含记忆副本' : '独立基线'), run.message && h('small', null, run.message))))), !state?.runs.length && h('p', { className: 'whale-muted' }, '每次完成后，画像会保存在这里。'))
          )
        ),
        h('section', { ref: reportView, className: 'whale-portrait', 'aria-label': '画像报告' }, selected ? h('iframe', { key: selected.id, className: 'whale-report', title: '鲸相画像报告', src: API + '/report?id=' + encodeURIComponent(selected.id), sandbox: 'allow-scripts allow-downloads', referrerPolicy: 'no-referrer' }) : h('div', { className: 'whale-card whale-empty' }, h('div', null, h(WhaleIcon), h('p', null, '测试完成后，画像会出现在这里。'))))
      ));
    }
    return {
      name: 'dsh-harness-battery', inject: ['slots', 'layout'],
      apply(ctx) {
        // The renderer provides slots before the layout declares main during cold boot.
        // Slot injection also disposes and remounts this entry when its owner reloads.
        ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: PANEL }, Page));
        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({ name: 'sidebar.panellist', id: PANEL, order: 140, label: '鲸相' }, WhaleIcon));
      },
    };
  },
});
