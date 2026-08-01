"""CLI-level safety checks for the live demo batch workflow."""

import io
import json
import sys

from scripts import demo_check


class TerminalInput(io.StringIO):
    def __init__(self, text: str, interactive: bool = True):
        super().__init__(text)
        self.interactive = interactive
        self.read_count = 0

    def isatty(self) -> bool:
        return self.interactive

    def readline(self, *args, **kwargs) -> str:
        self.read_count += 1
        return super().readline(*args, **kwargs)


DEVICES = [
    {"id": 1, "hostname": "INTERNAL-RTR"},
    {"id": 2, "hostname": "DIST-SW1"},
    {"id": 3, "hostname": "ACC-SW1"},
]


def _child(change_id: int, device: dict, status: str = "pending_approval") -> dict:
    return {
        "id": change_id,
        "status": status,
        "risk_level": "high",
        "device": {"id": device["id"], "hostname": device["hostname"]},
        "execution_mode": "exec",
        "commands": ["write memory"],
        "error_message": None,
    }


def _batch(children: list[dict], status: str = "pending_approval") -> dict:
    return {
        "id": 42,
        "status": status,
        "risk_level": "high",
        "requires_confirmation": True,
        "confirmation_text": "CONFIRM ALL",
        "changes": children,
    }


def _run_demo(
    monkeypatch,
    *,
    preview_children: list[dict],
    applied_children: list[dict],
    terminal: TerminalInput,
) -> tuple[int, list[tuple[str, str, object]]]:
    calls: list[tuple[str, str, object]] = []

    def fake_call(base_url, path, method="GET", body=None, token=None):
        calls.append((path, method, body))
        if path == "/api/auth/login":
            return 200, json.dumps({"access_token": "test-token"})
        if path == "/api/devices":
            return 200, json.dumps({"items": DEVICES})
        if path == "/api/commands/execute-readonly":
            return 200, json.dumps({"output": "GigabitEthernet0/0"})
        if path == "/api/devices/1/refresh":
            return 200, json.dumps({"status": "online"})
        if path == "/api/ai/chat" and body["message"] == demo_check.WRITE_ALL_REQUEST:
            return 200, json.dumps({"batch": _batch(preview_children)})
        if path == "/api/ai/chat":
            return 200, json.dumps({"intent": "monitor"})
        if path == "/api/change-batches/42/approve":
            return 200, json.dumps(_batch(preview_children, status="approved"))
        if path == "/api/change-batches/42/apply":
            return 200, json.dumps(_batch(applied_children, status="success"))
        raise AssertionError(f"Unexpected HTTP call: {method} {path}")

    monkeypatch.setattr(demo_check, "call", fake_call)
    monkeypatch.setattr(sys, "stdin", terminal)
    monkeypatch.setattr(
        sys,
        "argv",
        ["demo_check.py", "--username", "operator", "--password", "operator-password"],
    )
    return demo_check.main(), calls


def _approval_or_apply_calls(calls: list[tuple[str, str, object]]) -> list[str]:
    return [path for path, _, _ in calls if path.endswith(("/approve", "/apply"))]


def test_duplicate_preview_child_aborts_before_approval(monkeypatch, capsys):
    """A duplicate frozen child must not pass set-based scope validation."""
    preview_children = [_child(index + 10, device) for index, device in enumerate(DEVICES)]
    preview_children.append(_child(10, DEVICES[0]))

    result, calls = _run_demo(
        monkeypatch,
        preview_children=preview_children,
        applied_children=preview_children,
        terminal=TerminalInput("CONFIRM ALL\n"),
    )

    assert result == 1
    assert _approval_or_apply_calls(calls) == []
    assert "ALL STEPS PASSED" not in capsys.readouterr().out


def test_malformed_preview_child_aborts_before_approval(monkeypatch, capsys):
    """A malformed child response must fail closed instead of crashing or approving."""
    preview_children = [
        _child(index + 10, device) for index, device in enumerate(DEVICES[:2])
    ] + ["not-a-child"]

    result, calls = _run_demo(
        monkeypatch,
        preview_children=preview_children,
        applied_children=[],
        terminal=TerminalInput("CONFIRM ALL\n"),
    )

    assert result == 1
    assert _approval_or_apply_calls(calls) == []
    assert "ALL STEPS PASSED" not in capsys.readouterr().out


def test_wrong_confirmation_aborts_before_approval(monkeypatch, capsys):
    """Anything except the exact confirmation cannot reach batch approval."""
    children = [_child(index + 10, device) for index, device in enumerate(DEVICES)]

    result, calls = _run_demo(
        monkeypatch,
        preview_children=children,
        applied_children=children,
        terminal=TerminalInput("CONFIRM\n"),
    )

    assert result == 1
    assert _approval_or_apply_calls(calls) == []
    assert "confirmation was not accepted" in capsys.readouterr().out


def test_eof_confirmation_aborts_before_approval(monkeypatch, capsys):
    """EOF at the operator gate cannot approve or apply a frozen batch."""
    children = [_child(index + 10, device) for index, device in enumerate(DEVICES)]

    result, calls = _run_demo(
        monkeypatch,
        preview_children=children,
        applied_children=children,
        terminal=TerminalInput(""),
    )

    assert result == 1
    assert _approval_or_apply_calls(calls) == []
    assert "confirmation was not accepted" in capsys.readouterr().out


def test_noninteractive_confirmation_aborts_before_approval(monkeypatch, capsys):
    """Piped stdin cannot authorize a live write-all operation."""
    children = [_child(index + 10, device) for index, device in enumerate(DEVICES)]

    result, calls = _run_demo(
        monkeypatch,
        preview_children=children,
        applied_children=children,
        terminal=TerminalInput("CONFIRM ALL\n", interactive=False),
    )

    assert result == 1
    assert _approval_or_apply_calls(calls) == []
    assert "interactive terminal" in capsys.readouterr().out


def test_truncated_apply_results_cannot_report_success(monkeypatch, capsys):
    """A successful batch response is unsafe when a frozen child is missing."""
    children = [_child(index + 10, device) for index, device in enumerate(DEVICES)]

    result, calls = _run_demo(
        monkeypatch,
        preview_children=children,
        applied_children=[
            _child(index + 10, device, status="success")
            for index, device in enumerate(DEVICES[:2])
        ],
        terminal=TerminalInput("CONFIRM ALL\n"),
    )

    assert result == 1
    assert _approval_or_apply_calls(calls) == [
        "/api/change-batches/42/approve",
        "/api/change-batches/42/apply",
    ]
    assert "ALL STEPS PASSED" not in capsys.readouterr().out


def test_terminal_exact_confirmation_applies_the_frozen_batch(monkeypatch, capsys):
    """An operator may apply only after typing the exact confirmation at a TTY."""
    children = [_child(index + 10, device) for index, device in enumerate(DEVICES)]
    terminal = TerminalInput("  CONFIRM ALL  \n")

    result, calls = _run_demo(
        monkeypatch,
        preview_children=children,
        applied_children=[
            _child(index + 10, device, status="success")
            for index, device in enumerate(DEVICES)
        ],
        terminal=terminal,
    )

    output = capsys.readouterr().out
    assert result == 0
    assert terminal.read_count == 1
    assert _approval_or_apply_calls(calls) == [
        "/api/change-batches/42/approve",
        "/api/change-batches/42/apply",
    ]
    assert "operator-password" not in output
    assert "ALL STEPS PASSED" in output
