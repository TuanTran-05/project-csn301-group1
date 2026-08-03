"""Pure capability assessment contracts for frozen configuration previews."""

from dataclasses import dataclass
from typing import Literal

CapabilityTier = Literal["level_a_core", "level_a_extended", "best_effort"]
VerificationLevel = Literal["semantic", "best_effort"]


@dataclass(frozen=True)
class OperationExpectation:
    family: str
    data: dict[str, object]

    def to_dict(self) -> dict:
        return {"family": self.family, "data": self.data}


@dataclass(frozen=True)
class CapabilityAssessment:
    capability_tier: CapabilityTier
    verification_level: VerificationLevel
    expectations: tuple[OperationExpectation, ...]

    @property
    def operation_families(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.family for item in self.expectations))


def assess_change(
    commands: list[str], execution_mode: str, device_type: str
) -> CapabilityAssessment:
    """Return the conservative baseline until family recognition is enabled."""
    return CapabilityAssessment("best_effort", "best_effort", ())
