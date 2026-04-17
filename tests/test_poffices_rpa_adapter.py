from raft.contracts.models import ToolCall
from raft.rpa.poffices_rpa import PofficesRPA


class _DummyRPA:
    def __init__(self) -> None:
        self._has_completed_first_query = False


def test_execute_uses_registry_first(monkeypatch) -> None:
    expected = {"success": True}

    class _FakeRegistry:
        def register(self, block_id, block):  # noqa: ANN001
            return None

        def execute(self, block_id, *, params, context):  # noqa: ANN001
            assert block_id == "poffices_bootstrap"
            assert params == {}
            assert "step_index" in context
            return expected

    monkeypatch.setattr("raft.rpa.blocks.get_default_block_registry", lambda: _FakeRegistry())

    rpa = object.__new__(PofficesRPA)
    result = PofficesRPA.execute(rpa, 0, ToolCall(tool_name="poffices_bootstrap", params={}))
    assert result is expected


def test_execute_fallback_unknown_tool(monkeypatch) -> None:
    class _FakeRegistry:
        def register(self, block_id, block):  # noqa: ANN001
            return None

        def execute(self, block_id, *, params, context):  # noqa: ANN001
            return None

    monkeypatch.setattr("raft.rpa.blocks.get_default_block_registry", lambda: _FakeRegistry())

    rpa = object.__new__(PofficesRPA)
    rpa.timeout_ms = 1000
    rpa._has_completed_first_query = False
    rpa._username = "u"
    rpa._password = "p"
    rpa._ensure_page = lambda: object()

    result = PofficesRPA.execute(rpa, 0, ToolCall(tool_name="not_supported", params={}))
    assert result.success is False
    assert result.error_type == "unknown_tool"
    assert isinstance(result.raw_response, str)
    assert "[fallback]" in result.raw_response


def test_public_state_api_wraps_internal_state() -> None:
    rpa = object.__new__(PofficesRPA)
    rpa.timeout_ms = 1234
    rpa.query_wait_sec = 45
    rpa._username = "u"
    rpa._password = "p"
    rpa._has_completed_first_query = False
    rpa._ensure_page = lambda: "PAGE"

    assert rpa.get_page() == "PAGE"
    assert rpa.get_timeout_ms() == 1234
    assert rpa.get_query_wait_sec() == 60
    assert rpa.get_credentials() == ("u", "p")
    assert rpa.is_followup_query() is False

    rpa.mark_query_completed()
    assert rpa.is_followup_query() is True
