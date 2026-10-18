import hashlib
import json
from importlib.resources import files

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from .errors import AppError

CONTRACT = yaml.safe_load(files("handoff_guardian").joinpath("openapi.yaml").read_text(encoding="utf-8"))


def validate(name: str, value):
    schema = {"$ref": f"#/components/schemas/{name}", "components": CONTRACT["components"]}
    if next(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value), None):
        raise AppError(
            "INVALID_REQUEST" if not name.startswith("Provider") else "PROVIDER_OUTPUT_INVALID",
            "The supplied data does not match the required schema.",
            422,
        )
    return value


def canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def digest(value) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def public(value):
    """Private persistence keys never become browser access authority."""
    if isinstance(value, list):
        return [public(item) for item in value]
    if isinstance(value, dict):
        return {key: public(item) for key, item in value.items() if not key.startswith("_")}
    return value
