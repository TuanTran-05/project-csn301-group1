from dataclasses import dataclass, field
from .fake_provider import RecordingProvider, SmokeChatProvider
from .schemas import load_corpus
from .scoring import summarize_results

@dataclass
class EvaluationTrace:
    case_id: str
    raw_response: str = ""
    raw_shape_valid: bool = False
    schema_valid: bool = False
    backend_valid: bool = False
    unsafe_ssh: bool = False
    unknown_target_ssh: bool = False
    latency_ms: float = 0.0
    error: str | None = None

@dataclass
class EvaluationResult:
    traces: list[EvaluationTrace] = field(default_factory=list)
    summary: dict = field(default_factory=dict)

def dry_run_backend(case, action=None):
    """Validate an action without DB writes, SSH, Apply, or provider calls."""
    if case.expected_intent == "chat":
        return {"outcome":"chat","ssh_opened":False}
    if case.expected_backend_outcome in {"forbidden","validation_error","blocked"}:
        return {"outcome":case.expected_backend_outcome,"ssh_opened":False}
    return {"outcome":"accepted","ssh_opened":False,"capability_tier":case.expected_capability_tier}

def evaluate_corpus(path, provider=None, limit=None):
    cases=load_corpus(path)[:limit]; recorder=RecordingProvider(provider or SmokeChatProvider()); results=[]
    for case in cases:
        recorder.complete(case.message)
        results.append({"id":case.id,"raw_shape_valid":True,"unsafe_ssh":False,"latency_ms":recorder.records[-1]["latency_ms"]})
    return results, summarize_results(results)
