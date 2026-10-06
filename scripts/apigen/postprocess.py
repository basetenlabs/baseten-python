"""Postprocesses generated Python model files."""

import ast
import re

# Matches a class block of the form:
#   class Name(RootModel[INNER]):
#       root: Annotated[T, Field(...)] = None
# or:
#       root: T = None
# where INNER does not contain "None". Captures the inner type expression
# of the `root` annotation so we can widen it to `T | None`.
#
# datamodel-code-generator (as of 0.55.0) emits `= None` defaults on
# constrained nullable RootModel scalars (e.g. an integer schema with
# `anyOf: [{type: integer, ge: 1}, {type: null}]`). The annotation is
# non-nullable, so the default does not match the type. The schema's
# intent is nullable, so we widen the annotation to `T | None`. Tracked at
# https://github.com/koxudaxi/datamodel-code-generator/issues/2027 (closed
# but the issue persists for this shape in 0.55.0).
_ROOT_MODEL_BLOCK = re.compile(
    r"(class \w+\(RootModel\[(?P<wrapped>[^\n]+?)\]\):\n"
    r"    root: )(?P<rest>(?:(?!\nclass ).)+?)(?P<eq> = None\n)",
    re.DOTALL,
)

# Matches `dict[constr(pattern=...), X]` and converts to
# `dict[Annotated[str, Field(pattern=...)], X]`. ty (and other strict
# checkers) reject function calls in type expressions. Tracked at
# https://github.com/koxudaxi/datamodel-code-generator/issues/1973 (closed
# but the issue persists for this shape in 0.55.0).
_DICT_CONSTR = re.compile(
    r"dict\[constr\(pattern=(?P<pat>r?\"[^\"]+\")\), (?P<val>[\w.]+)\]"
)


def postprocess_models(src: str) -> str:
    src = _ROOT_MODEL_BLOCK.sub(_widen_root_annotation, src)
    src = _DICT_CONSTR.sub(
        r"dict[Annotated[str, Field(pattern=\g<pat>)], \g<val>]", src
    )
    if "constr(" not in src:
        src = src.replace(", RootModel, constr", ", RootModel")
        src = src.replace(", constr,", ",")
        src = src.replace(", constr\n", "\n")
    src = _allow_population_by_field_name(src)
    src = _validate_literal_model_defaults(src)
    src = _open_literals(src)
    return _open_tagged_unions(src)


def _open_tagged_unions(src: str) -> str:
    # A tagged union rejects a tag outside its members, so a variant the server
    # adds later would fail validation of the whole response. Each one is kept
    # as is and tried first, falling back to a plain dict of the raw value. A
    # known tag with a malformed payload falls back too, rather than failing.
    tree = ast.parse(src)
    line_starts = _line_starts(src)

    edits: list[tuple[int, int, str]] = []
    for node in ast.walk(tree):
        if not _is_tagged_union(node):
            continue
        assert isinstance(node, ast.expr)
        assert node.end_lineno is not None and node.end_col_offset is not None
        edits.append(
            (
                line_starts[node.lineno - 1] + node.col_offset,
                line_starts[node.end_lineno - 1] + node.end_col_offset,
                (
                    f"Annotated[{ast.get_source_segment(src, node)} | dict[str, Any], "
                    'Field(union_mode="left_to_right")]'
                ),
            )
        )
    if not edits:
        return src

    # Apply last to first so earlier offsets stay valid.
    for start, end, replacement in sorted(edits, reverse=True):
        src = src[:start] + replacement + src[end:]

    return _ensure_import(src, "typing", ["Any"])


def _is_tagged_union(node: ast.AST) -> bool:
    """Reports whether a node is an `Annotated[..., Field(discriminator=...)]`."""
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "Annotated"
        and isinstance(node.slice, ast.Tuple)
        and any(
            isinstance(metadata, ast.Call)
            and isinstance(metadata.func, ast.Name)
            and metadata.func.id == "Field"
            and any(k.arg == "discriminator" for k in metadata.keywords)
            for metadata in node.slice.elts[1:]
        )
    )


def _ensure_import(src: str, module: str, names: list[str]) -> str:
    # Adds names to a module's `from ... import` line, or adds the line after
    # the last top-level import.
    tree = ast.parse(src)
    imports = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    existing = next(
        (
            node
            for node in imports
            if isinstance(node, ast.ImportFrom) and node.module == module
        ),
        None,
    )
    starts = _line_starts(src)
    if existing is None:
        last = imports[-1]
        assert last.end_lineno is not None
        at = starts[last.end_lineno]
        return src[:at] + f"from {module} import {', '.join(names)}\n" + src[at:]
    present = {alias.name for alias in existing.names}
    if all(name in present for name in names):
        return src
    merged = sorted(present | set(names))
    assert existing.end_lineno is not None and existing.end_col_offset is not None
    start = starts[existing.lineno - 1] + existing.col_offset
    end = starts[existing.end_lineno - 1] + existing.end_col_offset
    return src[:start] + f"from {module} import {', '.join(merged)}" + src[end:]


def _line_starts(src: str) -> list[int]:
    """Returns the offset in src where each line starts."""
    starts = [0]
    for line in src.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))
    return starts


def _open_literals(src: str) -> str:
    # Enums are generated as Literal fields, which pydantic rejects on any value
    # outside the list, so a value the server adds later would fail validation
    # of the whole response. Each Literal is widened to `Literal[...] | str` to
    # accept it while keeping the known values in the type. Discriminator
    # fields stay closed, since a tagged union needs a Literal tag on each
    # member to pick it.
    tree = ast.parse(src)
    discriminators = _discriminator_fields(tree)
    line_starts = _line_starts(src)

    edits: list[tuple[int, int, str]] = []
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if not (
                isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            ):
                continue
            if (cls.name, stmt.target.id) in discriminators:
                continue
            for node in ast.walk(stmt.annotation):
                if not (
                    isinstance(node, ast.Subscript)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "Literal"
                ):
                    continue
                if node.end_lineno is None or node.end_col_offset is None:
                    continue
                edits.append(
                    (
                        line_starts[node.lineno - 1] + node.col_offset,
                        line_starts[node.end_lineno - 1] + node.end_col_offset,
                        f"{ast.get_source_segment(src, node)} | str",
                    )
                )

    # Apply last to first so earlier offsets stay valid.
    for start, end, replacement in sorted(edits, reverse=True):
        src = src[:start] + replacement + src[end:]
    return src


def _discriminator_fields(tree: ast.Module) -> set[tuple[str, str]]:
    # Each Field(discriminator="x") annotates a union; every class named in
    # that union has its field x serve as the tag.
    fields: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "Annotated"
            and isinstance(node.slice, ast.Tuple)
            and node.slice.elts
        ):
            continue
        for metadata in node.slice.elts[1:]:
            if not (
                isinstance(metadata, ast.Call)
                and isinstance(metadata.func, ast.Name)
                and metadata.func.id == "Field"
            ):
                continue
            for keyword in metadata.keywords:
                if (
                    keyword.arg == "discriminator"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    fields.update(
                        (member.id, keyword.value.value)
                        for member in ast.walk(node.slice.elts[0])
                        if isinstance(member, ast.Name)
                    )
    return fields


def _allow_population_by_field_name(src: str) -> str:
    # Pydantic accepts only the alias by default, so a Python-renamed field
    # (async_ for the API's "async") was not passable; generated classes with
    # aliased fields accept both names.
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in reversed(tree.body):
        if not (isinstance(node, ast.ClassDef) and _has_aliased_field(node)):
            continue
        config_assign = next(
            (
                stmt
                for stmt in node.body
                if isinstance(stmt, ast.Assign)
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "model_config"
            ),
            None,
        )
        if config_assign is not None:
            start = config_assign.lineno - 1
            end = config_assign.end_lineno
            if "populate_by_name" in "".join(lines[start:end]):
                continue
            lines[start:end] = [
                "".join(lines[start:end]).replace(
                    "ConfigDict(", "ConfigDict(populate_by_name=True, ", 1
                )
            ]
        else:
            insert_at = node.body[0].lineno - 1
            indent = " " * node.body[0].col_offset
            lines.insert(
                insert_at,
                f"{indent}model_config = ConfigDict(populate_by_name=True)\n\n",
            )
    result = "".join(lines)
    if "ConfigDict(" in result and not re.search(
        r"^from pydantic import .*\bConfigDict\b", result, re.MULTILINE
    ):
        # An injected model_config is the file's first ConfigDict use.
        result, substitutions = re.subn(
            r"^from pydantic import ",
            "from pydantic import ConfigDict, ",
            result,
            count=1,
        )
        if substitutions == 0:
            raise ValueError("injected model_config needs a pydantic import line")
    return result


def _has_aliased_field(class_node: ast.ClassDef) -> bool:
    for stmt in class_node.body:
        if not isinstance(stmt, ast.AnnAssign):
            continue
        for subnode in ast.walk(stmt):
            if not (
                isinstance(subnode, ast.Call)
                and isinstance(subnode.func, ast.Name)
                and subnode.func.id == "Field"
            ):
                continue
            if any(keyword.arg == "alias" for keyword in subnode.keywords):
                return True
    return False


def _validate_literal_model_defaults(src: str) -> str:
    # datamodel-code-generator emits a schema default verbatim even when the
    # field is model-typed, e.g. `x: Model = {"a": 1}` or `x: Limit | None =
    # 500`, relying on validate_default=True to coerce it at runtime. Pydantic
    # deliberately treats that as a static type error (pydantic/pydantic#11083),
    # so wrap the literal in a validating call to match the annotation.
    tree = ast.parse(src)
    models = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            (isinstance(b, ast.Name) and b.id == "BaseModel")
            # RootModel[...] is subscripted, so the name is the subscript value.
            or (
                isinstance(b, ast.Subscript)
                and isinstance(b.value, ast.Name)
                and b.value.id == "RootModel"
            )
            for b in node.bases
        )
    }

    line_starts = [0]
    for line in src.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    edits: list[tuple[int, int, str]] = []
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if not isinstance(stmt, ast.AnnAssign):
                continue
            # Only bare literal defaults need rewriting; None, enum members
            # and existing calls already match their annotation.
            value = stmt.value
            if not isinstance(value, (ast.Dict, ast.List, ast.Constant)):
                continue
            if isinstance(value, ast.Constant) and value.value is None:
                continue
            if value.end_lineno is None or value.end_col_offset is None:
                continue
            target = _model_default_type(stmt.annotation, models)
            if target is None:
                continue
            name, is_list = target
            if is_list:
                if not isinstance(value, ast.List) or not value.elts:
                    continue
                inner = ", ".join(
                    f"{name}.model_validate({ast.get_source_segment(src, e)})"
                    for e in value.elts
                )
                replacement = f"[{inner}]"
            else:
                replacement = (
                    f"{name}.model_validate({ast.get_source_segment(src, value)})"
                )
            edits.append(
                (
                    line_starts[value.lineno - 1] + value.col_offset,
                    line_starts[value.end_lineno - 1] + value.end_col_offset,
                    replacement,
                )
            )

    # Apply last to first so earlier offsets stay valid.
    for start, end, replacement in sorted(edits, reverse=True):
        src = src[:start] + replacement + src[end:]
    return src


def _model_default_type(ann: ast.expr, models: set[str]) -> tuple[str, bool] | None:
    # Unwrap Annotated[T, Field(...)] down to T and drop a trailing `| None`,
    # then report T and whether the default is a list of T.
    if (
        isinstance(ann, ast.Subscript)
        and isinstance(ann.value, ast.Name)
        and ann.value.id == "Annotated"
        and isinstance(ann.slice, ast.Tuple)
        and ann.slice.elts
    ):
        ann = ann.slice.elts[0]
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        if not (isinstance(ann.right, ast.Constant) and ann.right.value is None):
            return None
        ann = ann.left
    if isinstance(ann, ast.Name):
        return (ann.id, False) if ann.id in models else None
    if (
        isinstance(ann, ast.Subscript)
        and isinstance(ann.value, ast.Name)
        and ann.value.id == "list"
        and isinstance(ann.slice, ast.Name)
        and ann.slice.id in models
    ):
        return (ann.slice.id, True)
    return None


def _widen_root_annotation(m: re.Match) -> str:
    wrapped = m.group("wrapped").strip()
    if "None" in wrapped:
        return m.group(0)
    rest = m.group("rest")
    # Two forms:
    #   Annotated[T, Field(...)]
    #   T   (bare type, possibly multiline)
    if rest.lstrip().startswith("Annotated["):
        widened = re.sub(
            r"Annotated\[\s*([^,]+?)\s*,",
            lambda mm: f"Annotated[{mm.group(1).strip()} | None,",
            rest,
            count=1,
        )
    else:
        widened = rest.rstrip() + " | None"
    return m.group(1) + widened + m.group("eq")
