"""A small, safe templating engine used to render dotfile sources.

Supported syntax
----------------
``{{ expression }}``            - interpolate a value (auto converts to text)
``{{ x | upper | default("") }}`` - filters, chained with ``|``
``{% if cond %} … {% elif %} … {% else %} … {% endif %}``
``{% for item in seq %} … {% else %} … {% endfor %}`` (``loop.index``, ``loop.first`` …)
``{% set name = expr %}``       - bind a variable for the rest of the scope
``{% include "partial.conf" %}`` - inline another template relative to this one
``{% raw %} … {% endraw %}``    - verbatim
``{# comment #}``               - discarded

Escaping: emit literal braces with an expression, e.g. ``{{ "{{" }}``, or wrap a
whole region in ``{% raw %} … {% endraw %}``.

Expressions support literals, attribute/index access, arithmetic, comparisons,
``in``, ``and``/``or``/``not``, parentheses, function calls and filters.
Everything is evaluated in-process with no ``eval``/``exec``, so templates are
data, never code.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import platform
import posixpath
import re
import shlex
import socket
import time
import unicodedata
import uuid as uuid_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .errors import TemplateError

__all__ = ["Environment", "Template", "Undefined", "default_filters", "default_functions", "render"]


# --------------------------------------------------------------------------- #
# value model
# --------------------------------------------------------------------------- #
class Undefined:
    """Placeholder for a missing value.

    Falsy, renders as the empty string, but remembers its name so strict
    environments can raise a helpful error.
    """

    __slots__ = ("hint", "name")

    def __init__(self, name: str = "", hint: Optional[str] = None) -> None:
        self.name = name
        self.hint = hint

    def __bool__(self) -> bool:
        return False

    def __str__(self) -> str:
        return ""

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"Undefined({self.name!r})"

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, Undefined) or other is None

    def __ne__(self, other: Any) -> bool:
        return not self.__eq__(other)

    def __hash__(self) -> int:
        return hash(("Undefined", self.name))

    def __len__(self) -> int:
        return 0

    def __iter__(self):  # pragma: no cover - convenience
        return iter(())

    def __getattr__(self, item: str) -> "Undefined":
        return Undefined(f"{self.name}.{item}" if self.name else item)

    def __getitem__(self, item: Any) -> "Undefined":
        return Undefined(f"{self.name}[{item!r}]" if self.name else str(item))


class AttrDict(dict):
    """A dict whose keys are also reachable as attributes (used for contexts)."""

    def __getattr__(self, item: str) -> Any:
        try:
            return self[item]
        except KeyError as exc:  # pragma: no cover - normal flow
            raise AttributeError(item) from exc


# --------------------------------------------------------------------------- #
# expression lexer
# --------------------------------------------------------------------------- #
TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<num>\d+\.\d+|\.\d+|\d+)
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>==|!=|<=|>=|//|\*\*|\|\||&&|[<>+\-*/%~|.,()\[\]{}:=!])
    """,
    re.VERBOSE,
)

KEYWORDS = {"and", "or", "not", "in", "true", "false", "none", "null", "is"}


@dataclass
class Token:
    kind: str
    value: Any
    pos: int
    line: int

    def __repr__(self) -> str:  # pragma: no cover
        return f"Token({self.kind},{self.value!r})"


def _unescape_string(raw: str) -> str:
    body = raw[1:-1]
    out: List[str] = []
    it = iter(range(len(body)))
    idx = 0
    mapping = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "0": "\0"}
    while idx < len(body):
        ch = body[idx]
        if ch == "\\" and idx + 1 < len(body):
            nxt = body[idx + 1]
            out.append(mapping.get(nxt, nxt))
            idx += 2
            continue
        out.append(ch)
        idx += 1
    del it
    return "".join(out)


def lex_expression(source: str, line: int = 1) -> List[Token]:
    tokens: List[Token] = []
    pos = 0
    length = len(source)
    while pos < length:
        match = TOKEN_RE.match(source, pos)
        if not match:
            raise TemplateError(f"unexpected character {source[pos]!r} in expression {source!r} (line {line})")
        pos = match.end()
        kind = match.lastgroup or ""
        text = match.group()
        if kind == "ws":
            continue
        if kind == "num":
            value: Any = float(text) if ("." in text) else int(text)
        elif kind == "str":
            value = _unescape_string(text)
        else:
            value = text
        tokens.append(Token(kind, value, match.start(), line))
    tokens.append(Token("eof", "", length, line))
    return tokens


# --------------------------------------------------------------------------- #
# expression parser (Pratt / precedence climbing)
# --------------------------------------------------------------------------- #
class Expr:
    def eval(self, env: "Environment") -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def __repr__(self) -> str:  # pragma: no cover
        return f"<{type(self).__name__}>"


@dataclass
class Literal(Expr):
    value: Any

    def eval(self, env: "Environment") -> Any:
        return self.value


@dataclass
class Name(Expr):
    name: str
    line: int = 1

    def eval(self, env: "Environment") -> Any:
        return env.lookup(self.name, self.line)


@dataclass
class GetAttr(Expr):
    target: Expr
    attr: str
    line: int = 1

    def eval(self, env: "Environment") -> Any:
        base = self.target.eval(env)
        return env.get_member(base, self.attr, self.line)


@dataclass
class GetItem(Expr):
    target: Expr
    index: Expr
    line: int = 1

    def eval(self, env: "Environment") -> Any:
        base = self.target.eval(env)
        index = self.index.eval(env)
        return env.get_index(base, index, self.line)


@dataclass
class Call(Expr):
    func: Expr
    args: List[Expr] = field(default_factory=list)
    kwargs: Dict[str, Expr] = field(default_factory=dict)
    line: int = 1

    def eval(self, env: "Environment") -> Any:
        target = self.func.eval(env)
        if isinstance(target, Undefined):
            if env.strict:
                raise TemplateError(f"unknown function {target.name!r} (line {self.line})")
            return Undefined(f"{target.name}()")
        positional = [a.eval(env) for a in self.args]
        named = {k: v.eval(env) for k, v in self.kwargs.items()}
        return env.call(target, positional, named, self.line)


@dataclass
class Filter(Expr):
    value: Expr
    name: str
    args: List[Expr]
    kwargs: Dict[str, Expr]
    line: int = 1

    def eval(self, env: "Environment") -> Any:
        inner = self.value.eval(env)
        positional = [a.eval(env) for a in self.args]
        named = {k: v.eval(env) for k, v in self.kwargs.items()}
        return env.apply_filter(inner, self.name, positional, named, self.line)


@dataclass
class Unary(Expr):
    op: str
    operand: Expr
    line: int = 1

    def eval(self, env: "Environment") -> Any:
        value = self.operand.eval(env)
        if self.op == "not":
            return not truthy(value)
        if self.op == "-":
            return -to_number(value)
        if self.op == "+":
            return +to_number(value)
        raise TemplateError(f"unsupported unary operator {self.op!r}")  # pragma: no cover


@dataclass
class Binary(Expr):
    op: str
    left: Expr
    right: Expr
    line: int = 1

    def eval(self, env: "Environment") -> Any:
        op = self.op
        if op == "and":
            left = self.left.eval(env)
            return self.right.eval(env) if truthy(left) else left
        if op == "or":
            left = self.left.eval(env)
            return left if truthy(left) else self.right.eval(env)
        left = self.left.eval(env)
        right = self.right.eval(env)
        return env.binary(op, left, right, self.line)


@dataclass
class ListLiteral(Expr):
    items: List[Expr]

    def eval(self, env: "Environment") -> Any:
        return [item.eval(env) for item in self.items]


@dataclass
class DictLiteral(Expr):
    items: List[Tuple[Any, Expr]]

    def eval(self, env: "Environment") -> Any:
        return {key: value.eval(env) for key, value in self.items}


@dataclass
class Conditional(Expr):
    """``a if cond else b``"""

    body: Expr
    cond: Expr
    orelse: Expr

    def eval(self, env: "Environment") -> Any:
        return self.body.eval(env) if truthy(self.cond.eval(env)) else self.orelse.eval(env)


COMPARISON_OPS = {"==", "!=", "<", "<=", ">", ">=", "in", "notin"}


class ExprParser:
    def __init__(self, tokens: List[Token]) -> None:
        self.tokens = tokens
        self.index = 0

    # -- helpers -----------------------------------------------------------
    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def peek(self, offset: int = 1) -> Token:
        idx = min(self.index + offset, len(self.tokens) - 1)
        return self.tokens[idx]

    def advance(self) -> Token:
        token = self.tokens[self.index]
        if token.kind != "eof":
            self.index += 1
        return token

    def at_op(self, *values: str) -> bool:
        tok = self.current
        return tok.kind == "op" and tok.value in values

    def at_name(self, *values: str) -> bool:
        tok = self.current
        return tok.kind == "name" and tok.value in values

    def expect_op(self, value: str) -> Token:
        if not self.at_op(value):
            raise TemplateError(
                f"expected {value!r} but found {self.current.value!r} (line {self.current.line})"
            )
        return self.advance()

    # -- grammar -----------------------------------------------------------
    def parse(self) -> Expr:
        expr = self.parse_conditional()
        if self.current.kind != "eof":
            raise TemplateError(
                f"unexpected token {self.current.value!r} in expression (line {self.current.line})"
            )
        return expr

    def parse_conditional(self) -> Expr:
        body = self.parse_or()
        if self.at_name("if"):
            line = self.advance().line
            cond = self.parse_or()
            if not self.at_name("else"):
                raise TemplateError(f"inline 'if' requires an 'else' branch (line {line})")
            self.advance()
            orelse = self.parse_conditional()
            return Conditional(body, cond, orelse)
        return body

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.at_name("or") or self.at_op("||"):
            line = self.advance().line
            right = self.parse_and()
            left = Binary("or", left, right, line)
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.at_name("and") or self.at_op("&&"):
            line = self.advance().line
            right = self.parse_not()
            left = Binary("and", left, right, line)
        return left

    def parse_not(self) -> Expr:
        if self.at_name("not") and not (self.peek().kind == "name" and self.peek().value == "in"):
            line = self.advance().line
            return Unary("not", self.parse_not(), line)
        return self.parse_comparison()

    def parse_comparison(self) -> Expr:
        left = self.parse_concat()
        while True:
            if self.at_op("==", "!=", "<", "<=", ">", ">=") or self.at_name("in"):
                tok = self.advance()
                op = tok.value
                right = self.parse_concat()
                left = Binary(op, left, right, tok.line)
                continue
            if self.at_name("not") and self.peek().kind == "name" and self.peek().value == "in":
                line = self.advance().line
                self.advance()
                right = self.parse_concat()
                left = Binary("notin", left, right, line)
                continue
            break
        return left

    def parse_concat(self) -> Expr:
        left = self.parse_additive()
        while self.at_op("~"):
            line = self.advance().line
            right = self.parse_additive()
            left = Binary("~", left, right, line)
        return left

    def parse_additive(self) -> Expr:
        left = self.parse_multiplicative()
        while self.at_op("+", "-"):
            tok = self.advance()
            right = self.parse_multiplicative()
            left = Binary(tok.value, left, right, tok.line)
        return left

    def parse_multiplicative(self) -> Expr:
        left = self.parse_unary()
        while self.at_op("*", "/", "//", "%"):
            tok = self.advance()
            right = self.parse_unary()
            left = Binary(tok.value, left, right, tok.line)
        return left

    def parse_unary(self) -> Expr:
        if self.at_op("-", "+"):
            tok = self.advance()
            return Unary(tok.value, self.parse_unary(), tok.line)
        return self.parse_postfix()

    def parse_postfix(self) -> Expr:
        expr = self.parse_primary()
        while True:
            if self.at_op("."):
                line = self.advance().line
                tok = self.advance()
                if tok.kind not in {"name", "num"}:
                    raise TemplateError(f"expected an attribute name after '.' (line {line})")
                expr = GetAttr(expr, str(tok.value), line)
                continue
            if self.at_op("["):
                line = self.advance().line
                index = self.parse_or()
                self.expect_op("]")
                expr = GetItem(expr, index, line)
                continue
            if self.at_op("("):
                line = self.advance().line
                args, kwargs = self.parse_arguments()
                expr = Call(expr, args, kwargs, line)
                continue
            if self.at_op("|"):
                line = self.advance().line
                tok = self.advance()
                if tok.kind != "name":
                    raise TemplateError(f"expected a filter name after '|' (line {line})")
                args: List[Expr] = []
                kwargs: Dict[str, Expr] = {}
                if self.at_op("("):
                    self.advance()
                    args, kwargs = self.parse_arguments()
                expr = Filter(expr, tok.value, args, kwargs, line)
                continue
            break
        return expr

    def parse_arguments(self) -> Tuple[List[Expr], Dict[str, Expr]]:
        args: List[Expr] = []
        kwargs: Dict[str, Expr] = {}
        if self.at_op(")"):
            self.advance()
            return args, kwargs
        while True:
            if self.current.kind == "name" and self.peek().kind == "op" and self.peek().value == "=":
                key = str(self.advance().value)
                self.advance()
                kwargs[key] = self.parse_or()
            else:
                if kwargs:
                    raise TemplateError("positional argument after keyword argument (line "
                                        f"{self.current.line})")
                args.append(self.parse_or())
            if self.at_op(","):
                self.advance()
                if self.at_op(")"):
                    break
                continue
            break
        self.expect_op(")")
        return args, kwargs

    def parse_primary(self) -> Expr:
        tok = self.current
        if tok.kind == "num":
            self.advance()
            return Literal(tok.value)
        if tok.kind == "str":
            self.advance()
            return Literal(tok.value)
        if tok.kind == "name":
            low = str(tok.value).lower()
            if low in {"true", "false"}:
                self.advance()
                return Literal(low == "true")
            if low in {"none", "null"}:
                self.advance()
                return Literal(None)
            self.advance()
            return Name(str(tok.value), tok.line)
        if tok.kind == "op" and tok.value == "(":
            self.advance()
            if self.at_op(")"):  # empty tuple
                self.advance()
                return ListLiteral([])
            expr = self.parse_conditional()
            if self.at_op(","):
                items = [expr]
                while self.at_op(","):
                    self.advance()
                    if self.at_op(")"):
                        break
                    items.append(self.parse_or())
                self.expect_op(")")
                return ListLiteral(items)
            self.expect_op(")")
            return expr
        if tok.kind == "op" and tok.value == "[":
            self.advance()
            items = []
            while not self.at_op("]"):
                items.append(self.parse_or())
                if self.at_op(","):
                    self.advance()
                    continue
                break
            self.expect_op("]")
            return ListLiteral(items)
        if tok.kind == "op" and tok.value == "{":
            self.advance()
            entries: List[Tuple[Any, Expr]] = []
            while not self.at_op("}"):
                key_tok = self.advance()
                if key_tok.kind not in {"str", "name", "num"}:
                    raise TemplateError(f"invalid dict key {key_tok.value!r} (line {key_tok.line})")
                key = str(key_tok.value) if key_tok.kind == "name" else key_tok.value
                self.expect_op(":")
                entries.append((key, self.parse_or()))
                if self.at_op(","):
                    self.advance()
                    continue
                break
            self.expect_op("}")
            return DictLiteral(entries)
        raise TemplateError(f"unexpected token {tok.value!r} in expression (line {tok.line})")


def parse_expression(source: str, line: int = 1) -> Expr:
    return ExprParser(lex_expression(source, line)).parse()


# --------------------------------------------------------------------------- #
# value helpers
# --------------------------------------------------------------------------- #
def truthy(value: Any) -> bool:
    if isinstance(value, Undefined):
        return False
    if value is None:
        return False
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"", "0", "false", "no", "off", "null", "none"}:
            return False
        return True
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return bool(value)


def to_number(value: Any) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return value  # type: ignore[return-value]
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text) if "." in text else int(text)
        except ValueError as exc:
            raise TemplateError(f"cannot use {value!r} as a number") from exc
    if value is None or isinstance(value, Undefined):
        return 0
    raise TemplateError(f"cannot use {type(value).__name__} as a number")


def to_text(value: Any) -> str:
    if isinstance(value, Undefined):
        return ""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, (list, tuple)):
        return ", ".join(to_text(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
def _f_default(value: Any, fallback: Any = "", boolean: Any = False) -> Any:
    if isinstance(value, Undefined) or value is None:
        return fallback
    if truthy(boolean) and not value:
        return fallback
    return value


def _f_join(value: Any, sep: str = ", ") -> str:
    if isinstance(value, Undefined) or value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(sep).join(to_text(v) for v in value)


def _f_replace(value: Any, old: str, new: str = "", count: Any = -1) -> str:
    try:
        return to_text(value).replace(str(old), str(new), int(count))
    except (TypeError, ValueError):
        return to_text(value).replace(str(old), str(new))


def _f_indent(value: Any, width: Any = 4, first: Any = False) -> str:
    pad = " " * int(width) if str(width).isdigit() or isinstance(width, int) else str(width)
    lines = to_text(value).splitlines(True)
    out = []
    for idx, line in enumerate(lines):
        if idx == 0 and not truthy(first):
            out.append(line)
        else:
            out.append(pad + line if line.strip() else line)
    text = "".join(out)
    return text.rstrip("\n") if not text.endswith("\n") else text


def _f_quote(value: Any) -> str:
    return shlex.quote(to_text(value))


def _f_basename(value: Any) -> str:
    return os.path.basename(to_text(value).rstrip("/"))


def _f_dirname(value: Any) -> str:
    return os.path.dirname(to_text(value))


def _f_expanduser(value: Any) -> str:
    return os.path.expanduser(os.path.expandvars(to_text(value)))


def _f_json(value: Any, indent: Any = 2) -> str:
    try:
        level = int(indent)
    except (TypeError, ValueError):
        level = 2
    if level <= 0:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=level)


def _f_truncate(value: Any, length: Any = 80, ellipsis: str = "…") -> str:
    text = to_text(value)
    n = int(length)
    if len(text) <= n:
        return text
    return text[: max(0, n - len(ellipsis))] + ellipsis


def _f_slug(value: Any) -> str:
    text = unicodedata.normalize("NFKD", to_text(value))
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_]+", "-", text)


def _f_regex_replace(value: Any, pattern: str, repl: str = "") -> str:
    return re.sub(pattern, repl, to_text(value))


def _f_match(value: Any, pattern: str) -> bool:
    return bool(re.search(pattern, to_text(value)))


def _f_sort(value: Any, reverse: Any = False, key: Any = None) -> Any:
    items = list(value or [])
    if key:
        name = str(key)

        def _key(item: Any) -> Any:
            if isinstance(item, dict) and name in item:
                return item[name]
            return getattr(item, name, item)

        return sorted(items, key=_key, reverse=truthy(reverse))
    try:
        return sorted(items, reverse=truthy(reverse))
    except TypeError:
        return sorted(items, key=to_text, reverse=truthy(reverse))


def _f_unique(value: Any) -> List[Any]:
    out: List[Any] = []
    for item in value or []:
        if item not in out:
            out.append(item)
    return out


def _f_sha256(value: Any) -> str:
    return hashlib.sha256(to_text(value).encode("utf-8")).hexdigest()


def _f_b64(value: Any, encoding: str = "utf-8") -> str:
    return base64.b64encode(to_text(value).encode(encoding)).decode("ascii")


def _f_b64decode(value: Any, encoding: str = "utf-8") -> str:
    return base64.b64decode(to_text(value)).decode(encoding, errors="replace")


def _f_lines(value: Any) -> List[str]:
    return to_text(value).splitlines()


def _f_abspath(value: Any, base: Any = None) -> str:
    text = os.path.expanduser(os.path.expandvars(to_text(value)))
    if not os.path.isabs(text):
        text = os.path.join(to_text(base) if base else os.getcwd(), text)
    return os.path.normpath(text)


def _f_relpath(value: Any, start: Any = ".") -> str:
    return os.path.relpath(to_text(value), to_text(start))


def _f_human(value: Any) -> str:
    from .util import human_size

    try:
        return human_size(float(value))
    except (TypeError, ValueError):
        return to_text(value)


def _f_items(value: Any) -> List[Any]:
    if isinstance(value, dict):
        return [[k, v] for k, v in value.items()]
    return [[i, v] for i, v in enumerate(value or [])]


def _f_first(value: Any) -> Any:
    if not value:
        return Undefined("first")
    try:
        return next(iter(value))
    except StopIteration:  # pragma: no cover - guarded by the truthiness check
        return Undefined("first")


def _f_last(value: Any) -> Any:
    if not value:
        return Undefined("last")
    try:
        return list(value)[-1]
    except IndexError:  # pragma: no cover
        return Undefined("last")


def _f_get(value: Any, key: Any, fallback: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, fallback)
    if isinstance(value, (list, tuple)):
        try:
            return value[int(key)]
        except (IndexError, ValueError, TypeError):
            return fallback
    return fallback


def _f_pluralize(value: Any, singular: str = "", plural: str = "s") -> str:
    try:
        count = float(value)
    except (TypeError, ValueError):
        count = 0
    return singular if count == 1 else plural


def _f_replace_glob(value: Any) -> str:  # pragma: no cover - convenience
    return to_text(value)


def default_filters() -> Dict[str, Callable[..., Any]]:
    return {
        "upper": lambda v: to_text(v).upper(),
        "lower": lambda v: to_text(v).lower(),
        "title": lambda v: to_text(v).title(),
        "capitalize": lambda v: to_text(v).capitalize(),
        "trim": lambda v: to_text(v).strip(),
        "strip": lambda v: to_text(v).strip(),
        "lstrip": lambda v: to_text(v).lstrip(),
        "rstrip": lambda v: to_text(v).rstrip(),
        "default": _f_default,
        "d": _f_default,
        "join": _f_join,
        "replace": _f_replace,
        "regex_replace": _f_regex_replace,
        "match": _f_match,
        "split": lambda v, sep=None, maxsplit=-1: to_text(v).split(sep, int(maxsplit)),
        "lines": _f_lines,
        "first": _f_first,
        "last": _f_last,
        "length": lambda v: len(v) if hasattr(v, "__len__") else 0,
        "len": lambda v: len(v) if hasattr(v, "__len__") else 0,
        "count": lambda v: len(v) if hasattr(v, "__len__") else 0,
        "reverse": lambda v: list(reversed(list(v))) if not isinstance(v, str) else v[::-1],
        "sort": _f_sort,
        "unique": _f_unique,
        "keys": lambda v: list(v.keys()) if isinstance(v, dict) else [],
        "values": lambda v: list(v.values()) if isinstance(v, dict) else [],
        "items": _f_items,
        "get": _f_get,
        "indent": _f_indent,
        "quote": _f_quote,
        "shell_quote": _f_quote,
        "basename": _f_basename,
        "dirname": _f_dirname,
        "expanduser": _f_expanduser,
        "abspath": _f_abspath,
        "relpath": _f_relpath,
        "json": _f_json,
        "tojson": _f_json,
        "truncate": _f_truncate,
        "slug": _f_slug,
        "sha256": _f_sha256,
        "b64encode": _f_b64,
        "b64decode": _f_b64decode,
        "human_size": _f_human,
        "pluralize": _f_pluralize,
        "ljust": lambda v, width=10, fill=" ": to_text(v).ljust(int(width), str(fill)[:1] or " "),
        "rjust": lambda v, width=10, fill=" ": to_text(v).rjust(int(width), str(fill)[:1] or " "),
        "center": lambda v, width=10, fill=" ": to_text(v).center(int(width), str(fill)[:1] or " "),
        "int": lambda v, default=0: _safe_int(v, default),
        "float": lambda v, default=0.0: _safe_float(v, default),
        "string": to_text,
        "str": to_text,
        "bool": truthy,
        "map_attr": lambda v, attr: [_get_attr(i, str(attr)) for i in (v or [])],
        "noop": _f_replace_glob,
        "compact": lambda v: [i for i in (v or []) if truthy(i)],
        "totitle": lambda v: to_text(v).title(),
    }


def _safe_int(value: Any, fallback: Any = 0) -> Any:
    try:
        return int(to_number(value))
    except TemplateError:
        return fallback


def _safe_float(value: Any, fallback: Any = 0.0) -> Any:
    try:
        return float(to_number(value))
    except TemplateError:
        return fallback


def _get_attr(item: Any, name: str) -> Any:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


# --------------------------------------------------------------------------- #
# built-in functions
# --------------------------------------------------------------------------- #
def default_functions(env: "Environment") -> Dict[str, Callable[..., Any]]:
    def _range(*args: Any) -> List[int]:
        if len(args) == 1:
            return list(range(int(to_number(args[0]))))
        if len(args) == 2:
            return list(range(int(to_number(args[0])), int(to_number(args[1]))))
        return list(range(int(to_number(args[0])), int(to_number(args[1])), int(to_number(args[2])) or 1))

    def _env(name: Any, fallback: Any = None) -> Any:
        key = to_text(name)
        if key in os.environ:
            return os.environ[key]
        return fallback if fallback is not None else Undefined(f"env.{key}")

    def _now(fmt: str = "%Y-%m-%dT%H:%M:%SZ") -> str:
        return time.strftime(fmt, time.gmtime())

    def _local_now(fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        return time.strftime(fmt, time.localtime())

    def _file(path: Any, fallback: Any = None, strip: Any = True) -> Any:
        expanded = os.path.expanduser(os.path.expandvars(to_text(path)))
        if not os.path.isabs(expanded) and env.base_dir:
            expanded = os.path.join(str(env.base_dir), expanded)
        try:
            with open(expanded, "r", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            return fallback if fallback is not None else Undefined(f"file({path})")
        return text.strip() if truthy(strip) else text

    def _env_file(path: Any, key: Any, fallback: Any = None) -> Any:  # pragma: no cover - convenience
        text = _file(path, fallback="")
        for line in to_text(text).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == to_text(key):
                return v.strip().strip("'\"")
        return fallback if fallback is not None else Undefined(f"env_file({path},{key})")

    def _secret(name: Any, fallback: Any = None) -> Any:
        if env.secret_resolver is None:
            if fallback is not None:
                return fallback
            raise TemplateError(
                f"template requested secret {to_text(name)!r} but no secret resolver is configured"
            )
        value = env.secret_resolver(to_text(name))
        if value is None:
            if fallback is not None:
                return fallback
            raise TemplateError(f"secret {to_text(name)!r} could not be resolved")
        return value

    def _glob(pattern: Any, base: Any = None) -> List[str]:
        import glob as glob_mod

        root = to_text(base) if base else (str(env.base_dir) if env.base_dir else os.getcwd())
        expanded = os.path.expanduser(os.path.expandvars(to_text(pattern)))
        if not os.path.isabs(expanded):
            expanded = os.path.join(root, expanded)
        return sorted(glob_mod.glob(expanded))

    def _exists(path: Any) -> bool:
        expanded = os.path.expanduser(os.path.expandvars(to_text(path)))
        if not os.path.isabs(expanded) and env.base_dir:
            expanded = os.path.join(str(env.base_dir), expanded)
        return os.path.exists(expanded)

    def _which(program: Any) -> Any:
        import shutil

        found = shutil.which(to_text(program))
        return found or Undefined(f"which({program})")

    def _include(path: Any, **kwargs: Any) -> str:
        return env.render_include(to_text(path), kwargs) if env.loader else ""

    def _run(command: Any, fallback: Any = "", timeout: Any = 10) -> str:
        from .util import run_command

        try:
            result = run_command(to_text(command), timeout=float(to_number(timeout)))
        except Exception:
            return to_text(fallback)
        return result.stdout if result.ok else to_text(fallback)

    def _fnmatch(name: Any, pattern: Any) -> bool:
        return fnmatch.fnmatch(to_text(name), to_text(pattern))

    funcs: Dict[str, Callable[..., Any]] = {
        "range": _range,
        "env": _env,
        "now": _now,
        "local_now": _local_now,
        "today": lambda fmt="%Y-%m-%d": time.strftime(fmt, time.gmtime()),
        "file": _file,
        "env_file": _env_file,
        "read_file": _file,
        "secret": _secret,
        "glob": _glob,
        "exists": _exists,
        "which": _which,
        "include": _include,
        "run": _run,
        "fnmatch": _fnmatch,
        "hostname": lambda: socket.gethostname(),
        "user": lambda: os.environ.get("USER") or os.environ.get("USERNAME") or "user",
        "platform": lambda: platform.system().lower(),
        "uuid": lambda: str(uuid_mod.uuid4()),
        "min": lambda *a: min(a),
        "max": lambda *a: max(a),
        "sum": lambda values: sum(to_number(v) for v in values),
        "len": lambda value: len(value) if hasattr(value, "__len__") else 0,
        "join_path": lambda *parts: posixpath.join(*(to_text(p) for p in parts)),
        "escape": lambda value: to_text(value).replace("{{", "\\{{"),
    }
    return funcs


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #
class Environment:
    """Holds variables and drives expression evaluation."""

    def __init__(
        self,
        variables: Optional[Dict[str, Any]] = None,
        *,
        strict: bool = True,
        filters: Optional[Dict[str, Callable[..., Any]]] = None,
        functions: Optional[Dict[str, Callable[..., Any]]] = None,
        base_dir: Optional[Path] = None,
        loader: Optional[Callable[[str], Optional[str]]] = None,
        secret_resolver: Optional[Callable[[str], Optional[str]]] = None,
        max_include_depth: int = 16,
    ) -> None:
        self.vars: Dict[str, Any] = dict(variables or {})
        self.strict = strict
        self.filters = default_filters()
        if filters:
            self.filters.update(filters)
        self.base_dir = Path(base_dir) if base_dir else None
        self.loader = loader
        self.secret_resolver = secret_resolver
        self.max_include_depth = max_include_depth
        self._include_stack: List[str] = []
        self._scopes: List[Dict[str, Any]] = []
        self.undefined_seen: Dict[str, int] = {}
        self.functions = default_functions(self)
        if functions:
            self.functions.update(functions)

    # -- lookups -----------------------------------------------------------
    def child(self, variables: Dict[str, Any]) -> "Environment":
        clone = Environment(
            self.vars,
            strict=self.strict,
            filters=self.filters,
            functions=self.functions,
            base_dir=self.base_dir,
            loader=self.loader,
            secret_resolver=self.secret_resolver,
            max_include_depth=self.max_include_depth,
        )
        clone._scopes = [*self._scopes, dict(variables)]
        clone._include_stack = list(self._include_stack)
        clone.undefined_seen = self.undefined_seen
        return clone

    def push(self, variables: Dict[str, Any]) -> Dict[str, Any]:
        self._scopes.append(dict(variables))
        return variables

    def pop(self) -> None:
        if self._scopes:
            self._scopes.pop()

    def set(self, name: str, value: Any, *, local: bool = False) -> None:
        if local or not self._scopes:
            self.vars[name] = value
        else:
            self._scopes[-1][name] = value

    def lookup(self, name: str, line: int = 1) -> Any:
        for scope in reversed(self._scopes):
            if name in scope:
                return scope[name]
        if name in self.vars:
            return self.vars[name]
        if name in self.functions:
            return self.functions[name]
        # Undefined is returned lazily so `{{ x | default('y') }}` works even in
        # strict mode; the error is raised when the value is actually emitted.
        self.undefined_seen[name] = line
        return Undefined(name)

    def get_member(self, base: Any, attr: str, line: int = 1) -> Any:
        if isinstance(base, Undefined):
            return Undefined(f"{base.name}.{attr}")
        if isinstance(base, dict):
            if attr in base:
                return base[attr]
            self.undefined_seen[f"{attr}"] = line
            return Undefined(attr)
        if hasattr(base, attr) and not attr.startswith("_"):
            return getattr(base, attr)
        self.undefined_seen[f"{type(base).__name__}.{attr}"] = line
        return Undefined(f"{type(base).__name__}.{attr}")

    def get_index(self, base: Any, index: Any, line: int = 1) -> Any:
        if isinstance(base, Undefined):
            return base
        if isinstance(base, dict):
            key = index
            if key in base:
                return base[key]
            key_str = to_text(key)
            if key_str in base:
                return base[key_str]
            self.undefined_seen[to_text(key)] = line
            return Undefined(to_text(key))
        try:
            if isinstance(index, str) and isinstance(base, (list, tuple)):
                idx = int(index)
            else:
                idx = int(to_number(index))
            return base[idx]
        except (IndexError, KeyError, TypeError, ValueError):
            self.undefined_seen[f"index {index!r}"] = line
            return Undefined(f"index {index!r}")

    def call(self, target: Any, args: Sequence[Any], kwargs: Dict[str, Any], line: int = 1) -> Any:
        if not callable(target):
            raise TemplateError(f"{target!r} is not callable (line {line})")
        try:
            return target(*args, **kwargs)
        except TypeError as exc:
            raise TemplateError(f"error calling function at line {line}: {exc}") from exc

    def apply_filter(self, value: Any, name: str, args: Sequence[Any], kwargs: Dict[str, Any], line: int = 1) -> Any:
        func = self.filters.get(name)
        if func is None:
            raise TemplateError(f"unknown filter {name!r} (line {line})")
        try:
            return func(value, *args, **kwargs)
        except TemplateError:
            raise
        except Exception as exc:
            raise TemplateError(f"filter {name!r} failed at line {line}: {exc}") from exc

    def binary(self, op: str, left: Any, right: Any, line: int = 1) -> Any:
        try:
            if op == "==":
                return _loose_eq(left, right)
            if op == "!=":
                return not _loose_eq(left, right)
            if op == "~":
                return to_text(left) + to_text(right)
            if op == "in":
                return _contains(right, left)
            if op == "notin":
                return not _contains(right, left)
            if op in {"<", "<=", ">", ">="}:
                lval, rval = _comparable(left, right)
                if op == "<":
                    return lval < rval
                if op == "<=":
                    return lval <= rval
                if op == ">":
                    return lval > rval
                return lval >= rval
            if op == "+":
                if isinstance(left, str) or isinstance(right, str):
                    return to_text(left) + to_text(right)
                if isinstance(left, list) and isinstance(right, list):
                    return list(left) + list(right)
                return to_number(left) + to_number(right)
            if op == "-":
                if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
                    return [item for item in left if item not in right]
                return to_number(left) - to_number(right)
            if op == "*":
                if isinstance(left, str) and isinstance(right, (int, float)):
                    return left * int(right)
                return to_number(left) * to_number(right)
            if op == "/":
                divisor = to_number(right)
                if divisor == 0:
                    raise TemplateError(f"division by zero (line {line})")
                return to_number(left) / divisor
            if op == "//":
                divisor = to_number(right)
                if divisor == 0:
                    raise TemplateError(f"division by zero (line {line})")
                return to_number(left) // divisor
            if op == "%":
                divisor = to_number(right)
                if divisor == 0:
                    raise TemplateError(f"division by zero (line {line})")
                return to_number(left) % divisor
        except TemplateError:
            raise
        except Exception as exc:
            raise TemplateError(f"operator {op!r} failed at line {line}: {exc}") from exc
        raise TemplateError(f"unsupported operator {op!r}")  # pragma: no cover

    # -- rendering ---------------------------------------------------------
    def render_include(self, name: str, extra: Dict[str, Any]) -> str:
        if self.loader is None:
            if self.strict:
                raise TemplateError(f"include is not available ({name!r})")
            return ""
        if len(self._include_stack) >= self.max_include_depth:
            raise TemplateError(f"include depth exceeded while including {name!r}")
        source = self.loader(name)
        if source is None:
            raise TemplateError(f"included template not found: {name!r}")
        self._include_stack.append(name)
        try:
            template = Template(source, name=name)
            if extra:
                return template.render(self.child({str(k): v for k, v in extra.items()}))
            return template.render(self)
        finally:
            self._include_stack.pop()


def _loose_eq(left: Any, right: Any) -> bool:
    if isinstance(left, Undefined) or isinstance(right, Undefined):
        return isinstance(left, Undefined) and isinstance(right, Undefined)
    if isinstance(left, bool) or isinstance(right, bool):
        return truthy(left) == truthy(right)
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if type(left) is type(right):
        return bool(left == right)
    if left is None or right is None:
        return left is right
    return to_text(left) == to_text(right)


def _comparable(left: Any, right: Any) -> Tuple[Any, Any]:
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left, right
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return list(left), list(right)
    return to_text(left), to_text(right)


def _contains(haystack: Any, needle: Any) -> bool:
    if isinstance(haystack, Undefined) or haystack is None:
        return False
    if isinstance(haystack, dict):
        return needle in haystack
    if isinstance(haystack, str):
        return to_text(needle) in haystack
    try:
        return needle in haystack
    except TypeError:  # pragma: no cover
        return False


# --------------------------------------------------------------------------- #
# template AST + parser
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    line: int = 0


@dataclass
class TextNode(Node):
    text: str = ""


@dataclass
class OutputNode(Node):
    expr: Optional[Expr] = None
    raw: str = ""


@dataclass
class IfNode(Node):
    branches: List[Tuple[Expr, List[Node]]] = field(default_factory=list)
    else_body: List[Node] = field(default_factory=list)


@dataclass
class ForNode(Node):
    target: str = ""
    index_target: Optional[str] = None
    iterable: Optional[Expr] = None
    body: List[Node] = field(default_factory=list)
    else_body: List[Node] = field(default_factory=list)
    condition: Optional[Expr] = None


@dataclass
class SetNode(Node):
    name: str = ""
    expr: Optional[Expr] = None


@dataclass
class IncludeNode(Node):
    path: Expr = None  # type: ignore[assignment]
    extra: Dict[str, Expr] = field(default_factory=dict)


@dataclass
class RawNode(Node):
    text: str = ""


TOKEN_SPLIT_RE = re.compile(r"(\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\})", re.DOTALL)


class Template:
    """Compiled template. Parsing happens once, rendering is cheap and repeated."""

    def __init__(self, source: str, name: str = "<string>") -> None:
        self.source = source
        self.name = name
        self.nodes: List[Node] = self._parse()

    # -- parsing -----------------------------------------------------------
    def _parse(self) -> List[Node]:
        return _TemplateParser(self.source, self.name).parse()

    def render(self, env: Optional[Environment] = None, **variables: Any) -> str:
        if env is None:
            env = Environment({})
        elif variables:
            env = env.child(variables)
        chunks: List[str] = []
        _render_nodes(self.nodes, env, chunks)
        return "".join(chunks)

    def render_str(self, variables: Optional[Dict[str, Any]] = None, *, strict: bool = True,
                   **extra: Any) -> str:
        merged = dict(variables or {})
        merged.update(extra)
        return self.render(Environment(merged, strict=strict))

    @property
    def variables(self) -> List[str]:
        """Names referenced by the template (best effort, useful for ``doctor``)."""

        names: set = set()

        def walk(expr: Optional[Expr]) -> None:
            if expr is None:
                return
            if isinstance(expr, Name):
                names.add(expr.name)
                return
            for value in vars(expr).values():
                if isinstance(value, Expr):
                    walk(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, Expr):
                            walk(item)
                        elif isinstance(item, tuple):
                            for sub in item:
                                walk(sub) if isinstance(sub, Expr) else None
                elif isinstance(value, dict):
                    for item in value.values():
                        walk(item)

        def walk_nodes(nodes: List[Node]) -> None:
            for node in nodes:
                if isinstance(node, OutputNode):
                    walk(node.expr)
                elif isinstance(node, IfNode):
                    for cond, body in node.branches:
                        walk(cond)
                        walk_nodes(body)
                    walk_nodes(node.else_body)
                elif isinstance(node, ForNode):
                    walk(node.iterable)
                    walk(node.condition)
                    walk_nodes(node.body)
                    walk_nodes(node.else_body)
                elif isinstance(node, SetNode):
                    walk(node.expr)
                elif isinstance(node, IncludeNode):
                    walk(node.path)

        walk_nodes(self.nodes)
        return sorted(names)


class _TemplateParser:
    """Turns source text into a node tree.

    Tags that occupy a line on their own are trimmed together with the
    surrounding whitespace/newline (``trim_blocks`` + ``lstrip_blocks``
    semantics), which is what users expect from block-only lines.
    """

    def __init__(self, source: str, name: str) -> None:
        self.name = name
        self.tokens = self._tokenize(source)
        self.index = 0

    @staticmethod
    def _line_has_only_blanks(source: str, index: int) -> Optional[int]:
        """If only whitespace precedes *index* on its line, return where the line starts."""

        line_start = source.rfind("\n", 0, index) + 1
        if source[line_start:index].strip() == "":
            return line_start
        return None

    @staticmethod
    def _skip_trailing_newline(source: str, index: int) -> int:
        if source.startswith("\r\n", index):
            return index + 2
        if source.startswith("\n", index):
            return index + 1
        return index

    def _tokenize(self, source: str) -> List[Tuple[str, str, int]]:
        tokens: List[Tuple[str, str, int]] = []
        pos = 0
        line = 1
        while pos < len(source):
            idx = source.find("{", pos)
            if idx == -1:
                tokens.append(("text", source[pos:], line))
                break
            if idx + 1 >= len(source) or source[idx + 1] not in "{%#":
                # lone brace - literal text
                tokens.append(("text", source[pos : idx + 1], line))
                pos = idx + 1
                continue
            marker = source[idx + 1]
            closer = {"{": "}}", "%": "%}", "#": "#}"}[marker]
            end = source.find(closer, idx + 2)
            if end == -1:
                raise TemplateError(
                    f"unclosed '{source[idx:idx + 2]}' in {self.name} (line {line})"
                )
            text_end = idx
            after = end + 2
            if marker in "%#":
                # trim the whole line when the tag stands alone
                line_start = self._line_has_only_blanks(source, idx)
                if line_start is not None:
                    trimmed = self._skip_trailing_newline(source, after)
                    if trimmed != after:
                        text_end = line_start
                        after = trimmed
                        if text_end > pos:
                            tokens.append(("text", source[pos:text_end], line))
                        line += source.count("\n", pos, text_end)
                        pos = text_end
                        inner = source[idx + 2 : end]
                        kind = "stmt" if marker == "%" else "comment"
                        tokens.append((kind, inner.strip() if kind == "stmt" else inner, line))
                        line += source.count("\n", idx, after)
                        pos = after
                        continue
            if idx > pos:
                tokens.append(("text", source[pos:idx], line))
            line += source.count("\n", pos, idx)
            inner = source[idx + 2 : end]
            kind = {"{": "expr", "%": "stmt", "#": "comment"}[marker]
            tokens.append((kind, inner.strip() if kind == "stmt" else inner, line))
            line += source.count("\n", idx, after)
            pos = after
        return tokens

    # -- recursive descent -------------------------------------------------
    def parse(self) -> List[Node]:
        nodes, terminator = self._parse_block()
        if terminator is not None:
            raise TemplateError(f"unexpected {{% {terminator} %}} in {self.name}")
        return nodes

    def _parse_block(self, stop: Optional[Sequence[str]] = None) -> Tuple[List[Node], Optional[str]]:
        stop = stop or ()
        nodes: List[Node] = []
        while self.index < len(self.tokens):
            kind, value, line = self.tokens[self.index]
            if kind == "comment":
                self.index += 1
                continue
            if kind == "text":
                self.index += 1
                if value:
                    nodes.append(TextNode(line=line, text=value))
                continue
            if kind == "expr":
                self.index += 1
                if not value.strip():
                    raise TemplateError(f"empty expression in {self.name} (line {line})")
                nodes.append(OutputNode(line=line, expr=parse_expression(value, line), raw=value))
                continue
            # statement
            head = value.split(None, 1)[0] if value else ""
            if head in stop:
                return nodes, value
            if head in {"endif", "endfor", "else", "elif"}:
                raise TemplateError(f"unexpected {{% {value} %}} in {self.name} (line {line})")
            self.index += 1
            if head == "if":
                nodes.append(self._parse_if(value, line))
            elif head == "for":
                nodes.append(self._parse_for(value, line))
            elif head == "set":
                nodes.append(self._parse_set(value, line))
            elif head == "include":
                nodes.append(self._parse_include(value, line))
            elif head == "raw":
                nodes.append(self._parse_raw(line))
            elif head in {"do", "apply"}:
                raise TemplateError(f"unsupported tag {head!r} in {self.name} (line {line})")
            else:
                raise TemplateError(f"unknown tag {head!r} in {self.name} (line {line})")
        return nodes, None

    def _parse_if(self, statement: str, line: int) -> IfNode:
        parts = statement.split(None, 1)
        if len(parts) < 2:
            raise TemplateError(f"'if' requires a condition in {self.name} (line {line})")
        branches: List[Tuple[Expr, List[Node]]] = []
        else_body: List[Node] = []
        condition = parse_expression(parts[1], line)
        while True:
            body, terminator = self._parse_block(("elif", "else", "endif"))
            branches.append((condition, body))
            if terminator is None:
                raise TemplateError(f"missing {{% endif %}} in {self.name} (line {line})")
            self.index += 1  # consume the terminator tag
            tag, _, rest = terminator.partition(" ")
            if tag == "elif":
                source = rest.strip()
                if not source:
                    raise TemplateError(f"'elif' requires a condition in {self.name} (line {line})")
                condition = parse_expression(source, line)
                continue
            if tag == "else":
                else_body, closing = self._parse_block(("endif",))
                if closing is None:
                    raise TemplateError(f"missing {{% endif %}} in {self.name} (line {line})")
                self.index += 1  # consume endif
            break
        return IfNode(line=line, branches=branches, else_body=else_body)

    def _parse_for(self, statement: str, line: int) -> ForNode:
        body_text = statement[3:].strip()
        if not body_text:
            raise TemplateError(f"'for' requires a target and an iterable in {self.name} (line {line})")
        # optional trailing inline condition: for x in seq if x
        condition_source = None
        if " if " in body_text:
            body_text, _, condition_source = body_text.partition(" if ")
        if " in " not in body_text:
            raise TemplateError(f"'for' expects 'name in iterable' in {self.name} (line {line})")
        target_text, _, iterable_text = body_text.partition(" in ")
        target_text = target_text.strip()
        index_target = None
        if "," in target_text:
            first, second = [p.strip() for p in target_text.split(",", 1)]
            target_text, index_target = first, second
        iterable = parse_expression(iterable_text.strip(), line)
        condition = parse_expression(condition_source.strip(), line) if condition_source else None
        body, terminator = self._parse_block(("else", "endfor"))
        else_body: List[Node] = []
        if terminator is not None and terminator.split(None, 1)[0] == "else":
            self.index += 1  # consume {% else %}
            else_body, terminator = self._parse_block(("endfor",))
        if terminator is None:
            raise TemplateError(f"missing {{% endfor %}} in {self.name} (line {line})")
        self.index += 1  # consume {% endfor %}
        return ForNode(
            line=line,
            target=target_text,
            index_target=index_target,
            iterable=iterable,
            body=body,
            else_body=else_body,
            condition=condition,
        )

    def _parse_set(self, statement: str, line: int) -> SetNode:
        rest = statement[3:].strip()
        if "=" not in rest:
            raise TemplateError(f"'set' expects 'name = value' in {self.name} (line {line})")
        name, _, source = rest.partition("=")
        name = name.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise TemplateError(f"invalid variable name {name!r} in {self.name} (line {line})")
        return SetNode(line=line, name=name, expr=parse_expression(source.strip(), line))

    def _parse_include(self, statement: str, line: int) -> IncludeNode:
        rest = statement[len("include") :].strip()
        if not rest:
            raise TemplateError(f"'include' requires a path in {self.name} (line {line})")
        # support 'include "file" with a=b, c=d'
        extra_source = ""
        if " with " in rest:
            rest, _, extra_source = rest.partition(" with ")
        path_expr = parse_expression(rest.strip(), line)
        extra: Dict[str, Expr] = {}
        if extra_source:
            for chunk in _split_top_level(extra_source, ","):
                if not chunk.strip():
                    continue
                if "=" not in chunk:
                    raise TemplateError(f"invalid include parameter {chunk!r} (line {line})")
                key, _, val = chunk.partition("=")
                extra[key.strip()] = parse_expression(val.strip(), line)
        return IncludeNode(line=line, path=path_expr, extra=extra)

    def _parse_raw(self, line: int) -> RawNode:
        collected: List[str] = []
        while self.index < len(self.tokens):
            kind, value, _ = self.tokens[self.index]
            if kind == "stmt" and value.split(None, 1)[0] == "endraw":
                self.index += 1
                return RawNode(line=line, text="".join(collected))
            self.index += 1
            if kind == "text":
                collected.append(value)
            elif kind == "expr":
                collected.append("{{" + value + "}}")
            elif kind == "stmt":
                collected.append("{%" + value + "%}")
            elif kind == "comment":
                collected.append("{#" + value + "#}")
        raise TemplateError(f"missing {{% endraw %}} in {self.name} (line {line})")


def _split_top_level(text: str, sep: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    current: List[str] = []
    quote: Optional[str] = None
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return parts


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _render_nodes(nodes: List[Node], env: Environment, out: List[str]) -> None:
    for node in nodes:
        if isinstance(node, TextNode):
            out.append(node.text)
        elif isinstance(node, OutputNode):
            value = node.expr.eval(env) if node.expr is not None else ""
            if isinstance(value, Undefined) and env.strict:
                name = value.name or node.raw.strip()
                raise TemplateError(
                    f"undefined value {name!r} used in '{{{{ {node.raw.strip()} }}}}' (line {node.line})"
                )
            out.append(to_text(value))
        elif isinstance(node, IfNode):
            for cond, body in node.branches:
                if truthy(cond.eval(env)):
                    _render_nodes(body, env, out)
                    break
            else:
                _render_nodes(node.else_body, env, out)
        elif isinstance(node, ForNode):
            _render_for(node, env, out)
        elif isinstance(node, SetNode):
            env.set(node.name, node.expr.eval(env) if node.expr is not None else None)
        elif isinstance(node, IncludeNode):
            path = to_text(node.path.eval(env))
            extra = {key: expr.eval(env) for key, expr in node.extra.items()}
            out.append(env.render_include(path, extra))
        elif isinstance(node, RawNode):
            out.append(node.text)


def _render_for(node: ForNode, env: Environment, out: List[str]) -> None:
    iterable = node.iterable.eval(env) if node.iterable is not None else []
    if isinstance(iterable, dict):
        items: List[Any] = list(iterable.items())
    elif isinstance(iterable, Undefined) or iterable is None:
        items = []
    elif isinstance(iterable, (str, bytes)):
        items = list(iterable)
    else:
        try:
            items = list(iterable)
        except TypeError as exc:
            raise TemplateError(f"cannot iterate over {type(iterable).__name__} (line {node.line})") from exc

    selected: List[Tuple[Any, Any]] = []
    for position, item in enumerate(items):
        if node.index_target is None and isinstance(item, tuple) and len(item) == 2 and "," in node.target + ",":
            # unreachable safeguard
            pass
        if node.condition is not None:
            scope = {node.target: item, "loop": _LoopState(len(items), len(selected), position)}
            env.push(scope)
            try:
                keep = truthy(node.condition.eval(env))
            finally:
                env.pop()
            if not keep:
                continue
        selected.append((position, item))

    if not selected:
        _render_nodes(node.else_body, env, out)
        return

    total = len(selected)
    for ordinal, (position, item) in enumerate(selected):
        scope: Dict[str, Any] = {"loop": _LoopState(total, ordinal, position)}
        if node.index_target and isinstance(item, (list, tuple)) and len(item) == 2:
            scope[node.target] = item[0]
            scope[node.index_target] = item[1]
        elif isinstance(item, (list, tuple)) and len(item) == 2 and _looks_like_pair(node.target):
            scope[node.target] = item[0]
            scope[node.target + "_key" if False else "loop"] = scope["loop"]
            scope[item[0] if isinstance(item[0], str) else "key"] = item[0]
            scope["value"] = item[1]
            if isinstance(item[0], str):
                scope[node.target] = item[1]
                scope["key"] = item[0]
        else:
            scope[node.target] = item
        env.push(scope)
        try:
            _render_nodes(node.body, env, out)
        finally:
            env.pop()


def _looks_like_pair(target: str) -> bool:
    return False


@dataclass
class _LoopState:
    length: int
    index0: int
    source_index: int

    @property
    def index(self) -> int:
        return self.index0 + 1

    @property
    def revindex(self) -> int:
        return self.length - self.index0

    @property
    def revindex0(self) -> int:
        return self.length - self.index0 - 1

    @property
    def first(self) -> bool:
        return self.index0 == 0

    @property
    def last(self) -> bool:
        return self.index0 == self.length - 1

    @property
    def previtem(self) -> Any:
        return Undefined("previtem")

    @property
    def nextitem(self) -> Any:
        return Undefined("nextitem")

    @property
    def cycle(self) -> Callable[..., Any]:
        def _cycle(*values: Any) -> Any:
            if not values:
                return Undefined("cycle")
            return values[self.index0 % len(values)]

        return _cycle


# --------------------------------------------------------------------------- #
# convenience
# --------------------------------------------------------------------------- #
def render(source: str, variables: Optional[Dict[str, Any]] = None, *, strict: bool = True, **extra: Any) -> str:
    template = Template(source)
    return template.render_str(variables, strict=strict, **extra)


def make_loader(base_dir: Path, suffixes: Sequence[str] = ("", ".tmpl", ".tpl", ".template")) -> Callable[[str], Optional[str]]:
    """Create an include loader restricted to *base_dir* (path traversal safe)."""

    base = Path(base_dir).resolve()

    def loader(name: str) -> Optional[str]:
        candidates = []
        for suffix in suffixes:
            candidates.append(base / (name + suffix))
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:  # pragma: no cover
                continue
            if not str(resolved).startswith(str(base)):
                raise TemplateError(f"include escapes the project root: {name!r}")
            if resolved.is_file():
                return resolved.read_text(encoding="utf-8", errors="replace")
        return None

    return loader


def template_from_file(path: Path, base_dir: Optional[Path] = None) -> Template:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return Template(text, name=str(path))
