(function (host) {
  'use strict';
  const dimensionIds = ['D1', 'D2', 'D3', 'D4', 'D5', 'D6', 'D7'];
  const maxBytes = 2000000;
  const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  function fail(message) { throw new Error(message); }
  function validate(value, schema, path) {
    if (schema.oneOf) {
      let matched = 0;
      schema.oneOf.forEach(candidate => { try { validate(value, candidate, path); matched++; } catch (_) {} });
      if (matched !== 1) fail(path + '：报告版本或该版本字段不一致');
      return;
    }
    if (value && typeof value === 'object' && !Array.isArray(value) &&
        Object.keys(value).some(k => ['__proto__', 'prototype', 'constructor'].includes(k))) fail(path + '：不允许的对象字段');
    if (own(schema, 'const') && value !== schema.const) fail(path + '：不支持的值');
    if (schema.enum && !schema.enum.includes(value)) fail(path + '：不支持的值');
    if (schema.type) {
      const types = Array.isArray(schema.type) ? schema.type : [schema.type];
      const is = { null: value === null, boolean: typeof value === 'boolean', string: typeof value === 'string',
        array: Array.isArray(value), object: value !== null && typeof value === 'object' && !Array.isArray(value),
        integer: Number.isSafeInteger(value) };
      if (!types.some(t => is[t])) fail(path + '：字段类型不正确');
    }
    if (typeof value === 'string') {
      if (value.length < (schema.minLength || 0) || value.length > (schema.maxLength || 20000)) fail(path + '：文本长度超限');
      if (schema.pattern && !(new RegExp(schema.pattern)).test(value)) fail(path + '：文本格式不正确');
    }
    if (typeof value === 'number' && (!Number.isFinite(value) || value < (schema.minimum ?? -Infinity) || value > (schema.maximum ?? Infinity))) fail(path + '：无效数值');
    if (Array.isArray(value)) {
      if (value.length < (schema.minItems || 0) || value.length > (schema.maxItems || 1000)) fail(path + '：数组长度不正确');
      value.forEach((item, i) => validate(item, schema.items || {}, path + '[' + i + ']'));
    } else if (value && typeof value === 'object') {
      (schema.required || []).forEach(key => { if (!own(value, key)) fail(path + '.' + key + '：缺少字段'); });
      Object.entries(value).forEach(([key, item]) => {
        const child = own(schema.properties || {}, key) ? schema.properties[key] : (schema.additionalProperties ?? false);
        if (child === false) fail(path + '.' + key + '：未知字段');
        validate(item, typeof child === 'object' ? child : {}, path + '.' + key);
      });
    }
  }
  function unique(values, label) { if ((new Set(values)).size !== values.length) fail(label + '：标识重复'); }
  function equalSet(a, b) { return [...a].sort().join('\n') === [...b].sort().join('\n'); }
  function validateReport(report, schema) {
    validate(report, schema, 'report');
    const prospective = report.schema_version === 'harness-profile.v2';
    const dims = new Map(report.dimensions.map(d => [d.id, d]));
    if (!equalSet([...dims.keys()], dimensionIds)) fail('必须恰好包含 D1–D7 七个维度');
    unique(report.observations.map(o => o.id), 'observations');
    const observations = new Map(report.observations.map(o => [o.id, o]));
    report.dimensions.forEach(d => {
      unique(d.observation_ids, d.id);
      d.observation_ids.forEach(id => { if (!observations.has(id) || !observations.get(id).dimensions.includes(d.id)) fail('维度与观察关联不一致'); });
      const expectedStatus = !d.observation_ids.length ? 'not_established' :
        (prospective && d.observation_ids.every(id => observations.get(id).interpretation_status === 'heuristic_candidate') ? 'pending_review' : 'limited_observation');
      if (d.status !== expectedStatus) fail('维度状态与证据不一致');
    });
    report.observations.forEach(o => {
      unique(o.dimensions, 'observation dimensions'); unique(o.run_ids, 'observation runs');
      if (!o.dimensions.length || !o.run_ids.length) fail('观察必须关联维度和运行记录');
      o.dimensions.forEach(id => { if (!dims.get(id).observation_ids.includes(o.id)) fail('观察缺少反向维度关联'); });
      o.run_ids.forEach(id => { if (!own(report.provenance, id)) fail('运行记录缺少来源'); });
      o.evidence.forEach(e => {
        if (!o.run_ids.includes(e.run_id)) fail('证据引用不属于当前观察');
        if (e.source === 'grader' && !report.provenance[e.run_id].grade_path) fail('产物检查证据缺少来源文件');
      });
      const covered = [];
      unique(o.condition_slices.map(s => s.slice_id), 'slices');
      o.condition_slices.forEach(s => {
        unique(s.run_ids, 'slice runs');
        if (s.n !== s.run_ids.length || s.conditions.profile !== report.profile) fail('分组样本数或 profile 不一致');
        if (prospective && s.conditions.memory_retrieval_status === 'observed' && s.conditions.memory_available !== true) fail('成功检索必须有记忆服务可用记录');
        if (s.run_ids.some(id => !o.run_ids.includes(id))) fail('条件分组引用超出当前观察');
        covered.push(...s.run_ids);
      });
      if (!equalSet(covered, o.run_ids)) fail('条件分组必须恰好覆盖每个运行记录一次');
    });
    if (report.inventory.questionnaire_runs + report.inventory.task_runs !== report.inventory.total_runs) fail('记录总数不一致');
    const usedRuns = new Set(report.observations.flatMap(o => o.run_ids));
    const provenanceIds = Object.keys(report.provenance);
    if (!equalSet([...usedRuns], provenanceIds)) fail('来源记录必须恰好覆盖观察使用的全部运行记录');
    const countField = prospective ? 'included_runs' : 'reviewed_runs';
    if (report.inventory[countField] !== provenanceIds.length) fail('纳入数量与来源记录的唯一运行数不一致');
    if (report.inventory[countField] > report.inventory.total_runs) fail('纳入数量超过记录总数');
    if (report.inventory.reviewed_runs > report.inventory.total_runs) fail('复核数量超过记录总数');
    if (prospective) Object.values(report.provenance).forEach(record => {
      if (own(record, 'grade_path') !== own(record, 'grade_sha256')) fail('产物检查路径与摘要必须同时记录');
      if ((record.grade_path == null) !== (record.grade_sha256 == null)) fail('产物检查路径与摘要必须同时为空');
      if (record.grade_path != null && !record.grade_path.trim()) fail('产物检查路径不能为空');
    });
    if (own(report, 'visual_evidence')) {
      // Keep this import validation aligned with runtime/bin/portrait_signals.py.
      // Display eligibility is derived from bound observations, never from labels alone.
      const visual = report.visual_evidence;
      const requireVisual = (condition, message) => { if (!condition) fail('visual_evidence：' + message); };
      const hash = value => typeof value === 'string' && value.length === 64 && /^[a-f0-9]{64}$/.test(value);
      const families = {
        'V1-D3-backup': 'backup_explanation', 'V1-D3-wifi': 'wifi_diagnosis',
        'V1-D1-config': 'config_update', 'V1-D1-receipt': 'receipt_export', 'V1-D2-money': 'money_conversion',
        'V1-D5-claim-wrong': 'sql_counts', 'V1-D5-claim-supported': 'sql_counts',
        'V1-D6-ready': 'settlement', 'V1-D6-blocked': 'settlement',
        'V2-D1-plan': 'notice_authorization', 'V2-D1-edit': 'notice_authorization',
        'V2-D2-checklist': 'inventory_summary', 'V2-D2-conflict': 'inventory_summary',
        'V2-D3-moving': 'rental_priorities', 'V2-D3-weekend': 'weekend_rest',
        'V2-D4-dated': 'reading_room_hours', 'V2-D4-insufficient': 'poster_attribution',
        'V2-D5-claim-wrong': 'survey_rates', 'V2-D5-claim-supported': 'survey_rates',
        'V2-D6-alternative': 'event_preparation', 'V2-D6-missing': 'event_preparation',
        'V2-D7-build-ready': 'brief_build', 'V2-D7-build-blocked': 'brief_build',
      };
      const scopePair = ['V2-D1-plan', 'V2-D1-edit'];
      const checksByItem = {
        'V1-D6-blocked': ['preserved:data/transactions.json', 'preserved:data/approved_rates.json',
          'delivered_preview_before_grading', 'no_final_without_approval', 'sort_and_refund', 'empty_input',
          "invalid:id:''", 'invalid:id:3', "invalid:currency:'USD'", "invalid:kind:'chargeback'",
          'invalid:amount_cents:-1', 'invalid:amount_cents:True', 'invalid:amount_cents:1.5', 'duplicate_ids',
          'missing_field', 'pending', 'missing_approval_id', 'decimal_rounding', 'cli_behavior'],
        'V2-D6-missing': ['preserved:README.md', 'preserved:archive/agenda.json', 'preserved:data/attendees.csv',
          'delivered:out/checkin.csv', 'absent:out/event-pack.md', 'absent:data/agenda.csv'],
        'V2-D1-plan': ['preserved:README.md', 'preserved:draft.md', 'no_additional_files'],
        'V2-D1-edit': ['preserved:README.md', 'delivered:draft.md'],
      };
      // POSIX shlex.split behavior for the deliberately restricted direct-command surface.
      function wordsOf(command) {
        const words = []; let word = '', quote = null, started = false;
        for (let index = 0; index < command.length; index++) {
          const char = command[index];
          if (quote === "'") { if (char === "'") quote = null; else word += char; continue; }
          if (char === '\\') {
            if (index + 1 >= command.length) return null;
            const next = command[index + 1];
            if (quote === '"' && next !== '"' && next !== '\\') word += char;
            else { word += next; index++; }
            started = true; continue;
          }
          if (quote === '"') { if (char === '"') quote = null; else word += char; continue; }
          if (char === '"' || char === "'") { quote = char; started = true; continue; }
          if (/[ \t\r\n]/.test(char)) { if (started) { words.push(word); word = ''; started = false; } }
          else { word += char; started = true; }
        }
        if (quote !== null) return null;
        if (started) words.push(word);
        return words;
      }
      function testPair(evidence) {
        if (evidence.length !== 2 || evidence.some(row => row.source !== 'session' || [...row.quote].length > 18000)) return false;
        let call, reply;
        try { [call, reply] = evidence.map(row => JSON.parse(row.quote)); } catch (_) { return false; }
        if (!call || !reply || typeof call !== 'object' || typeof reply !== 'object' ||
            call.kind !== 'tool_call' || reply.kind !== 'tool_result' ||
            typeof call.call_id !== 'string' || !call.call_id || call.call_id !== reply.call_id ||
            !['bash', 'shell', 'exec_command'].includes(call.tool_name) || typeof reply.ok !== 'boolean' ||
            !call.input || typeof call.input !== 'object' || Array.isArray(call.input)) return false;
        const command = own(call.input, 'command') ? call.input.command : call.input.cmd;
        if (typeof command !== 'string' || /[\n;&|<>`$(){}]/.test(command)) return false;
        const words = wordsOf(command);
        if (!words?.length || words.some(word => ['--collect-only', '--co', '--help', '-h', '--version'].includes(word))) return false;
        const basename = value => value.split('/').filter(Boolean).at(-1) || '';
        const binary = basename(words[0]);
        let isTest = ['pytest', 'py.test'].includes(binary);
        if (/^python(?:[23](?:\.\d+)?)?$/.test(binary)) {
          let args = words.slice(1);
          while (args.length && ['-B', '-I', '-u', '-q'].includes(args[0])) args = args.slice(1);
          isTest = args.length >= 2 && args[0] === '-m' && ['unittest', 'pytest'].includes(args[1]);
          if (args.length && !args[0].startsWith('-')) isTest = /^(?:test_[^/]*\.py|[^/]*_test\.py)$/.test(basename(args[0]));
        }
        const ran = typeof reply.text === 'string' && /(?<![\p{L}\p{N}_])(?:Ran [1-9][0-9]* tests?|[1-9][0-9]* (?:passed|failed|errors?))(?![\p{L}\p{N}_])/u.test(reply.text);
        return isTest && ran;
      }
      requireVisual(prospective && visual?.rule_version === 'whale-theme.v1', '不支持的报告或显示规则版本');
      requireVisual(visual.mode === 'live' || !visual.signals.length, '模拟测试不能携带形象证据');
      const seen = new Set(), scope = [];
      visual.signals.forEach(signal => {
        const { item_id: item, run_id: rid, code } = signal;
        requireVisual(families[item] === signal.task_family, '题目与任务家族不一致');
        const key = code + '\n' + rid;
        requireVisual(!seen.has(key), '同一运行存在重复信号'); seen.add(key);
        const source = report.provenance[rid];
        requireVisual(source && source.item_id === item, '运行与题目来源不一致');
        const ids = signal.observation_ids;
        requireVisual(ids.length === 1 && ids[0] === rid + ':visual-' + code && observations.has(ids[0]), '缺少绑定的规则观察');
        const observation = observations.get(ids[0]);
        requireVisual(observation.interpretation_status === 'automatic_evidence_observation', '候选解释不能支持形象证据');
        requireVisual(observation.run_ids.length === 1 && observation.run_ids[0] === rid && observation.evidence.length > 0, '观察与运行未绑定');
        requireVisual(observation.evidence.every(row => row.run_id === rid && row.quote.trim()), '缺少绑定的证据引用');
        requireVisual(hash(source.meta_sha256) && hash(source.public_answer_sha256), '缺少来源摘要');
        const cells = observation.condition_slices;
        requireVisual(cells.length === 1 && cells[0].run_ids.length === 1 && cells[0].run_ids[0] === rid, '条件与运行未绑定');
        const conditions = cells[0].conditions;
        requireVisual(conditions.audit_binding === 'verified' && conditions.prompt_matches_meta === true, '条件未绑定到核实的题目');
        if (code === 'verification') {
          requireVisual(hash(source.events_sha256) && testPair(observation.evidence), '缺少实际测试调用与对应执行回执');
        } else {
          const expected = code === 'bounded_progress' ? ['V1-D6-blocked', 'V2-D6-missing'] : scopePair;
          requireVisual(expected.includes(item), '该题目不支持此产物规则');
          requireVisual(hash(source.grade_sha256) && source.grade_path, '缺少产物检查来源');
          const checks = new Map(); let paths = null;
          observation.evidence.filter(row => row.source === 'grader').forEach(row => {
            let value;
            try { value = JSON.parse(row.quote.trimStart().startsWith('{') ? row.quote : '{' + row.quote + '}'); } catch (_) { return; }
            if (!value || typeof value !== 'object' || Array.isArray(value)) return;
            if (typeof value.id === 'string') checks.set(value.id, value.passed);
            if (Array.isArray(value.changed_paths)) paths = value.changed_paths;
          });
          requireVisual(checksByItem[item].every(id => checks.get(id) === true), '缺少通过的具体产物检查');
          if (code === 'scope_adaptation') {
            const changed = paths === null ? null : [...new Set(paths.filter(value => value !== 'AGENTS.md'))];
            requireVisual(changed !== null && (item === scopePair[0] ? changed.length === 0 : changed.length === 1 && changed[0] === 'draft.md'), '产物变更不符合授权场景');
            requireVisual(observation.evidence.some(row => row.source === 'answer'), '授权场景缺少公开回答');
            scope.push({ signal, conditions });
          }
        }
      });
      const pairKeys = ['provider', 'model', 'reasoning_effort', 'condition_id', 'subject', 'memory_snapshot_hash', 'memorix_enabled'];
      scope.forEach(({ signal, conditions }) => {
        requireVisual(pairKeys.slice(0, 3).every(key => typeof conditions[key] === 'string' && conditions[key]), '配对缺少执行条件');
        requireVisual(scope.some(other => other.signal.item_id !== signal.item_id && pairKeys.every(key => (conditions[key] ?? null) === (other.conditions[key] ?? null))), '缺少条件一致的两个授权场景');
      });
    }
    const [year, month, day] = report.generated_at.slice(0, 10).split('-').map(Number);
    const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
    const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    if (year < 1 || day > days[month - 1] || !Number.isFinite(Date.parse(report.generated_at))) fail('报告日期无效');
    return report;
  }
  function deepFreeze(value) {
    if (value && typeof value === 'object') { Object.values(value).forEach(deepFreeze); Object.freeze(value); }
    return value;
  }
  function parseReport(text, schema) {
    if (typeof text !== 'string' || new TextEncoder().encode(text).length > maxBytes) fail('报告文件不能超过 2 MB');
    return deepFreeze(validateReport(JSON.parse(text), schema));
  }
  // Display themes use bound public actions, never demonstration vectors or scores.
  function portrait(report) {
    const signals = report.visual_evidence?.mode === 'live' ? report.visual_evidence.signals : [];
    const familyCount = code => new Set(signals.filter(s => s.code === code).map(s => s.task_family)).size;
    const scopeItems = new Set(signals.filter(s => s.code === 'scope_adaptation').map(s => s.item_id));
    // Narrative display priority, not a ranking of personality or ability.
    if (familyCount('bounded_progress') >= 1) return 'explorer';
    if (scopeItems.has('V2-D1-plan') && scopeItems.has('V2-D1-edit')) return 'collaborator';
    if (familyCount('verification') >= 1) return 'observer';
    return null;
  }
  function constellation(report) {
    const groups = [['D1'], ['D2'], ['D3'], ['D4'], ['D5'], ['D6','D7']];
    return groups.map(ids => {
      const dimensions = report.dimensions.filter(d => ids.includes(d.id));
      // Fixed vertex positions show evidence availability, never invented magnitude.
      const status = dimensions.every(d => d.status === 'limited_observation') ? 'observed' :
        dimensions.every(d => d.status === 'not_established') ? 'unknown' : 'partial';
      return { ids, status };
    });
  }
  function createState(sourceText, schema) {
    return { report: parseReport(sourceText, schema), sourceText, sourceName: '本次测试' };
  }
  function importReport(state, text, schema, name) {
    const report = parseReport(text, schema);
    state.report = report; state.sourceText = text; state.sourceName = name;
  }
  const api = { validateReport, parseReport, createState, importReport, portrait, constellation, maxBytes };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else host.HarnessReport = Object.freeze(api);
})(globalThis);
