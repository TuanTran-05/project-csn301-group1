from collections.abc import Iterable
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AIOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_hostnames: list[str] = Field(min_length=1)
    execution_mode: Literal["config", "exec"]
    commands: list[str] = Field(min_length=1)
    verification_commands: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_targets(self):
        if "*" in self.device_hostnames and self.device_hostnames != ["*"]:
            raise ValueError("'*' cannot be mixed with explicit device hostnames")
        return self


class DiagramNode(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=60)
    kind: Literal["internet", "router", "firewall", "switch", "server", "host"]


class DiagramLink(BaseModel):
    source: str
    target: str
    label: str = Field(default="", max_length=80)


class NetworkDiagram(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    nodes: list[DiagramNode] = Field(min_length=2, max_length=16)
    links: list[DiagramLink] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_links(self):
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("Diagram node IDs must be unique")
        for link in self.links:
            if (
                link.source not in ids
                or link.target not in ids
                or link.source == link.target
            ):
                raise ValueError("Diagram links must connect two distinct existing nodes")
        return self


class DeviceConfiguration(BaseModel):
    hostname: str = Field(min_length=1, max_length=64)
    device_type: Literal["cisco_ios", "cisco_asa"]
    commands: list[str] = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_commands(self):
        if any(
            not command.strip()
            or len(command) > 500
            or any(char in command for char in "\r\n\x00")
            or re.match(
                r"(?i)^\s*(?:reload\b|write\s+erase\b|erase\b|format\b|delete\b)",
                command,
            )
            for command in self.commands
        ):
            raise ValueError(
                "Configuration commands must be safe, non-empty single lines"
            )
        return self


class AIAdvice(BaseModel):
    explanation: str = Field(min_length=1)
    diagram: NetworkDiagram | None = None
    configurations: list[DeviceConfiguration] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def validate_configuration_targets(self):
        hostnames = [item.hostname.casefold() for item in self.configurations]
        if len(hostnames) != len(set(hostnames)):
            raise ValueError("Each device can only have one configuration file")
        if self.configurations and self.diagram is None:
            raise ValueError("Device configuration suggestions need a matching diagram")
        return self


def diagram_schema() -> dict:
    # Gemini supports a subset of JSON Schema. Keep this provider schema
    # deliberately small; Pydantic enforces the stronger runtime bounds.
    return {
        "type": "object",
        "required": ["title", "nodes", "links"],
        "properties": {
            "title": {"type": "string"},
            "nodes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "label", "kind"],
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string"},
                        "kind": {
                            "type": "string",
                            "enum": ["internet", "router", "firewall", "switch", "server", "host"],
                        },
                    },
                },
            },
            "links": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["source", "target"],
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                        "label": {"type": "string"},
                    },
                },
            },
        },
    }


def configurations_schema() -> dict:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "required": ["hostname", "device_type", "commands"],
            "properties": {
                "hostname": {"type": "string"},
                "device_type": {"type": "string", "enum": ["cisco_ios", "cisco_asa"]},
                "commands": {"type": "array", "items": {"type": "string"}},
            },
        },
    }


class AIAction(BaseModel):
    """The only plan shape the model is allowed to return.

    The AI never executes anything: this object is a *proposal* that the policy
    engine and the change workflow then accept or refuse.

    "chat" is the one intent that carries no operations: it is a plain
    conversational reply (a greeting, a capability question, general
    networking knowledge) whose answer is the explanation itself. Every
    other intent still requires at least one operation, so relaxing the
    field constraint is expressed as a validator rather than by weakening
    the field.
    """

    model_config = ConfigDict(extra="ignore")

    intent: Literal["monitor", "configure", "troubleshoot", "chat"]
    # Still a required key - the provider schema declares it required too.
    # Only the "at least one entry" part moves into the validator below, so
    # a chat action must send an explicit empty list, never omit the field.
    operations: list[AIOperation]
    explanation: str
    diagram: NetworkDiagram | None = None

    @model_validator(mode="after")
    def validate_operations_match_intent(self):
        if self.intent == "chat":
            if self.operations:
                raise ValueError("a chat action must not carry operations")
        elif not self.operations:
            raise ValueError("at least one operation is required")
        return self


def build_ai_action_schema(device_hostnames: Iterable[str]) -> dict:
    """Build the provider schema from the inventory visible to the model."""
    target_branches = [
        {
            "type": "array",
            "minItems": 1,
            "maxItems": 1,
            "items": {"type": "string", "enum": ["*"]},
        }
    ]
    known_hostnames = sorted(set(device_hostnames))
    if known_hostnames:
        target_branches.append(
            {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": known_hostnames},
            }
        )

    # google-genai's response_json_schema accepts standard JSON Schema names
    # and only a documented subset of keywords. Inventory enums encode the
    # explicit-target branch without relying on unsupported pattern/not rules.
    return {
        "type": "object",
        "required": ["intent", "operations", "explanation"],
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["monitor", "configure", "troubleshoot", "chat"],
            },
            "operations": {
                "type": "array",
                # Provider decoding must be able to emit the deliberate refusal
                # handled by AIService.interpret(). AIAction itself intentionally
                # remains stricter (min_length=1) after that branch is handled.
                "minItems": 0,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "device_hostnames",
                        "execution_mode",
                        "commands",
                    ],
                    "properties": {
                        "device_hostnames": {"anyOf": target_branches},
                        "execution_mode": {
                            "type": "string",
                            "enum": ["config", "exec"],
                        },
                        "commands": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                        "verification_commands": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "explanation": {"type": "string"},
            "diagram": diagram_schema(),
        },
    }


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)
    session_id: int | None = None
    group_id: int | None = None
    # "design": answer with a whole L2/L3 design for the project to apply.
    mode: Literal["design"] | None = None
