"""Public roles implemented by the real analysis, diagnosis and repair flows."""

ROLES = (
    "repository_analyst",
    "problem_investigator",
    "reproduction_assistant",
    "repair_agent",
    "independent_reviewer",
)

FLOW_ROLES = {
    "analysis": (ROLES[0], ROLES[1], ROLES[2], ROLES[4]),
    "diagnosis": (ROLES[0], ROLES[2], ROLES[4]),
    "repair": (ROLES[3], ROLES[4]),
}

ROLE_DETAILS = {
    "repository_analyst": {
        "name": "仓库分析", "description": "拆解原始任务范围、入口和平台，梳理源码与执行证据。",
        "flows": ["仓库分析", "环境诊断"],
    },
    "problem_investigator": {
        "name": "问题调查", "description": "追踪缺陷与相似实现，补充遗漏的验收项和受阻条件。",
        "flows": ["仓库分析"],
    },
    "reproduction_assistant": {
        "name": "复现辅助", "description": "逐项整理复现步骤和必要验证；复现结果仍由本人确认。",
        "flows": ["仓库分析", "环境诊断"],
    },
    "repair_agent": {
        "name": "代码修复", "description": "修复本人确认的问题并新增回归测试，保留范围内未解决项。",
        "flows": ["自动修复"],
    },
    "independent_reviewer": {
        "name": "独立审查",
        "description": "审查任务范围、排除项、实际差异与逐项证据，分别判断覆盖和补丁。",
        "flows": ["仓库分析", "环境诊断", "自动修复"],
    },
}


def model_for_role(model, role):
    """Also accept a single injected SDK model for development integrations."""
    return model[role] if isinstance(model, dict) else model


def reasoning_for_role(effort, role):
    return effort[role] if isinstance(effort, dict) else effort
