"""Shared read-only presentation contracts for capability grant subjects."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CapabilityPresentationSubjects:
    roles: tuple[tuple[int, str], ...]
    members: tuple[tuple[int, str], ...]
