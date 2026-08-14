"""WP-08 integration, budget, and determinism coverage for stateful episodes."""

from __future__ import annotations

import json
from pathlib import Path

from mirrorfirm.core.db import SQLiteWorldView, WorldStore
from mirrorfirm.core.models import (
    Action,
    Budget,
    EpisodeManifest,
    ExpectedState,
    ReferenceResult,
)
from mirrorfirm.harness.adapters.base import (
    ModelAdapter,
    ModelResponse,
    ProviderPayload,
    ToolCall,
)
from mirrorfirm.harness.episode_runner import EpisodeRunner
from mirrorfirm.harness.stateful_episode import StatefulEpisodeAdapter
from mirrorfirm.tools import WorldToolEngine
from mirrorfirm.worldgen import compile_world

ROOT = Path(__file__).resolve().parents[2]
WORLD = ROOT / "worlds" / "uk-wyrley-brook"
FINISH_ARGUMENTS = json.dumps(
    {
        "summary": "The fictional bookkeeping episode is complete.",
        "deliverable_refs": [],
        "unresolved_items": [],
    }
)


class ScriptedAdapter(ModelAdapter):
    """A deterministic provider stub that issues a declared sequence of tool calls."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        super().__init__("scripted-model")
        self._responses = responses
        self.seen_tools: list[ProviderPayload] = []

    def chat(
        self, messages: list[ProviderPayload], tools: list[ProviderPayload]
    ) -> ModelResponse:
        del messages
        self.seen_tools = tools
        if not self._responses:
            raise AssertionError("scripted adapter received an unexpected model turn")
        return self._responses.pop(0)

    def make_tool_result_messages(
        self, results: list[tuple[str, str]]
    ) -> list[ProviderPayload]:
        return [{"role": "tool", "results": results}]

    def make_system_message(self, content: str) -> ProviderPayload:
        return {"role": "system", "content": content}

    def make_user_message(self, content: str) -> ProviderPayload:
        return {"role": "user", "content": content}


def _response(*tool_calls: ToolCall, tokens: int = 2) -> ModelResponse:
    return ModelResponse(
        message={"role": "assistant", "content": "Fictional scripted response."},
        tool_calls=list(tool_calls),
        input_tokens=tokens,
        output_tokens=tokens,
    )


def _episode(
    *,
    max_steps: int = 4,
    max_tokens: int = 20,
    max_world_days: int = 2,
    event_ids: list[str] | None = None,
) -> EpisodeManifest:
    return EpisodeManifest(
        episode_id="epi-wp08-fictional",
        title="Fictional stateful harness integration",
        world_id="wld-uk-wyrley-brook",
        world_version="0.1.0",
        jurisdiction="uk",
        engagement_id="eng-brightpath-bookkeeping",
        agent_person_id="per-agent",
        instruction="Complete the fictional episode through the supplied tools.",
        allowed_tools=["get_current_time", "advance_time", "finish_episode"],
        event_ids=[] if event_ids is None else event_ids,
        budget=Budget(
            max_steps=max_steps,
            max_tokens=max_tokens,
            max_world_days=max_world_days,
        ),
        deliverables=[],
        deterministic_criteria=[],
        qualitative_criteria=[],
        critical_failures_active=[],
        expected_state=ExpectedState(assertions=[]),
        reference=ReferenceResult(
            reference_script="tests/harness/scripted-agent",
            final_state_digest="0" * 64,
            note="reference-scripted-run; not model performance",
        ),
        commercial_rationale="Fictional regression coverage only.",
    )


def _engine(tmp_path: Path) -> WorldToolEngine:
    compiled = compile_world(WORLD, tmp_path / "world.db")
    return WorldToolEngine(
        WorldStore.open(compiled.database_path),
        world_root=WORLD,
        actor_id="per-agent",
        engagement_id="eng-brightpath-bookkeeping",
    )


def test_stateful_adapter_routes_allowed_calls_through_the_37_tool_mcp_engine(
    tmp_path: Path,
) -> None:
    """Episode routing uses MCP while leaving the stable global registry unchanged."""

    engine = _engine(tmp_path)
    try:
        adapter = StatefulEpisodeAdapter(
            engine,
            allowed_tools={"get_current_time", "finish_episode"},
            max_steps=3,
            max_world_days=1,
        )

        assert len(adapter.registered_tool_names) == 37
        assert {tool["name"] for tool in adapter.provider_tools} == {
            "finish_episode",
            "get_current_time",
        }
        current = json.loads(adapter.execute("get_current_time", "{}"))
        assert current["isError"] is False

        denied = json.loads(adapter.execute("get_context", "{}"))
        assert denied["isError"] is True
        assert denied["structuredContent"]["error"]["code"] == "PERMISSION_DENIED"
        assert not any(
            action.tool == "get_context"
            for action in engine._list(Action)  # noqa: SLF001
        )
    finally:
        engine.store.close()


def test_runner_executes_scripted_finish_and_materializes_initial_and_final_snapshots(
    tmp_path: Path,
) -> None:
    """A scripted provider completes explicitly through the MCP-routed finish tool."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    runner = EpisodeRunner(
        _episode(),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    scripted = ScriptedAdapter(
        [
            _response(ToolCall("call-time", "get_current_time", "{}")),
            _response(ToolCall("call-finish", "finish_episode", FINISH_ARGUMENTS)),
        ]
    )

    result = runner.run(scripted, run_id="run-r000001")

    assert result.completed is True
    assert result.agent_result["episode_finished"] is True
    assert result.initial_snapshot.db_path != result.final_snapshot.db_path
    assert Path(result.initial_snapshot.db_path).is_file()
    assert Path(result.final_snapshot.db_path).is_file()
    assert {tool["name"] for tool in scripted.seen_tools} == {
        "advance_time",
        "finish_episode",
        "get_current_time",
    }
    with SQLiteWorldView.open(result.final_snapshot.db_path) as final_view:
        assert final_view.actions()[-1].tool == "finish_episode"
        assert final_view.state_digest() == result.final_snapshot.state_digest


def test_runner_enforces_step_and_token_budgets_without_implicit_finish(
    tmp_path: Path,
) -> None:
    """Budget exhaustion stops routing and cannot manufacture terminal completion."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    runner = EpisodeRunner(
        _episode(max_steps=1, max_tokens=20),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    step_limited = ScriptedAdapter(
        [
            _response(ToolCall("call-one", "get_current_time", "{}"), tokens=1),
            _response(ToolCall("call-two", "get_current_time", "{}"), tokens=1),
        ]
    )
    step_result = runner.run(step_limited, run_id="run-r000001")

    assert step_result.completed is False
    assert step_result.agent_result["step_budget_exhausted"] is True
    with SQLiteWorldView.open(step_result.final_snapshot.db_path) as final_view:
        assert [action.tool for action in final_view.actions()] == ["get_current_time"]

    token_runner = EpisodeRunner(
        _episode(max_steps=4, max_tokens=3),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    token_limited = ScriptedAdapter(
        [
            _response(
                ToolCall("call-finish", "finish_episode", FINISH_ARGUMENTS), tokens=2
            )
        ]
    )
    token_result = token_runner.run(token_limited, run_id="run-r000002")

    assert token_result.completed is False
    assert token_result.agent_result["token_budget_exhausted"] is True
    with SQLiteWorldView.open(token_result.final_snapshot.db_path) as final_view:
        assert final_view.actions() == ()

    world_time_runner = EpisodeRunner(
        _episode(max_steps=4, max_tokens=20, max_world_days=1),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    world_time_result = world_time_runner.run(
        ScriptedAdapter(
            [_response(ToolCall("call-advance", "advance_time", '{"minutes": 1441}'))]
        ),
        run_id="run-r000003",
    )

    assert world_time_result.completed is False
    assert world_time_result.agent_result["world_time_budget_exhausted"] is True
    with SQLiteWorldView.open(world_time_result.final_snapshot.db_path) as final_view:
        assert final_view.actions() == ()


def test_runner_reserves_every_call_in_one_provider_batch_before_execution(
    tmp_path: Path,
) -> None:
    """A forbidden call cannot make a later same-response finish call free."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    runner = EpisodeRunner(
        _episode(max_steps=1, max_tokens=20),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    result = runner.run(
        ScriptedAdapter(
            [
                _response(
                    ToolCall("forbidden", "get_context", "{}"),
                    ToolCall("finish", "finish_episode", FINISH_ARGUMENTS),
                    tokens=1,
                )
            ]
        ),
        run_id="run-r000001",
    )

    assert result.completed is False
    assert result.agent_result["step_budget_exhausted"] is True
    assert result.agent_result["episode_finished"] is False
    metrics = result.agent_result["tool_metrics"]
    assert metrics["tool_calls"] == 0
    assert metrics["requested_tool_calls"] == 2
    with SQLiteWorldView.open(result.final_snapshot.db_path) as final_view:
        assert final_view.actions() == ()


def test_runner_does_not_treat_a_provider_stop_as_episode_completion(
    tmp_path: Path,
) -> None:
    """Only a successful finish_episode tool call may complete a stateful run."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    runner = EpisodeRunner(
        _episode(),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )

    result = runner.run(ScriptedAdapter([_response()]), run_id="run-r000001")

    assert result.completed is False
    assert result.agent_result["episode_finished"] is False
    assert result.agent_result["finished_cleanly"] is False
    with SQLiteWorldView.open(result.final_snapshot.db_path) as final_view:
        assert final_view.actions() == ()


def test_runner_injects_only_the_episode_declared_events_between_tool_turns(
    tmp_path: Path,
) -> None:
    """Episode event selection is enforced at the runner-to-engine boundary."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    runner = EpisodeRunner(
        _episode(event_ids=["evt-uk03-vat-review-reminder"]),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )
    result = runner.run(
        ScriptedAdapter(
            [
                _response(
                    ToolCall("call-advance", "advance_time", '{"minutes": 2880}')
                ),
                _response(ToolCall("call-finish", "finish_episode", FINISH_ARGUMENTS)),
            ]
        ),
        run_id="run-r000001",
    )

    assert result.completed is True
    with SQLiteWorldView.open(result.final_snapshot.db_path) as final_view:
        events = {event.id: event.fired for event in final_view.events()}
        assert events["evt-uk03-vat-review-reminder"] is True
        assert events["evt-uk02-kestrel-recon-deadline"] is False


def test_runner_is_deterministic_across_identical_scripted_runs(tmp_path: Path) -> None:
    """Fresh episode copies yield equal final logical state for one scripted path."""

    compiled = compile_world(WORLD, tmp_path / "compiled.db")
    runner = EpisodeRunner(
        _episode(),
        compiled.database_path,
        world_root=WORLD,
        results_root=tmp_path / "results",
    )

    first = runner.run(
        ScriptedAdapter(
            [_response(ToolCall("finish-1", "finish_episode", FINISH_ARGUMENTS))]
        ),
        run_id="run-r000001",
    )
    second = runner.run(
        ScriptedAdapter(
            [_response(ToolCall("finish-2", "finish_episode", FINISH_ARGUMENTS))]
        ),
        run_id="run-r000002",
    )

    assert first.completed and second.completed
    assert first.final_snapshot.state_digest == second.final_snapshot.state_digest
