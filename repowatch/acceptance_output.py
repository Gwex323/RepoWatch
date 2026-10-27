"""Value-free output validation feedback; rejected text stays in the current model call."""

import json

from pydantic import ValidationError

from .store import Conflict

ERROR_TYPES = {
    "missing", "extra_forbidden", "model_type", "model_attributes_type", "dict_type", "list_type",
    "string_type", "string_too_long", "string_too_short", "string_pattern_mismatch", "bool_type",
    "int_type", "greater_than_equal", "less_than_equal", "literal_error", "too_long", "too_short",
    "finite_number", "json_invalid",
}


class AcceptanceOutputError(Conflict):
    def __init__(self, details):
        super().__init__("acceptance_output_invalid")
        self.validation = details


def validation_feedback(response, output_type):
    parts = [part for item in response.output if item.type == "message" for part in item.content]
    text = "".join(part.text for part in parts if part.type == "output_text")
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return {"category": "invalid_json", "position": exc.pos,
                "line": exc.lineno, "column": exc.colno}, text
    fields = set()

    def collect(schema):
        if isinstance(schema, dict):
            fields.update(schema.get("properties", {}))
            for value in schema.values():
                collect(value)
        elif isinstance(schema, list):
            for value in schema:
                collect(value)

    collect(output_type.model_json_schema())
    errors = []
    try:
        output_type.model_validate_json(text, strict=True)
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_context=False, include_url=False)[:12]:
            path = [part if isinstance(part, str) and part in fields
                    or type(part) is int and 0 <= part <= 1000000 else "unknown_field"
                    for part in error["loc"][:12]]
            errors.append({"path": path, "type": error["type"] if error["type"] in ERROR_TYPES
                           else "invalid_field"})
    return {"category": "schema_mismatch", "errors": errors}, text
