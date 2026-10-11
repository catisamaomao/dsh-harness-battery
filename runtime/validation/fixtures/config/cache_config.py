import json
from pathlib import Path

def load_settings(path=None):
    return json.loads(Path(path or Path(__file__).with_name("settings.json")).read_text())
