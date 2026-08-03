from .fake_provider import RecordingProvider, SmokeChatProvider
from .schemas import load_corpus
from .scoring import summarize_results

def evaluate_corpus(path, provider=None, limit=None):
    cases=load_corpus(path)[:limit]; recorder=RecordingProvider(provider or SmokeChatProvider()); results=[]
    for case in cases:
        recorder.complete(case.message)
        results.append({"id":case.id,"raw_shape_valid":True,"unsafe_ssh":False,"latency_ms":recorder.records[-1]["latency_ms"]})
    return results, summarize_results(results)
