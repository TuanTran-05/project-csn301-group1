import pytest

from network_copilot.chat.onboarding import requests_network_design


@pytest.mark.parametrize("message", [
    "tôi muốn tạo tiệm net 30pcs",
    "tôi muốn tạo sơ đồ tiệm net bao gồm 30 pcs",
    "Draw a network diagram for a computer lab",
    "Design an internet cafe network",
    "hãy xây 1 hệ thống mạng gồm 1 router, 1 switch kết nối với router và 6 pc ket noi voi switch",
    "Dựng mạng gồm 2 router và 4 switch",
    "Xây dựng một hệ thống mạng cho văn phòng",
    "Tạo 1 router, 2 switch và 10 PC",
    "Build a network with 1 router and 3 PCs",
])
def test_requests_network_design_recognizes_diagram_requests(message):
    assert requests_network_design(message)


@pytest.mark.parametrize("message", [
    "Tạo VLAN 25 trên ACC-SW1",
    "Tạo VLAN guest trên mạng guest",
    "Cấu hình VLAN cho tiệm net",
    "Kiểm tra OSPF trên R1",
    "Hello, what can you do?",
    "Cấu hình OSPF trên 2 router và 1 switch",
    "Tạo VLAN 10 cho 6 PC",
    "Create a VLAN on the guest network",
])
def test_requests_network_design_does_not_capture_device_actions_or_chat(message):
    assert not requests_network_design(message)
