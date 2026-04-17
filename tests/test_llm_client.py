"""LLM 客户端封装与重试逻辑的单测。"""
import os

import pytest

from raft.core.llm_client import _resolve_llm_config, chat_completion_with_retry


def test_resolve_llm_config_default(monkeypatch) -> None:
    """默认 provider 时使用 qwen 配置。"""
    monkeypatch.delenv("RAFT_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    key, model, base, prov = _resolve_llm_config(api_key=None, base_url=None)
    assert prov == "qwen"
    assert model == "deepseek-v3"
    assert "dashscope" in (base or "")


def test_resolve_llm_config_env_override_openai(monkeypatch) -> None:
    """RAFT_LLM_PROVIDER=openai 时使用 OpenAI 配置。"""
    monkeypatch.setenv("RAFT_LLM_PROVIDER", "openai")
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    key, model, base, prov = _resolve_llm_config(api_key=None, base_url=None)
    assert prov == "openai"
    assert model == "gpt-4o-mini"
    assert base is None or base == ""


def test_resolve_llm_config_qwen(monkeypatch) -> None:
    """provider=qwen 时使用 Qwen 配置。"""
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    key, model, base, prov = _resolve_llm_config(provider="qwen")
    assert prov == "qwen"
    assert model == "deepseek-v3"
    assert "dashscope" in (base or "")


def test_resolve_llm_config_grok(monkeypatch) -> None:
    """provider=grok 时使用 xAI 配置。"""
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    key, model, base, prov = _resolve_llm_config(provider="grok", base_url=None)
    assert prov == "grok"
    assert "grok" in model.lower()
    assert "x.ai" in (base or "")


def test_resolve_llm_config_grok_ignores_qwen_model_name(monkeypatch) -> None:
    """provider=grok 时勿使用调用方误传的 deepseek/gpt 等模型名。"""
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("XAI_MODEL", raising=False)
    _, model, _, prov = _resolve_llm_config(provider="grok", model="deepseek-v3")
    assert prov == "grok"
    assert model == "grok-beta"


def test_chat_completion_with_retry_no_key_raises(monkeypatch) -> None:
    """未设置 API Key 时抛出 ValueError。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("RAFT_LLM_PROVIDER", "openai")  # 强制非 azure，避免从 AZURE_OPENAI_API_KEY 取 key
    with pytest.raises(ValueError, match="未设置"):
        chat_completion_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            api_key="",
        )


def test_chat_completion_with_retry_openai_not_installed(monkeypatch) -> None:
    """openai 未安装时抛出 ImportError。"""
    import raft.core.llm_client as m

    orig = m.OpenAI
    monkeypatch.setattr(m, "OpenAI", None)
    try:
        with pytest.raises(ImportError, match="openai"):
            chat_completion_with_retry(
                messages=[{"role": "user", "content": "hi"}],
                api_key="sk-fake",
            )
    finally:
        monkeypatch.setattr(m, "OpenAI", orig)
