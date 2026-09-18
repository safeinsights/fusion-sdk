"""The fusion envelope and the typed-columnar fusion table (spec/envelope.schema.json, ADR 0001).

Cell rules are normative in spec/fixtures/README.md; every branch here has a fixture.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    import pandas as pd

JSON = Any

ENVELOPE_VERSION = 1
ENCODING_JSON = "json"
REMOTE_CODES = ("UNKNOWN_OPERATION", "BAD_PARAMS", "HANDLER_ERROR", "GUARD_REFUSED")
COLUMN_TYPES = ("string", "integer", "integer64", "number", "boolean", "date", "datetime")
ColumnType = Literal["string", "integer", "integer64", "number", "boolean", "date", "datetime"]

SAFE_INT = 2**53 - 1
_OP_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
_INT64_RE = re.compile(r"^-?[0-9]+$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$")


class EnvelopeError(ValueError):
    """The JSON is not a valid envelope or table. `code` is BAD_PARAMS-style reason text for logs."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def canonical_bytes(obj: JSON) -> int:
    """Byte count the tunnel meters: compact UTF-8 JSON (ask T4)."""
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# -- table ---------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    name: str
    type: ColumnType


@dataclass
class Table:
    """A typed-columnar table with native Python cells.

    Cell types per column: string→str, integer/integer64→int, number→float, boolean→bool,
    date→datetime.date, datetime→timezone-aware datetime (UTC). None is null in every column.
    """

    columns: list[Column] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)

    # -- construction --

    @classmethod
    def from_columns(cls, data: Mapping[str, Sequence[Any]], types: Mapping[str, ColumnType] | None = None) -> Table:
        """Build from {name: values}. Types are inferred unless given."""
        names = list(data)
        lengths = {len(v) for v in data.values()}
        if len(lengths) > 1:
            raise EnvelopeError("columns have different lengths")
        n = lengths.pop() if lengths else 0
        cols: list[Column] = []
        for name in names:
            t = (types or {}).get(name) or infer_type(data[name])
            if t not in COLUMN_TYPES:
                raise EnvelopeError(f"unknown column type {t!r}")
            cols.append(Column(name, t))
        rows = [[_coerce_native(data[c.name][i], c.type) for c in cols] for i in range(n)]
        return cls(cols, rows)

    @classmethod
    def from_records(cls, records: Sequence[Mapping[str, Any]], types: Mapping[str, ColumnType] | None = None) -> Table:
        names: list[str] = []
        for rec in records:
            for k in rec:
                if k not in names:
                    names.append(k)
        return cls.from_columns({k: [rec.get(k) for rec in records] for k in names}, types)

    @classmethod
    def from_pandas(cls, df: pd.DataFrame, types: Mapping[str, ColumnType] | None = None) -> Table:
        import pandas as pd

        data: dict[str, list[Any]] = {}
        inferred: dict[str, ColumnType] = {}
        for name in df.columns:
            series = df[name]
            key = str(name)
            dtype = str(series.dtype)
            values: list[Any]
            if dtype.startswith("datetime64"):
                inferred[key] = "datetime"
                values = [None if pd.isna(v) else v.to_pydatetime() for v in series]
            elif dtype in {"bool", "boolean"}:
                inferred[key] = "boolean"
                values = [None if pd.isna(v) else bool(v) for v in series]
            elif dtype.startswith(("int", "Int", "uint", "UInt")):
                values = [None if pd.isna(v) else int(v) for v in series]
                inferred[key] = "integer64" if any(v is not None and abs(v) > SAFE_INT for v in values) else "integer"
            elif dtype.startswith(("float", "Float")):
                inferred[key] = "number"
                values = [None if pd.isna(v) else float(v) for v in series]
            else:
                values = [
                    None if v is None or (not isinstance(v, (str, list, dict)) and pd.isna(v)) else v for v in series
                ]
                inferred[key] = infer_type(values)
            data[key] = values
        merged: dict[str, ColumnType] = {**inferred, **(types or {})}
        return cls.from_columns(data, merged)

    # -- conversion --

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def nrow(self) -> int:
        return len(self.rows)

    def column(self, name: str) -> list[Any]:
        idx = self.names.index(name)
        return [r[idx] for r in self.rows]

    def to_records(self) -> list[dict[str, Any]]:
        return [dict(zip(self.names, row, strict=True)) for row in self.rows]

    def to_pandas(self) -> pd.DataFrame:
        import pandas as pd

        df = pd.DataFrame({c.name: self.column(c.name) for c in self.columns})
        for c in self.columns:
            if c.type == "datetime" and len(df):
                df[c.name] = pd.to_datetime(df[c.name], utc=True)
            elif c.type in ("integer", "integer64") and len(df):
                df[c.name] = df[c.name].astype("Int64")
            elif c.type == "boolean" and len(df):
                df[c.name] = df[c.name].astype("boolean")
        return df

    def to_json(self) -> dict[str, Any]:
        return {
            "__table__": 1,
            "columns": [{"name": c.name, "type": c.type} for c in self.columns],
            "rows": [[_encode_cell(v, c.type) for v, c in zip(row, self.columns, strict=True)] for row in self.rows],
        }

    @classmethod
    def from_json(cls, obj: JSON) -> Table:
        if not isinstance(obj, dict) or obj.get("__table__") != 1 or isinstance(obj.get("__table__"), bool):
            raise EnvelopeError("not a fusion table")
        if set(obj) != {"__table__", "columns", "rows"}:
            raise EnvelopeError("table must have exactly __table__, columns, rows")
        cols_raw, rows_raw = obj["columns"], obj["rows"]
        if not isinstance(cols_raw, list) or not isinstance(rows_raw, list):
            raise EnvelopeError("columns and rows must be arrays")
        cols: list[Column] = []
        for c in cols_raw:
            if not isinstance(c, dict) or set(c) != {"name", "type"} or not isinstance(c["name"], str) or not c["name"]:
                raise EnvelopeError("column must be {name, type} with a non-empty name")
            if c["type"] not in COLUMN_TYPES:
                raise EnvelopeError(f"unknown column type {c['type']!r}")
            cols.append(Column(c["name"], c["type"]))
        names = [c.name for c in cols]
        if len(set(names)) != len(names):
            raise EnvelopeError("duplicate column names")
        rows: list[list[Any]] = []
        for i, r in enumerate(rows_raw):
            if not isinstance(r, list) or len(r) != len(cols):
                raise EnvelopeError(f"row {i} must be an array of {len(cols)} cells")
            rows.append([_decode_cell(v, c.type, c.name) for v, c in zip(r, cols, strict=True)])
        return cls(cols, rows)


def is_table(body: JSON) -> bool:
    return isinstance(body, dict) and "__table__" in body


def infer_type(values: Iterable[Any]) -> ColumnType:
    """Infer a column type from native values, ignoring None/NaN. Empty → string."""
    seen: set[str] = set()
    big = False
    for v in values:
        if v is None or (isinstance(v, float) and math.isnan(v)):
            continue
        if isinstance(v, bool):
            seen.add("boolean")
        elif isinstance(v, int):
            seen.add("integer")
            big = big or abs(v) > SAFE_INT
        elif isinstance(v, float):
            seen.add("number")
        elif isinstance(v, str):
            seen.add("string")
        elif isinstance(v, datetime):
            seen.add("datetime")
        elif isinstance(v, date):
            seen.add("date")
        else:
            raise EnvelopeError(f"cannot infer a column type for {type(v).__name__}")
    if not seen:
        return "string"
    if seen == {"integer"}:
        return "integer64" if big else "integer"
    if seen <= {"integer", "number"}:
        return "number"
    if len(seen) == 1:
        t = seen.pop()
        return t  # type: ignore[return-value]
    raise EnvelopeError(f"mixed value types in one column: {sorted(seen)}")


def _coerce_native(v: Any, t: ColumnType) -> Any:
    """Normalise a native value for a declared type (encode-side leniency: int→float for number, etc.)."""
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    if t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise EnvelopeError("number column needs numeric values")
        return float(v)
    if t in ("integer", "integer64"):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise EnvelopeError(f"{t} column needs integer values")
        if isinstance(v, float):
            if not v.is_integer():
                raise EnvelopeError(f"{t} column needs whole numbers")
            v = int(v)
        if t == "integer" and abs(v) > SAFE_INT:
            raise EnvelopeError("integer beyond 2^53-1: declare the column integer64")
        return v
    if t == "boolean":
        if not isinstance(v, bool):
            raise EnvelopeError("boolean column needs bool values")
        return v
    if t == "string":
        if not isinstance(v, str):
            raise EnvelopeError("string column needs str values")
        return v
    if t == "date":
        if isinstance(v, datetime) or not isinstance(v, date):
            raise EnvelopeError("date column needs datetime.date values")
        return v
    if not isinstance(v, datetime):
        raise EnvelopeError("datetime column needs datetime values")
    return v if v.tzinfo is not None else v.replace(tzinfo=timezone.utc)


def _encode_cell(v: Any, t: ColumnType) -> JSON:
    if v is None:
        return None
    if t == "integer64":
        return str(int(v))
    if t == "date":
        return v.isoformat() if isinstance(v, date) else v
    if t == "datetime":
        return format_datetime(v)
    if t == "number" and isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def format_datetime(v: datetime) -> str:
    """UTC, `T` separator, 0 or exactly 3 fractional digits (truncated), `Z`."""
    if v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    v = v.astimezone(timezone.utc)
    base = v.strftime("%Y-%m-%dT%H:%M:%S")
    ms = v.microsecond // 1000
    return f"{base}.{ms:03d}Z" if ms else f"{base}Z"


def parse_datetime(s: str) -> datetime:
    m = _DATETIME_RE.match(s)
    if not m:
        raise EnvelopeError("datetime must be RFC 3339 with a T separator and a zone")
    y, mo, d, hh, mm, ss, frac, zone = m.groups()
    micro = int((frac or "0").ljust(6, "0")[:6])
    micro -= micro % 1000  # truncate to milliseconds
    try:
        naive = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss), micro)
    except ValueError as exc:
        raise EnvelopeError("datetime is out of range") from exc
    if zone == "Z":
        return naive.replace(tzinfo=timezone.utc)
    sign = 1 if zone[0] == "+" else -1
    offset = timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6])) * sign
    return (naive - offset).replace(tzinfo=timezone.utc)


def parse_date(s: str) -> date:
    m = _DATE_RE.match(s)
    if not m:
        raise EnvelopeError("date must be YYYY-MM-DD")
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        raise EnvelopeError("date is not a real calendar date") from exc


def _decode_cell(v: JSON, t: ColumnType, name: str) -> Any:
    if v is None:
        return None
    where = f"column {name!r}"
    if t == "string":
        if not isinstance(v, str):
            raise EnvelopeError(f"{where}: expected a string")
        return v
    if t == "boolean":
        if not isinstance(v, bool):
            raise EnvelopeError(f"{where}: expected a boolean")
        return v
    if t == "integer":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise EnvelopeError(f"{where}: expected an integer")
        if isinstance(v, float):
            if not v.is_integer():
                raise EnvelopeError(f"{where}: expected a whole number")
            v = int(v)
        if abs(v) > SAFE_INT:
            raise EnvelopeError(f"{where}: integer beyond 2^53-1 must be integer64")
        return v
    if t == "integer64":
        if not isinstance(v, str) or not _INT64_RE.match(v):
            raise EnvelopeError(f"{where}: integer64 cells are decimal strings")
        return int(v)
    if t == "number":
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise EnvelopeError(f"{where}: expected a number")
        return float(v)
    if t == "date":
        if not isinstance(v, str):
            raise EnvelopeError(f"{where}: expected a date string")
        return parse_date(v)
    if not isinstance(v, str):
        raise EnvelopeError(f"{where}: expected a datetime string")
    return parse_datetime(v)


# -- envelopes -------------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    operation: str
    params: dict[str, Any]


@dataclass(frozen=True)
class RemoteErrorInfo:
    code: str
    message: str
    detail: dict[str, Any]


@dataclass(frozen=True)
class ResponseEnvelope:
    status: Literal["ok", "error"]
    body: JSON = None
    error: RemoteErrorInfo | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def encode_query(operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(operation, str) or not _OP_RE.match(operation) or len(operation) > 128:
        raise EnvelopeError("operation must match ^[A-Za-z_][A-Za-z0-9_.-]*$ and be at most 128 characters")
    if not isinstance(params, Mapping):
        raise EnvelopeError("params must be a mapping")
    return {
        "v": ENVELOPE_VERSION,
        "kind": "query",
        "operation": operation,
        "params": _jsonable(dict(params), "params"),
        "encoding": ENCODING_JSON,
    }


def encode_response_ok(body: Any) -> dict[str, Any]:
    return {
        "v": ENVELOPE_VERSION,
        "kind": "response",
        "status": "ok",
        "body": encode_body(body),
        "encoding": ENCODING_JSON,
    }


def encode_response_error(code: str, message: str, detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if code not in REMOTE_CODES:
        raise EnvelopeError(f"unknown remote error code {code!r}")
    err: dict[str, Any] = {"code": code, "message": str(message)[:65536]}
    if detail:
        err["detail"] = _jsonable(dict(detail), "detail")
    return {"v": ENVELOPE_VERSION, "kind": "response", "status": "error", "error": err, "encoding": ENCODING_JSON}


def encode_body(body: Any) -> JSON:
    """Table or DataFrame → fusion table JSON; anything else must already be JSON-able."""
    if isinstance(body, Table):
        return body.to_json()
    if _is_dataframe(body):
        return Table.from_pandas(body).to_json()
    return _jsonable(body, "body")


def _is_dataframe(obj: Any) -> bool:
    cls = type(obj)
    # pandas 3 sets DataFrame.__module__ to "pandas"; earlier versions use "pandas.core.frame".
    return cls.__name__ == "DataFrame" and (cls.__module__ == "pandas" or cls.__module__.startswith("pandas."))


def _jsonable(value: Any, what: str) -> JSON:
    """Round-trip through json to guarantee a plain JSON value (rejects NaN/Infinity and non-JSON types)."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise EnvelopeError(f"{what} is not JSON-serializable ({type(exc).__name__})") from None


def decode(obj: JSON) -> Query | ResponseEnvelope:
    """Decode a received envelope. Raises EnvelopeError; never touches the body's content beyond shape."""
    if not isinstance(obj, dict):
        raise EnvelopeError("envelope must be an object")
    if obj.get("v") != ENVELOPE_VERSION or isinstance(obj.get("v"), bool):
        raise EnvelopeError("unsupported envelope version")
    if obj.get("encoding", ENCODING_JSON) != ENCODING_JSON:
        raise EnvelopeError("unsupported encoding")
    kind = obj.get("kind")
    if kind == "query":
        if set(obj) - {"v", "kind", "operation", "params", "encoding"} or "operation" not in obj or "params" not in obj:
            raise EnvelopeError("query must have exactly v, kind, operation, params[, encoding]")
        op = obj["operation"]
        if not isinstance(op, str) or not _OP_RE.match(op) or len(op) > 128:
            raise EnvelopeError("invalid operation name")
        if not isinstance(obj["params"], dict):
            raise EnvelopeError("params must be an object")
        return Query(op, obj["params"])
    if kind == "response":
        status = obj.get("status")
        if status == "ok":
            if set(obj) - {"v", "kind", "status", "body", "encoding"} or "body" not in obj:
                raise EnvelopeError("ok response must have exactly v, kind, status, body[, encoding]")
            body = obj["body"]
            if is_table(body):
                Table.from_json(body)  # validate now so ProtocolError is raised at decode time
            return ResponseEnvelope("ok", body=body)
        if status == "error":
            if set(obj) - {"v", "kind", "status", "error", "encoding"} or "error" not in obj:
                raise EnvelopeError("error response must have exactly v, kind, status, error[, encoding]")
            err = obj["error"]
            if (
                not isinstance(err, dict)
                or set(err) - {"code", "message", "detail"}
                or "code" not in err
                or "message" not in err
            ):
                raise EnvelopeError("error must be {code, message[, detail]}")
            if err["code"] not in REMOTE_CODES:
                raise EnvelopeError("unknown remote error code")
            if not isinstance(err["message"], str):
                raise EnvelopeError("error.message must be a string")
            detail = err.get("detail", {})
            if not isinstance(detail, dict):
                raise EnvelopeError("error.detail must be an object")
            return ResponseEnvelope("error", error=RemoteErrorInfo(err["code"], err["message"], detail))
        raise EnvelopeError("response status must be ok or error")
    raise EnvelopeError("kind must be query or response")


__all__ = [
    "COLUMN_TYPES",
    "ENCODING_JSON",
    "ENVELOPE_VERSION",
    "REMOTE_CODES",
    "SAFE_INT",
    "Column",
    "ColumnType",
    "EnvelopeError",
    "Query",
    "RemoteErrorInfo",
    "ResponseEnvelope",
    "Table",
    "canonical_bytes",
    "decode",
    "encode_body",
    "encode_query",
    "encode_response_error",
    "encode_response_ok",
    "format_datetime",
    "infer_type",
    "is_table",
    "parse_date",
    "parse_datetime",
]
