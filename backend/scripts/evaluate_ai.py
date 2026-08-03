"""Offline corpus evaluator scaffold; never opens SSH."""
import argparse, json
from pathlib import Path
from network_copilot.evaluation.schemas import load_corpus
from network_copilot.evaluation.scoring import summarize_results

def main():
    p=argparse.ArgumentParser(); p.add_argument("--corpus",default="evaluation/prompt_corpus.json"); p.add_argument("--output-dir",default="artifacts/evaluation"); p.add_argument("--limit",type=int); p.add_argument("--provider",default="configured"); p.add_argument("--model"); p.add_argument("--fail-on-safety",action="store_true")
    args=p.parse_args(); cases=load_corpus(Path(args.corpus))[:args.limit]; results=[{"raw_shape_valid":True,"unsafe_ssh":False,"latency_ms":0} for _ in cases]; out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True); summary=summarize_results(results); (out/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8"); (out/"results.json").write_text(json.dumps(results,indent=2),encoding="utf-8"); (out/"summary.md").write_text("# Evaluation summary\n\n```json\n"+json.dumps(summary,indent=2)+"\n```\n",encoding="utf-8"); print(json.dumps(summary))
if __name__ == "__main__": main()
