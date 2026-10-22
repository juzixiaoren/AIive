"""Replay Eval runner tests: stable repeatable run + fail-closed on broken scenario."""
from aiive.eval.replay import ReplayRunner, run_replay_isolated
from aiive.eval.scenarios import Scenario, ToolStep


def test_replay_all_scenarios_pass_on_session(db_session):
    result = ReplayRunner().run(db_session)
    failed = [(c.scenario_id, c.error, [a.message for a in c.assertions if not a.ok])
              for c in result.cases if not c.ok]
    assert result.ok, failed
    assert result.passed == len(result.cases)


def test_replay_isolated_ok_and_stable():
    r1 = run_replay_isolated()
    r2 = run_replay_isolated()
    assert r1.ok, r1.error
    assert r2.ok, r2.error
    assert r1.passed == r2.passed == len(r1.cases)


def test_replay_fails_closed_on_broken_scenario(db_session):
    broken = Scenario(
        id="broken.eval", description="a scenario that must fail",
        steps=[ToolStep("nonexistent_tool_xyz", {}, expect_ok=True)],
    )
    result = ReplayRunner(scenarios=[broken]).run(db_session)
    assert result.ok is False
    assert result.failed == 1
    assert result.cases[0].ok is False


def test_replay_tool_unauthorized_is_refused(db_session):
    from aiive.eval.scenarios import ToolStep
    scn = Scenario(
        id="authz.eval", description="unauthorized source refused",
        steps=[ToolStep("echo", {"message": "x"}, expect_ok=False, source="untrusted_bot")],
    )
    result = ReplayRunner(scenarios=[scn]).run(db_session)
    assert result.ok is True
