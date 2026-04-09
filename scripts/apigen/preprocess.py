"""Preprocesses OpenAPI specs for code generation."""

import json
import re


def preprocess_spec(data: bytes) -> bytes:
    doc = json.loads(data)

    # datamodel-code-generator has --openapi-scopes for schemas and
    # requestbodies, but not for responses. Hoist inline schemas from
    # both responses and requestBodies into components/schemas so they
    # get generated as models. We hoist requestBodies ourselves (rather
    # than using the requestbodies scope) to only take the JSON content
    # type and skip $refs to existing schemas, avoiding duplicate and
    # wrapper classes.
    _hoist_component_schemas(doc)

    # datamodel-code-generator generates empty BaseModel classes for
    # schemas that are bare type: object with no properties (e.g.
    # PredictInput). Adding additionalProperties makes it correctly
    # generate dict[str, Any] instead.
    _fix_bare_object_schemas(doc)

    # Management API schemas are suffixed with V1 (e.g. ModelV1). Strip
    # the suffix so generated class names are cleaner (e.g. Model).
    schema_renames = _build_v1_renames(doc)
    if schema_renames:
        _rename_refs(doc, schema_renames)
        schemas = doc.get("components", {}).get("schemas", {})
        for old, new in schema_renames.items():
            schemas[new] = schemas.pop(old)

    return json.dumps(doc, indent=2).encode()


def _hoist_component_schemas(doc: dict) -> None:
    # Hoist inline JSON schemas from components/responses and
    # components/requestBodies into components/schemas. Entries that are
    # just a $ref to an existing schema (or whose JSON content schema is
    # a $ref) are skipped — they'd only produce pointless wrapper classes.
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})

    for section in ("responses", "requestBodies"):
        entries = doc.get("components", {}).get(section)
        if not entries:
            continue
        for name, entry in entries.items():
            if "$ref" in entry:
                continue
            content = entry.get("content", {}).get("application/json", {})
            schema = content.get("schema")
            if schema is None:
                continue
            if "$ref" in schema and schema["$ref"].startswith("#/components/schemas/"):
                continue
            schemas[name] = schema
            content["schema"] = {"$ref": f"#/components/schemas/{name}"}


def _fix_bare_object_schemas(doc: dict) -> None:
    schemas = doc.get("components", {}).get("schemas", {})
    for schema in schemas.values():
        if not isinstance(schema, dict):
            continue
        if (
            schema.get("type") == "object"
            and "properties" not in schema
            and "additionalProperties" not in schema
            and "allOf" not in schema
            and "oneOf" not in schema
            and "anyOf" not in schema
        ):
            schema["additionalProperties"] = {}


def _build_v1_renames(doc: dict) -> dict[str, str]:
    schemas = doc.get("components", {}).get("schemas", {})
    renames = {}
    for name in schemas:
        if name.endswith("V1"):
            renames[name] = name[:-2]
    return renames


_REF_PATTERN = re.compile(r"#/components/schemas/(\w+)")


def _rename_refs(node: object, renames: dict[str, str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")  # ty: ignore[invalid-argument-type]
        if isinstance(ref, str):
            m = _REF_PATTERN.fullmatch(ref)
            if m and m.group(1) in renames:
                node["$ref"] = f"#/components/schemas/{renames[m.group(1)]}"  # ty: ignore[invalid-assignment]
        for child in node.values():
            _rename_refs(child, renames)
    elif isinstance(node, list):
        for child in node:
            _rename_refs(child, renames)
