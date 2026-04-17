# B7: RPA Adapter — 默认 PlaywrightRPA，可选 VibiumRPA、PofficesRPA，未安装则回退 MockRPA。
# FaultInjectionRPA 包装器用于鲁棒性/压力测试。
from raft.rpa.mock_rpa import MockRPA
from raft.rpa.fault_injection import FaultInjectionRPA, wrap_rpa_with_fault_injection

try:
    from raft.rpa.vibium_rpa import VibiumRPA
except ImportError:
    VibiumRPA = None  # type: ignore[misc]


def get_default_rpa(
    backend: str = "playwright",
    **kwargs: object,
) -> "object":
    """
    返回 RPA 适配器。backend 可选：playwright（默认）、vibium、poffices、poffices-vibium、mock。
    若指定 backend 的依赖未安装，则回退 MockRPA。kwargs 传给对应 RPA 构造器。
    """
    if backend == "mock":
        return MockRPA()

    if backend == "poffices":
        try:
            from raft.rpa.poffices_rpa import PofficesRPA
            return PofficesRPA(**kwargs)
        except ImportError:
            return MockRPA()
        except Exception:
            return MockRPA()

    if backend == "poffices-vibium":
        try:
            from raft.rpa.poffices_rpa import PofficesVibiumRPA
            return PofficesVibiumRPA(**kwargs)
        except ImportError:
            return MockRPA()
        except Exception:
            return MockRPA()

    if backend == "vibium":
        try:
            from raft.rpa.vibium_rpa import VibiumRPA
            return VibiumRPA(**kwargs)
        except ImportError:
            return MockRPA()
        except Exception:
            return MockRPA()

    # 默认 playwright
    try:
        from raft.rpa.playwright_rpa import PlaywrightRPA
        return PlaywrightRPA(**kwargs)
    except ImportError:
        return MockRPA()
    except Exception:
        return MockRPA()


__all__ = ["MockRPA", "VibiumRPA", "get_default_rpa", "FaultInjectionRPA", "wrap_rpa_with_fault_injection"]
