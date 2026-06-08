"""Sphinx configuration for the API docs.

Build with ``poe generate-docs``, which runs ``sphinx-build`` over this
directory into ``_site``. Uses autosummary (recursive) to walk the package and
autodoc to import and document it, with autodoc_pydantic rendering the pydantic
models.
"""

import importlib
import inspect
import pathlib

project = "Baseten Python SDK"
html_title = "Baseten SDK"
html_theme = "furo"
templates_path = ["_templates"]
html_static_path = ["_static"]
html_css_files = ["custom.css"]

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinxcontrib.autodoc_pydantic",
]

# httpx ships no Sphinx inventory (objects.inv), so it cannot be linked here.
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest", None),
}

autosummary_generate = True
# The public API is re-exported into package __init__s via imports; without this
# autosummary would treat those names as imported members and skip them.
autosummary_imported_members = True
autodoc_default_options = {"members": True, "show-inheritance": True}
autodoc_typehints = "description"
# Members are one-per-class-page, so drop the class prefix in the right-hand TOC.
toc_object_entries_show_parents = "hide"

# Render pydantic models without the inherited BaseModel boilerplate.
autodoc_pydantic_model_show_config_summary = False
autodoc_pydantic_model_show_validator_summary = False
autodoc_pydantic_model_show_json = False
autodoc_pydantic_model_member_order = "bysource"

# For these modules, only the listed classes appear in the left nav. Every other
# class still gets its own page (generated as an orphan below) and is listed in
# the module's on-page table, but is kept out of the sidebar. Keep in sync with
# the matching table in _templates/autosummary/module.rst.
NAV_ONLY = {
    "baseten.client.managementapi": ["ApiClient", "AsyncApiClient"],
    "baseten.client.inferenceapi": ["ApiClient", "AsyncApiClient"],
    "baseten.client.modelconfig": ["ModelConfig"],
}


def _generate_class_pages(app):
    """Generate per-class stub pages for the NAV_ONLY modules.

    autosummary couples page generation to nav inclusion (its ``:toctree:``
    both writes the stub and adds it to the sidebar). To give every class a
    page while keeping only the allowlisted ones in the nav, we generate the
    stubs ourselves: allowlisted classes are normal pages (wired into the nav by
    a hidden toctree in the module template), the rest are orphans.
    """
    api_dir = pathlib.Path(app.srcdir) / "api"
    api_dir.mkdir(exist_ok=True)
    for module_name, nav_classes in NAV_ONLY.items():
        module = importlib.import_module(module_name)
        for name in getattr(module, "__all__", []):
            if not inspect.isclass(getattr(module, name)):
                continue
            header = ":orphan:\n\n" if name not in nav_classes else ""
            (api_dir / f"{module_name}.{name}.rst").write_text(
                f"{header}{name}\n{'=' * len(name)}\n\n"
                f".. currentmodule:: {module_name}\n\n"
                f".. autoclass:: {name}\n",
                encoding="utf-8",
            )


def setup(app):
    app.connect("builder-inited", _generate_class_pages)
