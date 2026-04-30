"""Postprocesses generated Python model files."""

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
    r"(class \w+\(RootModel\[(?P<wrapped>[^\]]+)\]\):\n"
    r"    root: )(?P<rest>.+?)(?P<eq> = None\n)",
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
    return src


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
