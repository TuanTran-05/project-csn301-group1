"""Validate a course scenario and emit a safe preview-only evidence record."""
import argparse, json
from pathlib import Path

def main():
    p=argparse.ArgumentParser(); p.add_argument("scenario",type=Path); p.add_argument("--output",type=Path,default=Path("artifacts/course-evidence.json")); p.add_argument("--apply",action="store_true"); p.add_argument("--preview-only",action="store_true")
    a=p.parse_args(); data=json.loads(a.scenario.read_text(encoding="utf-8")); required={"name","devices","extension_mode"}; missing=required-set(data)
    if missing: raise SystemExit(f"missing fields: {sorted(missing)}")
    out={"scenario":data["name"],"devices":data["devices"],"extension_mode":data["extension_mode"],"mode":"apply" if a.apply else "preview-only","ssh_opened":False}
    a.output.parent.mkdir(parents=True,exist_ok=True); a.output.write_text(json.dumps(out,indent=2),encoding="utf-8")
if __name__ == "__main__": main()
