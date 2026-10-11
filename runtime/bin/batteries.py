"""Named, versioned local question packs. Reading a catalogue never calls a model."""
import json
from pathlib import Path

from validation_run import bundle_loader


BATTERIES = {'core': 'validation', 'extended': 'batteries/extended-v2'}


def battery_path(project, name):
    if name not in BATTERIES:
        raise ValueError('unknown battery: ' + str(name))
    return Path(project).resolve() / BATTERIES[name]


def catalogue(project, name):
    root = battery_path(project, name)
    frozen = bundle_loader()(root)
    items = []
    for item in frozen['items']:
        meta = json.loads((root / item['metadata_path']).read_text())
        prompt = (root / item['prompt_path']).read_text()
        items.append({
            'id': item['id'], 'dimensions': item['dimensions'], 'protocol': item['protocol'],
            'origin': 'inherited_v1' if item['id'].startswith('V1-') else 'new_v2',
            'prompt': prompt, 'prompt_path': str(root / item['prompt_path']),
            'metadata_path': str(root / item['metadata_path']),
            'status': meta['status'],
        })
    return {'battery': name, 'bundle_id': frozen['bundle_id'],
            'bundle_path': str(root), 'bundle_sha256': frozen['bundle_sha256'],
            'item_count': len(items), 'items': items, 'model_started': False}
