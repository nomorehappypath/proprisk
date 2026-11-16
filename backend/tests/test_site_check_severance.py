"""AI-severance guard for the Site Check package (site_check.md §3.2).

Acceptance-testable statement from the spec: the Phase A tree contains
no import of `anthropic`, no `llm_gateway`, and none of the excluded
modules. This test walks every import statement in
backend/app/services/site_check/ via the AST (so string payloads and
comments cannot false-positive) and asserts exactly that.

Runs without a database or the app importable — it reads source files
only.
"""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = (
    Path(__file__).resolve().parent.parent
    / "app" / "services" / "site_check"
)

# Modules excluded from the Phase A port (site_check.md §3.2).
#
# `map_context` was on this list as a Phase C deferral. The map is now in
# scope, so the module IS ported — deterministically, with no AI imports,
# which the other guards in this file still assert over it.
EXCLUDED_MODULE_NAMES = {
    "card_narratives",
    "flu_reader",
    "zoning_reader",
    "plat_reader",
    # `historical_aerials` was on this list as a Phase B deferral. "What this
    # land was before" is now in scope, so the module IS ported — its
    # deterministic half verbatim and its AI half rebuilt on OpenAI, which is
    # exactly what the anthropic/llm_gateway guards below still assert.
    "zoning_navigator",
    "zoning_navigator_job",
    "zoning_impact_summary",
    "zoning_project_evidence",
    "brand_dictionary",
    "_shipped_secrets",
    "official_records",
    "official_zoning_records",
}

# Forbidden import roots / dotted prefixes anywhere in the package.
FORBIDDEN_PREFIXES = (
    "anthropic",
    "app.core.llm_gateway",
    "app.services.public_intel",
    "app.services.analysis_inputs",
    "app.services.coverage_gap_service",
    "app.services.entity_audit",
    "app.services.data_center_public_sources",
    "app.schemas.zoning_navigator",
)

# the reference app model names that must not be referenced from imports.
FORBIDDEN_IMPORTED_NAMES = {
    "Document",
    "Project",
    "AnalysisRun",
    "AIJob",
    "PublicIntelRun",
    "PublicDataProviderUsage",
    "EntityAuditLog",
}


def _iter_package_files() -> list[Path]:
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert files, f"site_check package not found at {PACKAGE_ROOT}"
    return files


def _imports_in(path: Path) -> list[tuple[str, list[str]]]:
    """[(dotted_module, [imported names])] for every import statement."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, list[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((alias.name, []))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found.append((module, [alias.name for alias in node.names]))
    return found


def test_no_excluded_modules_are_present_as_files() -> None:
    present = {p.stem for p in _iter_package_files()}
    leaked = present & EXCLUDED_MODULE_NAMES
    assert not leaked, (
        f"excluded the reference app modules copied into site_check: {sorted(leaked)}"
    )


def test_no_forbidden_imports_anywhere_in_package() -> None:
    violations: list[str] = []
    for path in _iter_package_files():
        rel = path.relative_to(PACKAGE_ROOT)
        for module, names in _imports_in(path):
            root = module.split(".")[0]
            if root == "anthropic" or any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in FORBIDDEN_PREFIXES
            ):
                violations.append(f"{rel}: import of {module!r}")
                continue
            # intra-package import of an excluded module name
            if module.startswith("app.services.site_check"):
                leaf = module.split(".")[-1]
                if leaf in EXCLUDED_MODULE_NAMES:
                    violations.append(f"{rel}: import of {module!r}")
                for name in names:
                    if name in EXCLUDED_MODULE_NAMES:
                        violations.append(
                            f"{rel}: from {module} import {name}"
                        )
            if module == "app.models":
                for name in names:
                    if name in FORBIDDEN_IMPORTED_NAMES:
                        violations.append(
                            f"{rel}: from app.models import {name}"
                        )
    assert not violations, "AI-severance violations:\n" + "\n".join(violations)


def test_no_anthropic_or_llm_gateway_text_anywhere() -> None:
    """Belt-and-braces raw grep (case-insensitive) over the package for
    the two hard-forbidden tokens, matching the spec's GWT wording."""
    hits: list[str] = []
    for path in _iter_package_files():
        text = path.read_text(encoding="utf-8").lower()
        for token in ("llm_gateway", "call_anthropic", "import anthropic"):
            if token in text:
                hits.append(f"{path.relative_to(PACKAGE_ROOT)}: {token}")
    assert not hits, "forbidden AI tokens present:\n" + "\n".join(hits)
