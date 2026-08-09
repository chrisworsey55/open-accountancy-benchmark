"""Regression coverage for stable per-tool output schemas."""

from __future__ import annotations

from mirrorfirm.tools import MCPToolServer, WorldToolEngine
from mirrorfirm.tools.registry import DEFAULT_REGISTRY
from mirrorfirm.tools.schemas import ToolCallResult


def test_all_stable_tools_have_distinct_typed_output_models_and_mcp_schemas(
    engine: WorldToolEngine,
) -> None:
    """The registry must not expose one generic JSON result for every tool."""

    definitions = DEFAULT_REGISTRY.definitions()
    assert len(definitions) == 37
    assert all(
        definition.output_model is not ToolCallResult for definition in definitions
    )
    assert len({definition.output_model for definition in definitions}) == 37

    mcp_tools = MCPToolServer(engine).list_tools()["tools"]
    output_schemas = {tool["name"]: tool["outputSchema"] for tool in mcp_tools}
    for definition in definitions:
        assert (
            output_schemas[definition.name]
            == definition.output_model.model_json_schema()
        )
