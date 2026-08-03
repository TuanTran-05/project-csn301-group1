from types import SimpleNamespace
from fakes.fake_ssh_client import FakeSSHClient
from network_copilot.changes.verification import run_verification, is_sensitive_verification_command

def test_sensitive_verification_is_redacted():
    assert is_sensitive_verification_command("show running-config interface Gi0/1")
    plan=[{"id":"generic:show-running-config","label":"show running-config","strategy":"generic","commands":["show running-config"],"required":True,"sensitive":True}]
    passed, result=run_verification(SimpleNamespace(verification_plan=plan,verification_commands=[]),FakeSSHClient(default_output="SECRET"))
    assert passed and result["generic:show-running-config"]["output"]==""
