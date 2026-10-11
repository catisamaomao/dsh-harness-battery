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
  function validateDemo(values) {
    if (!Array.isArray(values) || values.length !== 6 || !values.every(v => typeof v === 'number' && Number.isFinite(v) && v >= 0 && v <= 100)) fail('演示参数必须是六个 0–100 数值');
    return [...values];
  }
  function createState(sourceText, schema, demoValues) {
    return { mode: 'report', report: parseReport(sourceText, schema), sourceText,
      sourceName: '内置报告', cover: 'explorer', demo: { values: validateDemo(demoValues) } };
  }
  function importReport(state, text, schema, name) {
    const report = parseReport(text, schema); // Validate completely before replacing visible state.
    state.report = report; state.sourceText = text; state.sourceName = name; state.mode = 'report';
  }
  function setDemoValues(state, values) { state.demo.values = validateDemo(values); }
  const api = { validateReport, parseReport, createState, importReport, setDemoValues, maxBytes };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else host.HarnessReport = Object.freeze(api);
})(globalThis);
