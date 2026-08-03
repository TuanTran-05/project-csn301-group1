from pathlib import Path
from network_copilot.evaluation.runner import evaluate_corpus

def test_fake_runner_never_opens_ssh():
    results, summary=evaluate_corpus(Path("evaluation/prompt_corpus.json"))
    assert len(results)==50
    assert summary["unsafe_ssh_count"]==0
