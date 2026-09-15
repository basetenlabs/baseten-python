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
    return _validate_literal_model_defaults(src)


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
