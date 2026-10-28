"""Prompt sources.

Three kinds, all answering the same two questions — what prompts are there,
and can results be written back:

* ``google_sheet`` via CSV export — no credentials, read-only
* ``service_account`` — authenticated Google Sheets, writes results back
* ``file`` — a local .csv, .tsv, .xlsx or .txt
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import SheetsConfig
from .sheets import CsvSheet, PromptRow, SheetError, SheetSync, _fields

#: Anything that supplies prompts. The runner only needs these four members,
#: and all three implementations provide them.
class PromptSource(Protocol):
    writable: bool

    @property
    def title(self) -> str: ...

    def read_rows(self) -> list[PromptRow]: ...

    def read_prompts(self) -> list[tuple[int, str]]: ...

    def queue_result(self, *args, **kwargs) -> None: ...

    def flush(self, force: bool = False) -> int: ...


TEXT_SUFFIXES = {".txt"}
CSV_SUFFIXES = {".csv", ".tsv"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
SUPPORTED = TEXT_SUFFIXES | CSV_SUFFIXES | EXCEL_SUFFIXES


@dataclass
class FileSource:
    """Prompts from a local spreadsheet or text file. Read-only."""

    cfg: SheetsConfig
    path: Path = Path()
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    writable: bool = False
    _prompt_index: int = 0

    @property
    def title(self) -> str:
        return self.path.name

    @classmethod
    def connect(cls, cfg: SheetsConfig) -> "FileSource":
        if not cfg.file_path:
            raise SheetError("no file chosen for this project")
        path = Path(cfg.file_path).expanduser()
        if not path.exists():
            raise SheetError(f"file not found: {path}")
        if path.suffix.lower() not in SUPPORTED:
            raise SheetError(
                f"unsupported file type '{path.suffix}' — "
                f"expected one of {', '.join(sorted(SUPPORTED))}"
            )
        source = cls(cfg=cfg, path=path)
        source._load()
        return source

    def _load(self) -> None:
        suffix = self.path.suffix.lower()
        if suffix in TEXT_SUFFIXES:
            self._load_text()
            return
        rows = self._read_excel() if suffix in EXCEL_SUFFIXES else self._read_csv()
        if not rows:
            raise SheetError(f"{self.path.name} is empty")

        self.headers = [str(h or "").strip() for h in rows[0]]
        wanted = self.cfg.prompt_column.casefold()
        index = next(
            (i for i, h in enumerate(self.headers) if h.casefold() == wanted), None
        )
        if index is None:
            raise SheetError(
                f"no '{self.cfg.prompt_column}' column in row 1 of "
                f"{self.path.name}. Found: {self.headers}"
            )
        self._prompt_index = index
        self.rows = rows[1:]

    def _read_csv(self) -> list[list[str]]:
        delimiter = "\t" if self.path.suffix.lower() == ".tsv" else ","
        with self.path.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            # csv.reader, not DictReader: DictReader drops blank lines, which
            # would shift every row number after a gap in the sheet.
            return list(csv.reader(fh, delimiter=delimiter))

    def _read_excel(self) -> list[list[str]]:
        from openpyxl import load_workbook

        book = load_workbook(self.path, read_only=True, data_only=True)
        try:
            name = self.cfg.worksheet
            sheet = book[name] if name in book.sheetnames else book.worksheets[0]
            return [
                ["" if cell is None else str(cell) for cell in row]
                for row in sheet.iter_rows(values_only=True)
            ]
        finally:
            book.close()

    def _load_text(self) -> None:
        """One prompt per line; '#' comments ignored, line numbers preserved."""
        self.headers = [self.cfg.prompt_column]
        self._prompt_index = 0
        self.rows = [
            [] if line.lstrip().startswith("#") else [line]
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        ]

    def read_rows(self) -> list[PromptRow]:
        start = 1 if self.path.suffix.lower() in TEXT_SUFFIXES else 2
        rows = []
        for number, row in enumerate(self.rows, start=start):
            if len(row) <= self._prompt_index:
                continue
            text = (row[self._prompt_index] or "").strip()
            if text:
                rows.append(PromptRow(number, text, _fields(self.headers, row)))
        return rows

    def read_prompts(self) -> list[tuple[int, str]]:
        return [(r.row_number, r.prompt) for r in self.read_rows()]

    def queue_result(self, *args, **kwargs) -> None:
        return None

    def flush(self, force: bool = False) -> int:
        return 0


def sheet_tabs(cfg: SheetsConfig) -> list[str]:
    """Worksheet names for a local Excel file, for the UI's tab picker."""
    path = Path(cfg.file_path).expanduser()
    if not cfg.file_path or path.suffix.lower() not in EXCEL_SUFFIXES:
        return []
    from openpyxl import load_workbook

    book = load_workbook(path, read_only=True)
    try:
        return list(book.sheetnames)
    finally:
        book.close()


def build_source(cfg: SheetsConfig):
    """Open the prompt source named by sheets.mode."""
    if cfg.mode == "file":
        return FileSource.connect(cfg)
    if cfg.mode == "csv":
        return CsvSheet.connect(cfg)
    if cfg.mode == "service_account":
        return SheetSync.connect(cfg)
    raise SheetError(
        f"unknown sheets.mode {cfg.mode!r} — expected 'csv', 'file' or 'service_account'"
    )
