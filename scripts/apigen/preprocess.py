"""Preprocesses OpenAPI specs for code generation."""

import copy
import json
import re
from collections.abc import Callable

from scripts.apigen.clientgen import (
    query_params_model_name,
    resolve_method_names,
    response_type_model_name,
)


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
        ref = node.get("$ref")
        if isinstance(ref, str):
            m = _DEFS_REF_PATTERN.fullmatch(ref)
            if m and m.group(1) in renames:
                node["$ref"] = f"#/$defs/{renames[m.group(1)]}"
        for child in node.values():
            _rename_defs_refs(child, renames)
    elif isinstance(node, list):
        for child in node:
            _rename_defs_refs(child, renames)


def preprocess_spec(
    data: bytes,
    *,
    query_and_body_allowed: Callable[[str], bool] | None = None,
) -> bytes:
    doc = json.loads(data)

    _flatten_parameters(doc)

    # datamodel-code-generator has --openapi-scopes for schemas and
    # requestbodies, but not for responses. Hoist inline schemas from
    # both responses and requestBodies into components/schemas so they
    # get generated as models. We hoist requestBodies ourselves (rather
    # than using the requestbodies scope) to only take the JSON content
    # type and skip $refs to existing schemas, avoiding duplicate and
    # wrapper classes.
    _hoist_component_schemas(doc)

    # Prune before injecting: enums used only by inline query parameters stay
    # reachable through the operations, orphaned request/response models are
    # dropped, and the query schemas we add next are never at risk.
    _prune_unused_schemas(doc)

    # Synthesize a params schema per operation's query parameters so
    # datamodel-code-generator emits a typed model for them (it only
    # generates query-parameter models under the paths scope, which drags
    # in unwanted per-operation wrappers). Injected before the V1 rename
    # below so their $refs to enums are rewritten with everything else.
    _inject_query_params_schemas(doc, query_and_body_allowed=query_and_body_allowed)

    # Hoist inline 2xx application/json response schemas into
    # components/schemas so datamodel-code-generator emits a named model the
    # client can return. Without this, an operation whose success response is
    # an inline oneOf, array, or free-form object has no resolvable type name.
    # Injected before the V1 rename, for the same reason as the query schemas.
    _inject_response_schemas(doc)

    # datamodel-code-generator generates empty BaseModel classes for
    # schemas that are bare type: object with no properties (e.g.
    # PredictInput). Adding additionalProperties makes it correctly
    # generate dict[str, Any] instead.
    _fix_bare_object_schemas(doc)

    # Schema renames: a trailing V1 is stripped (ModelV1 -> Model) and names
    # that are not valid Python identifiers are folded to PascalCase
    # (archive.Change -> ArchiveChange).
    schema_renames = _build_v1_renames(doc)
    if schema_renames:
        _rename_refs(doc, schema_renames)
        schemas = doc.get("components", {}).get("schemas", {})
        for old, new in schema_renames.items():
            schemas[new] = schemas.pop(old)

    return json.dumps(doc, indent=2).encode()


def _flatten_parameters(doc: dict) -> None:
    # Operations inherit path-item parameters, and either level may $ref
    # components/parameters; inline both so downstream steps see concrete
    # parameter objects.
    component_params = doc.get("components", {}).get("parameters", {})

    def resolve(param: dict) -> dict:
        ref = param.get("$ref")
        if ref is None:
            return param
        resolved = component_params.get(ref.rsplit("/", 1)[-1])
        if resolved is None:
            raise ValueError(f"unresolved parameter reference {ref}")
        return copy.deepcopy(resolved)

    for path_item in doc.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        item_params = [
            resolve(p) for p in path_item.pop("parameters", []) if isinstance(p, dict)
        ]
        for http_method, op in path_item.items():
            if http_method == "parameters" or not isinstance(op, dict):
                continue
            op["parameters"] = copy.deepcopy(item_params) + [
                resolve(p) for p in op.get("parameters", []) if isinstance(p, dict)
            ]


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


def _prune_unused_schemas(doc: dict) -> None:
    # Remove component schemas not reachable from any operation. Reachability
    # roots are every schema $ref outside components/schemas (paths and the
    # other component sections); each reachable schema's own $refs are then
    # followed transitively.
    components = doc.get("components", {})
    schemas = components.get("schemas", {})

    roots: set[str] = set()
    _collect_schema_refs(doc.get("paths", {}), roots)
    for section, value in components.items():
        if section != "schemas":
            _collect_schema_refs(value, roots)

    reachable: set[str] = set()
    queue = [name for name in roots if name in schemas]
    while queue:
        name = queue.pop()
        if name in reachable:
            continue
        reachable.add(name)
        refs: set[str] = set()
        _collect_schema_refs(schemas[name], refs)
        for ref in refs:
            if ref not in reachable and ref in schemas:
                queue.append(ref)

    for name in list(schemas):
        if name not in reachable:
            del schemas[name]


def _collect_schema_refs(node: object, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            m = _REF_PATTERN.fullmatch(ref)
            if m:
                out.add(m.group(1))
        # Discriminator mapping values are schema refs but not under a $ref key.
        disc = node.get("discriminator")
        if isinstance(disc, dict):
            mapping = disc.get("mapping")
            if isinstance(mapping, dict):
                for value in mapping.values():
                    if isinstance(value, str):
                        m = _REF_PATTERN.fullmatch(value)
                        if m:
                            out.add(m.group(1))
        for child in node.values():
            _collect_schema_refs(child, out)
    elif isinstance(node, list):
        for child in node:
            _collect_schema_refs(child, out)


def _inject_query_params_schemas(
    doc: dict, *, query_and_body_allowed: Callable[[str], bool] | None
) -> None:
    # Build an object schema whose properties are the operation's query
    # parameters, named to match its client method (e.g. get_users ->
    # GetUsersParams). Each parameter's own schema (enum $refs, arrays,
    # nullable wrappers, constraints) is reused verbatim so the third
    # party types every field.
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
            if "requestBody" in op and not (
                query_and_body_allowed and query_and_body_allowed(path)
            ):
                raise ValueError(
                    f"{http_method.upper()} {path} has both a request body and "
                    "query parameters, which this API does not allow"
                )
            name = query_params_model_name(method_names[(path, http_method)])
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


def _inject_response_schemas(doc: dict) -> None:
    schemas = doc.setdefault("components", {}).setdefault("schemas", {})
    method_names = resolve_method_names(doc)

    for path, path_item in doc.get("paths", {}).items():
        for http_method, op in path_item.items():
            if http_method == "parameters" or not isinstance(op, dict):
                continue
            responses = op.get("responses", {})
            success_codes = sorted(
                c
                for c in responses
                if c.isdigit()
                and 200 <= int(c) < 300
                and isinstance(responses[c], dict)
            )
            for code in success_codes:
                json_content = (
                    responses[code].get("content", {}).get("application/json")
                )
                schema = (
                    json_content.get("schema")
                    if isinstance(json_content, dict)
                    else None
                )
                if not isinstance(schema, dict) or "$ref" in schema:
                    continue
                base = response_type_model_name(method_names[(path, http_method)])
                # Only the first success code gets the bare name, so several
                # 2xx bodies yield one schema per code, not a collision.
                schema_name = base if code == success_codes[0] else f"{base}{code}"
                if schema_name in schemas:
                    raise ValueError(
                        f"injected response schema {schema_name} collides with an existing schema"
                    )
                schemas[schema_name] = schema
                json_content["schema"] = {"$ref": f"#/components/schemas/{schema_name}"}


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
    # A trailing V1 is dropped, and a name that is not a valid Python
    # identifier (e.g. archive.Change) is folded into PascalCase so it can
    # be emitted and imported by name.
    schemas = doc.get("components", {}).get("schemas", {})
    renames: dict[str, str] = {}
    for name in schemas:
        renamed = name.removesuffix("V1")
        if not re.fullmatch(r"[A-Za-z_]\w*", renamed):
            renamed = "".join(
                part[0].upper() + part[1:]
                for part in re.split(r"[.-]", renamed)
                if part
            )
        if renamed != name:
            renames[name] = renamed
    taken = {n for n in schemas if n not in renames}
    for old, new in renames.items():
        if new in taken:
            raise ValueError(
                f"schema rename {old} -> {new} collides with an existing schema name"
            )
        taken.add(new)
    return renames


_REF_PATTERN = re.compile(r"#/components/schemas/([^/]+)")


def _rename_refs(node: object, renames: dict[str, str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            m = _REF_PATTERN.fullmatch(ref)
            if m and m.group(1) in renames:
                node["$ref"] = f"#/components/schemas/{renames[m.group(1)]}"
        # Discriminator mapping values are schema refs but not under a $ref key.
        # Leaving them stale makes datamodel-code-generator fail to resolve the
        # tag for unions discriminated on an enum field, silently dropping the
        # discriminator (and crashing outright on some versions).
        disc = node.get("discriminator")
        if isinstance(disc, dict):
            mapping = disc.get("mapping")
            if isinstance(mapping, dict):
                for key, value in mapping.items():
                    if isinstance(value, str):
                        m = _REF_PATTERN.fullmatch(value)
                        if m and m.group(1) in renames:
                            mapping[key] = f"#/components/schemas/{renames[m.group(1)]}"
        for child in node.values():
            _rename_refs(child, renames)
    elif isinstance(node, list):
        for child in node:
            _rename_refs(child, renames)
