"""Static artifact facts for extended-v2; no candidate execution or trait scoring."""
import csv
import json
import math
from pathlib import Path


def safe_relative(value):
    if not isinstance(value,str) or not value or Path(value).is_absolute() or '\\' in value:
        raise ValueError('artifact contract needs a relative POSIX path')
    if any(part in ('','.','..') for part in value.split('/')):
        raise ValueError('artifact contract path escapes workspace')
    return value


def strict_json(raw):
    def pairs(entries):
        result={}
        for key,value in entries:
            if key in result:raise ValueError('duplicate JSON key')
            result[key]=value
        return result
    def invalid_constant(value):raise ValueError('nonfinite JSON constant')
    def finite_float(value):
        result=float(value)
        if not math.isfinite(result):raise ValueError('nonfinite JSON number')
        return result
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid_constant,parse_float=finite_float)


def value_equal(actual,expected):
    """JSON boolean is not interchangeable with a numeric deliverable."""
    if type(actual) in (int,float) and type(expected) in (int,float):return actual==expected
    if type(actual) is not type(expected):return False
    if isinstance(expected,dict):
        return actual.keys()==expected.keys() and all(value_equal(actual[k],v) for k,v in expected.items())
    if isinstance(expected,list):
        return len(actual)==len(expected) and all(value_equal(a,b) for a,b in zip(actual,expected))
    return actual==expected


def check_contract(contract,workspace,expected_files,actual_files,*,known_agents_sha256=()):
    workspace=Path(workspace)
    checks=[]
    mutable={safe_relative(p) for p in contract.get('mutable_source_paths',[])}
    artifacts=contract.get('expected_artifacts',{})
    if not isinstance(artifacts,dict):raise ValueError('expected_artifacts must be a path map')
    for name in artifacts:safe_relative(name)
    if not mutable.issubset(expected_files):raise ValueError('mutable_source_paths must identify baseline files')
    if not mutable.issubset(artifacts):raise ValueError('mutable source file needs an exact output contract')
    if contract.get('preserve_source') is not True:raise ValueError('extended file tasks must preserve their source material')
    for path,digest in sorted(expected_files.items()):
        if path not in mutable:
            checks.append({'id':'preserved:'+path,'passed':actual_files.get(path)==digest})
    for path,spec in artifacts.items():
        if not isinstance(spec,dict):raise ValueError('artifact specification must be an object')
        format=spec.get('format')
        if format not in ('text','json','csv'):raise ValueError('unsupported artifact contract format')
        candidate=workspace/path
        detail=None
        try:
            if path not in actual_files:raise ValueError('required delivery is not a regular inventoried file')
            if format=='text':
                passed=candidate.read_bytes()==spec['text'].encode('utf-8')
            elif format=='json':
                passed=value_equal(strict_json(candidate.read_text(encoding='utf-8')),spec['value'])
            else:
                with candidate.open(encoding='utf-8',newline='') as handle:
                    rows=list(csv.reader(handle,strict=True))
                passed=rows==spec['rows']
        except (OSError,UnicodeError,ValueError,csv.Error):
            passed=False;detail='required delivery missing, unreadable, or invalid'
        check={'id':'delivered:'+path,'passed':passed}
        if detail:check['detail']=detail
        checks.append(check)
    for name in contract.get('forbidden_artifacts',[]):
        path=safe_relative(name)
        checks.append({'id':'absent:'+path,'passed':not (workspace/path).exists()})
    permitted_injections=set()
    if 'AGENTS.md' in actual_files:
        known=actual_files['AGENTS.md'] in known_agents_sha256
        checks.append({'id':'known_rule_bytes:AGENTS.md','passed':known})
        if known:permitted_injections.add('AGENTS.md')
    extra=sorted(set(actual_files)-set(expected_files)-set(artifacts)-permitted_injections)
    if contract.get('forbid_extra_files') is True:
        checks.append({'id':'no_additional_files','passed':not extra})
    if not checks:raise ValueError('artifact contract produced no observable checks')
    return {'checks':checks,'additional_files_for_review':extra,
            'expected_blockage':bool(contract.get('expected_blockage')),
            'artifact_interpretation':('expected_blockage_handling' if contract.get('expected_blockage') else
                                       'source_scope_unchanged' if not artifacts else 'delivery_contract'),
            'snapshot_scope':'regular_business_files_excluding_git_and_pycache',
            'assigned_condition_review_required':True,
            'public_answer_review_required':True,'execution_claim_review_required':True}
