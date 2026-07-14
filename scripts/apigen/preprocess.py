"""Preprocesses OpenAPI specs for code generation."""

import copy
import json
import re

from scripts.apigen.clientgen import query_request_model_name, resolve_method_names


def preprocess_truss_config_schema(data: bytes) -> bytes:
    doc = json.loads(data)

    # Rename Truss-prefixed definitions and the root title to Model-prefixed.
    # Field names (e.g. truss_*) are property keys, not definition names, and
    # are left untouched.
    defs = doc.get("$defs", {})
    renames = {
        name: "Model" + name[len("Truss") :]
        for name in defs
        if name.startswith("Truss")
    }
    if renames:
        _rename_defs_refs(doc, renames)
        for old, new in renames.items():
            defs[new] = defs.pop(old)
    title = doc.get("title")
    if isinstance(title, str) and title.startswith("Truss"):
        doc["title"] = "Model" + title[len("Truss") :]

    return json.dumps(doc, indent=2).encode()


_DEFS_REF_PATTERN = re.compile(r"#/\$defs/(\w+)")


def _rename_defs_refs(node: object, renames: dict[str, str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")  # ty: ignore[invalid-argument-type]
        if isinstance(ref, str):
            m = _DEFS_REF_PATTERN.fullmatch(ref)
            if m and m.group(1) in renames:
                node["$ref"] = f"#/$defs/{renames[m.group(1)]}"  # ty: ignore[invalid-assignment]
        for child in node.values():
            _rename_defs_refs(child, renames)
    elif isinstance(node, list):
        for child in node:
            _rename_defs_refs(child, renames)


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

    # Synthesize a request schema per GET operation's query parameters so
    # datamodel-code-generator emits a typed model for them (it only
    # generates query-parameter models under the paths scope, which drags
    # in unwanted per-operation wrappers). Injected before the V1 rename
    # below so their $refs to enums are rewritten with everything else.
    _inject_query_request_schemas(doc)

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


def _inject_query_request_schemas(doc: dict) -> None:
    # Build an object schema whose properties are the operation's query
    # parameters, named to match its client method (e.g. get_users ->
    # GetUsersRequest). Each parameter's own schema (enum $refs, arrays,
    # nullable wrappers, constraints) is reused verbatim so the third
    # party types every field. GET carries only query params and every
    # other method only a body, so this name never collides with a body.
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    method_names = resolve_method_names(doc)

    for path, path_item in doc.get("paths", {}).items():
        for http_method, op in path_item.items():
            if http_method == "parameters" or not isinstance(op, dict):
                continue
            query_params = [
                p
                for p in op.get("parameters", [])
                if isinstance(p, dict) and p.get("in") == "query"
            ]
            if not query_params:
                continue
            if "requestBody" in op:
                raise ValueError(
                    f"{http_method.upper()} {path} has both a request body and "
                    "query parameters; the client generator assumes GET carries "
                    "only query parameters and other methods only a body"
                )
            name = query_request_model_name(method_names[(path, http_method)])
            if name in schemas:
                raise ValueError(
                    f"injected query schema {name} collides with an existing schema"
                )
            properties: dict = {}
            required: list[str] = []
            for p in query_params:
                schema = copy.deepcopy(p.get("schema", {}))
                if "description" not in schema and p.get("description"):
                    schema["description"] = p["description"]
                properties[p["name"]] = schema
                if p.get("required"):
                    required.append(p["name"])
            obj: dict = {"type": "object", "title": name, "properties": properties}
            if required:
                obj["required"] = required
            schemas[name] = obj


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
