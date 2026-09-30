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

_JSON_CONTENT = "application/json"
_MULTIPART_CONTENT = "multipart/form-data"


@dataclass
class _Operation:
    name: str
    http_method: str
    path: str
    path_params: list[str]
    query_ref: str
    query_required: bool
    has_body: bool
    req_body_ref: str
    body_content_type: str
    json_responses: list[tuple[int, str]]
    raw_accepts: list[str]
    success_codes: list[int]
    error_codes: dict[int, str] | None
    summary: str


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


def query_params_model_name(method_name: str) -> str:
    """Model name for an operation's injected query-parameter schema."""
    return _snake_to_pascal(method_name) + "Params"


def response_type_model_name(method_name: str) -> str:
    """Model name for an operation's injected inline response schema."""
    return _snake_to_pascal(method_name) + "Response"


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
            ops.append(
                _Operation(
                    name=name,
                    http_method=http_method.upper(),
                    path=path,
                    path_params=_PATH_PARAM_RE.findall(path),
                    query_ref=query_params_model_name(name) if query_params else "",
                    query_required=any(p.get("required") for p in query_params),
                    has_body="requestBody" in op_data,
                    req_body_ref=_body_schema_ref(spec, op_data),
                    body_content_type=_body_content_type(spec, op_data),
                    json_responses=_json_response_refs(spec, op_data),
                    raw_accepts=_raw_response_accepts(spec, op_data),
                    success_codes=_extract_success_codes(op_data, http_method, path),
                    error_codes=_error_code_map(spec, op_data),
                    summary=op_data.get("summary", ""),
                )
            )
    ops.sort(key=lambda o: o.name)
    return ops


def _extract_success_codes(op: dict, http_method: str, path: str) -> list[int]:
    responses = op.get("responses", {})
    codes = sorted(int(c) for c in responses if c.isdigit() and 200 <= int(c) < 300)
    if not codes:
        raise ValueError(
            f"expected at least one 2xx response for {http_method.upper()} {path}"
        )
    return codes


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
        node.get("content", {}).get(_JSON_CONTENT, {}).get("schema", {}).get("$ref", "")
    )
    return ref.rsplit("/", 1)[-1] if ref else ""


def _body_schema_ref(spec: dict, op: dict) -> str:
    rb = op.get("requestBody")
    if not isinstance(rb, dict):
        return ""
    return _json_content_schema_ref(_resolve_ref(spec, rb))


def _body_content_type(spec: dict, op: dict) -> str:
    """Non-JSON request body content type, or "" for JSON or no body.

    JSON is preferred when an operation declares several body encodings.
    """
    rb = _resolve_ref(spec, op.get("requestBody"))
    if rb is None:
        return ""
    content_types = list(rb.get("content", {}))
    if _JSON_CONTENT in content_types:
        return ""
    return content_types[0] if content_types else ""


def _success_responses(op: dict) -> dict[str, dict]:
    responses = op.get("responses", {})
    return {
        code: resp
        for code, resp in responses.items()
        if code.isdigit() and 200 <= int(code) < 300 and isinstance(resp, dict)
    }


def _json_response_refs(spec: dict, op: dict) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for code, resp_node in _success_responses(op).items():
        resolved = _resolve_ref(spec, resp_node)
        if ref := _json_content_schema_ref(resolved):
            result.append((int(code), ref))
        elif resp_node.get("$ref") and _has_json_content(resolved):
            # A $ref to components/responses with JSON content: use the
            # response component name as the type.
            result.append((int(code), resp_node["$ref"].rsplit("/", 1)[-1]))
    return result


def _raw_response_accepts(spec: dict, op: dict) -> list[str]:
    accepts: set[str] = set()
    for resp_node in _success_responses(op).values():
        resolved = _resolve_ref(spec, resp_node)
        if resolved is None:
            continue
        for content_type in resolved.get("content", {}):
            if content_type != _JSON_CONTENT:
                accepts.add(content_type)
    return sorted(accepts)


def _has_json_content(node: dict | None) -> bool:
    if node is None:
        return False
    return _JSON_CONTENT in node.get("content", {})


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
    has_typed_resp = any(op.json_responses for op in ops)
    # Status dispatch covers an operation with more than one success code and
    # at least one JSON schema among them, since the others may be bodyless.
    has_status_resp = any(op.json_responses and len(op.success_codes) > 1 for op in ops)
    has_raw = any(op.raw_accepts for op in ops)
    has_no_resp = any(not op.json_responses and not op.raw_accepts for op in ops)

    error_refs = sorted({ref for op in ops for ref in (op.error_codes or {}).values()})

    model_imports: set[str] = set()
    for op in ops:
        if op.req_body_ref:
            model_imports.add(op.req_body_ref)
        if op.query_ref:
            model_imports.add(op.query_ref)
        for _, ref in op.json_responses:
            model_imports.add(ref)
        for ref in (op.error_codes or {}).values():
            model_imports.add(ref)

    src = f"""\
# Code generated by apigen/clientgen. DO NOT EDIT.

from __future__ import annotations

import contextlib
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, IO, Literal, TypeVar, cast

import httpx
from pydantic import BaseModel, ValidationError

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
    success_codes: list[int]
    error_codes: dict[int, str] | None
    body_content_type: str | None = None
    accept: str | None = None
"""

    src += "\n\n" + _render_client_class(
        ops, has_typed_resp, has_status_resp, has_raw, has_no_resp, is_async=False
    )
    src += "\n\n" + _render_client_class(
        ops, has_typed_resp, has_status_resp, has_raw, has_no_resp, is_async=True
    )
    src += "\n"
    return src


def _render_client_class(
    ops: list[_Operation],
    has_typed_resp: bool,
    has_status_resp: bool,
    has_raw: bool,
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
        if op.json_responses:
            src += "\n" + _render_method(op, is_async=is_async, is_raw=False)
            if op.raw_accepts:
                src += "\n" + _render_method(op, is_async=is_async, is_raw=True)
        elif op.raw_accepts:
            src += "\n" + _render_method(op, is_async=is_async, is_raw=True)
        else:
            src += "\n" + _render_method(op, is_async=is_async, is_raw=False)

    error_dispatch = ""
    if any(op.error_codes for op in ops):
        error_dispatch = """\
            if request.error_codes and response.status_code in request.error_codes:
                error_name = request.error_codes[response.status_code]
                if error_name in _ERROR_TYPES:
                    model_cls, exc_cls, field_name = _ERROR_TYPES[error_name]
                    # A body that does not match the declared error schema
                    # falls through to the generic ResponseError below.
                    model = None
                    with contextlib.suppress(ValidationError):
                        model = model_cls.model_validate_json(response.content)
                    if model is not None:
                        raise exc_cls(
                            status_code=response.status_code,  # ty: ignore[unknown-argument]
                            **{field_name: model},
                        )
"""

    src += f"""
    def _build_request(self, request: _ApiRequest) -> httpx.Request:
        path = request.path_fmt.format(
            *[urllib.parse.quote(a, safe="") for a in request.path_args]
        )
        json_body = None
        content_body = None
        files_body = None
        headers: dict[str, str] = {{}}
        if request.accept is not None:
            headers["Accept"] = request.accept
        if request.body is not None:
            if request.body_content_type is None:
                if isinstance(request.body, BaseModel):
                    # Only fields the caller set are sent, so unset fields fall
                    # back to the server default rather than being reset here.
                    # An explicit None is kept, since null can mean "clear".
                    # by_alias: a field renamed for Python (e.g. async_ for
                    # "async") must serialize under its API name.
                    json_body = request.body.model_dump(
                        mode="json", exclude_unset=True, by_alias=True
                    )
                else:
                    json_body = request.body
            elif request.body_content_type == "{_MULTIPART_CONTENT}":
                # httpx derives the multipart Content-Type, boundary included.
                files_body = request.body
            else:
                headers["Content-Type"] = request.body_content_type
                content_body = request.body
        params = None
        if request.query is not None:
            if isinstance(request.query, BaseModel):
                # As above, plus dropping None: a null query parameter is
                # meaningless and would otherwise serialize as an empty string.
                params = request.query.model_dump(
                    mode="json", exclude_unset=True, exclude_none=True, by_alias=True
                )
            else:
                params = request.query
        return self._http_client.build_request(
            request.method,
            path,
            json=json_body,
            content=content_body,
            files=files_body,
            params=params,
            headers=headers,
        )

    {adef} _do(self, request: _ApiRequest) -> httpx.Response:
        response = {aw}self._http_client.send(self._build_request(request))
        if response.status_code not in request.success_codes:
{error_dispatch}\
            raise ResponseError(status_code=response.status_code, body=response.text)
        return response
"""

    if has_raw:
        resp_read = "await response.aread()" if is_async else "response.read()"
        src += f"""
    {adef} _do_raw(self, request: _ApiRequest) -> httpx.Response:
        response = {aw}self._http_client.send(self._build_request(request), stream=True)
        if response.status_code not in request.success_codes:
            {resp_read}
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

    if has_status_resp:
        src += f"""
    {adef} _do_json_with_status(
        self, response_types: Mapping[int, type[_T]], request: _ApiRequest
    ) -> _T:
        response = {aw}self._do(request)
        response_type = response_types.get(response.status_code)
        if response_type is None:
            # A bodyless success code, such as 204 alongside a JSON 200. The
            # call site's return type includes None only when one is declared,
            # so this cast is unreachable otherwise.
            return cast(_T, None)
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


def _render_method(op: _Operation, *, is_async: bool, is_raw: bool) -> str:
    adef = "async def" if is_async else "def"
    aw = "await " if is_async else ""

    # Query params go on `params`, request bodies on `request` (JSON) or
    # `files`/`content` for non-JSON. Bodies are always required so an empty
    # body still sends `{{}}`; query params only when the spec marks one.
    kwargs: list[str] = [f"{_camel_to_snake(p)}: str" for p in op.path_params]
    if op.query_ref:
        if op.query_required:
            kwargs.append(f"params: {op.query_ref}")
        else:
            kwargs.append(f"params: {op.query_ref} | None = None")
    if op.has_body:
        if op.body_content_type == _MULTIPART_CONTENT:
            kwargs.append("files: Any")
        elif op.body_content_type:
            kwargs.append("content: bytes | IO[bytes] | str")
        elif op.req_body_ref:
            kwargs.append(f"request: {op.req_body_ref}")
        else:
            kwargs.append("request: Any")

    # Every content type the success responses can produce. More than one
    # means the caller has to say which it wants via `accept`.
    content_types = ([_JSON_CONTENT] if op.json_responses else []) + op.raw_accepts
    if is_raw and len(content_types) > 1:
        accept_type = "Literal[" + ", ".join(repr(c) for c in content_types) + "]"
        kwargs.append(f"accept: {accept_type}")
        accept_expr = "accept"
    elif is_raw:
        accept_expr = repr(content_types[0])
    else:
        accept_expr = "None"

    if op.has_body:
        if op.body_content_type == _MULTIPART_CONTENT:
            body_arg = "files"
        elif op.body_content_type:
            body_arg = "content"
        else:
            body_arg = "request"
    else:
        body_arg = "None"
    query_arg = "params" if op.query_ref else "None"

    if op.error_codes:
        codes = sorted(op.error_codes.items())
        error_expr = "{" + ", ".join(f"{c}: {ref!r}" for c, ref in codes) + "}"
    else:
        error_expr = "None"

    req_parts = [
        f"method={op.http_method!r}",
        f"path_fmt={_path_fmt(op.path)!r}",
        "path_args=[" + ", ".join(_camel_to_snake(p) for p in op.path_params) + "]"
        if op.path_params
        else "path_args=[]",
        f"body={body_arg}",
        f"query={query_arg}",
        f"success_codes={op.success_codes!r}",
        f"error_codes={error_expr}",
    ]
    if op.body_content_type:
        req_parts.append(f"body_content_type={op.body_content_type!r}")
    if accept_expr != "None":
        req_parts.append(f"accept={accept_expr}")
    req = "_ApiRequest(" + ", ".join(req_parts) + ")"

    # The suffix marks the sibling of a JSON method, so a raw-only operation,
    # having no sibling to be confused with, keeps the plain name.
    name = f"{op.name}_raw" if is_raw and op.json_responses else op.name
    sig_args = ["self"]
    if kwargs:
        sig_args.append("*")

    if is_raw:
        ret = "httpx.Response"
        body = f"return {aw}self._do_raw({req})"
    elif op.json_responses and len(op.success_codes) > 1:
        # Success codes without a JSON schema (e.g. a 204 alongside a JSON
        # 200) yield None.
        json_refs = dict(op.json_responses)
        types_expr = (
            "{" + ", ".join(f"{code}: {ref}" for code, ref in op.json_responses) + "}"
        )
        refs = list(dict.fromkeys(ref for _, ref in op.json_responses))
        if any(code not in json_refs for code in op.success_codes):
            refs.append("None")
        ret = " | ".join(refs)
        body = f"return {aw}self._do_json_with_status({types_expr}, {req})"
    elif op.json_responses:
        ret = op.json_responses[0][1]
        body = f"return {aw}self._do_json({op.json_responses[0][1]}, {req})"
    else:
        ret = "None"
        body = f"{aw}self._do_no_response({req})"

    sig = f"    {adef} {name}({', '.join(sig_args + kwargs)}) -> {ret}:"
    if op.summary:
        summary = op.summary
        if is_raw:
            summary += (
                ". Returns the response unread, in the requested content"
                " type. The caller must close the response."
            )
        sig += f'\n        """{summary}"""'
    return f"{sig}\n        {body}\n"
