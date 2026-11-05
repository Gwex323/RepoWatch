"""累计授权与单步预留的值对象，不依赖数据库或模型 SDK。"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

MAX_AMOUNT = 2**53 - 1
DIMENSIONS = ("active_ms", "model_calls", "tokens", "repair_rounds", "recovery_rounds")


def amounts(value: Mapping[str, Any], *, complete=False, positive=False) -> dict[str, int]:
    if set(value) - set(DIMENSIONS) or (complete and set(value) != set(DIMENSIONS)):
        raise ValueError("预算维度不完整或含未知维度")
    result = {}
    for key, number in value.items():
        if type(number) is not int or not (1 if positive else 0) <= number <= MAX_AMOUNT:
            raise ValueError("预算必须为有限、非负的安全整数；授权总额必须为正")
        result[key] = number
    return result


def vector(value: Mapping[str, Any]) -> dict[str, int]:
    validated = amounts(value)
    return {key: validated.get(key, 0) for key in DIMENSIONS}


def plus(left, right) -> dict[str, int]:
    return amounts({key: left.get(key, 0) + right.get(key, 0) for key in DIMENSIONS})


def available(limits, used, reserved) -> dict[str, int]:
    return {key: max(0, limits[key] - used[key] - reserved[key]) for key in DIMENSIONS}


def is_exhausted(limits, used) -> bool:
    return bool(limits) and any(used[key] >= limits[key] for key in DIMENSIONS)


@dataclass(frozen=True)
class BudgetAmounts:
    active_ms: int = 0
    model_calls: int = 0
    tokens: int = 0
    repair_rounds: int = 0
    recovery_rounds: int = 0

    def __post_init__(self):
        amounts(asdict(self), complete=True)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class RenewalPlan:
    remaining_goals: tuple[str, ...]
    strategy: str
    next_goal: str
    estimate_basis: str = "待核实：未进行额外模型估算"
    uncertainty: str = "待核实"

    def __post_init__(self):
        if (
            not self.remaining_goals
            or any(not goal.strip() for goal in self.remaining_goals)
            or not self.strategy.strip()
            or not self.next_goal.strip()
            or not self.estimate_basis.strip()
            or not self.uncertainty.strip()
        ):
            raise ValueError("续期必须说明剩余目标、继续策略、下一目标及估计不确定性")

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BudgetDecision:
    status: str
    reservation_id: str | None = None
    warning: bool = False

    @property
    def allowed(self) -> bool:
        return self.status == "reserved"


class BudgetDenied(PermissionError):
    def __init__(self, decision: BudgetDecision):
        self.decision = decision
        super().__init__(f"预算未授权本步执行：{decision.status}")
