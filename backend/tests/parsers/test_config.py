from network_copilot.parsers.config import extract_interface_stanza, normalize_ios_config

CONFIG="""Building configuration...\ninterface GigabitEthernet0/2\n description STUDENT\n shutdown\ninterface GigabitEthernet0/3\n description OTHER\nend\n"""

def test_config_stanza_is_bounded():
    stanza=extract_interface_stanza(CONFIG,"Gi0/2")
    assert " description STUDENT" in stanza
    assert not any("0/3" in line for line in stanza)
    assert "Building configuration..." not in normalize_ios_config(CONFIG)
