"""第 0 阶段清理的模块边界，防止旧模块回流或误删保留模块。"""

from importlib.util import find_spec

import pytest


@pytest.mark.parametrize("module_name", [
    "app.agent.aiops",
    "app.agent.mcp_client",
    "app.agent.mcp_demo.registry_client",
    "app.agent.mcp_demo.time_client",
])
def test_legacy_module_is_removed(module_name):
    assert find_spec(module_name) is None


@pytest.mark.parametrize("module_name", [
    "app.agent.mcp_demo.time_server",
    "app.agent.mcp_demo.web_search_client",
    "app.services.vector_index_service",
])
def test_required_module_is_kept(module_name):
    assert find_spec(module_name) is not None
