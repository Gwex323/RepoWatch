"""运行安全上限及比较指纹，忽略非语义元数据。"""

from dataclasses import asdict, dataclass

from repowatch.domain.task_records import content_digest


@dataclass(frozen=True)
class RuntimePolicy:
    max_steps: int = 32
    max_active_ms: int = 300000
    no_progress_attempts: int = 3
    oscillation_cycles: int = 2

    def __post_init__(self):
        for key, maximum in (
            ("max_steps", 128),
            ("max_active_ms", 3600000),
            ("no_progress_attempts", 20),
            ("oscillation_cycles", 5),
        ):
            if type(getattr(self, key)) is not int or not 1 <= getattr(self, key) <= maximum:
                raise ValueError("运行安全上限必须是有限的安全整数")

    def as_dict(self):
        return asdict(self)


NON_SEMANTIC = frozenset(
    {
        "id",
        "run_id",
        "batch_id",
        "sha",
        "target_sha",
        "base_sha",
        "candidate_sha",
        "timestamp",
        "created_at",
        "updated_at",
        "elapsed_ms",
        "duration_ms",
        "random_id",
    }
)


def normalize_comparison(value):
    if isinstance(value, dict):
        return {
            key: normalize_comparison(item)
            for key, item in value.items()
            if key not in NON_SEMANTIC
        }
    if isinstance(value, list):
        return [normalize_comparison(item) for item in value]
    if value is None or type(value) in {str, int, bool}:
        return value
    raise ValueError("比较事实必须为受限 JSON")


def fingerprint(objective, comparison):
    return content_digest({"objective": objective, "comparison": normalize_comparison(comparison)})
