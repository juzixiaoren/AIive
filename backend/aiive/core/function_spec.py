"""Non-executable model control contracts, separate from the capability registry."""

from dataclasses import dataclass
from typing import Any

from aiive.tools.registry import CapabilitySafetySchema


@dataclass(frozen=True)
class FunctionSpec:
    safety: CapabilitySafetySchema
    description: str
    input_schema: dict[str, Any]
    parameters: None = None


class FunctionSpecs:
    def __init__(self):
        self._specs: dict[str, FunctionSpec] = {}

    def register(self, spec: FunctionSpec) -> None:
        self._specs[spec.safety.capability_id] = spec

    def get(self, name: str) -> FunctionSpec | None:
        return self._specs.get(name)

    def registrations(self) -> list[FunctionSpec]:
        return list(self._specs.values())

    def catalog(self) -> list[dict[str, str]]:
        return [
            {"capability_id": item.safety.capability_id}
            for item in self._specs.values()
        ]
