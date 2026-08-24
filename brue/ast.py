# AST node types. The kind_str names are observable: the runtime's
# expression fingerprint falls back to "{kind}_{line}_{col}" and those keys
# decide series-cache identity, so they are locked by the conformance goldens.
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class Loc:
    line: int
    col: int


class Expr:
    """Base class for all expression nodes."""

    # The kind string (used by the fingerprint fallback). Each subclass
    # sets _KIND.
    _KIND = ""

    def kind_str(self) -> str:
        return self._KIND


@dataclass
class KeywordArg:
    name: str
    value: Expr


@dataclass
class NumberLiteral(Expr):
    value: float
    loc: Loc
    _KIND = "NumberLiteral"


@dataclass
class StringLiteral(Expr):
    value: str
    loc: Loc
    _KIND = "StringLiteral"


@dataclass
class BoolLiteral(Expr):
    value: bool
    loc: Loc
    _KIND = "BoolLiteral"


@dataclass
class NaLiteral(Expr):
    loc: Loc
    _KIND = "NaLiteral"


@dataclass
class ColorLiteral(Expr):
    r: float
    g: float
    b: float
    a: float
    loc: Loc
    _KIND = "ColorLiteral"


@dataclass
class Identifier(Expr):
    name: str
    loc: Loc
    _KIND = "Identifier"


@dataclass
class Binary(Expr):
    op: str
    left: Expr
    right: Expr
    loc: Loc
    _KIND = "BinaryExpr"


@dataclass
class Unary(Expr):
    op: str
    operand: Expr
    loc: Loc
    _KIND = "UnaryExpr"


@dataclass
class Call(Expr):
    callee: str
    args: List[Expr]
    kwargs: List[KeywordArg]
    loc: Loc
    _KIND = "CallExpr"


@dataclass
class History(Expr):
    source: Expr
    offset: Expr
    loc: Loc
    _KIND = "HistoryExpr"


@dataclass
class Ternary(Expr):
    condition: Expr
    if_true: Expr
    if_false: Expr
    loc: Loc
    _KIND = "TernaryExpr"


@dataclass
class ArrayLiteral(Expr):
    elements: List[Expr]
    loc: Loc
    _KIND = "ArrayLiteral"


@dataclass
class MapLiteral(Expr):
    entries: List[Tuple[Expr, Expr]]
    loc: Loc
    _KIND = "MapLiteral"


@dataclass
class Member(Expr):
    object: Expr
    member: str
    loc: Loc
    _KIND = "MemberExpr"


@dataclass
class MethodCall(Expr):
    object: Expr
    method: str
    args: List[Expr]
    loc: Loc
    _KIND = "MethodCallExpr"


class FmtPart:
    """F-string parts; variants are the nested classes FmtPart.Text /
    FmtPart.Expr."""

    @dataclass
    class Text:
        value: str

    @dataclass
    class Expr:
        expr: "Expr"


@dataclass
class FormatString(Expr):
    parts: List[object]  # FmtPart.Text | FmtPart.Expr
    specs: List[Optional[str]]
    loc: Loc
    _KIND = "FormatStringExpr"


@dataclass
class FunctionParam:
    name: str
    default_value: Optional[Expr]


@dataclass
class TypeField:
    name: str
    field_type: str
    default_value: Optional[Expr]


@dataclass
class DeclarationStmt:
    args: List[Expr]
    kwargs: List[KeywordArg]
    symbol: Optional[str]
    timeframe: Optional[str]
    execution_mode: str
    broker: Optional[str]
    account: Optional[str]
    password: Optional[str]
    loc: Loc
    # Scripts have no header; the parser always builds this headerless
    # placeholder (pure logic: qualified series, symbol-carrying orders).
    headerless: bool = False


@dataclass
class UseStmt:
    canonical: str
    timeframe: Optional[str]
    alias: str
    alias_explicit: bool
    is_dataset: bool
    loc: Loc


class Stmt:
    """Base class for all statement nodes."""


@dataclass
class Assign(Stmt):
    target: str
    op: str
    value: Expr
    loc: Loc


@dataclass
class Persist(Stmt):
    target: str
    value: Expr
    loc: Loc


@dataclass
class Const(Stmt):
    target: str
    value: Expr
    loc: Loc


@dataclass
class If(Stmt):
    condition: Expr
    body: List[Stmt]
    elifs: List[Tuple[Expr, List[Stmt]]]
    else_body: List[Stmt]
    loc: Loc


@dataclass
class For(Stmt):
    variable: str
    iterable: Expr
    body: List[Stmt]
    loc: Loc


@dataclass
class While(Stmt):
    condition: Expr
    body: List[Stmt]
    loc: Loc


@dataclass
class Break(Stmt):
    loc: Loc


@dataclass
class Continue(Stmt):
    loc: Loc


@dataclass
class FunctionDef(Stmt):
    name: str
    params: List[FunctionParam]
    body: List[Stmt]
    loc: Loc


@dataclass
class Return(Stmt):
    value: Optional[Expr]
    loc: Loc


@dataclass
class ExprStmt(Stmt):
    expr: Expr
    loc: Loc


@dataclass
class MemberAssign(Stmt):
    target: Expr
    value: Expr
    loc: Loc


@dataclass
class MultiAssign(Stmt):
    targets: List[str]
    value: Expr
    loc: Loc


@dataclass
class TypeDef(Stmt):
    name: str
    fields: List[TypeField]
    loc: Loc


@dataclass
class EnumDef(Stmt):
    name: str
    members: List[Tuple[str, Optional[str]]]
    loc: Loc


@dataclass
class Program:
    declaration: DeclarationStmt
    uses: List[UseStmt]
    body: List[Stmt]
