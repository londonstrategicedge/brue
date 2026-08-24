# Result structures + JSON serialization. The JSON is the canonical
# execution output the conformance goldens lock: numbers via JS ToString,
# NaN/Infinity as marker strings ("__NaN__", "__+Inf__", "__-Inf__"),
# undefined optional fields omitted, timing omitted entirely. Key order is
# NOT significant to the comparator (it deep-compares parsed JSON), but the
# byte sequence is kept stable anyway.
#
# Serializer convention: `out` is a list of str fragments, joined once at
# the end (Python strings are immutable, and repeated += would be quadratic).
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from . import jsnum
from .value import Value

INF = float("inf")


@dataclass
class ErrorInfo:
    message: str
    line: int
    col: int
    phase: str
    suggestion: Optional[str]


@dataclass
class InputInfo:
    id: str
    itype: str
    title: Value
    default_value: Value
    # min/max/step/options in kwarg-encounter order
    extras: List[Tuple[str, Value]]


@dataclass
class LiveOrder:
    side: str
    order_type: str
    symbol: str
    quantity: float
    price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]


class LiveCloseTarget:
    """Target of a live close command; variants are the nested classes."""

    @dataclass
    class PositionId:
        id: str

    @dataclass
    class Symbol:
        symbol: str

    @dataclass
    class All:
        pass


class Command:
    """Output commands; variants are the nested dataclasses below.
    Constructed by the runtime/strategy layers, serialized by write_command."""

    @dataclass
    class Shape:
        bar_index: int
        shape_type: str
        location: str
        color: str
        size: str
        text: str
        text_color: str

    @dataclass
    class Label:
        bar_index: int
        text: str
        x: Value
        y: Value
        location: str
        color: str
        bg_color: str
        size: str
        tooltip: str
        align: str

    @dataclass
    class BgColor:
        bar_index: int
        color: str
        panel: str

    @dataclass
    class HLine:
        price: Value
        color: str
        style: str
        width: Value
        title: str
        panel: str

    @dataclass
    class Fill:
        plot_id1: Value
        plot_id2: Value
        colors_len: int
        color: str
        title: str

    @dataclass
    class Table:
        position: str
        bg_color: str
        cells: List["TableCell"]

    @dataclass
    class TradeEntry:
        bar_index: int
        direction: str
        price: float
        qty: float
        id: str

    @dataclass
    class TradeExit:
        bar_index: int
        price: float
        qty: float
        pnl: float
        id: str
        from_entry: str

    @dataclass
    class LiveOrderCmd:
        # index into live_orders; serialized as null
        value: int


@dataclass
class TableCell:
    row: float
    col: float
    text: str
    color: str
    bg_color: str


# ---- JSON writer --------------------------------------------------------


def json_escape(s: str, out: List[str]) -> None:
    out.append('"')
    for c in s:
        if c == '"':
            out.append('\\"')
        elif c == "\\":
            out.append("\\\\")
        elif c == "\n":
            out.append("\\n")
        elif c == "\r":
            out.append("\\r")
        elif c == "\t":
            out.append("\\t")
        elif c == "\u0008":
            out.append("\\b")
        elif c == "\u000c":
            out.append("\\f")
        elif ord(c) < 0x20:
            out.append("\\u{:04x}".format(ord(c)))
        else:
            out.append(c)
    out.append('"')


def json_num(x: float, out: List[str]) -> None:
    if x != x:  # NaN
        out.append('"__NaN__"')
    elif x == INF:
        out.append('"__+Inf__"')
    elif x == -INF:
        out.append('"__-Inf__"')
    else:
        out.append(jsnum.js_number_to_string(x))


def json_value(v: Value, out: List[str]) -> None:
    """JSON.stringify semantics for a Brue value (with non-finite markers).
    Map serializes as {} (JSON.stringify(Map) does), colors as their object."""
    k = v.kind
    if k == "undefined" or k == "null":
        out.append("null")
    elif k == "num":
        json_num(v.v, out)
    elif k == "bool":
        out.append("true" if v.v else "false")
    elif k == "str":
        json_escape(v.v, out)
    elif k == "color":
        c = v.v
        out.append('{"__type":"color","r":')
        json_num(c.r, out)
        out.append(',"g":')
        json_num(c.g, out)
        out.append(',"b":')
        json_num(c.b, out)
        out.append(',"a":')
        json_num(c.a, out)
        out.append("}")
    elif k == "array":
        out.append("[")
        for i, el in enumerate(v.v):
            if i > 0:
                out.append(",")
            json_value(el, out)
        out.append("]")
    elif k == "map":
        out.append("{}")
    else:  # udt
        u = v.v
        out.append('{"__udt":')
        json_escape(u.type_name, out)
        for key, val in u.fields.entries:
            out.append(",")
            json_escape(key, out)
            out.append(":")
            json_value(val, out)
        out.append("}")


def json_opt_num_field(name: str, v: Optional[float], first: List[bool], out: List[str]) -> None:
    # `first` is a 1-element list so the comma state can be cleared in place.
    if v is not None:
        if not first[0]:
            out.append(",")
        first[0] = False
        json_escape(name, out)
        out.append(":")
        json_num(v, out)


def write_command(c, out: List[str]) -> None:
    if isinstance(c, Command.Shape):
        out.append('{"type":"shape","barIndex":')
        json_num(float(c.bar_index), out)
        out.append(',"shapeType":')
        json_escape(c.shape_type, out)
        out.append(',"location":')
        json_escape(c.location, out)
        out.append(',"color":')
        json_escape(c.color, out)
        out.append(',"size":')
        json_escape(c.size, out)
        out.append(',"text":')
        json_escape(c.text, out)
        out.append(',"textColor":')
        json_escape(c.text_color, out)
        out.append("}")
    elif isinstance(c, Command.Label):
        out.append('{"type":"label","barIndex":')
        json_num(float(c.bar_index), out)
        out.append(',"text":')
        json_escape(c.text, out)
        out.append(',"x":')
        json_value(c.x, out)
        out.append(',"y":')
        json_value(c.y, out)
        out.append(',"location":')
        json_escape(c.location, out)
        out.append(',"color":')
        json_escape(c.color, out)
        out.append(',"bgColor":')
        json_escape(c.bg_color, out)
        out.append(',"size":')
        json_escape(c.size, out)
        out.append(',"tooltip":')
        json_escape(c.tooltip, out)
        out.append(',"align":')
        json_escape(c.align, out)
        out.append("}")
    elif isinstance(c, Command.BgColor):
        out.append('{"type":"bgcolor","barIndex":')
        json_num(float(c.bar_index), out)
        out.append(',"color":')
        json_escape(c.color, out)
        out.append(',"panel":')
        json_escape(c.panel, out)
        out.append("}")
    elif isinstance(c, Command.HLine):
        out.append('{"type":"hline","price":')
        json_value(c.price, out)
        out.append(',"color":')
        json_escape(c.color, out)
        out.append(',"style":')
        json_escape(c.style, out)
        out.append(',"width":')
        json_value(c.width, out)
        out.append(',"title":')
        json_escape(c.title, out)
        out.append(',"panel":')
        json_escape(c.panel, out)
        out.append("}")
    elif isinstance(c, Command.Fill):
        out.append('{"type":"fill","plotId1":')
        json_value(c.plot_id1, out)
        out.append(',"plotId2":')
        json_value(c.plot_id2, out)
        out.append(',"colors":[')
        for i in range(c.colors_len):
            if i > 0:
                out.append(",")
            json_escape(c.color, out)
        out.append('],"title":')
        json_escape(c.title, out)
        out.append("}")
    elif isinstance(c, Command.Table):
        out.append('{"type":"table","position":')
        json_escape(c.position, out)
        out.append(',"bgColor":')
        json_escape(c.bg_color, out)
        out.append(',"cells":[')
        for i, cell in enumerate(c.cells):
            if i > 0:
                out.append(",")
            out.append('{"row":')
            json_num(cell.row, out)
            out.append(',"col":')
            json_num(cell.col, out)
            out.append(',"text":')
            json_escape(cell.text, out)
            out.append(',"color":')
            json_escape(cell.color, out)
            out.append(',"bgColor":')
            json_escape(cell.bg_color, out)
            out.append(',"align":"left","size":"normal"}')
        out.append("]}")
    elif isinstance(c, Command.TradeEntry):
        out.append('{"type":"trade_entry","barIndex":')
        json_num(float(c.bar_index), out)
        out.append(',"direction":')
        json_escape(c.direction, out)
        out.append(',"price":')
        json_num(c.price, out)
        out.append(',"qty":')
        json_num(c.qty, out)
        out.append(',"id":')
        json_escape(c.id, out)
        out.append("}")
    elif isinstance(c, Command.TradeExit):
        out.append('{"type":"trade_exit","barIndex":')
        json_num(float(c.bar_index), out)
        out.append(',"price":')
        json_num(c.price, out)
        out.append(',"qty":')
        json_num(c.qty, out)
        out.append(',"pnl":')
        json_num(c.pnl, out)
        out.append(',"id":')
        json_escape(c.id, out)
        out.append(',"fromEntry":')
        json_escape(c.from_entry, out)
        out.append("}")
    else:  # Command.LiveOrderCmd
        out.append("null")


@dataclass
class ExecOutput:
    success: bool
    commands: List[object]
    errors: List[ErrorInfo]
    inputs: List[InputInfo]
    live_orders: List[LiveOrder]
    live_closes: List[object]
    decl_title: Value
    decl_overlay: Value
    decl_symbol: Optional[str]
    decl_timeframe: Optional[str]
    # log() lines as (time_ms, message). Serialized only when non-empty so
    # outputs stay byte-identical to the conformance goldens.
    logs: List[object] = field(default_factory=list)

    def to_json(self) -> str:
        out: List[str] = []
        out.append('{"success":')
        out.append("true" if self.success else "false")

        out.append(',"commands":[')
        for i, c in enumerate(self.commands):
            if i > 0:
                out.append(",")
            write_command(c, out)
        out.append("]")

        out.append(',"errors":[')
        for i, e in enumerate(self.errors):
            if i > 0:
                out.append(",")
            out.append('{"message":')
            json_escape(e.message, out)
            out.append(',"line":')
            json_num(float(e.line), out)
            out.append(',"col":')
            json_num(float(e.col), out)
            out.append(',"phase":')
            json_escape(e.phase, out)
            out.append(',"suggestion":')
            if e.suggestion is not None:
                json_escape(e.suggestion, out)
            else:
                out.append("null")
            out.append("}")
        out.append("]")

        out.append(',"inputs":[')
        for i, inp in enumerate(self.inputs):
            if i > 0:
                out.append(",")
            out.append('{"id":')
            json_escape(inp.id, out)
            out.append(',"type":')
            json_escape(inp.itype, out)
            out.append(',"title":')
            json_value(inp.title, out)
            out.append(',"defaultValue":')
            json_value(inp.default_value, out)
            for k, v in inp.extras:
                out.append(",")
                json_escape(k, out)
                out.append(":")
                json_value(v, out)
            out.append("}")
        out.append("]")

        out.append(',"liveOrders":[')
        for i, o in enumerate(self.live_orders):
            if i > 0:
                out.append(",")
            out.append('{"type":"live_order","side":')
            json_escape(o.side, out)
            out.append(',"orderType":')
            json_escape(o.order_type, out)
            out.append(',"symbol":')
            json_escape(o.symbol, out)
            out.append(',"quantity":')
            json_num(o.quantity, out)
            first = [False]
            json_opt_num_field("price", o.price, first, out)
            json_opt_num_field("stopLoss", o.stop_loss, first, out)
            json_opt_num_field("takeProfit", o.take_profit, first, out)
            out.append("}")
        out.append("]")

        out.append(',"liveCloses":[')
        for i, c in enumerate(self.live_closes):
            if i > 0:
                out.append(",")
            if isinstance(c, LiveCloseTarget.PositionId):
                out.append('{"type":"live_close","target":{"kind":"position_id","id":')
                json_escape(c.id, out)
                out.append("}}")
            elif isinstance(c, LiveCloseTarget.Symbol):
                out.append('{"type":"live_close","target":{"kind":"symbol","symbol":')
                json_escape(c.symbol, out)
                out.append("}}")
            else:  # LiveCloseTarget.All
                out.append('{"type":"live_close","target":{"kind":"all"}}')
        out.append("]")

        out.append(',"declaration":{"mode":"strategy","title":')
        json_value(self.decl_title, out)
        out.append(',"overlay":')
        json_value(self.decl_overlay, out)
        if self.decl_symbol is not None:
            out.append(',"symbol":')
            json_escape(self.decl_symbol, out)
        if self.decl_timeframe is not None:
            out.append(',"timeframe":')
            json_escape(self.decl_timeframe, out)
        out.append("}")

        if self.logs:
            out.append(',"logs":[')
            for i, lg in enumerate(self.logs):
                if i > 0:
                    out.append(",")
                out.append('{"time":')
                json_num(float(lg[0]), out)
                out.append(',"message":')
                json_escape(lg[1], out)
                out.append("}")
            out.append("]")

        out.append("}")
        return "".join(out)


def trade_command_to_output(tc):
    """Convert engine trade commands to output commands. The exit command is
    the one that carries from_entry; dispatching on that field keeps this
    independent of how strategy.py names its classes."""
    if hasattr(tc, "from_entry"):
        return Command.TradeExit(
            bar_index=tc.bar_index,
            price=tc.price,
            qty=tc.qty,
            pnl=tc.pnl,
            id=tc.id,
            from_entry=tc.from_entry,
        )
    return Command.TradeEntry(
        bar_index=tc.bar_index,
        direction=tc.direction,
        price=tc.price,
        qty=tc.qty,
        id=tc.id,
    )
