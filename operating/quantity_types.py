"""Quantity types of computed values: a small closed algebra over the expression tree.

Planned, manufactured, actual, filled, ordered and damaged quantities share a dimension
but are different quantities (AO-26005B: planned 1,509 / manufactured 1,503 /
filled 1,502). A calculation's result is a quantity of one of those types only when the
arithmetic actually makes it one. This module decides that from the **expression tree
and the inputs' types alone**: never from a relation's ``as`` name, never from the order
in which a caller wrote or declared the operands.

The expression is simplified symbolically into a polynomial over its inputs (exact
``Fraction`` coefficients; a cited input is the source cell it reads, so the same cell
read twice is one variable; division by a sum is an opaque factor). Then:

* **typed T**: every term is a *pure* term (one typed input to the power 1, coefficient
  +1 or -1), no constant term, the positive terms are all of type ``T`` (``T + T`` keeps
  ``T``; a single input is its type), and the negative terms are either absent or all of
  a type ``T'`` that ``SUBTRACTIONS`` lists as a documented part of ``T`` (``manufactured -
  damaged`` is good produced units, Portal ``schema.prisma``; ``planned - damaged`` has
  no such evidence and is untyped);
* **untyped** (``None``): everything else: mixed-type sums, any difference outside
  ``SUBTRACTIONS`` (also ``T - T``), division nested deeper than ``MAX_OPAQUE_DEPTH``,
  products, ratios, scalings (``2 * p``,
  ``p / per_unit``), constants, or a polynomial that cancels to nothing.

Because the simplification is exact, identities cannot relabel a figure:
``m + (p - m)`` *is* ``p`` (planned), ``p - (p - a)`` is ``a`` (actual), ``p + a - a`` and
``p + a * 0`` are ``p``, and ``p + d`` is untyped however its relations are named.

A relation's ``as`` name (and a calculation claim's derived role) is a **label**. It
types nothing; it must only be consistent with the form it names (``label_problem``):
the quantity-type words before the first connective (``not``, ``minus``, ``less``,
``net``, ``over``, ...) must name exactly the types of the positive terms, and the type
words after it must name types of the negative terms. So ``planned_not_produced`` names
``planned - actual``, ``produced_total`` cannot name ``m + (p - m)`` (a planned figure)
and ``damaged_total`` cannot name ``planned + damaged``.

Stdlib only; no dependency on the case store, so ``cases`` and ``claim_checks`` share
one definition.
"""

from __future__ import annotations

import ast
import re
from fractions import Fraction
from typing import Any, Hashable, Mapping

QUANTITY_TYPES = frozenset({
    "planned", "produced", "manufactured", "actual", "filled", "inspected", "accepted",
    "damaged", "ordered", "received", "shipped", "delivered", "rejected", "retained",
    "returned", "committed", "invoiced"})
# A word type that also accepts a quantity of another type (``produced`` names a
# manufactured or an actual figure). Used for claims, prose and labels alike.
TYPE_ALIASES = {"produced": frozenset({"manufactured", "actual"})}

# The documented sub-quantity rules: (T, T') -> why T - T' is still a T-family figure.
SUBTRACTIONS: Mapping[tuple[str, str], str] = {
    ("manufactured", "damaged"): (
        "good produced units: Portal portal/prisma/schema.prisma@878705b line 2534, "
        "app_manufacturing_order.qtyDamaged: 'Unit cost denominator is good qty = "
        "produced - this'"),
    ("actual", "damaged"): "good produced units (as manufactured - damaged)",
    ("produced", "damaged"): "good produced units (as manufactured - damaged)",
}

# Words of a label that name a quantity type (the types themselves first).
LABEL_WORDS = {**{name: name for name in QUANTITY_TYPES},
               "plan": "planned", "made": "produced", "output": "produced",
               "production": "produced", "producing": "produced", "yield": "produced",
               "manufacture": "manufactured", "fill": "filled", "filling": "filled",
               "inspection": "inspected", "qc": "inspected", "order": "ordered",
               "damage": "damaged", "ship": "shipped", "receipt": "received"}
CONNECTIVES = frozenset({"not", "minus", "less", "without", "excluding", "after", "net",
                         "over", "under", "short", "vs", "versus", "than", "below",
                         "above", "beyond", "unmade", "unfilled"})
MAX_TERMS = 64
# Divisions by a sum nest at most this deep; deeper is no closed form (untyped).
MAX_OPAQUE_DEPTH = 8
_UNICODE_OPS = {ord("×"): "*", ord("÷"): "/", ord("−"): "-"}
_MIXED = "<mixed>"

# A monomial is a frozenset of (atom, power) pairs (no ordering, cheap to hash); an
# opaque atom (division by a sum) is ("opaque", frozenset(form items), depth).
Monomial = frozenset
Form = dict[Monomial, Fraction]


def _exact(value: Any) -> Any:
    """A read value compared exactly: a number by value (``1509.0`` is ``1509``)."""
    try:
        return Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        return str(value)


class FormError(ValueError):
    """The expression cannot be put in the closed form (too many terms, 0 divisor)."""


def accepts(word_type: str, quantity_type: str | None) -> bool:
    """Does a stated type (a claim role, a prose or label word) name ``quantity_type``?"""
    return quantity_type is not None and (
        word_type == quantity_type or quantity_type in TYPE_ALIASES.get(word_type, ()))


def cell(handle: str, field: str) -> tuple[str, str, str]:
    """The variable a cited input reads: one source row and column, whatever capture or
    generation read it. An ``api://`` handle is ``api://<relation>/<canonical key>@<tail>``
    (``source_mirror.make_citation``: the key is canonical JSON, ``@`` quoted inside it),
    so the row is the handle without its ``@`` tail; ``row.<c>`` and ``key.<c>`` (the key
    copy) are one column. Two reads of one cell are one variable only when they read the
    same value: ``Algebra`` refuses one cell read at two values (``one_cell_two_values``,
    untyped), because a recapture that changed the row must never cancel (``m + p - q``
    with ``p`` and ``q`` one planned cell at 1509 and 1600 is not ``m``)."""
    row = handle.rsplit("@", 1)[0] if handle.startswith("api://") and "@" in handle \
        else handle
    match = re.fullmatch(r"(?:row|key)\.([A-Za-z_][A-Za-z0-9_]*)", field)
    return ("cite", row, "row." + match.group(1) if match else field)


def parse(expression: str) -> tuple[str, ast.Expression]:
    """(normalised text, tree) of an already validated expression."""
    text = expression.translate(_UNICODE_OPS).strip()
    return text, ast.parse(text, mode="eval")


def _mul(left: Form, right: Form) -> Form:
    if len(left) * len(right) > MAX_TERMS * MAX_TERMS:
        raise FormError("too_many_terms")
    out: Form = {}
    for lmono, lcoef in left.items():
        for rmono, rcoef in right.items():
            powers = dict(lmono)
            for atom, power in rmono:
                powers[atom] = powers.get(atom, 0) + power
            mono = frozenset((atom, power) for atom, power in powers.items() if power)
            out[mono] = out.get(mono, Fraction(0)) + lcoef * rcoef
    out = {mono: coef for mono, coef in out.items() if coef}
    if len(out) > MAX_TERMS:
        raise FormError("too_many_terms")
    return out


def _add(left: Form, right: Form, sign: int) -> Form:
    out = dict(left)
    for mono, coef in right.items():
        out[mono] = out.get(mono, Fraction(0)) + sign * coef
    out = {mono: coef for mono, coef in out.items() if coef}
    if len(out) > MAX_TERMS:
        raise FormError("too_many_terms")
    return out


class Algebra:
    """The type algebra over one expression's inputs.

    ``inputs[name] = (atom, type)`` or ``(atom, type, value)``: ``atom`` identifies the
    value the name reads (``cell(handle, field)``, or the name itself for a literal),
    ``type`` its quantity type (a ``QUANTITY_TYPES`` member) or None, ``value`` the value
    it read (two inputs of one atom must have read the same value). ``text`` is the normalised expression
    (``parse``); nodes passed to ``form`` must come from a tree of that text."""

    def __init__(self, text: str, inputs: Mapping[str, tuple[Any, ...]],
                 constants: Mapping[str, Any] | None = None) -> None:
        self.text = text
        # Literal inputs given as exact numbers (their recorded value): ``p * k`` with
        # ``k = 1`` is ``p``, with ``k = -1`` it is ``-p``.
        self.constants = {name: Fraction(str(value)) for name, value in (constants or {}).items()}
        self.atoms = {name: ("in", spec[0]) for name, spec in inputs.items()}
        self.types: dict[Hashable, str | None] = {}
        values: dict[Hashable, Any] = {}
        for name, spec in inputs.items():
            key = self.atoms[name]
            kind = spec[1] if spec[1] in QUANTITY_TYPES else None
            if key in self.types and self.types[key] != kind:
                raise FormError("one_source_value_two_types")  # one cell, two declared roles
            self.types[key] = kind
            if len(spec) > 2:
                value = _exact(spec[2])
                if key in values and values[key] != value:
                    raise FormError("one_cell_two_values")  # the row changed between reads
                values[key] = value
        self.tainted: dict[Hashable, bool] = {}

    # ------------------------------------------------------------------ forms

    def form(self, node: ast.AST) -> Form:
        if isinstance(node, ast.Expression):
            return self.form(node.body)
        if isinstance(node, ast.Constant):
            segment = ast.get_source_segment(self.text, node)
            value = Fraction(segment if segment else repr(node.value))
            return {frozenset(): value} if value else {}
        if isinstance(node, ast.Name):
            if node.id in self.constants:
                value = self.constants[node.id]
                return {frozenset(): value} if value else {}
            return {frozenset({(self.atoms[node.id], 1)}): Fraction(1)}
        if isinstance(node, ast.UnaryOp):
            inner = self.form(node.operand)
            return {mono: -coef for mono, coef in inner.items()} \
                if isinstance(node.op, ast.USub) else inner
        if isinstance(node, ast.BinOp):
            left, right = self.form(node.left), self.form(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                return _add(left, right, 1 if isinstance(node.op, ast.Add) else -1)
            if isinstance(node.op, ast.Mult):
                return _mul(left, right)
            if isinstance(node.op, ast.Div):
                if not right:
                    raise FormError("division_by_zero")
                if len(right) == 1:
                    [(mono, coef)] = right.items()
                    return _mul(left, {frozenset((atom, -power) for atom, power in mono):
                                       1 / coef})
                # Structural, hashable and bounded: no text of the inner form is built.
                depth = 1 + max((atom[2] for mono in right for atom, _power in mono
                                 if atom[0] == "opaque"), default=0)
                if depth > MAX_OPAQUE_DEPTH:
                    raise FormError("division_by_a_sum_nested_too_deep")
                key = ("opaque", frozenset(right.items()), depth)
                self.tainted[key] = any(self._typed(atom) for mono in right
                                        for atom, _power in mono)
                return _mul(left, {frozenset({(key, -1)}): Fraction(1)})
        raise FormError(f"unsupported_node:{type(node).__name__}")

    def expression_form(self) -> Form:
        return self.form(ast.parse(self.text, mode="eval"))

    def _typed(self, atom: Hashable) -> bool:
        return bool(self.types.get(atom)) or self.tainted.get(atom, False)

    # ------------------------------------------------------------------ typing

    def _pure(self, mono: Monomial, coef: Fraction) -> str | None:
        if abs(coef) != 1 or len(mono) != 1:
            return None
        [(atom, power)] = mono
        return self.types.get(atom) if power == 1 else None

    def _family(self, mono: Monomial) -> str | None:
        """The type a term is a scaling of (labels only): its one typed atom at power 1
        beside untyped factors; ``_MIXED`` for several typed atoms or another power;
        None for a term of untyped factors only."""
        typed = [(atom, power) for atom, power in mono if self._typed(atom)]
        if not typed:
            return None
        if len(typed) == 1 and typed[0][1] == 1 and self.types.get(typed[0][0]):
            return self.types[typed[0][0]]
        return _MIXED

    def type_of(self, form: Form) -> str | None:
        """The quantity type of a form, or None (untyped). See the module rules."""
        positive, negative = set(), set()
        for mono, coef in form.items():
            kind = self._pure(mono, coef)
            if kind is None:
                return None
            (positive if coef > 0 else negative).add(kind)
        if len(positive) != 1:
            return None
        [kind] = positive
        if all((kind, other) in SUBTRACTIONS for other in negative):
            return kind
        return None

    def show(self, form: Form) -> str:
        """A readable form: ``planned - damaged``, ``2*planned``, ``planned*x^-1``."""
        if not form:
            return "0"
        names: dict[Hashable, str] = {}
        for name, key in self.atoms.items():
            names.setdefault(key, self.types.get(key) or f"untyped({name})")
        parts = []
        for mono, coef in form.items():
            factors = sorted((names.get(atom, "(sum)") + (f"^{power}" if power != 1 else "")
                              for atom, power in mono), key=lambda text: ("^-" in text, text))
            magnitude = abs(coef)
            body = "*".join(([] if magnitude == 1 and factors else [str(magnitude)]) + factors)
            parts.append((coef < 0, body))
        text = " ".join(("- " if negative else "+ ") + body for negative, body in sorted(parts))
        return text[2:] if text.startswith("+ ") else "-" + text[1:]

    def label_problem(self, label: str, form: Form) -> str | None:
        """Why ``label`` cannot name ``form`` (None when it is consistent)."""
        words = [word for word in label.split("_") if word]
        cut = next((index for index, word in enumerate(words) if word in CONNECTIVES),
                   len(words))
        head = {LABEL_WORDS[word] for word in words[:cut] if word in LABEL_WORDS}
        tail = {LABEL_WORDS[word] for word in words[cut:] if word in LABEL_WORDS}
        positive = {self._family(mono) for mono, coef in form.items() if coef > 0} - {None}
        negative = {self._family(mono) for mono, coef in form.items() if coef < 0} - {None}
        if negative and not positive:
            # A relation names a magnitude (``cases.quantity_role`` tracks signs: the
            # relation of ``-p - a`` is ``p + a``), so a wholly negative form is flipped.
            positive, negative = negative, positive
        shown = self.show(form)
        if head:
            if _MIXED in positive or not positive or not (
                    all(any(accepts(word, kind) for kind in positive) for word in head)
                    and all(any(accepts(word, kind) for word in head) for kind in positive)):
                return (f"the label '{label}' names {', '.join(sorted(head))} but the value "
                        f"is {shown}")
        if tail and (_MIXED in negative or not all(
                any(accepts(word, kind) for kind in negative) for word in tail)):
            return (f"the label '{label}' subtracts or compares {', '.join(sorted(tail))} "
                    f"but the value is {shown}")
        return None


def type_calculation(expression: str, inputs: Mapping[str, tuple[Any, ...]],
                     relations: Any = (), seen: Any = (), label: str | None = None,
                     constants: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """``{"type", "form", "problem"}`` of one calculation.

    ``type`` is the result's quantity type by the algebra (None: untyped); ``form`` the
    simplified expression as text; ``problem`` why a label does not fit: each relation's
    ``as`` against the subtree it was matched on (``seen``: ``(index, node)`` pairs from
    ``cases.quantity_role`` over the same expression), and ``label`` (the result's
    derived name, when it is not a source role) against the whole expression. A form
    that cannot be built is untyped, and any label on it is a problem."""
    text, tree = parse(expression)
    labels = [(str(relations[index]["as"]), node) for index, node in seen]
    if label:
        labels.append((label, tree))
    try:
        algebra = Algebra(text, inputs, constants)
        form = algebra.form(tree)
    except (FormError, ValueError, ZeroDivisionError, KeyError, ArithmeticError) as error:
        return {"type": None, "form": None,
                "problem": (f"the labels {', '.join(dict.fromkeys(name for name, _node in labels))} cannot "
                            f"be checked: {error}") if labels else None}
    problem = None
    for name, node in labels:
        try:
            problem = algebra.label_problem(name, algebra.form(node))
        except (FormError, ValueError, ZeroDivisionError) as error:
            problem = f"the label '{name}' cannot be checked: {error}"
        if problem:
            break
    return {"type": algebra.type_of(form), "form": algebra.show(form), "problem": problem}


__all__ = ["Algebra", "CONNECTIVES", "FormError", "LABEL_WORDS", "QUANTITY_TYPES",
           "SUBTRACTIONS", "TYPE_ALIASES", "accepts", "cell", "parse", "type_calculation"]
