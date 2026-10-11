"""Shared strict v1/v2 schemas and referential checks for the offline report UI."""
import copy
import importlib.util
import math
from pathlib import Path
import re
from datetime import datetime

MAX_REPORT_BYTES = 2_000_000
DIMENSIONS = [f"D{i}" for i in range(1, 8)]
_portrait_spec = importlib.util.spec_from_file_location('portrait_signal_validation',
    Path(__file__).resolve().parents[2] / 'bin/portrait_signals.py')
_portrait = importlib.util.module_from_spec(_portrait_spec)
_portrait_spec.loader.exec_module(_portrait)


def obj(properties, required=None, additional=False):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": additional}


def arr(items, maximum=1000):
    return {"type": "array", "items": items, "maxItems": maximum}


TEXT = {"type": "string", "maxLength": 20000}
NAME = {"type": "string", "minLength": 1, "maxLength": 200}
ID = {"type": "string", "pattern": r"^[A-Za-z0-9_.:-]{1,200}$"}
HASH = {"type": "string", "pattern": r"^[a-f0-9]{64}$"}
NULL = {"type": "null"}
BOOL_NULL = {"type": ["boolean", "null"]}
TEXT_NULL = {"type": ["string", "null"], "maxLength": 20000}
NONNEG = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
STRINGS = arr(TEXT)
IDS = arr(ID)

INJECTION = obj({"status": {"enum": ["observed", "none_observed", "unknown"]},
                 "type": NAME, "digests": STRINGS, "source": NAME, "evidence_issue": TEXT_NULL})
CONDITION = obj({
    "profile": NAME, "prompt_hash": TEXT_NULL, "prompt_hash_source": NAME,
    "declared_prompt_hash": TEXT_NULL, "prompt_matches_meta": BOOL_NULL,
    "provider": TEXT_NULL, "model": TEXT_NULL, "reasoning_effort": TEXT_NULL,
    "tools_exposed": {"type": ["array", "null"], "items": TEXT, "maxItems": 1000},
    "tool_rows_unknown": {"type": ["array", "null"], "items": TEXT, "maxItems": 1000},
    "row_states": {"type": ["object", "null"], "additionalProperties": TEXT},
    "agent_instructions_row": TEXT_NULL, "injection": INJECTION, "workspace": TEXT_NULL,
    "workspace_has_agents_md": BOOL_NULL, "workspace_content_hash": TEXT_NULL,
    "dsh_home": TEXT_NULL, "profile_patch_hash": TEXT_NULL, "extra_patch": TEXT_NULL,
    "extra_patch_hash": TEXT_NULL, "memorix_enabled": BOOL_NULL, "memorix_data_dir": TEXT_NULL,
    "memory_snapshot_hash": TEXT_NULL, "config_dump_sha256": TEXT_NULL,
})
# Optional additive v1 field: older exported reports remain structurally readable.
# Missing binding never implies verified provenance.
CONDITION['properties']['audit_binding'] = {'enum': [
    'verified', 'unbound_audit', 'stale_audit_session', 'stale_audit_metadata', 'not_provided']}
SLICE = obj({"slice_id": ID, "conditions": CONDITION, "missing_conditions": STRINGS,
             "run_ids": IDS, "n": NONNEG})
EVIDENCE = obj({"run_id": ID, "quote": TEXT, "source": {"enum": ["answer", "session"]},
                "session_line": {"type": "integer", "minimum": 1}},
               required=["run_id", "quote", "source"])
OBSERVATION = obj({"id": ID, "dimensions": arr({"enum": DIMENSIONS}, 7), "title": NAME,
                   "statement": TEXT, "limits": TEXT, "protocol": NAME,
                   "expected_prompt_sha256": HASH, "run_ids": IDS,
                   "evidence": arr(EVIDENCE), "condition_slices": arr(SLICE),
                   "pooled_effect_estimate": NULL,
                   "interpretation_status": {"const": "reviewed_descriptive_observation"}})
DIMENSION = obj({"id": {"enum": DIMENSIONS}, "label": NAME, "score": NULL, "grade": NULL,
                 "status": {"enum": ["limited_observation", "not_established"]},
                 "observation_ids": IDS, "note": TEXT})
PROVENANCE = obj({"meta": TEXT, "session_id": TEXT_NULL, "meta_sha256": HASH,
                  "events_sha256": TEXT_NULL, "public_answer_sha256": TEXT_NULL,
                  "session_log_sha256": TEXT_NULL, "config_dump_sha256": TEXT_NULL})
V1_SCHEMA = obj({
    "schema_version": {"const": "harness-profile.v1"}, "profile": NAME,
    "generated_at": {"type": "string", "pattern": r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$"},
    "scope": {"const": "exploratory_descriptive_profile"},
    "causal_attribution": {"const": "not_established"}, "character_assignment": NULL,
    "dimensions": {"type": "array", "items": DIMENSION, "minItems": 7, "maxItems": 7},
    "observations": arr(OBSERVATION),
    "inventory": obj({"questionnaire_runs": NONNEG, "task_runs": NONNEG, "total_runs": NONNEG,
                       "reviewed_runs": NONNEG, "audit_status": obj({}, [], NONNEG),
                       "prompt_integrity": obj({}, [], NONNEG)}),
    "prompt_mismatches": arr(obj({"run_id": ID, "actual_prompt_sha256": HASH})),
    "provenance": obj({}, [], PROVENANCE), "limitations": STRINGS,
})

# V2 records prospective, automatically extracted evidence. It cannot impersonate
# the human-reviewed v1 inventory or its interpretation status.
V2_SCHEMA = copy.deepcopy(V1_SCHEMA)
v2 = V2_SCHEMA['properties']
v2['schema_version'] = {'const': 'harness-profile.v2'}
v2['scope'] = {'const': 'prospective_behavior_profile'}
v2['inventory']['properties']['included_runs'] = NONNEG
v2['inventory']['required'].append('included_runs')
v2['inventory']['properties']['reviewed_runs'] = {'type': 'integer', 'const': 0}
v2['dimensions']['items']['properties']['status']['enum'].append('pending_review')
v2obs = v2['observations']['items']['properties']
v2obs['interpretation_status'] = {'enum': ['automatic_evidence_observation', 'heuristic_candidate']}
v2obs['rubric_version'] = NAME
v2obs['evidence']['items']['properties']['source']['enum'].append('grader')
v2condition = v2obs['condition_slices']['items']['properties']['conditions']
v2condition['properties'].update(condition_id=NAME, subject=NAME)
v2condition['required'].extend(['condition_id', 'subject'])
v2condition['properties'].update(
    memory_runtime_status=TEXT_NULL, memory_available=BOOL_NULL,
    memory_retrieval_status={'enum': ['observed', 'not_observed', 'unknown']})
v2['provenance']['additionalProperties']['properties'].update(
    item_id=ID,
    grade_path=TEXT_NULL,
    grade_sha256={'type': ['string', 'null'], 'pattern': r'^[a-f0-9]{64}$'})
v2['visual_evidence'] = obj({
    'rule_version': {'const': _portrait.RULE_VERSION}, 'mode': {'enum': ['live', 'mock']},
    'signals': arr(obj({'code': {'enum': list(_portrait.PRIORITY)}, 'item_id': ID,
        'task_family': {'enum': sorted(set(_portrait.TASK_FAMILIES.values()))}, 'run_id': ID,
        'observation_ids': {'type': 'array', 'items': ID, 'minItems': 1, 'maxItems': 1}})),
})
SCHEMA = {'oneOf': [V1_SCHEMA, V2_SCHEMA]}


def _validate(value, schema, path):
    if 'oneOf' in schema:
        matched = 0
        for candidate in schema['oneOf']:
            try:
                _validate(value, candidate, path)
                matched += 1
            except ValueError:
                pass
        if matched != 1:
            raise ValueError(f'{path}: unsupported or inconsistent report version')
        return
    if isinstance(value, dict) and any(k in ("__proto__", "prototype", "constructor") for k in value):
        raise ValueError(f"{path}: forbidden object key")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path}: unsupported constant")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path}: unsupported value")
    kind = schema.get("type")
    if kind:
        kinds = kind if isinstance(kind, list) else [kind]
        matches = {"null": value is None, "boolean": type(value) is bool,
                   "string": isinstance(value, str), "array": isinstance(value, list),
                   "object": isinstance(value, dict), "integer": type(value) is int}
        if not any(matches.get(k, False) for k in kinds):
            raise ValueError(f"{path}: expected {kind}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 20000):
            raise ValueError(f"{path}: text length out of range")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            raise ValueError(f"{path}: invalid text format")
    if type(value) in (int, float):
        if (not math.isfinite(value) or value < schema.get("minimum", float("-inf"))
                or value > schema.get("maximum", float("inf"))):
            raise ValueError(f"{path}: invalid number")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 1000):
            raise ValueError(f"{path}: invalid array length")
        for i, item in enumerate(value):
            _validate(item, schema.get("items", {}), f"{path}[{i}]")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{path}.{key}: required")
        properties = schema.get("properties", {})
        for key, item in value.items():
            child = properties.get(key, schema.get("additionalProperties", False))
            if child is False:
                raise ValueError(f"{path}.{key}: unknown field")
            _validate(item, child if isinstance(child, dict) else {}, f"{path}.{key}")


def _unique(values, name):
    if len(set(values)) != len(values):
        raise ValueError(f"{name}: duplicate identifier")


def validate_report(report):
    _validate(report, SCHEMA, "report")
    prospective = report['schema_version'] == 'harness-profile.v2'
    try:
        datetime.fromisoformat(report["generated_at"].replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid report date") from error
    dims = {d["id"]: d for d in report["dimensions"]}
    if sorted(dims) != DIMENSIONS:
        raise ValueError("exactly D1 through D7 are required")
    observations = {o["id"]: o for o in report["observations"]}
    _unique([o["id"] for o in report["observations"]], "observations")
    provenance = report["provenance"]
    for dim in report["dimensions"]:
        _unique(dim["observation_ids"], dim["id"])
        for oid in dim["observation_ids"]:
            if oid not in observations or dim["id"] not in observations[oid]["dimensions"]:
                raise ValueError("dimension observation reference is inconsistent")
        expected_status = 'not_established'
        if dim['observation_ids']:
            expected_status = ('pending_review' if prospective and all(
                observations[oid]['interpretation_status'] == 'heuristic_candidate'
                for oid in dim['observation_ids']) else 'limited_observation')
        if dim['status'] != expected_status:
            raise ValueError("dimension status does not match its evidence")
    for observation in report["observations"]:
        _unique(observation["dimensions"], "observation dimensions")
        _unique(observation["run_ids"], "observation runs")
        if not observation["dimensions"] or not observation["run_ids"]:
            raise ValueError("observation needs dimension and run references")
        for did in observation["dimensions"]:
            if observation["id"] not in dims[did]["observation_ids"]:
                raise ValueError("observation is missing its reverse dimension reference")
        for rid in observation["run_ids"]:
            if rid not in provenance:
                raise ValueError("observation run is missing provenance")
        for evidence in observation["evidence"]:
            if evidence["run_id"] not in observation["run_ids"]:
                raise ValueError("evidence run is outside its observation")
            if evidence['source'] == 'grader' and not provenance[evidence['run_id']].get('grade_path'):
                raise ValueError('grader evidence is missing its source artifact')
        covered = []
        _unique([s["slice_id"] for s in observation["condition_slices"]], "slices")
        for cell in observation["condition_slices"]:
            _unique(cell["run_ids"], "slice runs")
            if cell["n"] != len(cell["run_ids"]) or cell["conditions"]["profile"] != report["profile"]:
                raise ValueError("slice count or profile mismatch")
            if (prospective and cell['conditions'].get('memory_retrieval_status') == 'observed'
                    and cell['conditions'].get('memory_available') is not True):
                raise ValueError('successful retrieval requires observed memory availability')
            if any(r not in observation["run_ids"] for r in cell["run_ids"]):
                raise ValueError("slice run is outside its observation")
            covered.extend(cell["run_ids"])
        if sorted(covered) != sorted(observation["run_ids"]):
            raise ValueError("condition slices must cover each run once")
    inventory = report["inventory"]
    if inventory["questionnaire_runs"] + inventory["task_runs"] != inventory["total_runs"]:
        raise ValueError("inventory total is inconsistent")
    used_runs = {rid for observation in report["observations"] for rid in observation["run_ids"]}
    if used_runs != set(provenance):
        raise ValueError("provenance must exactly cover the observation run union")
    count_field = 'included_runs' if prospective else 'reviewed_runs'
    if inventory[count_field] != len(provenance):
        raise ValueError(f"{count_field} must match unique provenance runs")
    if inventory[count_field] > inventory['total_runs']:
        raise ValueError('included count exceeds total runs')
    if inventory["reviewed_runs"] > inventory["total_runs"]:
        raise ValueError("reviewed count exceeds total runs")
    if prospective:
        for record in provenance.values():
            if ('grade_path' in record) != ('grade_sha256' in record):
                raise ValueError('grader path and hash must be recorded together')
            if (record.get('grade_path') is None) != (record.get('grade_sha256') is None):
                raise ValueError('grader path and hash must be null together')
            if record.get('grade_path') is not None and not record['grade_path'].strip():
                raise ValueError('grader path must not be empty')
    _portrait.validate_visual_evidence(report)
    return report
