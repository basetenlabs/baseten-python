import json
import re
from dataclasses import dataclass
from pathlib import Path


def generate_client(spec_data: bytes, out_file: Path) -> None:
    spec = json.loads(spec_data)
    ops = _extract_operations(spec)
    src = _render_client(ops)
    out_file.write_text(src)


_PATH_PARAM_RE = re.compile(r"\{(\w+)\}")


@dataclass
class _Operation:
    name: str
    http_method: str
    path: str
    path_params: list[str]
    has_body: bool
    req_body_ref: str
    resp_ref: str
    success_code: int
    error_codes: dict[int, str] | None
    summary: str
    query_ref: str
    query_required: bool


def resolve_method_names(spec: dict) -> dict[tuple[str, str], str]:
    """Map each (path, http_method) to its resolved client method name.

    Names are derived from the method and path, using a trailing path
    parameter only where needed to disambiguate collisions. Shared with
    preprocessing so injected query-parameter schemas can be named to
    match their operation's method.
    """
    paths = spec.get("paths", {})
    raw: list[tuple[str, str, dict]] = []
    short_names: dict[str, int] = {}
    for path, path_item in paths.items():
        for http_method, op_data in path_item.items():
            if http_method == "parameters" or not isinstance(op_data, dict):
                continue
            raw.append((path, http_method, op_data))
            name = _derive_method_name(
                http_method, path, op_data, keep_trailing_param=False
            )
            short_names[name] = short_names.get(name, 0) + 1

    result: dict[tuple[str, str], str] = {}
    for path, http_method, op_data in raw:
        short = _derive_method_name(
            http_method, path, op_data, keep_trailing_param=False
        )
        if short_names[short] > 1:
            name = _derive_method_name(
                http_method, path, op_data, keep_trailing_param=True
            )
        else:
            name = short
        result[(path, http_method)] = name
    return result


def query_request_model_name(method_name: str) -> str:
    """Model name for an operation's injected query-parameter schema."""
    return _snake_to_pascal(method_name) + "Request"


def _snake_to_pascal(s: str) -> str:
    return "".join(part.capitalize() for part in s.split("_"))


def _extract_operations(spec: dict) -> list[_Operation]:
    paths = spec.get("paths", {})
    names = resolve_method_names(spec)

    ops: list[_Operation] = []
    for path, path_item in paths.items():
        for http_method, op_data in path_item.items():
            if http_method == "parameters" or not isinstance(op_data, dict):
                continue
            name = names[(path, http_method)]
            query_params = [
                p
                for p in op_data.get("parameters", [])
                if isinstance(p, dict) and p.get("in") == "query"
            ]
            query_ref = query_request_model_name(name) if query_params else ""
            query_required = any(p.get("required") for p in query_params)
            ops.append(
                _Operation(
                    name=name,
                    http_method=http_method.upper(),
                    path=path,
                    path_params=_PATH_PARAM_RE.findall(path),
                    has_body="requestBody" in op_data,
                    req_body_ref=_body_schema_ref(spec, op_data),
                    resp_ref=_response_schema_ref(spec, op_data),
                    success_code=_extract_success_code(op_data, http_method, path),
                    error_codes=_error_code_map(spec, op_data),
                    summary=op_data.get("summary", ""),
                    query_ref=query_ref,
                    query_required=query_required,
                )
            )
    ops.sort(key=lambda o: o.name)
    return ops


def _extract_success_code(op: dict, http_method: str, path: str) -> int:
    responses = op.get("responses", {})
    codes = [int(c) for c in responses if c.isdigit() and 200 <= int(c) < 300]
    if len(codes) != 1:
        raise ValueError(
            f"expected exactly one 2xx response for {http_method.upper()} {path}, got {codes}"
        )
    return codes[0]


def _derive_method_name(
    http_method: str, path: str, op: dict, *, keep_trailing_param: bool
) -> str:
    if op_id := op.get("operationId"):
        return _camel_to_snake(op_id)
    segments = path.removeprefix("/v1/").strip("/").split("/")
    result: list[str] = []
    for i, seg in enumerate(segments):
        m = _PATH_PARAM_RE.fullmatch(seg)
        if m:
            if keep_trailing_param and i == len(segments) - 1:
                result.append(m.group(1))
        else:
            result.append(seg)
    return http_method.lower() + "_" + "_".join(result).replace("-", "_")


def _camel_to_snake(s: str) -> str:
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s).lower()


def _resolve_ref(spec: dict, node: dict | None) -> dict | None:
    if node is None:
        return None
    ref = node.get("$ref")
    if not ref:
        return node
    cur: object = spec
    for p in ref.removeprefix("#/").split("/"):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur if isinstance(cur, dict) else None


def _json_content_schema_ref(node: dict | None) -> str:
    if node is None:
        return ""
    ref = (
        node.get("content", {})
        .get("application/json", {})
        .get("schema", {})
        .get("$ref", "")
    )
    return ref.rsplit("/", 1)[-1] if ref else ""


def _body_schema_ref(spec: dict, op: dict) -> str:
    rb = op.get("requestBody")
    if not isinstance(rb, dict):
        return ""
    return _json_content_schema_ref(_resolve_ref(spec, rb))


def _response_schema_ref(spec: dict, op: dict) -> str:
    responses = op.get("responses", {})
    for code in ("200", "201", "202"):
        resp_node = responses.get(code)
        if not isinstance(resp_node, dict):
            continue
        resolved = _resolve_ref(spec, resp_node)
        if ref := _json_content_schema_ref(resolved):
            return ref
        # If the response was a $ref to components/responses and has JSON
        # content, use the response component name as the type.
        if resp_node.get("$ref") and _has_json_content(resolved):
            return resp_node["$ref"].rsplit("/", 1)[-1]
    return ""


def _has_json_content(node: dict | None) -> bool:
    if node is None:
        return False
    return "application/json" in node.get("content", {})


def _error_code_map(spec: dict, op: dict) -> dict[int, str] | None:
    responses = op.get("responses", {})
    result: dict[int, str] = {}
    for code_str, resp_raw in responses.items():
        if not code_str.isdigit():
            continue
        code = int(code_str)
        if code < 400 or not isinstance(resp_raw, dict):
            continue
        resolved = _resolve_ref(spec, resp_raw)
        if ref := _json_content_schema_ref(resolved):
            result[code] = ref
    return result or None


def _path_fmt(path: str) -> str:
    return _PATH_PARAM_RE.sub("{}", path)


def _render_client(ops: list[_Operation]) -> str:
    has_typed_resp = any(op.resp_ref for op in ops)
    has_no_resp = any(not op.resp_ref for op in ops)

    error_refs = sorted({ref for op in ops for ref in (op.error_codes or {}).values()})

    model_imports: set[str] = set()
    for op in ops:
        if op.req_body_ref:
            model_imports.add(op.req_body_ref)
        if op.query_ref:
            model_imports.add(op.query_ref)
        if op.resp_ref:
            model_imports.add(op.resp_ref)
        for ref in (op.error_codes or {}).values():
            model_imports.add(ref)

    src = f"""\
# Code generated by apigen/clientgen. DO NOT EDIT.

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from ._models import (
{chr(10).join(f"    {name}," for name in sorted(model_imports))}
)

_T = TypeVar("_T", bound=BaseModel)


@dataclass
class ResponseError(Exception):
    status_code: int
    body: str

    def __str__(self) -> str:
        return f"baseten API error (HTTP {{self.status_code}}): {{self.body}}"
"""

    for ref in error_refs:
        field_name = _camel_to_snake(ref)
        src += f"""

@dataclass
class Response{ref}(Exception):
    status_code: int
    {field_name}: {ref}

    def __str__(self) -> str:
        return f"baseten API error (HTTP {{self.status_code}}): {{self.{field_name}.model_dump_json()}}"
"""

    if error_refs:
        entries = "\n".join(
            f'    "{ref}": ({ref}, Response{ref}, "{_camel_to_snake(ref)}"),'
            for ref in error_refs
        )
        src += f"""

_ERROR_TYPES: dict[str, tuple[type[BaseModel], type[Exception], str]] = {{
{entries}
}}
"""

    src += """

@dataclass
class _ApiRequest:
    method: str
    path_fmt: str
    path_args: list[str]
    body: Any
    query: Any
    success_code: int
    error_codes: dict[int, str] | None
"""

    src += "\n\n" + _render_client_class(
        ops, has_typed_resp, has_no_resp, is_async=False
    )
    src += "\n\n" + _render_client_class(
        ops, has_typed_resp, has_no_resp, is_async=True
    )
    src += "\n"
    return src


def _render_client_class(
    ops: list[_Operation],
    has_typed_resp: bool,
    has_no_resp: bool,
    *,
    is_async: bool,
) -> str:
    cls = "AsyncApiClient" if is_async else "ApiClient"
    http_cls = "httpx.AsyncClient" if is_async else "httpx.Client"
    aw = "await " if is_async else ""
    adef = "async def" if is_async else "def"

    src = f"""\
class {cls}:
    \"""Generated HTTP client for the Baseten API.

    Methods on this client are generated from the OpenAPI specification
    and are NOT covered by any stability or compatibility guarantees.
    They may change without notice between versions.
    \"""

    def __init__(self, http_client: {http_cls}) -> None:
        \"""Create a new client. The caller is responsible for closing *http_client*.\"""
        self._http_client = http_client
"""

    for op in ops:
        src += "\n" + _render_method(op, is_async=is_async)

    error_dispatch = ""
    if any(op.error_codes for op in ops):
        error_dispatch = """\
            if request.error_codes and response.status_code in request.error_codes:
                error_name = request.error_codes[response.status_code]
                if error_name in _ERROR_TYPES:
                    model_cls, exc_cls, field_name = _ERROR_TYPES[error_name]
                    try:
                        model = model_cls.model_validate_json(response.content)
                        raise exc_cls(
                            status_code=response.status_code,  # ty: ignore[unknown-argument]
                            **{field_name: model},
                        )
                    except exc_cls:
                        raise
                    except Exception:
                        pass
"""

    src += f"""
    {adef} _do(self, request: _ApiRequest) -> httpx.Response:
        path = request.path_fmt.format(
            *[urllib.parse.quote(a, safe="") for a in request.path_args]
        )
        json_body = None
        if request.body is not None:
            if isinstance(request.body, BaseModel):
                # Only fields the caller set are sent, so unset fields fall
                # back to the server default rather than being reset here.
                # An explicit None is kept, since null can mean "clear".
                json_body = request.body.model_dump(mode="json", exclude_unset=True)
            else:
                json_body = request.body
        params = None
        if request.query is not None:
            if isinstance(request.query, BaseModel):
                # As above, plus dropping None: a null query parameter is
                # meaningless and would otherwise serialize as an empty string.
                params = request.query.model_dump(
                    mode="json", exclude_unset=True, exclude_none=True
                )
            else:
                params = request.query
        response = {aw}self._http_client.request(request.method, path, json=json_body, params=params)
        if response.status_code != request.success_code:
{error_dispatch}\
            raise ResponseError(status_code=response.status_code, body=response.text)
        return response
"""

    if has_typed_resp:
        src += f"""
    {adef} _do_json(self, response_type: type[_T], request: _ApiRequest) -> _T:
        response = {aw}self._do(request)
        content_type = response.headers.get("content-type", "")
        if not content_type.startswith("application/json"):
            raise ValueError(f"unexpected content type {{content_type!r}}, expected application/json")
        return response_type.model_validate_json(response.content)
"""

    if has_no_resp:
        src += f"""
    {adef} _do_no_response(self, request: _ApiRequest) -> None:
        {aw}self._do(request)
"""

    return src


def _render_method(op: _Operation, *, is_async: bool) -> str:
    adef = "async def" if is_async else "def"
    aw = "await " if is_async else ""

    # An operation carries at most one input model: a request body (any
    # method other than GET) or query parameters (GET only). Both surface
    # as a single keyword-only `request` argument. Query requests with no
    # required fields are optional so callers can omit them entirely; body
    # requests are always required so an empty body still sends `{}`.
    if op.query_ref:
        input_type = op.query_ref
        input_required = op.query_required
    elif op.has_body:
        input_type = op.req_body_ref if op.req_body_ref else "Any"
        input_required = True
    else:
        input_type = ""
        input_required = False

    params = ["self"]
    if op.path_params or input_type:
        params.append("*")
    for p in op.path_params:
        params.append(f"{p}: str")
    if input_type:
        if input_required:
            params.append(f"request: {input_type}")
        else:
            params.append(f"request: {input_type} | None = None")

    ret = f" -> {op.resp_ref}" if op.resp_ref else " -> None"
    path_args = f"[{', '.join(op.path_params)}]" if op.path_params else "[]"
    body_arg = "request" if (input_type and not op.query_ref) else "None"
    query_arg = "request" if op.query_ref else "None"

    if op.error_codes:
        codes = sorted(op.error_codes.items())
        error_expr = "{" + ", ".join(f"{c}: {ref!r}" for c, ref in codes) + "}"
    else:
        error_expr = "None"

    req = (
        f"_ApiRequest("
        f"method={op.http_method!r}, "
        f"path_fmt={_path_fmt(op.path)!r}, "
        f"path_args={path_args}, "
        f"body={body_arg}, "
        f"query={query_arg}, "
        f"success_code={op.success_code}, "
        f"error_codes={error_expr})"
    )

    sig = f"    {adef} {op.name}({', '.join(params)}){ret}:"
    if op.summary:
        sig += f'\n        """{op.summary}"""'

    if op.resp_ref:
        return f"{sig}\n        return {aw}self._do_json({op.resp_ref}, {req})\n"
    else:
        return f"{sig}\n        {aw}self._do_no_response({req})\n"
