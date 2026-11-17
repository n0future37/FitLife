"""Return bounded schema facts, never validation messages containing input values."""

from itertools import islice

from jsonschema import Draft202012Validator, FormatChecker

# Only known input errors: a backend/permission failure must remain failed evidence.
INPUT_ERROR_CODES = frozenset(
    {"INVALID_TOOL_ARGUMENTS", "INVALID_WEEK_START", "INVALID_DATE_RANGE"}
)


def input_issues(schema, arguments):
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    issues = []
    for error in islice(validator.iter_errors(arguments), 3):
        # absolute_path may include user-defined map keys. Schema paths come from
        # our registered tool; report only fixed property names from that schema.
        path = list(error.absolute_schema_path)
        fields = [str(path[i + 1]) for i, value in enumerate(path[:-1]) if value == "properties"]
        issue = {"field": ".".join(fields) or "$", "rule": error.validator}
        if error.validator == "format" and error.validator_value in {"date", "date-time", "uuid"}:
            issue["expected_format"] = error.validator_value
        if error.validator == "required":
            issue["required_fields"] = list(error.validator_value)[:16]
        issues.append(issue)
    return issues
