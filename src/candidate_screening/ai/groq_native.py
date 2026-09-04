"""Groq GPT-OSS native structured-output compatibility.

Groq's GPT-OSS native JSON-schema endpoint accepts a deliberately small
strict-schema subset.  Pydantic emits a useful, general JSON Schema (including
``$defs``, ``$ref`` and validation metadata), while the endpoint requires all
object fields to be required and every object to be closed.  This module keeps
that adaptation at the provider wire boundary: the application schema used to
validate the response is never changed.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from typing import Any, cast

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.groq import GroqModel
from pydantic_ai.settings import ModelSettings

type JsonSchema = dict[str, Any]


class GroqNativeSchemaError(ValueError):
    """Raised when a schema cannot be represented in Groq's native subset."""


# Groq's strict schema dialect is structural.  Keep only these keys in the
# provider payload.  Pydantic's validation constraints are intentionally left
# to the application-side Pydantic validator, which remains authoritative.
_STRUCTURAL_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "anyOf",
        "enum",
    }
)

# These are valid JSON Schema annotations/constraints but are not needed by
# Groq's constrained decoder.  They are omitted from the wire schema rather
# than copied into a request that Groq may reject.  The application still
# validates all of them after receiving the model response.
_IGNORED_SCHEMA_KEYS = frozenset(
    {
        "$schema",
        "description",
        "title",
        "default",
        "examples",
        "example",
        "deprecated",
        "readOnly",
        "writeOnly",
        "minLength",
        "maxLength",
        "pattern",
        "format",
        "contentEncoding",
        "contentMediaType",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "minProperties",
        "maxProperties",
    }
)

# Unlike scalar constraints, these keywords change the shape of the accepted
# value.  Dropping one would silently weaken the application's requested
# contract, so reject it and make the incompatibility explicit.
_UNSUPPORTED_STRUCTURAL_KEYS = frozenset(
    {
        "oneOf",
        "allOf",
        "not",
        "if",
        "then",
        "else",
        "contains",
        "minContains",
        "maxContains",
        "prefixItems",
        "patternProperties",
        "propertyNames",
        "dependentRequired",
        "dependentSchemas",
        "unevaluatedProperties",
        "unevaluatedItems",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
    }
)

_LOCAL_DEFS_PREFIX = "#/$defs/"


def _pointer_parts(ref: str) -> tuple[str, ...]:
    """Return JSON-pointer parts for a local ``$defs`` reference.

    Groq receives an inlined schema, so only local definitions can be safely
    resolved here.  In particular, a URI or a reference into another part of
    the document must not be silently left on the wire.
    """

    if not ref.startswith(_LOCAL_DEFS_PREFIX):
        raise GroqNativeSchemaError(f"external or unsupported $ref: {ref!r}")
    suffix = ref[len(_LOCAL_DEFS_PREFIX) :]
    if not suffix:
        raise GroqNativeSchemaError(f"unresolved $ref: {ref!r}")
    parts = tuple(suffix.split("/"))
    decoded: list[str] = []
    for part in parts:
        # JSON Pointer's escaping is deliberately handled instead of treating
        # definition names as unrestricted dictionary keys.
        value: list[str] = []
        index = 0
        while index < len(part):
            char = part[index]
            if char != "~":
                value.append(char)
                index += 1
                continue
            if index + 1 >= len(part) or part[index + 1] not in {"0", "1"}:
                raise GroqNativeSchemaError(f"unresolved $ref: {ref!r}")
            value.append("~" if part[index + 1] == "0" else "/")
            index += 2
        decoded.append("".join(value))
    return tuple(decoded)


def _resolve_pointer(definitions: Mapping[str, Any], parts: tuple[str, ...], ref: str) -> Any:
    """Resolve a local JSON pointer below the root ``$defs`` object."""

    if not parts or parts[0] not in definitions:
        raise GroqNativeSchemaError(f"unresolved $ref: {ref!r}")
    value: Any = definitions[parts[0]]
    for part in parts[1:]:
        if not isinstance(value, Mapping) or part not in value:
            raise GroqNativeSchemaError(f"unresolved $ref: {ref!r}")
        value = cast(Mapping[str, Any], value)[part]
    return value


def _merge_ref_siblings(target: Any, siblings: Mapping[str, Any], ref: str) -> Any:
    """Merge legal ``$ref`` siblings without mutating the definition."""

    if not siblings:
        return deepcopy(target)
    if not isinstance(target, Mapping):
        raise GroqNativeSchemaError(f"unsupported $ref siblings for {ref!r}")
    target_mapping = cast(Mapping[str, Any], target)
    merged: JsonSchema = deepcopy(dict(target_mapping))
    # The structural walker will remove ignored annotation keys and reject
    # unsupported structural keys.  Keeping siblings here lets that same
    # validation apply to reference-site metadata without mutating the target.
    merged.update(deepcopy(dict(siblings)))
    return merged


def _resolve_refs(
    value: Any,
    definitions: Mapping[str, Any],
    stack: tuple[str, ...] = (),
) -> tuple[Any, tuple[str, ...]]:
    """Recursively inline local references and reject bad reference graphs."""

    if isinstance(value, bool):
        return value, stack
    if not isinstance(value, Mapping):
        raise GroqNativeSchemaError("schema nodes must be JSON objects")

    mapping = cast(Mapping[str, Any], value)
    ref = mapping.get("$ref")
    if ref is not None:
        if not isinstance(ref, str):
            raise GroqNativeSchemaError("$ref must be a string")
        parts = _pointer_parts(ref)
        # Keep the canonical pointer in the stack.  This catches cycles even
        # when two textual references reach the same definition via pointers.
        if ref in stack:
            chain = " -> ".join((*stack, ref))
            raise GroqNativeSchemaError(f"cyclic $ref: {chain}")
        target = _resolve_pointer(definitions, parts, ref)
        resolved, active_stack = _resolve_refs(target, definitions, (*stack, ref))
        siblings = {key: item for key, item in mapping.items() if key != "$ref"}
        if siblings:
            resolved = _merge_ref_siblings(resolved, siblings, ref)
            # A sibling may itself carry a local ref, so resolve it after the
            # merge as well.  The current ref remains on the stack to retain
            # cycle detection while processing those siblings.
            resolved, active_stack = _resolve_refs(resolved, definitions, active_stack)
        return resolved, active_stack

    # Do not recursively copy arbitrary annotation values.  They are removed
    # by the structural walker; recursively resolving the shape-bearing
    # values is enough to detect invalid refs in the emitted contract.
    return dict(mapping), stack


def _check_keys(node: Mapping[str, Any], path: str) -> None:
    for key in node:
        if key in {"$ref", "$defs"} or key in _STRUCTURAL_KEYS or key in _IGNORED_SCHEMA_KEYS:
            continue
        if key in _UNSUPPORTED_STRUCTURAL_KEYS:
            raise GroqNativeSchemaError(f"unsupported structural keyword {key!r} at {path}")
        raise GroqNativeSchemaError(f"unsupported schema keyword {key!r} at {path}")


def _transform_node(
    value: Any,
    definitions: Mapping[str, Any],
    *,
    path: str,
    ref_stack: tuple[str, ...] = (),
) -> Any:
    """Transform one schema node into Groq's strict structural subset."""

    if isinstance(value, bool):
        # Boolean schemas are valid JSON Schema, but do not describe one of
        # Groq's supported primitive/complex types.  Refuse them rather than
        # accidentally widening or narrowing a field.
        raise GroqNativeSchemaError(f"boolean schema is unsupported at {path}")
    if not isinstance(value, Mapping):
        raise GroqNativeSchemaError(f"schema node must be an object at {path}")

    resolved, active_ref_stack = _resolve_refs(value, definitions, ref_stack)
    if isinstance(resolved, bool) or not isinstance(resolved, Mapping):
        raise GroqNativeSchemaError(f"schema node must be an object at {path}")
    node = cast(Mapping[str, Any], resolved)
    _check_keys(node, path)

    raw_type = node.get("type")
    if raw_type is None or isinstance(raw_type, str):
        type_value: str | list[str] | None = raw_type
    elif isinstance(raw_type, list) and all(
        isinstance(item, str) for item in cast(list[Any], raw_type)
    ):
        type_value = cast(list[str], raw_type)
    else:
        raise GroqNativeSchemaError(f"schema type must be a string or list of strings at {path}")

    result: JsonSchema = {}
    if type_value is not None:
        result["type"] = deepcopy(type_value)
    if "enum" in node:
        enum = node["enum"]
        if not isinstance(enum, list) or not enum:
            raise GroqNativeSchemaError(f"enum must be a non-empty list at {path}")
        result["enum"] = deepcopy(cast(list[Any], enum))

    if "anyOf" in node:
        alternatives = node["anyOf"]
        if not isinstance(alternatives, list) or not alternatives:
            raise GroqNativeSchemaError(f"anyOf must be a non-empty list at {path}")
        # Do not collapse nullable unions into ``type: [..]``.  Groq accepts
        # anyOf and retaining it is important for Pydantic's nullable fields.
        result["anyOf"] = [
            _transform_node(
                item,
                definitions,
                path=f"{path}.anyOf[{index}]",
                ref_stack=active_ref_stack,
            )
            for index, item in enumerate(cast(list[Any], alternatives))
        ]

    if type_value == "object":
        properties = node.get("properties", {})
        if not isinstance(properties, Mapping):
            raise GroqNativeSchemaError(f"object properties must be an object at {path}")

        # Validate an authored additionalProperties schema even though strict
        # mode closes the object.  An unresolved reference must never be
        # silently discarded by the provider adapter.
        additional_properties = node.get("additionalProperties")
        if isinstance(additional_properties, Mapping):
            _transform_node(
                additional_properties,
                definitions,
                path=f"{path}.additionalProperties",
                ref_stack=active_ref_stack,
            )
        elif additional_properties is not None and not isinstance(additional_properties, bool):
            raise GroqNativeSchemaError(
                f"additionalProperties must be a boolean or object at {path}"
            )

        property_mapping = cast(Mapping[str, Any], properties)
        result["properties"] = {
            str(name): _transform_node(
                child,
                definitions,
                path=f"{path}.properties[{name!r}]",
                ref_stack=active_ref_stack,
            )
            for name, child in property_mapping.items()
        }
        result["required"] = list(result["properties"].keys())
        result["additionalProperties"] = False
    elif type_value == "array":
        if "items" not in node:
            raise GroqNativeSchemaError(f"array items are required at {path}")
        result["items"] = _transform_node(
            node["items"],
            definitions,
            path=f"{path}.items",
            ref_stack=active_ref_stack,
        )
    elif (
        "properties" in node
        or "required" in node
        or "additionalProperties" in node
        or "items" in node
    ):
        raise GroqNativeSchemaError(f"object/array keywords do not match type at {path}")

    return result


def transform_groq_native_schema(schema: Mapping[str, Any]) -> JsonSchema:
    """Return a Groq-compatible strict schema without mutating ``schema``.

    The function inlines every reachable local ``#/$defs/...`` reference,
    removes unsupported annotations/validation constraints, closes every
    object, and marks every object property as required.  External,
    unresolved, and cyclic references, as well as unsupported structural
    keywords, raise :class:`GroqNativeSchemaError`.
    """

    if not isinstance(schema, Mapping):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise GroqNativeSchemaError("root schema must be an object")
    source = deepcopy(dict(schema))
    raw_definitions = source.pop("$defs", {})
    if not isinstance(raw_definitions, Mapping):
        raise GroqNativeSchemaError("$defs must be an object")
    definitions = cast(Mapping[str, Any], raw_definitions)

    # Validate every definition, including definitions not currently reachable
    # from the root.  This prevents an invalid/cyclic local graph from being
    # hidden by the fact that the provider payload would otherwise drop
    # ``$defs`` wholesale.
    for name in definitions:
        _transform_node(
            {"$ref": f"{_LOCAL_DEFS_PREFIX}{name}"},
            definitions,
            path=f"$defs[{name!r}]",
        )

    transformed = _transform_node(source, definitions, path="$")
    return cast(JsonSchema, transformed)


# A concise alias is useful at provider call sites and keeps the public helper
# discoverable for callers that already use the ``groq_native`` module name.
groq_native_schema = transform_groq_native_schema


def _groq_error_code(body: object) -> str | None:
    """Extract only Groq's structured error code from a JSON-like body.

    Groq may return the error object either as the response body itself or
    nested under ``error``.  Read only the code field; provider messages and
    failed generations must never become model response content or metadata.
    """

    if not isinstance(body, Mapping):
        return None
    body_mapping = cast(Mapping[str, object], body)
    error = body_mapping.get("error")
    source: Mapping[str, object] = (
        cast(Mapping[str, object], error) if isinstance(error, Mapping) else body_mapping
    )
    code = source.get("code")
    return code if isinstance(code, str) else None


def _is_json_validate_failed(exc: ModelHTTPError) -> bool:
    """Return whether ``exc`` is Groq's native-output validation failure."""

    return exc.status_code == 400 and _groq_error_code(exc.body) == "json_validate_failed"


class GroqNativeModel(GroqModel):
    """Groq model that adapts only native output schemas on the wire."""

    def customize_request_parameters(
        self,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelRequestParameters:
        # Keep the profile's normal handling for function tools.  The Groq
        # compatibility pass is then applied only to the native response
        # object, leaving tool schemas on their established provider path.
        prepared = super().customize_request_parameters(model_request_parameters)
        if (
            prepared.output_mode == "native"
            and (output_object := prepared.output_object) is not None
        ):
            return replace(
                prepared,
                output_object=replace(
                    output_object,
                    json_schema=transform_groq_native_schema(output_object.json_schema),
                ),
            )
        return prepared

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """Turn Groq native JSON validation failures into output retries.

        Groq reports malformed native JSON as HTTP 400 rather than returning a
        response for Pydantic AI to validate.  Returning an empty response
        keeps the failure on Pydantic AI's normal bounded output-retry path.
        Only the exact native-output error is adapted; all other provider
        failures retain the base Groq model behavior.
        """

        try:
            return await super().request(messages, model_settings, model_request_parameters)
        except ModelHTTPError as exc:
            if model_request_parameters.output_mode == "native" and _is_json_validate_failed(exc):
                return ModelResponse(
                    parts=[],
                    model_name=exc.model_name,
                    provider_name=self._provider.name,
                    provider_url=self.base_url,
                    finish_reason="error",
                )
            raise


# Explicit model-family spelling for callers that prefer the endpoint name.
GroqGPTOSSModel = GroqNativeModel


__all__ = [
    "GroqGPTOSSModel",
    "GroqNativeModel",
    "GroqNativeSchemaError",
    "groq_native_schema",
    "transform_groq_native_schema",
]
