"""B7 故障注入包装器：在 RPA 下层注入延迟、随机错误、缺失元素/超时（鲁棒性/压力测试）。"""
import random
import time
from typing import Any

from raft.contracts.models import ExecutionResult, FaultInjectionConfig, RPAConfig, ToolCall


def get_default_fault_injection(mode: str, seed: int | None = None) -> FaultInjectionConfig:
    """根据 rpa_config.mode 返回默认故障注入配置。"""
    if mode == "normal":
        return FaultInjectionConfig(seed=seed)
    if mode == "robustness":
        return FaultInjectionConfig(
            delay_prob=0.1,
            delay_sec_range=(0.3, 1.0),
            error_prob=0.1,
            missing_element_steps=[],
            timeout_steps=[],
            seed=seed,
        )
    if mode == "stress":
        return FaultInjectionConfig(
            delay_prob=0.2,
            delay_sec_range=(0.5, 2.0),
            error_prob=0.2,
            missing_element_steps=[],
            timeout_steps=[],
            seed=seed,
        )
    return FaultInjectionConfig(seed=seed)


def wrap_rpa_with_fault_injection(rpa: Any, rpa_config: RPAConfig | dict | None) -> Any:
    """
    若 rpa_config 存在且 mode 非 normal 或 fault_injection 非空，则用 FaultInjectionRPA 包装并返回；
    否则返回原 rpa。
    """
    if rpa_config is None:
        return rpa
    if isinstance(rpa_config, dict):
        rpa_config = RPAConfig.model_validate(rpa_config)
    if rpa_config.mode == "normal" and (rpa_config.fault_injection is None or _is_empty_fault(rpa_config.fault_injection)):
        return rpa
    cfg = rpa_config.fault_injection
    if cfg is None:
        cfg = get_default_fault_injection(rpa_config.mode)
    return FaultInjectionRPA(rpa, cfg)


def _is_empty_fault(cfg: FaultInjectionConfig) -> bool:
    return (
        cfg.delay_prob <= 0
        and cfg.error_prob <= 0
        and len(cfg.missing_element_steps) == 0
        and len(cfg.timeout_steps) == 0
    )


class FaultInjectionRPA:
    """
    包装任意 RPA 适配器，按配置注入故障：
    - 随机延迟（delay_prob）
    - 随机失败（error_prob）
    - 指定步数返回 element_not_found（missing_element_steps）或 timeout（timeout_steps）
    """

    def __init__(self, rpa: Any, config: FaultInjectionConfig | dict | None = None) -> None:
        self._rpa = rpa
        if config is None:
            self._config = FaultInjectionConfig()
        elif isinstance(config, dict):
            self._config = FaultInjectionConfig.model_validate(config)
        else:
            self._config = config
        if self._config.seed is not None:
            self._rng = random.Random(self._config.seed)
        else:
            self._rng = random.Random()

    def execute(self, step_index: int, tool_call: ToolCall) -> ExecutionResult:
        cfg = self._config
        step_id = getattr(tool_call, "step_id", None)
        # 1) 指定步数强制失败
        if step_index in cfg.timeout_steps or (isinstance(step_id, str) and step_id in cfg.timeout_step_ids):
            return ExecutionResult(
                success=False,
                error_type="timeout",
                raw_response="FaultInjection: forced timeout",
                output_text="",
                ui_state_delta=None,
                tool_name=tool_call.tool_name,
                step_id=step_id,
            )
        if step_index in cfg.missing_element_steps or (isinstance(step_id, str) and step_id in cfg.missing_element_step_ids):
            return ExecutionResult(
                success=False,
                error_type="element_not_found",
                raw_response="FaultInjection: forced missing element",
                output_text="",
                ui_state_delta=None,
                tool_name=tool_call.tool_name,
                step_id=step_id,
            )
        # 2) 随机延迟
        if cfg.delay_prob > 0 and self._rng.random() < cfg.delay_prob:
            delay = self._rng.uniform(cfg.delay_sec_range[0], cfg.delay_sec_range[1])
            time.sleep(delay)
        # 3) 随机失败
        if cfg.error_prob > 0 and self._rng.random() < cfg.error_prob:
            return ExecutionResult(
                success=False,
                error_type="injected_error",
                raw_response="FaultInjection: random error",
                output_text="",
                ui_state_delta=None,
                tool_name=tool_call.tool_name,
                step_id=step_id,
            )
        # 4) 调用底层 RPA
        return self._rpa.execute(step_index, tool_call)

    def close(self) -> None:
        if hasattr(self._rpa, "close"):
            self._rpa.close()