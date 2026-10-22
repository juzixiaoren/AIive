"""AIive self-evolution safety gate: offline Replay Eval + Kernel Contract Checks.

Public surface:
  - contracts.run_contract_checks(session)
  - replay.ReplayRunner / replay.run_replay_isolated()
  - gate.SafetyGate / gate.evaluate_promotion_gate()
  - persistence.save_gate_result / load_runs / latest
"""
from aiive.eval import (
    contracts as contracts,
)
from aiive.eval import (
    gate as gate,
)
from aiive.eval import (
    persistence as persistence,
)
from aiive.eval import (
    replay as replay,
)
from aiive.eval import (
    scenarios as scenarios,
)
from aiive.eval.gate import SafetyGate as SafetyGate
from aiive.eval.gate import (
    evaluate_promotion_gate as evaluate_promotion_gate,
)
from aiive.eval.models import (
    ContractResult as ContractResult,
)
from aiive.eval.models import (
    EvalCaseResult as EvalCaseResult,
)
from aiive.eval.models import (
    EvalRunResult as EvalRunResult,
)
from aiive.eval.models import (
    GateResult as GateResult,
)
from aiive.eval.replay import (
    ReplayRunner as ReplayRunner,
)
from aiive.eval.replay import (
    isolated_eval_session as isolated_eval_session,
)
from aiive.eval.replay import (
    run_replay_isolated as run_replay_isolated,
)
