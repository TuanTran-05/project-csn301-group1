import pytest

from network_copilot.chat.onboarding import requests_network_design


@pytest.mark.parametrize("message", [
    "tôi muốn tạo tiệm net 30pcs",
    "tôi muốn tạo sơ đồ tiệm net bao gồm 30 pcs",
    "Draw a network diagram for a computer lab",
    "Design an internet cafe network",
])
def test_requests_network_design_recognizes_diagram_requests(message):
    assert requests_network_design(message)


@pytest.mark.parametrize("message", [
    "Tạo VLAN 25 trên ACC-SW1",
    "Tạo VLAN guest trên mạng guest",
    "Cấu hình VLAN cho tiệm net",
    "Kiểm tra OSPF trên R1",
    "Hello, what can you do?",
])
def test_requests_network_design_does_not_capture_device_actions_or_chat(message):
    assert not requests_network_design(message)
