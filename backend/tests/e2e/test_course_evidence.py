import json
import subprocess
import sys
from pathlib import Path

def test_course_evidence_defaults_to_preview_only(tmp_path):
    scenario=tmp_path/"scenario.json"
    scenario.write_text(json.dumps({"name":"test","devices":["ACC-SW1"],"extension_mode":"none"}),encoding="utf-8")
    output=tmp_path/"evidence.json"
    result=subprocess.run([sys.executable,"scripts/course_evidence.py",str(scenario),"--output",str(output),"--preview-only"],cwd=Path(__file__).parents[2],capture_output=True,text=True)
    assert result.returncode==0
    assert json.loads(output.read_text())["mode"]=="preview-only"
