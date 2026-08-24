# Error type + fuzzy matching. The formatted message string is part of the
# observable output (compared byte-for-byte in conformance), so the
# "Line X, col Y: ..." and "\n  Suggestion: ..." layout is fixed.
#
# `loc` is any object with .line and .col (normally an ast.Loc). BrueError
# is an Exception subclass so the runtime can raise it; its fields are
# message, loc, phase and suggestion.


class BrueError(Exception):
    def __init__(self, message, loc, phase, suggestion=None):
        # The fully formatted message (prefix + text + optional suggestion).
        prefix = "Line {}, col {}".format(loc.line, loc.col)
        if suggestion is not None:
            full = "{}: {}\n  Suggestion: {}".format(prefix, message, suggestion)
        else:
            full = "{}: {}".format(prefix, message)
        super().__init__(full)
        self.message = full
        self.loc = loc
        self.phase = phase  # "tokenize" | "parse" | "runtime"
        self.suggestion = suggestion

    @classmethod
    def new(cls, message, loc, phase, suggestion=None):
        return cls(message, loc, phase, suggestion)

    @classmethod
    def tokenize(cls, message, loc, suggestion=None):
        return cls(message, loc, "tokenize", suggestion)

    @classmethod
    def parse(cls, message, loc, suggestion=None):
        return cls(message, loc, "parse", suggestion)

    @classmethod
    def runtime(cls, message, loc, suggestion=None):
        return cls(message, loc, "runtime", suggestion)


def levenshtein(a, b):
    """Levenshtein distance (single-row DP)."""
    a = list(a)
    b = list(b)
    n = len(b)
    row = list(range(n + 1))
    for i in range(1, len(a) + 1):
        prev = i - 1
        row[0] = i
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            val = min(row[j] + 1, row[j - 1] + 1, prev + cost)
            prev = row[j]
            row[j] = val
    return row[n]


def find_closest_match(input, candidates):
    """Best candidate within min(3, floor(len/2)+1) edits. Strict `<`
    comparison means the FIRST candidate at the minimum distance wins, so
    candidate order matters: callers pass the registry order."""
    lower = input.lower()
    max_distance = min(3, len(input) // 2 + 1)
    best = None
    best_dist = None
    for c in candidates:
        d = levenshtein(lower, c.lower())
        if (best_dist is None or d < best_dist) and d <= max_distance:
            best_dist = d
            best = c
    return best
