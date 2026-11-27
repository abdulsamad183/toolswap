"""Resolve local YAML authoring inputs to strict versioned records."""

from collections.abc import Hashable
from pathlib import Path, PurePosixPath

import yaml
from pydantic import ValidationError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

from toolswap.contracts import SCHEMA_VERSION, ResolvedRunManifest, VersionedRecord
from toolswap.determinism import canonical_json

MAX_AUTHORING_BYTES = 128 * 1024


class InputError(ValueError):
    """Invalid configuration or contract data."""


class AuthoringLoader(yaml.SafeLoader):
    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Hashable, object]:
        self.flatten_mapping(node)
        mapping: dict[Hashable, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise InputError("YAML mapping keys must be strings")
            if key in mapping:
                raise InputError("duplicate YAML mapping key")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def scoped_path(root: Path, value: str) -> tuple[str, Path]:
    """Return a normalized relative reference and its resolved path within root."""
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or not path.parts
        or any(part.casefold() in {"..", ".git"} for part in path.parts)
        or any(char in value for char in "\\:~")
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise InputError("artifacts_dir must be a relative path within the workspace")
    base = root.resolve()
    try:
        resolved = (base / path).resolve()
    except RuntimeError as exc:
        raise InputError("artifacts_dir contains a symlink loop") from exc
    if resolved == base or not resolved.is_relative_to(base):
        raise InputError("artifacts_dir resolves outside its allowed scope")
    if any(part.casefold() == ".git" for part in resolved.relative_to(base).parts):
        raise InputError("artifacts_dir resolves outside its allowed scope")
    return path.as_posix(), resolved


def parse_yaml_record[Record: VersionedRecord](path: Path, model: type[Record]) -> Record:
    with path.open("rb") as source:
        raw = source.read(MAX_AUTHORING_BYTES + 1)
    if len(raw) > MAX_AUTHORING_BYTES:
        raise InputError("YAML input exceeds the authoring size limit")
    try:
        if any(isinstance(event, AliasEvent) for event in yaml.parse(raw, Loader=AuthoringLoader)):
            raise InputError("YAML aliases are unsupported")
        data = yaml.load(raw, Loader=AuthoringLoader)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise InputError(f"invalid YAML{location}") from exc
    except (yaml.YAMLError, RecursionError) as exc:
        raise InputError("invalid or deeply nested YAML") from exc
    if not isinstance(data, dict):
        raise InputError("YAML input must be a mapping")
    if "schema_version" in data and data["schema_version"] != SCHEMA_VERSION:
        raise InputError(f"unsupported schema_version; expected {SCHEMA_VERSION}")
    try:
        encoded = canonical_json(data)
    except (TypeError, ValueError, RecursionError) as exc:
        raise InputError("YAML values must be representable as JSON") from exc
    return model.model_validate_json(encoded)


def resolve_config(path: Path, root: Path) -> ResolvedRunManifest:
    config = parse_yaml_record(path, ResolvedRunManifest)
    normalized, _ = scoped_path(root, config.artifacts_dir)
    return config.model_copy(update={"artifacts_dir": normalized})


def validation_summary(error: ValidationError) -> str:
    issues = error.errors(include_input=False, include_context=False, include_url=False)
    return "\n".join(
        "record: contains an unknown field"
        if item["type"] == "extra_forbidden"
        else f"{'.'.join(map(str, item['loc'])) or 'record'}: {item['msg']}"
        for item in issues
    )
