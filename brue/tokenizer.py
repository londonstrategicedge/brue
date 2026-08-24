# Tokenizer: indentation blocks, hex-color-vs-comment disambiguation,
# f-string splitting, backslash continuation. Error strings are part of the
# observable result and are locked by the conformance goldens.
from dataclasses import dataclass
from enum import Enum
from typing import List

from .ast import Loc
from .errors import BrueError


class TokType(Enum):
    # The enum value is the token type name that appears inside parser
    # error messages (see ts_name()).
    Number = "NUMBER"
    String = "STRING"
    FStringStart = "FSTRING_START"
    FStringEnd = "FSTRING_END"
    Identifier = "IDENTIFIER"
    Keyword = "KEYWORD"
    Operator = "OPERATOR"
    Comparison = "COMPARISON"
    Assign = "ASSIGN"
    Logical = "LOGICAL"
    LParen = "LPAREN"
    RParen = "RPAREN"
    LBracket = "LBRACKET"
    RBracket = "RBRACKET"
    LBrace = "LBRACE"
    RBrace = "RBRACE"
    Comma = "COMMA"
    Colon = "COLON"
    Dot = "DOT"
    Newline = "NEWLINE"
    Indent = "INDENT"
    Dedent = "DEDENT"
    Eof = "EOF"

    def ts_name(self) -> str:
        return self.value


@dataclass
class Token:
    ttype: TokType
    value: str
    loc: Loc


KEYWORDS = [
    "strategy", "input", "persist", "const", "if", "elif", "else", "for", "in", "while", "break",
    "continue", "def", "return", "true", "false", "na", "range", "export", "type", "enum", "use",
    "at", "as",
]
LOGICAL_KEYWORDS = ["and", "or", "not"]


def is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def is_id_start(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z") or c == "_"


def is_id_part(c: str) -> bool:
    return is_id_start(c) or is_digit(c)


def is_hex_digit(c: str) -> bool:
    return is_digit(c) or ("a" <= c <= "f") or ("A" <= c <= "F")


def get_indent_level(line: str) -> int:
    """Leading indentation, tabs = 4 spaces."""
    level = 0
    for c in line:
        if c == " ":
            level += 1
        elif c == "\t":
            level += 4
        else:
            break
    return level


def read_string(line: str, start: int, line_num: int):
    quote = line[start]
    value = []
    col = start + 1
    while col < len(line):
        ch = line[col]
        if ch == "\\" and col + 1 < len(line):
            nxt = line[col + 1]
            if nxt == "n":
                value.append("\n")
            elif nxt == "t":
                value.append("\t")
            elif nxt == "\\":
                value.append("\\")
            elif nxt == quote:
                value.append(quote)
            else:
                value.append("\\")
                value.append(nxt)
            col += 2
            continue
        if ch == quote:
            return "".join(value), col + 1
        value.append(ch)
        col += 1
    raise BrueError.tokenize(
        "Unterminated string.",
        Loc(line=line_num, col=start + 1),
        "Add a closing {} to end the string.".format(quote),
    )


def tokenize(source: str) -> List[Token]:
    tokens: List[Token] = []
    lines = source.split("\n")

    indent_stack = [0]
    bracket_depth = 0
    continued_from_prev = False

    line_idx = 0
    while line_idx < len(lines):
        line_num = line_idx + 1
        raw_line = lines[line_idx]
        trimmed = raw_line.strip()

        # Skip empty and comment-only lines
        if trimmed == "" or trimmed.startswith("#"):
            line_idx += 1
            continue

        # Multi-line comments (""")
        if trimmed.startswith('"""'):
            after_open = trimmed[3:]
            if '"""' in after_open:
                line_idx += 1
                continue
            end_idx = line_idx + 1
            while end_idx < len(lines) and '"""' not in lines[end_idx]:
                end_idx += 1
            line_idx = end_idx + 1
            continue

        # Indentation handling
        if bracket_depth == 0 and not continued_from_prev:
            indent = get_indent_level(raw_line)
            current_indent = indent_stack[-1]
            if indent > current_indent:
                indent_stack.append(indent)
                tokens.append(Token(ttype=TokType.Indent, value="", loc=Loc(line=line_num, col=1)))
            elif indent < current_indent:
                while len(indent_stack) > 1 and indent_stack[-1] > indent:
                    indent_stack.pop()
                    tokens.append(Token(ttype=TokType.Dedent, value="", loc=Loc(line=line_num, col=1)))
                if indent_stack[-1] != indent:
                    raise BrueError.tokenize(
                        "Indentation does not match any outer block.",
                        Loc(line=line_num, col=1),
                        "Check that your indentation uses consistent spaces (not mixed tabs/spaces).",
                    )

        # Tokenize line content
        col = get_indent_level(raw_line)
        line = raw_line
        line_continuation = False

        while col < len(line):
            ch = line[col]
            loc = Loc(line=line_num, col=col + 1)

            if ch == " " or ch == "\t":
                col += 1
                continue

            # Hex color literal check BEFORE comment
            if ch == "#" and col + 1 < len(line) and is_hex_digit(line[col + 1]):
                hexs = "#"
                hex_start = col
                scan = col + 1
                while scan < len(line) and is_hex_digit(line[scan]):
                    hexs += line[scan]
                    scan += 1
                valid_len = len(hexs) == 4 or len(hexs) == 7 or len(hexs) == 9
                clean_boundary = scan >= len(line) or not is_id_part(line[scan])
                if valid_len and clean_boundary:
                    tokens.append(Token(
                        ttype=TokType.String,
                        value=hexs,
                        loc=Loc(line=line_num, col=hex_start + 1),
                    ))
                    col = scan
                    continue
                break  # not a color: comment to end of line

            if ch == "#":
                break  # comment

            # Numbers
            if is_digit(ch) or (ch == "." and col + 1 < len(line) and is_digit(line[col + 1])):
                num = ""
                start = col
                has_dot = False
                while col < len(line) and (is_digit(line[col]) or (line[col] == "." and not has_dot)):
                    if line[col] == ".":
                        has_dot = True
                    num += line[col]
                    col += 1
                if num.endswith("."):
                    raise BrueError.tokenize(
                        "Invalid number '{}'.".format(num),
                        Loc(line=line_num, col=start + 1),
                        "Write '{}0' for a decimal number, or '{}' for an integer.".format(
                            num, num[: len(num) - 1]
                        ),
                    )
                tokens.append(Token(ttype=TokType.Number, value=num, loc=loc))
                continue

            # Strings
            if ch == '"' or ch == "'":
                value, end_col = read_string(line, col, line_num)
                tokens.append(Token(ttype=TokType.String, value=value, loc=loc))
                col = end_col
                continue

            # Identifiers and keywords
            if is_id_start(ch):
                ident = ""
                start = col
                while col < len(line) and is_id_part(line[col]):
                    ident += line[col]
                    col += 1

                # f-string: f"..." or f'...'
                if ident == "f" and col < len(line) and (line[col] == '"' or line[col] == "'"):
                    # An f-string tokenizes as FSTRING_START + STRING + FSTRING_END.
                    value, end_col = read_string(line, col, line_num)
                    tokens.append(Token(
                        ttype=TokType.FStringStart,
                        value='f"',
                        loc=Loc(line=line_num, col=col),
                    ))
                    tokens.append(Token(
                        ttype=TokType.String,
                        value=value,
                        loc=Loc(line=line_num, col=col + 1),
                    ))
                    tokens.append(Token(
                        ttype=TokType.FStringEnd,
                        value='"',
                        loc=Loc(line=line_num, col=end_col),
                    ))
                    # Resume scanning at the FSTRING_END column.
                    col = end_col
                    continue

                if ident in LOGICAL_KEYWORDS:
                    tokens.append(Token(
                        ttype=TokType.Logical,
                        value=ident,
                        loc=Loc(line=line_num, col=start + 1),
                    ))
                    continue
                if ident in KEYWORDS:
                    tokens.append(Token(
                        ttype=TokType.Keyword,
                        value=ident,
                        loc=Loc(line=line_num, col=start + 1),
                    ))
                    continue
                tokens.append(Token(
                    ttype=TokType.Identifier,
                    value=ident,
                    loc=Loc(line=line_num, col=start + 1),
                ))
                continue

            # Two-character operators
            if col + 1 < len(line):
                two = ch + line[col + 1]
                matched = {
                    "**": (TokType.Operator, "**"),
                    "==": (TokType.Comparison, "=="),
                    "!=": (TokType.Comparison, "!="),
                    ">=": (TokType.Comparison, ">="),
                    "<=": (TokType.Comparison, "<="),
                    "+=": (TokType.Assign, "+="),
                    "-=": (TokType.Assign, "-="),
                    "*=": (TokType.Assign, "*="),
                    "/=": (TokType.Assign, "/="),
                }.get(two)
                if matched is not None:
                    tt, v = matched
                    tokens.append(Token(ttype=tt, value=v, loc=loc))
                    col += 2
                    continue

            # Single-character tokens
            single = None
            if ch in "+-*/%":
                single = (TokType.Operator, ch)
            elif ch in "><":
                single = (TokType.Comparison, ch)
            elif ch == "=":
                single = (TokType.Assign, ch)
            elif ch == "(":
                bracket_depth += 1
                single = (TokType.LParen, ch)
            elif ch == ")":
                bracket_depth = max(bracket_depth - 1, 0)
                single = (TokType.RParen, ch)
            elif ch == "[":
                bracket_depth += 1
                single = (TokType.LBracket, ch)
            elif ch == "]":
                bracket_depth = max(bracket_depth - 1, 0)
                single = (TokType.RBracket, ch)
            elif ch == "{":
                bracket_depth += 1
                single = (TokType.LBrace, ch)
            elif ch == "}":
                bracket_depth = max(bracket_depth - 1, 0)
                single = (TokType.RBrace, ch)
            elif ch == ",":
                single = (TokType.Comma, ch)
            elif ch == ":":
                single = (TokType.Colon, ch)
            elif ch == ".":
                single = (TokType.Dot, ch)
            elif ch == "?":
                single = (TokType.Operator, ch)
            if single is not None:
                tt, v = single
                tokens.append(Token(ttype=tt, value=v, loc=loc))
                col += 1
                continue

            # Backslash line continuation
            if ch == "\\":
                rest = line[col + 1:]
                idx = rest.find("#")
                tail = rest[:idx].strip() if idx != -1 else rest.strip()
                if tail == "":
                    line_continuation = True
                    break
                raise BrueError.tokenize(
                    "Unexpected character '\\'.",
                    loc,
                    "A backslash continues the line only when nothing follows it.",
                )

            raise BrueError.tokenize(
                "Unexpected character '{}'.".format(ch),
                loc,
                "If this is part of a string, make sure it's inside quotes.",
            )

        # NEWLINE at end of logical line
        if (
            bracket_depth == 0
            and not line_continuation
            and len(tokens) > 0
            and tokens[-1].ttype != TokType.Newline
        ):
            tokens.append(Token(
                ttype=TokType.Newline,
                value="\\n",
                loc=Loc(line=line_num, col=len(line) + 1),
            ))
        continued_from_prev = line_continuation
        line_idx += 1

    # Close remaining open indentation blocks
    while len(indent_stack) > 1:
        indent_stack.pop()
        tokens.append(Token(
            ttype=TokType.Dedent,
            value="",
            loc=Loc(line=len(lines), col=1),
        ))

    tokens.append(Token(
        ttype=TokType.Eof,
        value="",
        loc=Loc(line=len(lines), col=1),
    ))

    return tokens
