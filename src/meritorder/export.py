"""Ledger files for `meritorder export`: JSON lines, CSV, or Parquet (docs/08-reporting.md).

Rows arrive from the gateway's JSONL export one line at a time and are written as they come, so a month of requests
never has to fit in memory. Parquet buffers one row group at a time and needs pyarrow (the `parquet` extra).
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime
from pathlib import Path

from .store import LEDGER_FIELDS, SAVINGS_FIELDS

CONTENT_TYPES = {"jsonl": "application/x-ndjson", "csv": "text/csv", "parquet": "application/vnd.apache.parquet"}
# Rows per Parquet row group, the unit warehouses skip and read in parallel. Rows are decoded _BATCH_ROWS at a time and
# packed into Arrow arrays (about a fifth of the size of the Python objects), so a row group in waiting takes ~40 MB.
ROW_GROUP_ROWS = 100_000
_BATCH_ROWS = 10_000
_TEXT = ("user_id", "team", "key_id", "session_id", "client_format", "requested_model", "floor", "route_reason", "served_model",
         "upstream_model", "backend", "backend_kind", "error", "service_tier", "batch_id", "failed_over")
_COSTS = ("actual_cost", "served_api_cost", "baseline_cost", "list_cost", *SAVINGS_FIELDS)


def need_pyarrow():
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        raise SystemExit("Parquet export needs pyarrow, which the image doesn't include: run it from a clone of the repository "
                         "after `uv sync --extra parquet`, or build the image with the extra (docs/07-deployment.md), "
                         "or export --format jsonl or csv")


def parquet_schema():
    """Arrow types for every ledger column, as the Postgres table stores them (store.py). A column added to the ledger
    without a type here fails loudly rather than being exported as text."""
    import pyarrow as pa

    types = {
        "id": pa.string(),  # a UUID; text, because warehouses disagree on Parquet's UUID type
        "ts": pa.timestamp("us", tz="UTC"),
        "tags": pa.map_(pa.string(), pa.string()),
        "status": pa.int32(), "latency_ms": pa.int32(), "ttft_ms": pa.int32(),
        "stream": pa.bool_(), "upgraded": pa.bool_(), "proxy_cache": pa.bool_(),
        "input_tokens": pa.int64(), "cache_read_tokens": pa.int64(), "cache_write_tokens": pa.int64(), "output_tokens": pa.int64(),
        "pages": pa.int32(),
        **{f: pa.string() for f in _TEXT},
        **{f: pa.float64() for f in _COSTS},
    }
    return pa.schema([pa.field(f, types[f]) for f in LEDGER_FIELDS])


class LedgerWriter:
    """Writes ledger rows, given as the gateway's JSON lines, to a file in one format. ``rows`` counts what was written."""

    def __init__(self, fmt: str, path: Path):
        if fmt == "parquet":
            need_pyarrow()
        self.fmt, self.path, self.rows = fmt, path, 0
        self._csv = self._parquet = None
        self._file = None if fmt == "parquet" else open(path, "w", newline="" if fmt == "csv" else None)
        if fmt == "parquet":
            import pyarrow.parquet as pq

            self._schema = parquet_schema()
            self._parquet = pq.ParquetWriter(str(path), self._schema)
            self._columns: dict[str, list] = {f: [] for f in LEDGER_FIELDS}  # decoded rows, not yet packed
            self._pending = 0
            self._batches: list = []  # packed rows of the next row group
            self._packed = 0

    def write(self, line: str) -> None:
        if self.fmt == "jsonl":
            self._file.write(line + "\n")
        elif self.fmt == "csv":
            row = json.loads(line)
            if self._csv is None:
                self._csv = csv.DictWriter(self._file, fieldnames=list(row))
                self._csv.writeheader()
            row["tags"] = json.dumps(row.get("tags") or {})
            self._csv.writerow(row)
        else:
            self._add(json.loads(line))
        self.rows += 1

    def _add(self, row: dict) -> None:
        if self.rows == 0:
            extra = sorted(set(row) - set(LEDGER_FIELDS))
            if extra:  # a newer gateway: say so rather than drop columns silently
                print(f"note: this version of meritorder doesn't know the ledger columns {', '.join(extra)}; they're left out "
                      "of the Parquet file (upgrade it, or export --format jsonl)", file=sys.stderr)
        for f, column in self._columns.items():
            v = row.get(f)
            if f == "ts" and isinstance(v, str):
                v = datetime.fromisoformat(v)
            elif f == "tags":
                v = [(str(k), x if isinstance(x, str) else json.dumps(x)) for k, x in (v or {}).items()]
            column.append(v)
        self._pending += 1
        if self._pending >= min(_BATCH_ROWS, ROW_GROUP_ROWS - self._packed):
            self._pack()
            if self._packed >= ROW_GROUP_ROWS:
                self._write_row_group()

    def _pack(self) -> None:
        import pyarrow as pa

        if self._pending:
            self._batches.append(pa.RecordBatch.from_pydict(self._columns, schema=self._schema))
            self._columns = {f: [] for f in LEDGER_FIELDS}
            self._packed += self._pending
            self._pending = 0

    def _write_row_group(self) -> None:
        import pyarrow as pa

        if self._packed:
            self._parquet.write_table(pa.Table.from_batches(self._batches, schema=self._schema), row_group_size=ROW_GROUP_ROWS)
            self._batches, self._packed = [], 0

    def close(self) -> None:
        """Finish the file (for Parquet, the last row group and the footer). Safe to call twice."""
        if self._parquet is not None:
            try:
                self._pack()
                self._write_row_group()
            finally:
                self._parquet.close()
                self._parquet = None
        if self._file is not None:
            self._file.close()
            self._file = None
