from collections import Counter
from pathlib import Path
from network_copilot.evaluation.schemas import load_corpus

def test_approved_corpus_distribution():
    cases=load_corpus(Path("evaluation/prompt_corpus.json"))
    assert len(cases)==50
    assert Counter(c.category for c in cases)=={"chat":5,"monitor":6,"troubleshoot":6,"switching_interface":10,"ipv4_static_route":8,"acl_dhcp_ospf":7,"dangerous_unauthorized":5,"ambiguous_invalid":3}
    assert sum(c.language=="vi" for c in cases)>=25
    assert len({c.id for c in cases})==50
