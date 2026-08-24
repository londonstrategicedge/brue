# Recursive-descent parser. Every error message (and its Levenshtein-based
# suggestion) is part of the observable result, so the wording is locked by
# the conformance goldens.
from typing import List, Optional, Tuple

from .ast import (
    ArrayLiteral,
    Assign,
    Binary,
    BoolLiteral,
    Break,
    Call,
    ColorLiteral,
    Const,
    Continue,
    DeclarationStmt,
    EnumDef,
    Expr,
    ExprStmt,
    FmtPart,
    For,
    FormatString,
    FunctionDef,
    FunctionParam,
    History,
    Identifier,
    If,
    KeywordArg,
    Loc,
    MapLiteral,
    Member,
    MemberAssign,
    MethodCall,
    MultiAssign,
    NaLiteral,
    NumberLiteral,
    Persist,
    Program,
    Return,
    Stmt,
    StringLiteral,
    Ternary,
    TypeDef,
    TypeField,
    Unary,
    UseStmt,
    While,
)
from .errors import BrueError, find_closest_match
from .tokenizer import Token, TokType, tokenize

BRUE_DATASET_NAMES = ["econ", "earnings", "cot", "insider"]

LIVE_ORDER_VERBS = ["buy", "sell", "buy_limit", "sell_limit", "buy_stop", "sell_stop"]


def parse(tokens: List[Token]) -> Program:
    p = Parser(tokens)
    program = p.parse_program()
    validate_execution_mode(program)
    return program


EOF_TOKEN = Token(ttype=TokType.Eof, value="", loc=Loc(line=0, col=0))


class Parser:
    def __init__(self, tokens: List[Token]):
        self.tokens = tokens
        self.pos = 0

    def current(self) -> Token:
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return EOF_TOKEN

    def peek(self, offset: int) -> Token:
        idx = self.pos + offset
        if idx < len(self.tokens):
            return self.tokens[idx]
        return EOF_TOKEN

    def advance(self) -> Token:
        t = self.current()
        self.pos += 1
        return t

    def expect(self, ttype: TokType, value: Optional[str]) -> Token:
        t = self.current()
        if t.ttype != ttype or (value is not None and t.value != value):
            expected = "'{}'".format(value) if value is not None else ttype.ts_name()
            suggestion = get_suggestion_for_expected(ttype, value, t)
            raise BrueError.parse(
                "Expected {} but got '{}' ({}).".format(expected, t.value, t.ttype.ts_name()),
                t.loc,
                suggestion,
            )
        return self.advance()

    def match_tok(self, ttype: TokType, value: Optional[str]) -> bool:
        t = self.current()
        if t.ttype == ttype and (value is None or t.value == value):
            self.pos += 1
            return True
        return False

    def check(self, ttype: TokType, value: Optional[str]) -> bool:
        t = self.current()
        return t.ttype == ttype and (value is None or t.value == value)

    def skip_newlines(self):
        while self.current().ttype == TokType.Newline:
            self.pos += 1

    # -- Program --------------------------------------------------------

    def parse_program(self) -> Program:
        self.skip_newlines()
        declaration = self.parse_declaration()
        self.skip_newlines()

        if declaration.headerless and self.check(TokType.Keyword, "use"):
            raise BrueError.parse(
                "'use' is not part of the language: referencing a symbol "
                "declares it.",
                self.current().loc,
                "Write XAUUSD.h1.close directly (LSE naming), or "
                "XAUUSD.mybroker.h1.close for a named source, and feed the "
                'data as {"XAUUSD-h1": df} / {"XAUUSD-mybroker-h1": df} / '
                "--data KEY=path.")
        uses: List[UseStmt] = []
        seen_aliases: List[str] = []
        while self.check(TokType.Keyword, "use"):
            u = self.parse_use_stmt()
            if u.alias in seen_aliases:
                raise BrueError.parse(
                    "Duplicate use binding '{}'.".format(u.alias),
                    u.loc,
                    "Each use line must produce a unique binding. If two foreign symbols collapse to the same name, give one of them an explicit alias with 'as'.",
                )
            seen_aliases.append(u.alias)
            uses.append(u)
            self.skip_newlines()

        body: List[Stmt] = []
        while self.current().ttype != TokType.Eof:
            self.skip_newlines()
            if self.current().ttype == TokType.Eof:
                break
            body.append(self.parse_statement())
            self.skip_newlines()

        return Program(declaration=declaration, uses=uses, body=body)

    # -- Use statement ---------------------------------------------------

    def parse_use_stmt(self) -> UseStmt:
        t = self.advance()  # consume 'use'

        sym_tok = self.current()
        if sym_tok.ttype == TokType.Identifier:
            canonical = self.advance().value
            symbol_was_quoted = False
        elif sym_tok.ttype == TokType.String:
            canonical = self.advance().value
            symbol_was_quoted = True
        else:
            raise BrueError.parse(
                "Expected a symbol after 'use', got '{}'.".format(sym_tok.value),
                sym_tok.loc,
                "Examples: 'use SPY', 'use \"EUR/USD\" as eur', 'use BTCUSD at 1h as btc_h'.",
            )

        timeframe: Optional[str] = None
        if self.check(TokType.Keyword, "at"):
            self.advance()
            tf_tok = self.current()
            if tf_tok.ttype == TokType.Number:
                tf = self.advance().value
                nxt = self.current()
                if (
                    nxt.ttype == TokType.Identifier
                    and nxt.loc.line == tf_tok.loc.line
                    and nxt.loc.col == tf_tok.loc.col + len(tf_tok.value)
                ):
                    tf += self.advance().value
                timeframe = tf
            elif tf_tok.ttype == TokType.Identifier or tf_tok.ttype == TokType.String:
                timeframe = self.advance().value
            else:
                raise BrueError.parse(
                    "Expected a timeframe after 'at', got '{}'.".format(tf_tok.value),
                    tf_tok.loc,
                    "Use values like 1m, 5m, 15m, 1h, 4h, 1D.",
                )

        alias_explicit = False
        if self.check(TokType.Keyword, "as"):
            self.advance()
            alias_tok = self.current()
            if alias_tok.ttype != TokType.Identifier:
                raise BrueError.parse(
                    "Expected an alias identifier after 'as', got '{}'.".format(alias_tok.value),
                    alias_tok.loc,
                    "Aliases must be valid identifiers (letters, digits, underscores; cannot start with a digit).",
                )
            alias = self.advance().value
            alias_explicit = True
        else:
            # A bare symbol doubles as its binding name only when it is a
            # valid identifier: ASCII letter or '_' first, then ASCII
            # alphanumerics or '_'.
            valid = False
            if len(canonical) > 0:
                c0 = canonical[0]
                if ("a" <= c0 <= "z") or ("A" <= c0 <= "Z") or c0 == "_":
                    valid = all(
                        ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9") or c == "_"
                        for c in canonical[1:]
                    )
            if not valid:
                quoted = '"{}"'.format(canonical) if symbol_was_quoted else canonical
                raise BrueError.parse(
                    "Symbol '{}' contains characters that cannot be used as a binding name.".format(canonical),
                    sym_tok.loc,
                    "Add an explicit alias: 'use {} as my_alias'.".format(quoted),
                )
            alias = canonical

        if self.check(TokType.Newline, None):
            self.advance()

        is_dataset = canonical in BRUE_DATASET_NAMES
        if is_dataset and timeframe is not None:
            raise BrueError.parse(
                "Dataset '{}' does not support 'at TIMEFRAME'.".format(canonical),
                t.loc,
                "Remove the timeframe: 'use \"{}\" as {}'.".format(canonical, alias),
            )

        return UseStmt(
            canonical=canonical,
            timeframe=timeframe,
            alias=alias,
            alias_explicit=alias_explicit,
            is_dataset=is_dataset,
            loc=t.loc,
        )

    # -- Declaration -----------------------------------------------------

    def parse_declaration(self) -> DeclarationStmt:
        t = self.current()
        if t.ttype == TokType.Keyword and t.value == "strategy":
            # Scripts have no header. A script is pure logic: run
            # configuration (capital, costs, window) belongs to the run
            # call, display concerns to the host, and what the script
            # trades is written at each order.
            raise BrueError.parse(
                "the strategy() header is not part of the language.",
                t.loc,
                "Delete the header. Orders name their market and quantity "
                "(buy(\"EURUSD\", 0.5, sl=...)); capital and costs are run "
                "parameters (RunOptions(capital=...) on the API or "
                "--capital on the CLI); a script with no orders is an indicator and "
                "may plot.")
        return DeclarationStmt(
            args=[], kwargs=[], symbol=None, timeframe=None,
            execution_mode="backtest", broker=None, account=None,
            password=None, loc=t.loc, headerless=True)

    # -- Statements -------------------------------------------------------

    def parse_statement(self) -> Stmt:
        t = self.current()

        if t.ttype == TokType.Keyword:
            if t.value == "use":
                raise BrueError.parse(
                    "'use' declarations must come at the top of the script, before any other statement.",
                    t.loc,
                    "Move this 'use' line up so it sits right after the strategy(...) call.",
                )
            if t.value == "persist":
                return self.parse_persist_stmt()
            if t.value == "const":
                return self.parse_const_stmt()
            if t.value == "if":
                return self.parse_if_stmt()
            if t.value == "for":
                return self.parse_for_stmt()
            if t.value == "while":
                return self.parse_while_stmt()
            if t.value == "def":
                return self.parse_function_def()
            if t.value == "return":
                return self.parse_return_stmt()
            if t.value == "break":
                loc = self.advance().loc
                if self.check(TokType.Newline, None):
                    self.advance()
                return Break(loc=loc)
            if t.value == "continue":
                loc = self.advance().loc
                if self.check(TokType.Newline, None):
                    self.advance()
                return Continue(loc=loc)
            if t.value == "type":
                return self.parse_type_def_stmt()
            if t.value == "enum":
                return self.parse_enum_def_stmt()

        if t.ttype == TokType.Identifier:
            return self.parse_assign_or_expr_stmt()

        return self.parse_expr_stmt()

    def parse_persist_stmt(self) -> Stmt:
        t = self.advance()
        name = self.expect(TokType.Identifier, None).value
        self.expect(TokType.Assign, "=")
        value = self.parse_expression()
        if self.check(TokType.Newline, None):
            self.advance()
        return Persist(target=name, value=value, loc=t.loc)

    def parse_const_stmt(self) -> Stmt:
        t = self.advance()
        name = self.expect(TokType.Identifier, None).value
        self.expect(TokType.Assign, "=")
        value = self.parse_expression()
        if self.check(TokType.Newline, None):
            self.advance()
        return Const(target=name, value=value, loc=t.loc)

    def parse_if_stmt(self) -> Stmt:
        t = self.advance()
        condition = self.parse_expression()
        self.expect(TokType.Colon, None)
        if self.check(TokType.Newline, None):
            self.advance()
        body = self.parse_block()

        elifs: List[Tuple[Expr, List[Stmt]]] = []
        else_body: List[Stmt] = []

        self.skip_newlines()
        while self.check(TokType.Keyword, "elif"):
            self.advance()
            elif_cond = self.parse_expression()
            self.expect(TokType.Colon, None)
            if self.check(TokType.Newline, None):
                self.advance()
            elif_body = self.parse_block()
            elifs.append((elif_cond, elif_body))
            self.skip_newlines()

        if self.check(TokType.Keyword, "else"):
            self.advance()
            self.expect(TokType.Colon, None)
            if self.check(TokType.Newline, None):
                self.advance()
            else_body = self.parse_block()

        return If(condition=condition, body=body, elifs=elifs, else_body=else_body, loc=t.loc)

    def parse_for_stmt(self) -> Stmt:
        t = self.advance()
        variable = self.expect(TokType.Identifier, None).value
        self.expect(TokType.Keyword, "in")
        iterable = self.parse_expression()
        self.expect(TokType.Colon, None)
        if self.check(TokType.Newline, None):
            self.advance()
        body = self.parse_block()
        return For(variable=variable, iterable=iterable, body=body, loc=t.loc)

    def parse_while_stmt(self) -> Stmt:
        t = self.advance()
        condition = self.parse_expression()
        self.expect(TokType.Colon, None)
        if self.check(TokType.Newline, None):
            self.advance()
        body = self.parse_block()
        return While(condition=condition, body=body, loc=t.loc)

    def parse_function_def(self) -> Stmt:
        t = self.advance()
        name = self.expect(TokType.Identifier, None).value
        self.expect(TokType.LParen, None)
        params = self.parse_function_params()
        self.expect(TokType.RParen, None)
        self.expect(TokType.Colon, None)
        if self.check(TokType.Newline, None):
            self.advance()
        body = self.parse_block()
        return FunctionDef(name=name, params=params, body=body, loc=t.loc)

    def parse_function_params(self) -> List[FunctionParam]:
        params: List[FunctionParam] = []
        while not self.check(TokType.RParen, None) and self.current().ttype != TokType.Eof:
            name = self.expect(TokType.Identifier, None).value
            default_value = None
            if self.match_tok(TokType.Assign, "="):
                default_value = self.parse_expression()
            params.append(FunctionParam(name=name, default_value=default_value))
            if not self.check(TokType.RParen, None):
                self.expect(TokType.Comma, None)
        return params

    def parse_return_stmt(self) -> Stmt:
        t = self.advance()
        value = None
        if (
            not self.check(TokType.Newline, None)
            and self.current().ttype != TokType.Eof
            and not self.check(TokType.Dedent, None)
        ):
            value = self.parse_expression()
        if self.check(TokType.Newline, None):
            self.advance()
        return Return(value=value, loc=t.loc)

    def parse_type_def_stmt(self) -> Stmt:
        t = self.advance()
        name = self.expect(TokType.Identifier, None).value
        self.expect(TokType.Colon, None)
        if self.check(TokType.Newline, None):
            self.advance()
        fields: List[TypeField] = []
        if self.check(TokType.Indent, None):
            self.advance()
            while not self.check(TokType.Dedent, None) and self.current().ttype != TokType.Eof:
                self.skip_newlines()
                if self.check(TokType.Dedent, None) or self.current().ttype == TokType.Eof:
                    break
                field_name = self.expect(TokType.Identifier, None).value
                field_type = "any"
                if self.match_tok(TokType.Colon, None):
                    field_type = self.expect(TokType.Identifier, None).value
                default_value = None
                if self.match_tok(TokType.Assign, "="):
                    default_value = self.parse_expression()
                fields.append(TypeField(name=field_name, field_type=field_type, default_value=default_value))
                if self.check(TokType.Newline, None):
                    self.advance()
            if self.check(TokType.Dedent, None):
                self.advance()
        return TypeDef(name=name, fields=fields, loc=t.loc)

    def parse_enum_def_stmt(self) -> Stmt:
        t = self.advance()
        name = self.expect(TokType.Identifier, None).value
        self.expect(TokType.Colon, None)
        if self.check(TokType.Newline, None):
            self.advance()
        members: List[Tuple[str, Optional[str]]] = []
        if self.check(TokType.Indent, None):
            self.advance()
            while not self.check(TokType.Dedent, None) and self.current().ttype != TokType.Eof:
                self.skip_newlines()
                if self.check(TokType.Dedent, None) or self.current().ttype == TokType.Eof:
                    break
                member_name = self.expect(TokType.Identifier, None).value
                title = None
                if self.match_tok(TokType.Assign, "="):
                    title = self.expect(TokType.String, None).value
                members.append((member_name, title))
                if self.check(TokType.Comma, None):
                    self.advance()
                if self.check(TokType.Newline, None):
                    self.advance()
            if self.check(TokType.Dedent, None):
                self.advance()
        return EnumDef(name=name, members=members, loc=t.loc)

    def parse_assign_or_expr_stmt(self) -> Stmt:
        t = self.current()

        if t.ttype == TokType.Identifier and self.looks_like_multi_assign():
            return self.parse_multi_assign_stmt()

        if t.ttype == TokType.Identifier and self.peek(1).ttype == TokType.Assign:
            return self.parse_assign_stmt()

        return self.parse_expr_stmt()

    def looks_like_multi_assign(self) -> bool:
        # Scan ahead: IDENT (COMMA IDENT)* ASSIGN(=), needs >= 2 idents
        i = self.pos
        ident_count = 0
        while True:
            tok = self.tokens[i] if i < len(self.tokens) else None
            if tok is not None and tok.ttype == TokType.Identifier:
                ident_count += 1
                i += 1
                nxt = self.tokens[i] if i < len(self.tokens) else None
                if nxt is not None:
                    if nxt.ttype == TokType.Comma:
                        i += 1
                        continue
                    if nxt.ttype == TokType.Assign and nxt.value == "=":
                        return ident_count >= 2
                return False
            return False

    def parse_multi_assign_stmt(self) -> Stmt:
        loc = self.current().loc
        targets: List[str] = []
        targets.append(self.expect(TokType.Identifier, None).value)
        while self.match_tok(TokType.Comma, None):
            targets.append(self.expect(TokType.Identifier, None).value)
        self.expect(TokType.Assign, "=")
        value = self.parse_expression()
        if self.check(TokType.Newline, None):
            self.advance()
        return MultiAssign(targets=targets, value=value, loc=loc)

    def parse_assign_stmt(self) -> Stmt:
        t = self.advance()
        op = self.advance().value
        value = self.parse_expression()
        if self.check(TokType.Newline, None):
            self.advance()
        return Assign(target=t.value, op=op, value=value, loc=t.loc)

    def parse_expr_stmt(self) -> Stmt:
        loc = self.current().loc
        expr = self.parse_expression()
        if self.check(TokType.Assign, "=") and isinstance(expr, (Member, History)):
            self.advance()
            value = self.parse_expression()
            if self.check(TokType.Newline, None):
                self.advance()
            return MemberAssign(target=expr, value=value, loc=loc)
        if self.check(TokType.Newline, None):
            self.advance()
        return ExprStmt(expr=expr, loc=loc)

    def parse_block(self) -> List[Stmt]:
        body: List[Stmt] = []

        if not self.check(TokType.Indent, None):
            if (
                not self.check(TokType.Newline, None)
                and self.current().ttype != TokType.Eof
                and not self.check(TokType.Dedent, None)
            ):
                body.append(self.parse_statement())
            return body

        self.expect(TokType.Indent, None)
        while not self.check(TokType.Dedent, None) and self.current().ttype != TokType.Eof:
            self.skip_newlines()
            if self.check(TokType.Dedent, None) or self.current().ttype == TokType.Eof:
                break
            body.append(self.parse_statement())
            self.skip_newlines()
        if self.check(TokType.Dedent, None):
            self.advance()
        return body

    # -- Expressions -------------------------------------------------------

    def parse_expression(self) -> Expr:
        return self.parse_ternary()

    def parse_ternary(self) -> Expr:
        expr = self.parse_or()

        if self.check(TokType.Keyword, "if"):
            loc = self.current().loc
            self.advance()
            condition = self.parse_or()
            self.expect(TokType.Keyword, "else")
            if_false = self.parse_ternary()
            return Ternary(condition=condition, if_true=expr, if_false=if_false, loc=loc)

        if self.check(TokType.Operator, "?"):
            loc = self.current().loc
            self.advance()
            if_true = self.parse_or()
            self.expect(TokType.Colon, None)
            if_false = self.parse_ternary()
            return Ternary(condition=expr, if_true=if_true, if_false=if_false, loc=loc)

        return expr

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.check(TokType.Logical, "or"):
            loc = self.current().loc
            self.advance()
            right = self.parse_and()
            left = Binary(op="or", left=left, right=right, loc=loc)
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.check(TokType.Logical, "and"):
            loc = self.current().loc
            self.advance()
            right = self.parse_not()
            left = Binary(op="and", left=left, right=right, loc=loc)
        return left

    def parse_not(self) -> Expr:
        if self.check(TokType.Logical, "not"):
            loc = self.current().loc
            self.advance()
            operand = self.parse_not()
            return Unary(op="not", operand=operand, loc=loc)
        return self.parse_comparison()

    def parse_comparison(self) -> Expr:
        first = self.parse_addition()
        if self.current().ttype != TokType.Comparison:
            return first
        # Python-style chained comparisons desugar to and-chains
        prev_operand = first
        combined: Optional[Expr] = None
        while self.current().ttype == TokType.Comparison:
            loc = self.current().loc
            op = self.advance().value
            right = self.parse_addition()
            cmp = Binary(op=op, left=prev_operand, right=right, loc=loc)
            if combined is None:
                combined = cmp
            else:
                combined = Binary(op="and", left=combined, right=cmp, loc=loc)
            prev_operand = right
        return combined

    def parse_addition(self) -> Expr:
        left = self.parse_multiply()
        while self.check(TokType.Operator, "+") or self.check(TokType.Operator, "-"):
            loc = self.current().loc
            op = self.advance().value
            right = self.parse_multiply()
            left = Binary(op=op, left=left, right=right, loc=loc)
        return left

    def parse_multiply(self) -> Expr:
        left = self.parse_power()
        while (
            self.check(TokType.Operator, "*")
            or self.check(TokType.Operator, "/")
            or self.check(TokType.Operator, "%")
        ):
            loc = self.current().loc
            op = self.advance().value
            right = self.parse_power()
            left = Binary(op=op, left=left, right=right, loc=loc)
        return left

    def parse_power(self) -> Expr:
        left = self.parse_unary()
        if self.check(TokType.Operator, "**"):
            loc = self.current().loc
            self.advance()
            right = self.parse_power()  # right-associative
            return Binary(op="**", left=left, right=right, loc=loc)
        return left

    def parse_unary(self) -> Expr:
        if self.check(TokType.Operator, "-"):
            loc = self.current().loc
            self.advance()
            operand = self.parse_unary()
            if isinstance(operand, NumberLiteral):
                return NumberLiteral(value=-operand.value, loc=loc)
            return Unary(op="-", operand=operand, loc=loc)
        return self.parse_postfix()

    def parse_postfix(self) -> Expr:
        expr = self.parse_primary()

        while True:
            if self.check(TokType.LBracket, None):
                loc = self.current().loc
                self.advance()
                offset = self.parse_expression()
                self.expect(TokType.RBracket, None)
                expr = History(source=expr, offset=offset, loc=loc)
                continue

            if self.check(TokType.Dot, None):
                loc = self.current().loc
                self.advance()
                member = self.expect(TokType.Identifier, None).value

                if self.check(TokType.LParen, None):
                    self.advance()
                    args: List[Expr] = []
                    while not self.check(TokType.RParen, None) and self.current().ttype != TokType.Eof:
                        args.append(self.parse_expression())
                        if not self.check(TokType.RParen, None):
                            self.match_tok(TokType.Comma, None)
                    self.expect(TokType.RParen, None)
                    expr = MethodCall(object=expr, method=member, args=args, loc=loc)
                else:
                    expr = Member(object=expr, member=member, loc=loc)
                continue

            break

        return expr

    def parse_primary(self) -> Expr:
        t = self.current()

        if t.ttype == TokType.Number:
            self.advance()
            # Number tokens are plain decimal strings; float() parses them exactly.
            try:
                value = float(t.value)
            except ValueError:
                value = float("nan")
            return NumberLiteral(value=value, loc=t.loc)

        if t.ttype == TokType.String:
            self.advance()
            if t.value.startswith("#"):
                rgba = parse_hex_color(t.value)
                if rgba is not None:
                    r, g, b, a = rgba
                    return ColorLiteral(r=r, g=g, b=b, a=a, loc=t.loc)
            return StringLiteral(value=t.value, loc=t.loc)

        if t.ttype == TokType.FStringStart:
            self.advance()  # consume f"
            if self.current().ttype == TokType.String:
                raw = self.advance().value
            else:
                raw = ""
            if self.check(TokType.FStringEnd, None):
                self.advance()

            if "{" not in raw:
                return StringLiteral(value=raw, loc=t.loc)

            return parse_format_string(raw, t.loc)

        if t.ttype == TokType.Keyword and t.value == "true":
            self.advance()
            return BoolLiteral(value=True, loc=t.loc)
        if t.ttype == TokType.Keyword and t.value == "false":
            self.advance()
            return BoolLiteral(value=False, loc=t.loc)
        if t.ttype == TokType.Keyword and t.value == "na":
            self.advance()
            return NaLiteral(loc=t.loc)

        if t.ttype == TokType.Identifier:
            name = self.advance().value
            if self.check(TokType.LParen, None):
                self.advance()
                args, kwargs = self.parse_arg_list()
                self.expect(TokType.RParen, None)
                return Call(callee=name, args=args, kwargs=kwargs, loc=t.loc)
            return Identifier(name=name, loc=t.loc)

        # Keywords usable as calls / namespace identifiers
        if t.ttype == TokType.Keyword and (t.value == "input" or t.value == "range" or t.value == "strategy"):
            name = self.advance().value
            if self.check(TokType.LParen, None):
                self.advance()
                args, kwargs = self.parse_arg_list()
                self.expect(TokType.RParen, None)
                return Call(callee=name, args=args, kwargs=kwargs, loc=t.loc)
            return Identifier(name=name, loc=t.loc)

        if t.ttype == TokType.LParen:
            self.advance()
            expr = self.parse_expression()
            self.expect(TokType.RParen, None)
            return expr

        if t.ttype == TokType.LBracket:
            self.advance()
            elements: List[Expr] = []
            while not self.check(TokType.RBracket, None) and self.current().ttype != TokType.Eof:
                elements.append(self.parse_expression())
                if not self.check(TokType.RBracket, None):
                    self.match_tok(TokType.Comma, None)
            self.expect(TokType.RBracket, None)
            return ArrayLiteral(elements=elements, loc=t.loc)

        if t.ttype == TokType.LBrace:
            self.advance()
            entries: List[Tuple[Expr, Expr]] = []
            while not self.check(TokType.RBrace, None) and self.current().ttype != TokType.Eof:
                self.skip_newlines()
                if self.check(TokType.RBrace, None):
                    break
                key = self.parse_expression()
                self.expect(TokType.Colon, None)
                value = self.parse_expression()
                entries.append((key, value))
                if not self.check(TokType.RBrace, None):
                    self.match_tok(TokType.Comma, None)
                self.skip_newlines()
            self.expect(TokType.RBrace, None)
            return MapLiteral(entries=entries, loc=t.loc)

        suggestion = get_suggestion_for_unexpected(t)
        raise BrueError.parse(
            "Unexpected {} '{}'.".format(t.ttype.ts_name(), t.value),
            t.loc,
            suggestion,
        )

    def parse_arg_list(self) -> Tuple[List[Expr], List[KeywordArg]]:
        args: List[Expr] = []
        kwargs: List[KeywordArg] = []

        while not self.check(TokType.RParen, None) and self.current().ttype != TokType.Eof:
            if (
                self.current().ttype == TokType.Identifier
                and self.peek(1).ttype == TokType.Assign
                and self.peek(1).value == "="
            ):
                name = self.advance().value
                self.advance()  # consume =
                value = self.parse_expression()
                kwargs.append(KeywordArg(name=name, value=value))
            else:
                args.append(self.parse_expression())
            if not self.check(TokType.RParen, None):
                self.match_tok(TokType.Comma, None)

        return args, kwargs


def parse_hex_color(hexs: str):
    """Parse a hex color literal (#RGB, #RRGGBB, #RRGGBBAA) into (r, g, b, a)."""
    h = hexs.replace("#", "")

    def hex_val(s: str):
        try:
            return float(int(s, 16))
        except ValueError:
            return None

    if len(h) == 3:
        vals = [hex_val(c + c) for c in h]
        if any(v is None for v in vals):
            return None
        return (vals[0], vals[1], vals[2], 1.0)
    if len(h) == 6:
        r = hex_val(h[0:2])
        g = hex_val(h[2:4])
        b = hex_val(h[4:6])
        if r is None or g is None or b is None:
            return None
        return (r, g, b, 1.0)
    if len(h) == 8:
        r = hex_val(h[0:2])
        g = hex_val(h[2:4])
        b = hex_val(h[4:6])
        a = hex_val(h[6:8])
        if r is None or g is None or b is None or a is None:
            return None
        return (r, g, b, a / 255.0)
    return None


def parse_format_string(raw: str, loc: Loc) -> Expr:
    """F-string body parsing: walk each {...} group (no nested braces) and
    mini-parse the embedded expression."""
    parts: List[object] = []
    specs: List[Optional[str]] = []
    last = 0  # start of pending literal text
    search = 0

    while search < len(raw):
        # find next '{'
        opn = raw.find("{", search)
        if opn == -1:
            break
        # regex [^{}]* then '}': scan forward
        j = opn + 1
        while j < len(raw) and raw[j] != "{" and raw[j] != "}":
            j += 1
        if j >= len(raw) or raw[j] != "}":
            # no match at this position; regex advances one char
            search = opn + 1
            continue
        # matched {inner}
        literal = raw[last:opn]
        parts.append(FmtPart.Text(literal))
        specs.append(None)
        inner = raw[opn + 1:j]

        # split trailing format spec ":.Nf"
        colon_idx = inner.rfind(":")
        expr_text = inner
        spec: Optional[str] = None
        if colon_idx > 0:
            tail = inner[colon_idx + 1:]
            is_spec = (
                tail.startswith(".")
                and tail.endswith("f")
                and len(tail) > 2
                and all(("0" <= c <= "9") for c in tail[1:len(tail) - 1])
            )
            if is_spec:
                expr_text = inner[:colon_idx]
                spec = tail
        if expr_text.strip() == "":
            raise BrueError.parse(
                "Empty expression inside f-string braces.",
                loc,
                'Put an expression between { and }, e.g. f"rsi {rsi_val:.1f}".',
            )

        # Recursive mini-parse (parse() is pure and re-entrant): the
        # embedded expression is parsed as a one-line headerless script.
        mini_src = '__fexpr = {}\n'.format(expr_text)
        embedded: Optional[Expr] = None
        try:
            toks = tokenize(mini_src)
            prog = parse(toks)
            for s in prog.body:
                if isinstance(s, Assign) and s.target == "__fexpr":
                    embedded = s.value
                    break
        except BrueError:
            embedded = None

        # Tokenize/parse failures inside the f-string surface with the
        # nested error's first line; a missing assignment means "not a
        # single expression".
        if embedded is None:
            # Re-run to capture the underlying message for the suggestion
            nested_msg: Optional[str] = None
            try:
                toks = tokenize(mini_src)
                try:
                    parse(toks)
                except BrueError as e:
                    nested_msg = e.message
            except BrueError as e:
                nested_msg = e.message
            if nested_msg is not None:
                first_line = nested_msg.split("\n")[0]
                raise BrueError.parse(
                    "Invalid expression inside f-string: {{{}}}".format(inner),
                    loc,
                    "Fix the embedded expression ({}).".format(first_line),
                )
            raise BrueError.parse(
                "Invalid expression inside f-string: {{{}}}".format(inner),
                loc,
                "The braces must contain a single expression.",
            )

        parts.append(FmtPart.Expr(embedded))
        specs.append(spec)
        last = j + 1
        search = last

    tail = raw[last:]
    parts.append(FmtPart.Text(tail))
    specs.append(None)
    return FormatString(parts=parts, specs=specs, loc=loc)


def get_suggestion_for_expected(ttype: TokType, _value: Optional[str], got: Token) -> Optional[str]:
    if ttype == TokType.RParen and got.ttype == TokType.Eof:
        return "You have an unclosed parenthesis '('. Add a closing ')'."
    if ttype == TokType.Colon and got.ttype == TokType.Newline:
        return "Add a ':' at the end of the line (e.g., 'if condition:')."
    if ttype == TokType.Indent and got.ttype != TokType.Indent:
        return "The block body must be indented (use 4 spaces)."
    return None


def get_suggestion_for_unexpected(t: Token) -> Optional[str]:
    if t.ttype == TokType.Keyword and t.value == "else":
        return "'else' must follow an 'if' or 'elif' block at the same indentation level."
    if t.ttype == TokType.Keyword and t.value == "elif":
        return "'elif' must follow an 'if' block at the same indentation level."
    if t.ttype == TokType.Dedent:
        return "Unexpected end of indented block. Check your indentation."
    if t.ttype == TokType.Identifier:
        # Lazy import: only this suggestion path needs the builtin
        # registry, so the parser stays importable without it. Candidate
        # order is the registry order (find_closest_match keeps the FIRST
        # candidate at the minimum distance).
        from .builtins import all_builtin_names
        candidates = list(all_builtin_names())
        candidates.extend(["close", "open", "high", "low", "volume", "bar_index", "time"])
        m = find_closest_match(t.value, candidates)
        if m is not None:
            return "Did you mean '{}'?".format(m)
    return None


# -- Execution-mode invariant ---------------------------------------------


def validate_execution_mode(program: Program):
    # There is no backtest-vs-live split in the script: buy()/sell()/close()
    # are the one order vocabulary and are valid everywhere; the runner
    # (run_script or the live driver) decides what they do. Nothing to
    # validate; kept as a no-op so the call site and embedder imports stay
    # stable.
    return


def find_live_order_call(stmts: List[Stmt]) -> Optional[Tuple[str, Loc]]:
    for s in stmts:
        hit = find_live_order_in_stmt(s)
        if hit is not None:
            return hit
    return None


def find_live_order_in_stmt(s: Stmt) -> Optional[Tuple[str, Loc]]:
    if isinstance(s, Assign):
        return find_live_order_in_expr(s.value)
    if isinstance(s, Persist):
        return find_live_order_in_expr(s.value)
    if isinstance(s, Const):
        return find_live_order_in_expr(s.value)
    if isinstance(s, MultiAssign):
        return find_live_order_in_expr(s.value)
    if isinstance(s, ExprStmt):
        return find_live_order_in_expr(s.expr)
    if isinstance(s, Return):
        if s.value is not None:
            return find_live_order_in_expr(s.value)
        return None
    if isinstance(s, If):
        hit = find_live_order_in_expr(s.condition)
        if hit is not None:
            return hit
        hit = find_live_order_call(s.body)
        if hit is not None:
            return hit
        for c, b in s.elifs:
            hit = find_live_order_in_expr(c)
            if hit is not None:
                return hit
            hit = find_live_order_call(b)
            if hit is not None:
                return hit
        return find_live_order_call(s.else_body)
    if isinstance(s, For):
        hit = find_live_order_in_expr(s.iterable)
        if hit is not None:
            return hit
        return find_live_order_call(s.body)
    if isinstance(s, While):
        hit = find_live_order_in_expr(s.condition)
        if hit is not None:
            return hit
        return find_live_order_call(s.body)
    if isinstance(s, FunctionDef):
        return find_live_order_call(s.body)
    if isinstance(s, MemberAssign):
        hit = find_live_order_in_expr(s.target)
        if hit is not None:
            return hit
        return find_live_order_in_expr(s.value)
    return None


def find_live_order_in_expr(e: Expr) -> Optional[Tuple[str, Loc]]:
    if isinstance(e, Call):
        if e.callee in LIVE_ORDER_VERBS:
            return (e.callee, e.loc)
        for a in e.args:
            hit = find_live_order_in_expr(a)
            if hit is not None:
                return hit
        for kw in e.kwargs:
            hit = find_live_order_in_expr(kw.value)
            if hit is not None:
                return hit
        return None
    if isinstance(e, Binary):
        hit = find_live_order_in_expr(e.left)
        if hit is not None:
            return hit
        return find_live_order_in_expr(e.right)
    if isinstance(e, Unary):
        return find_live_order_in_expr(e.operand)
    if isinstance(e, History):
        hit = find_live_order_in_expr(e.source)
        if hit is not None:
            return hit
        return find_live_order_in_expr(e.offset)
    if isinstance(e, Ternary):
        hit = find_live_order_in_expr(e.condition)
        if hit is not None:
            return hit
        hit = find_live_order_in_expr(e.if_true)
        if hit is not None:
            return hit
        return find_live_order_in_expr(e.if_false)
    if isinstance(e, ArrayLiteral):
        for el in e.elements:
            hit = find_live_order_in_expr(el)
            if hit is not None:
                return hit
        return None
    if isinstance(e, MapLiteral):
        for k, v in e.entries:
            hit = find_live_order_in_expr(k)
            if hit is not None:
                return hit
            hit = find_live_order_in_expr(v)
            if hit is not None:
                return hit
        return None
    if isinstance(e, Member):
        return find_live_order_in_expr(e.object)
    if isinstance(e, MethodCall):
        hit = find_live_order_in_expr(e.object)
        if hit is not None:
            return hit
        for a in e.args:
            hit = find_live_order_in_expr(a)
            if hit is not None:
                return hit
        return None
    if isinstance(e, FormatString):
        for p in e.parts:
            if isinstance(p, FmtPart.Expr):
                hit = find_live_order_in_expr(p.expr)
                if hit is not None:
                    return hit
        return None
    return None
