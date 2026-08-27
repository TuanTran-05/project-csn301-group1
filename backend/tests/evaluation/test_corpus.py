import json
from collections import Counter
from pathlib import Path

from network_copilot.evaluation.schemas import load_corpus


CORPUS_PATH = Path("evaluation/prompt_corpus.json")
SCENARIO_PATH = Path("evaluation/pnetlab_scenario.example.json")
LIVE_INVENTORY = {"R1", "SW1", "SW2"}
RETIRED_INVENTORY = {
    "ISP-RTR",
    "FW-01",
    "INTERNAL-RTR",
    "DIST-SW1",
    "DIST-SW2",
    "ACC-SW1",
    "ACC-SW2",
    "ACC-SW3",
    "DMZ-SW",
}


def test_approved_corpus_distribution():
    cases = load_corpus(CORPUS_PATH)
    assert len(cases) == 50
    assert Counter(case.category for case in cases) == {
        "chat": 5,
        "monitor": 6,
        "troubleshoot": 6,
        "switching_interface": 10,
        "ipv4_static_route": 8,
        "acl_dhcp_ospf": 7,
        "dangerous_unauthorized": 5,
        "ambiguous_invalid": 3,
    }
    assert sum(case.language == "vi" for case in cases) >= 25
    assert len({case.id for case in cases}) == 50


def test_corpus_targets_only_current_inventory_and_covers_every_device():
    cases = load_corpus(CORPUS_PATH)
    targets = {
        target
        for case in cases
        for target in case.expected_targets
    }
    assert targets == LIVE_INVENTORY
    assert targets.isdisjoint(RETIRED_INVENTORY)


def test_pnetlab_scenario_matches_current_inventory():
    scenario = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    assert scenario["devices"] == ["R1", "SW1", "SW2"]
