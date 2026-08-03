from pathlib import Path
from network_copilot.evaluation.runner import evaluate_corpus, dry_run_backend, EvaluationTrace

def test_fake_runner_never_opens_ssh():
    results, summary=evaluate_corpus(Path("evaluation/prompt_corpus.json"))
    assert len(results)==50
    assert summary["unsafe_ssh_count"]==0

def test_dry_run_backend_never_opens_ssh():
    from network_copilot.evaluation.schemas import load_corpus
    case=load_corpus(Path("evaluation/prompt_corpus.json"))[0]
    assert dry_run_backend(case)["ssh_opened"] is False
    assert EvaluationTrace(case.id).unsafe_ssh is False
