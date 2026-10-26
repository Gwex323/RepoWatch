"""Shared analysis tool permissions, safe diagnostics and bounded output sizing."""

import json

from pydantic import ValidationError

from .roles import FLOW_ROLES

ANALYSIS_ROLES = FLOW_ROLES["analysis"]
SCOPE_BATCH_SIZE = 3
TOOL_ROLES = {
    "read_source_file": frozenset(ANALYSIS_ROLES),
    "read_execution_records": frozenset(ANALYSIS_ROLES),
    "register_task_scope": frozenset(role for role in ANALYSIS_ROLES
                                     if role != "reproduction_assistant"),
}


def tool_allowed(role, name):
    return role in TOOL_ROLES.get(name, ())


def tools_for_role(role, tools):
    return [tool for tool in tools if tool_allowed(role, tool.name)]


def high_reasoning_reserve(effort):
    return 5000 if effort in ("high", "xhigh", "max") else 0


def tool_output_tokens(settings, tools):
    """Reserve both reasoning and payload space while retaining the chosen effort."""
    if not tools:
        return settings.max_tokens
    effort = getattr(settings.reasoning, "effort", None)
    reserve = high_reasoning_reserve(effort) or 2000
    payload = 3000 if any(tool.name == "register_task_scope" for tool in tools) else 1000
    return max(settings.max_tokens or 0, reserve + payload)


def _resolve(schema, root):
    ref = schema.get("$ref", "")
    if ref.startswith("#/$defs/"):
        return root.get("$defs", {}).get(ref.removeprefix("#/$defs/"), {})
    return schema


def _field_contract(location, root):
    """Keep only schema-owned names; unknown keys can contain sensitive model text."""
    schema, path = root, ""
    for part in location:
        schema = _resolve(schema, root)
        if type(part) is int:
            path += f"[{part}]" if 0 <= part <= 100000 else "[]"
            schema = schema.get("items", {})
        elif isinstance(part, str) and part in schema.get("properties", {}):
            path += ("." if path else "") + part
            schema = schema["properties"][part]
        else:
            path += ("." if path else "") + "unexpected_field"
            schema = {}
    schema = _resolve(schema, root)
    expected = {key: schema[key] for key in (
        "type", "enum", "minLength", "maxLength", "minItems", "maxItems", "pattern",
        "minimum", "maximum",
    ) if key in schema}
    return path or "arguments", expected


def tool_argument_issue(response, tools, handoffs, parameter_models):
    """Preflight a whole response; never execute or repair its generated arguments."""
    contracts = {tool.name: tool.params_json_schema for tool in tools}
    contracts.update({item.tool_name: getattr(item, "input_json_schema", {}) for item in handoffs})
    for call in response.output:
        if call.type != "function_call" or call.name not in contracts:
            continue
        schema = contracts[call.name]
        model = parameter_models.get(call.name)
        arguments = call.arguments
        if arguments == "" and not schema.get("properties"):
            arguments = "{}"  # Native SDK convention for parameterless tools.
        detail = {"tool": call.name, "reason": "arguments_schema", "fields": [], "issues": []}
        try:
            value = json.loads(arguments)
        except (TypeError, ValueError, RecursionError) as exc:
            detail.update(reason="invalid_json", chars=len(arguments)
                          if isinstance(arguments, str) else 0)
            if isinstance(exc, json.JSONDecodeError):
                detail.update(position=exc.pos, line=exc.lineno, column=exc.colno)
        else:
            if not isinstance(value, dict):
                detail["reason"] = "object_required"
            elif schema.get("additionalProperties") is False and any(
                key not in schema.get("properties", {}) for key in value
            ):
                detail["reason"] = "unexpected_field"
            elif model is not None:
                try:
                    model.model_validate_json(arguments)
                except ValidationError as exc:
                    for error in exc.errors(
                        include_input=False, include_context=False, include_url=False,
                    )[:8]:
                        path, expected = _field_contract(error["loc"], schema)
                        code = {
                            "missing": "required_field", "string_type": "string_required",
                            "int_type": "integer_required", "int_parsing": "integer_required",
                            "int_from_float": "integer_required", "list_type": "list_required",
                            "model_type": "object_required", "literal_error": "enum_value",
                            "extra_forbidden": "unexpected_field", "too_long": "too_long",
                            "too_short": "too_short", "string_too_long": "too_long",
                            "string_too_short": "too_short", "string_pattern_mismatch": "pattern",
                        }.get(error["type"], "invalid_value")
                        detail["issues"].append({"field": path, "code": code,
                                                 "expected": expected})
                    detail["fields"] = sorted({item["field"] for item in detail["issues"]})
                else:
                    continue
            elif not schema.get("properties") and value:
                detail["reason"] = "unexpected_field"
            else:
                continue
        return detail, schema
    return None
