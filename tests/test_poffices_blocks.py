import raft.rpa.poffices_bootstrap as pb
from raft.rpa.poffices_blocks import AgentMasterRunFlowOnceBlock, PofficesBootstrapBlock, PofficesQueryBlock


class _FakePage:
    def wait_for_timeout(self, _ms: int) -> None:
        pass

    def evaluate(self, _expr: str) -> None:
        """PofficesQueryBlock 会调 page.evaluate 滚动页面；mock 无操作。"""
        pass


class _FakeRPA:
    def __init__(self) -> None:
        self._followup = False
        self._page = _FakePage()

    def get_page(self) -> object:
        return self._page

    def get_timeout_ms(self) -> int:
        return 10_000

    def get_query_wait_sec(self, *, minimum: int = 60) -> int:
        return max(60, minimum)

    def get_credentials(self) -> tuple[str | None, str | None]:
        return ("u", "p")

    def is_followup_query(self) -> bool:
        return self._followup

    def mark_query_completed(self) -> None:
        self._followup = True




def test_query_block_validation_error_when_query_missing() -> None:
    block = PofficesQueryBlock()
    result = block.run(params={}, context={"rpa": _FakeRPA()})
    assert result.success is False
    assert result.error_type == "validation_error"


def test_query_block_success(monkeypatch) -> None:
    def _fill(page, query, timeout_ms):  # noqa: ANN001
        assert query == "hello"
        assert timeout_ms == 10_000

    def _capture(page, timeout_sec=None, check_interval_sec=None, **kwargs):  # noqa: ANN001, ARG001
        assert timeout_sec == 60
        assert check_interval_sec == 2.0
        return {"text": "ok", "images": [], "links": []}

    monkeypatch.setattr(pb, "fill_query_and_send", _fill)
    monkeypatch.setattr(pb, "wait_and_capture_assets", _capture)

    rpa = _FakeRPA()
    result = PofficesQueryBlock().run(params={"query": "hello"}, context={"rpa": rpa})
    assert result.success is True
    assert result.error_type is None
    assert result.raw_response == {
        "query": "hello",
        "response": "ok",
        "text": "ok",
        "images": [],
        "links": [],
    }
    assert rpa.is_followup_query() is True


def test_agent_master_run_flow_once_prefers_queries_per_agent(monkeypatch) -> None:
    captured = {"query": None}

    def _fill(page, query, timeout_ms):  # noqa: ANN001
        captured["query"] = query
        assert timeout_ms == 10_000

    def _wait(page, timeout_sec):  # noqa: ANN001
        assert timeout_sec == 60

    def _has_next(page, timeout_ms):  # noqa: ANN001, ARG001
        return False

    def _click_next(page, timeout_ms):  # noqa: ANN001, ARG001
        raise AssertionError("should not click next")

    def _capture(page, timeout_sec=None, check_interval_sec=None, **kwargs):  # noqa: ANN001, ARG001
        return {"text": "final report", "images": ["https://x/img.png"], "links": [{"href": "https://a", "text": "t"}]}

    monkeypatch.setattr(pb, "fill_query_and_send", _fill)
    monkeypatch.setattr(pb, "wait_for_generation_complete", _wait)
    monkeypatch.setattr(pb, "has_next_step", _has_next)
    monkeypatch.setattr(pb, "click_next_step", _click_next)
    monkeypatch.setattr(pb, "wait_and_capture_assets", _capture)

    result = AgentMasterRunFlowOnceBlock().run(
        params={
            "agents": ["Research Proposal", "Literature Review"],
            "queries": ["设计调查方案与抽样量表", "综述近五年相关文献与研究空白"],
            "query": "旧兼容 query",
        },
        context={"rpa": _FakeRPA()},
    )

    assert result.success is True
    assert captured["query"] is not None
    assert "Research Proposal" in captured["query"]
    assert "Literature Review" in captured["query"]
    assert "设计调查方案与抽样量表" in captured["query"]
    assert "综述近五年相关文献与研究空白" in captured["query"]
    assert result.ui_state_delta["agent_master_queries"] == ["设计调查方案与抽样量表", "综述近五年相关文献与研究空白"]


def test_bootstrap_block_success(monkeypatch) -> None:
    """空 params 时使用默认 agent_name（Research Proposal）。"""
    def _run(  # noqa: ANN001
        page,
        username=None,
        password=None,
        agent_name="Research Proposal",
        timeout_ms=None,
        log_fn=None,
        resume_on_current_page=False,
        **kwargs,
    ):
        assert username == "u"
        assert password == "p"
        assert timeout_ms == 10_000
        assert agent_name == "Research Proposal"
        assert resume_on_current_page is False

    monkeypatch.setattr(pb, "run_bootstrap_on_page", _run)
    result = PofficesBootstrapBlock().run(params={}, context={"rpa": _FakeRPA()})
    assert result.success is True
    assert result.ui_state_delta == {"poffices_ready": True, "app_ready": True}


def test_bootstrap_block_new_question_path(monkeypatch) -> None:
    called = {"hit": False}

    def _new(page, timeout_ms):  # noqa: ANN001
        called["hit"] = True
        assert timeout_ms == 180_000  # max(rpa.get_timeout_ms()=10_000, 180_000) — 等待 New question 至少需要 3 分钟

    def _select_noop(page, agent_name, *, timeout_ms=None):  # noqa: ANN001, ARG001
        """select_agent_on_current_page 需要 Playwright page，mock 为无操作。"""
        pass

    monkeypatch.setattr(pb, "click_new_question", _new)
    monkeypatch.setattr(pb, "select_agent_on_current_page", _select_noop)
    rpa = _FakeRPA()
    rpa.mark_query_completed()
    result = PofficesBootstrapBlock().run(params={}, context={"rpa": rpa})
    assert result.success is True
    assert called["hit"] is True
