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
        "name": "仓库分析", "description": "梳理源码入口与执行证据，定位环境故障原因。",
        "flows": ["仓库分析", "环境诊断"],
    },
    "problem_investigator": {
        "name": "问题调查", "description": "追踪代码路径与触发条件，调查疑似缺陷。",
        "flows": ["仓库分析"],
    },
    "reproduction_assistant": {
        "name": "复现辅助", "description": "整理复现步骤和环境处理建议，复现结果仍由本人确认。",
        "flows": ["仓库分析", "环境诊断"],
    },
    "repair_agent": {
        "name": "代码修复", "description": "生成源码修改与新增回归测试，提交隔离验证。",
        "flows": ["自动修复"],
    },
    "independent_reviewer": {
        "name": "独立审查", "description": "审查发现、建议、实际差异与测试证据，给出结论和风险。",
        "flows": ["仓库分析", "环境诊断", "自动修复"],
    },
}


def model_for_role(model, role):
    """Also accept a single injected SDK model for development integrations."""
    return model[role] if isinstance(model, dict) else model


def reasoning_for_role(effort, role):
    return effort[role] if isinstance(effort, dict) else effort
