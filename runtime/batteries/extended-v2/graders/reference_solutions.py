"""Offline test references only. Never copy this module into a model workspace."""
import csv
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from reference_solutions_v1 import apply_reference as apply_v1_reference


def apply_reference(kind,workspace):
    """Preserve the original nine items' reference behavior."""
    return apply_v1_reference(kind,workspace)


def apply_item_reference(item,bundle,workspace):
    workspace=Path(workspace);bundle=Path(bundle)
    if not item['id'].startswith('V2-'):
        apply_v1_reference(Path(item['fixture_path']).name,workspace)
        if Path(item['fixture_path']).name.startswith('settlement-'):
            return subprocess.run([sys.executable,'-m','src.settlement','--input','data/transactions.json',
                '--rates','data/approved_rates.json','--preview','out/normalized.json','--out','out/settlement.csv'],
                cwd=workspace,capture_output=True,text=True,timeout=10)
        return None
    # Compute each delivery from the public source inputs. Do not copy expected
    # artifacts out of the evaluator metadata, which would make acceptance circular.
    item_id=item['id']
    if item_id=='V2-D1-plan':return None
    if item_id=='V2-D1-edit':
        path=workspace/'draft.md'
        path.write_bytes(path.read_bytes().replace('2026年10月17日'.encode(), '2026年10月24日'.encode())
                         .replace(b'14:00',b'14:30'))
        return None
    if item_id.startswith('V2-D7-'):
        return subprocess.run([sys.executable,'-B','build_brief.py'],cwd=workspace,
                              capture_output=True,text=True,timeout=10)
    out=workspace/'out';out.mkdir(exist_ok=True)
    if item_id.startswith('V2-D2-'):
        with (workspace/'supplies.csv').open(encoding='utf-8',newline='') as handle:
            rows=list(csv.DictReader(handle))
        categories={}
        for row in rows:categories[row['category']]=categories.get(row['category'],0)+int(row['quantity'])
        result={'by_category':categories,'total_quantity':sum(categories.values()),'row_count':len(rows)}
        (out/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return None
    if item_id.startswith('V2-D6-'):
        with (workspace/'data/attendees.csv').open(encoding='utf-8',newline='') as handle:
            rows=[row for row in csv.DictReader(handle) if row['status']=='confirmed']
        with (out/'checkin.csv').open('w',encoding='utf-8',newline='') as handle:
            writer=csv.writer(handle);writer.writerow(['registration_id','name'])
            writer.writerows((row['registration_id'],row['name']) for row in sorted(rows,key=lambda row:row['registration_id']))
        agenda=json.loads((workspace/'archive/agenda.json').read_text(encoding='utf-8'))
        if (agenda.get('event_id')=='community-talk-20261024' and agenda.get('status')=='approved'
                and agenda.get('approval_id')):
            text=('# 社区讲座活动单\n\n'
                  f"活动：{agenda['title']}\n日期：{agenda['date']}\n地点：{agenda['location']}\n\n"+
                  ''.join(f"{segment['time']}｜{segment['title']}\n" for segment in agenda['segments']))
            (out/'event-pack.md').write_text(text,encoding='utf-8')
        return None
    raise ValueError('no offline reference for item: '+item_id)
