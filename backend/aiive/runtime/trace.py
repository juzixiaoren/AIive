import uuid
from dataclasses import dataclass, field


@dataclass
class Trace:
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @staticmethod
    def new() -> "Trace":
        return Trace()
