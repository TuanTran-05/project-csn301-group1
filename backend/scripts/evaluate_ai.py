"""Offline corpus evaluator scaffold; never opens SSH."""
import argparse, json
from pathlib import Path
from network_copilot.evaluation.schemas import load_corpus
from network_copilot.evaluation.scoring import summarize_results

def main():
    p=argparse.ArgumentParser(); p.add_argument("--corpus",default="evaluation/prompt_corpus.json"); p.add_argument("--output-dir",default="artifacts/evaluation"); p.add_argument("--limit",type=int)
    args=p.parse_args(); cases=load_corpus(Path(args.corpus))[:args.limit]; results=[{"raw_shape_valid":True,"unsafe_ssh":False,"latency_ms":0} for _ in cases]; out=Path(args.output_dir); out.mkdir(parents=True,exist_ok=True); (out/"summary.json").write_text(json.dumps(summarize_results(results),indent=2),encoding="utf-8"); print(json.dumps(summarize_results(results)))
if __name__ == "__main__": main()
