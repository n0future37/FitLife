"""Tool schemas match patch semantics: omitted values are unchanged; null is invalid."""

from pydantic import model_validator

from fitlife.business import ProfilePatch

EXERCISE_FIELDS = frozenset(
    {
        "goal",
        "space",
        "equipment",
        "limitations",
        "preferred_activities",
        "available_weekdays",
        "weekly_sessions",
        "session_minutes",
    }
)


class ToolProfilePatch(ProfilePatch):
    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = handler.resolve_ref_schema(handler(core_schema))
        schema["minProperties"] = 1
        for prop in schema.get("properties", {}).values():
            if prop.get("default", object()) is None:
                prop.pop("default")
            variants = prop.get("anyOf")
            if variants:
                variants = [v for v in variants if v.get("type") != "null"]
                if len(variants) == 1:
                    extra = {k: v for k, v in prop.items() if k != "anyOf"}
                    prop.clear()
                    prop.update(variants[0])
                    prop.update(extra)
                else:
                    prop["anyOf"] = variants
        return schema


class ExerciseProfilePatch(ToolProfilePatch):
    @model_validator(mode="before")
    @classmethod
    def only_exercise_fields(cls, value):
        if isinstance(value, ProfilePatch):
            value = value.model_dump(exclude_unset=True)
        if isinstance(value, dict) and set(value) - EXERCISE_FIELDS:
            raise ValueError("PROFILE_FIELDS_OUTSIDE_DOMAIN")
        return value

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        schema = super().__get_pydantic_json_schema__(core_schema, handler)
        schema["properties"] = {
            k: v for k, v in schema["properties"].items() if k in EXERCISE_FIELDS
        }
        return schema
