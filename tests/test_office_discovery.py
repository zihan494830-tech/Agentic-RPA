"""Office Discovery 单测。"""
from raft.core.office_discovery import filter_agent_candidates, match_office, select_agents_for_topic


def test_filter_agent_candidates_strips_office_name() -> None:
    raw = ["HR Office", "Yearly Company Calendar", "HR Office (1/5 selected)"]
    out = filter_agent_candidates(raw, office_name="HR Office")
    assert "HR Office" not in out
    assert "Yearly Company Calendar" in out
    assert not any("selected" in x for x in out)


def test_match_office_direct() -> None:
    m = match_office("research", ["Research Office", "Business Office", "Strategy Office"])
    assert m == "Research Office"


def test_match_office_business() -> None:
    m = match_office("business", ["Research Office", "Business Office"])
    assert m == "Business Office"


def test_match_office_no_match() -> None:
    m = match_office("xyz", ["Research Office", "Business Office"])
    assert m is None


def test_select_agents_for_topic_fewer_than_count() -> None:
    agents = ["Research Proposal", "Market Analysis"]
    selected = select_agents_for_topic(agents, "石油价格", 5, provider="qwen")
    assert selected == ["Research Proposal", "Market Analysis"]


def test_select_agents_for_topic_empty() -> None:
    selected = select_agents_for_topic([], "石油价格", 3)
    assert selected == []
