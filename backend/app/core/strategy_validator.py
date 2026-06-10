"""Static (AST) safety scan for an uploaded partner strategy module.

A source upload is, by definition, code we will eventually import and run.
This module is the gate: it parses the source WITHOUT executing it and
rejects anything that could escape the strategy sandbox — disallowed
imports (default-deny), dangerous builtins (eval/exec/open/...), and dunder
attribute tricks used for sandbox escapes (``().__class__.__bases__`` etc.).

It also verifies the structural contract: exactly one class that subclasses
``Strategy`` and defines ``on_bar``, with a class-level ``name``.

The result is advisory-but-blocking: the public upload route stores the
verdict, and a submission with ANY ``block`` finding is held; the owner
sees every finding before approving. ``ok=False`` means do-not-import.

This is static analysis — it raises the bar, it is not a complete sandbox.
The human approval step and the per-strategy isolation account remain part
of the trust model.
"""
from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, field

# Default-deny: only these top-level modules may be imported.
_ALLOWED_TOPLEVEL = {
    "__future__",
    "math",
    "statistics",
    "datetime",
    "typing",
    "dataclasses",
    "collections",
    "json",
    "re",
    "decimal",
    "random",
    "itertools",
    "functools",
    "pandas",
    "numpy",
}
# Exact module paths allowed beyond the top-level set.
_ALLOWED_EXACT = {"app.strategies.base"}

# Builtins that enable code execution, filesystem, or introspection escapes.
_FORBIDDEN_CALLS = {
    "eval",
    "exec",
    "compile",
    "__import__",
    "open",
    "input",
    "breakpoint",
    "globals",
    "locals",
    "vars",
    "getattr",
    "setattr",
    "delattr",
    "memoryview",
    "exit",
    "quit",
    "help",
}

# Dunder attributes used in sandbox-escape chains.
_FORBIDDEN_DUNDERS = {
    "__globals__",
    "__builtins__",
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__class__",
    "__dict__",
    "__code__",
    "__closure__",
    "__func__",
    "__reduce__",
    "__reduce_ex__",
    "__getattribute__",
    "__import__",
    "__loader__",
    "__spec__",
    "__base__",
}

# Reject absurdly large uploads outright (chars). A strategy module is small.
_MAX_SOURCE_CHARS = 200_000


@dataclass
class Finding:
    level: str  # "block" | "warn"
    code: str
    message: str
    line: int = 0


@dataclass
class ValidationResult:
    ok: bool
    findings: list[Finding] = field(default_factory=list)
    strategy_class: str | None = None
    declared_name: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "strategy_class": self.strategy_class,
            "declared_name": self.declared_name,
            "findings": [asdict(f) for f in self.findings],
        }


def _module_allowed(module: str | None) -> bool:
    if not module:
        # `from . import x` (relative, level>0) has module=None — disallow.
        return False
    if module in _ALLOWED_EXACT:
        return True
    return module.split(".")[0] in _ALLOWED_TOPLEVEL


class _Scanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.strategy_classes: list[tuple[str, ast.ClassDef]] = []

    def _block(self, code: str, message: str, node: ast.AST) -> None:
        self.findings.append(
            Finding("block", code, message, getattr(node, "lineno", 0))
        )

    # ---- imports ---------------------------------------------------- #
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if not _module_allowed(alias.name):
                self._block(
                    "import_not_allowed",
                    f"import of {alias.name!r} is not allowed",
                    node,
                )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level and node.level > 0:
            self._block(
                "relative_import",
                "relative imports are not allowed",
                node,
            )
        elif not _module_allowed(node.module):
            self._block(
                "import_not_allowed",
                f"import from {node.module!r} is not allowed",
                node,
            )
        self.generic_visit(node)

    # ---- calls ------------------------------------------------------ #
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CALLS:
            self._block(
                "forbidden_call",
                f"call to {func.id!r} is not allowed",
                node,
            )
        self.generic_visit(node)

    # ---- attribute access ------------------------------------------- #
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in _FORBIDDEN_DUNDERS:
            self._block(
                "forbidden_attribute",
                f"access to {node.attr!r} is not allowed",
                node,
            )
        self.generic_visit(node)

    # ---- names (catch bare reference to dunder escape helpers) ------ #
    def visit_Name(self, node: ast.Name) -> None:
        if node.id in ("__import__", "__builtins__"):
            self._block(
                "forbidden_name",
                f"reference to {node.id!r} is not allowed",
                node,
            )
        self.generic_visit(node)

    # ---- classes ---------------------------------------------------- #
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for base in node.bases:
            base_name = (
                base.id if isinstance(base, ast.Name)
                else base.attr if isinstance(base, ast.Attribute)
                else None
            )
            if base_name == "Strategy":
                self.strategy_classes.append((node.name, node))
                break
        self.generic_visit(node)


def _class_has_on_bar(cls: ast.ClassDef) -> bool:
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "on_bar"
        for n in cls.body
    )


def _class_declared_name(cls: ast.ClassDef) -> str | None:
    for n in cls.body:
        if isinstance(n, ast.Assign):
            for tgt in n.targets:
                if (
                    isinstance(tgt, ast.Name)
                    and tgt.id == "name"
                    and isinstance(n.value, ast.Constant)
                    and isinstance(n.value.value, str)
                ):
                    return n.value.value
        elif (
            isinstance(n, ast.AnnAssign)
            and isinstance(n.target, ast.Name)
            and n.target.id == "name"
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        ):
            return n.value.value
    return None


def validate_strategy_source(
    source: str, *, expected_name: str | None = None
) -> ValidationResult:
    """Statically scan partner strategy source. ``ok=False`` ⇒ do not import."""
    if source is None or not source.strip():
        return ValidationResult(
            ok=False,
            findings=[Finding("block", "empty", "source is empty")],
        )
    if len(source) > _MAX_SOURCE_CHARS:
        return ValidationResult(
            ok=False,
            findings=[
                Finding(
                    "block",
                    "too_large",
                    f"source exceeds {_MAX_SOURCE_CHARS} chars",
                )
            ],
        )

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ValidationResult(
            ok=False,
            findings=[
                Finding("block", "syntax_error", f"syntax error: {exc.msg}", exc.lineno or 0)
            ],
        )

    scanner = _Scanner()
    scanner.visit(tree)
    findings = list(scanner.findings)

    strategy_class: str | None = None
    declared_name: str | None = None

    if not scanner.strategy_classes:
        findings.append(
            Finding(
                "block",
                "no_strategy_class",
                "no class subclassing `Strategy` found",
            )
        )
    else:
        if len(scanner.strategy_classes) > 1:
            names = ", ".join(n for n, _ in scanner.strategy_classes)
            findings.append(
                Finding(
                    "block",
                    "multiple_strategy_classes",
                    f"expected exactly one Strategy subclass, found: {names}",
                )
            )
        strategy_class, cls_node = scanner.strategy_classes[0]
        if not _class_has_on_bar(cls_node):
            findings.append(
                Finding(
                    "block",
                    "missing_on_bar",
                    f"class {strategy_class!r} does not define on_bar()",
                    cls_node.lineno,
                )
            )
        declared_name = _class_declared_name(cls_node)
        if not declared_name:
            findings.append(
                Finding(
                    "block",
                    "missing_name",
                    f"class {strategy_class!r} must set a class-level string `name`",
                    cls_node.lineno,
                )
            )
        elif expected_name and declared_name != expected_name:
            findings.append(
                Finding(
                    "block",
                    "name_mismatch",
                    f"class `name` is {declared_name!r} but the registry slug "
                    f"is {expected_name!r}; they must match",
                    cls_node.lineno,
                )
            )

    ok = not any(f.level == "block" for f in findings)
    return ValidationResult(
        ok=ok,
        findings=findings,
        strategy_class=strategy_class,
        declared_name=declared_name,
    )
