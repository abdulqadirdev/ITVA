"""Google Sheets access via a service account.

This is the one place a real credential is used, and it reaches only Sheets.
It has no bearing on Flow: Flow is driven through the browser's own session,
and no service account could ever stand in for that.
"""

from __future__ import annotations

import csv
import io
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from .config import SheetsConfig

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


@dataclass(frozen=True)
class PromptRow:
    """One source row: its number, its prompt, and its other columns.

    The other columns matter because filenames can be built from them — a
    storyboard names files by its own `sequence`, not by spreadsheet row.
    """

    row_number: int
    prompt: str
    fields: dict[str, str]


class SheetError(RuntimeError):
    pass


SETUP_HELP = """
To use a Google Sheet as the prompt source:

  1. console.cloud.google.com -> create (or pick) a project
  2. enable the "Google Sheets API" for it
  3. IAM & Admin -> Service Accounts -> create one -> Keys -> Add key ->
     JSON, and save it as secrets/service_account.json
  4. open that JSON, copy "client_email", and share the sheet with that
     address as Editor
  5. put the sheet id (the long part of its URL between /d/ and /edit)
     into sheets.spreadsheet_id in config/settings.toml

Then: python -m imgen.cli doctor

Or skip all of it for now and read prompts from a local file:
  python -m imgen.cli run --limit 5 --prompts-file prompts.sample.txt
"""

SHARING_HELP = """
For sheets.mode = "csv" the sheet must be readable without signing in:

  Share -> General access -> "Anyone with the link" -> Viewer

and sheets.spreadsheet_id must be the id from the sheet's URL
(the long part between /d/ and /edit), with sheets.worksheet set to the
tab's name.
"""


def open_worksheet(cfg: SheetsConfig) -> gspread.Worksheet:
    if not cfg.service_account_file.exists():
        raise SheetError(
            f"no service account key at {cfg.service_account_file}\n{SETUP_HELP}"
        )
    if not cfg.spreadsheet_id:
        raise SheetError(
            f"sheets.spreadsheet_id is empty in config/settings.toml\n{SETUP_HELP}"
        )

    creds = Credentials.from_service_account_file(
        str(cfg.service_account_file), scopes=SCOPES
    )
    client = gspread.authorize(creds)
    try:
        spreadsheet = client.open_by_key(cfg.spreadsheet_id)
    except gspread.exceptions.APIError as exc:
        raise SheetError(
            f"cannot open spreadsheet {cfg.spreadsheet_id}: {exc}\n"
            "Has the sheet been shared with the service account address as Editor?"
        ) from exc
    try:
        return spreadsheet.worksheet(cfg.worksheet)
    except gspread.exceptions.WorksheetNotFound as exc:
        titles = [w.title for w in spreadsheet.worksheets()]
        raise SheetError(
            f"worksheet '{cfg.worksheet}' not found. Available: {titles}"
        ) from exc


@dataclass
class SheetSync:
    """Reads prompts and writes results back, batching the writes.

    The Sheets API allows roughly 60 writes per minute per user, so results are
    queued and flushed in batches rather than written per row.
    """

    cfg: SheetsConfig
    worksheet: gspread.Worksheet
    headers: list[str] = field(default_factory=list)
    _pending: list[dict] = field(default_factory=list)
    writable: bool = True

    @property
    def title(self) -> str:
        return self.worksheet.title

    @classmethod
    def connect(cls, cfg: SheetsConfig) -> "SheetSync":
        ws = open_worksheet(cfg)
        sync = cls(cfg=cfg, worksheet=ws)
        sync._load_headers()
        return sync

    def _load_headers(self) -> None:
        self.headers = [h.strip() for h in self.worksheet.row_values(1)]
        if self.cfg.prompt_column not in self.headers:
            raise SheetError(
                f"no '{self.cfg.prompt_column}' column in row 1. Found: {self.headers}"
            )

    def _column_index(self, name: str, create: bool = False) -> int | None:
        """1-based column index for a header name."""
        for idx, header in enumerate(self.headers, start=1):
            if header.casefold() == name.casefold():
                return idx
        if not create:
            return None
        # Result columns are created on demand so a user only has to supply
        # a sheet with prompts in it.
        idx = len(self.headers) + 1
        self.worksheet.update_acell(rowcol_to_a1(1, idx), name)
        self.headers.append(name)
        return idx

    def read_rows(self) -> list[PromptRow]:
        grid = self.worksheet.get_all_values()
        if not grid:
            return []
        headers = [h.strip() for h in grid[0]]
        wanted = self.cfg.prompt_column.casefold()
        index = next(
            (i for i, h in enumerate(headers) if h.casefold() == wanted), None
        )
        if index is None:
            raise SheetError(
                f"no '{self.cfg.prompt_column}' column in row 1. Found: {headers}"
            )
        rows = []
        for number, row in enumerate(grid[1:], start=2):
            if len(row) <= index:
                continue
            text = (row[index] or "").strip()
            if text:
                rows.append(PromptRow(number, text, _fields(headers, row)))
        return rows

    def read_prompts(self) -> list[tuple[int, str]]:
        return [(r.row_number, r.prompt) for r in self.read_rows()]

    def queue_result(
        self,
        row_number: int,
        status: str,
        paths: list[str] | None = None,
        error: str | None = None,
        generated_at: str | None = None,
    ) -> None:
        cells = {
            self.cfg.status_column: status,
            self.cfg.paths_column: "\n".join(paths) if paths else "",
            self.cfg.error_column: error or "",
            self.cfg.generated_at_column: generated_at or "",
        }
        for name, value in cells.items():
            col = self._column_index(name, create=True)
            self._pending.append(
                {"range": rowcol_to_a1(row_number, col), "values": [[value]]}
            )

    @property
    def pending_rows(self) -> int:
        # Four cells are queued per finished job.
        return len(self._pending) // 4

    def flush(self, force: bool = False) -> int:
        if not self._pending:
            return 0
        if not force and self.pending_rows < self.cfg.write_batch_size:
            return 0
        batch, self._pending = self._pending, []
        self.worksheet.batch_update(batch, value_input_option="RAW")
        return len(batch)


@dataclass
class CsvSheet:
    """Read-only prompt source using Google's CSV export — no credentials.

    The sheet only has to be readable by "anyone with the link". Nothing can be
    written back, so results are recorded in a local manifest instead; see
    Runner. The interface matches SheetSync so the runner does not branch.
    """

    cfg: SheetsConfig
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    writable: bool = False
    _prompt_index: int = 0

    @property
    def title(self) -> str:
        return self.cfg.worksheet

    @classmethod
    def connect(cls, cfg: SheetsConfig) -> "CsvSheet":
        if not cfg.spreadsheet_id:
            raise SheetError(
                f"sheets.spreadsheet_id is empty in config/settings.toml\n{SETUP_HELP}"
            )
        source = cls(cfg=cfg)
        source._fetch()
        return source

    def _fetch(self) -> None:
        url = self.cfg.csv_url
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                payload = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise SheetError(
                f"CSV export returned HTTP {exc.code} for sheet "
                f"{self.cfg.spreadsheet_id}.\n{SHARING_HELP}"
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise SheetError(f"could not reach Google Sheets: {exc}") from exc

        # An unshared sheet answers 200 with a sign-in page, not an error.
        stripped = payload.lstrip()
        if stripped.startswith("<"):
            raise SheetError(
                "Google returned a web page instead of CSV, which means this "
                f"sheet is not readable without signing in.\n{SHARING_HELP}"
            )

        # csv.reader, not DictReader: DictReader silently drops blank lines,
        # which shifts every row number after a gap in the sheet. The row
        # number is how a result is traced back to its source row, so it has
        # to survive empty rows intact.
        rows = list(csv.reader(io.StringIO(payload)))
        if not rows:
            raise SheetError("the sheet is empty")

        self.headers = [h.strip() for h in rows[0]]
        # Case-insensitive, matching the service-account path — a header typed
        # "Image Prompt" in the sheet should not be a configuration error.
        wanted = self.cfg.prompt_column.casefold()
        index = next(
            (i for i, h in enumerate(self.headers) if h.casefold() == wanted), None
        )
        if index is None:
            raise SheetError(
                f"no '{self.cfg.prompt_column}' column in row 1. Found: {self.headers}"
            )
        self._prompt_index = index
        self.rows = rows[1:]

    def read_rows(self) -> list[PromptRow]:
        rows = []
        for number, row in enumerate(self.rows, start=2):
            if len(row) <= self._prompt_index:
                continue  # short or blank line — keeps the row number aligned
            text = (row[self._prompt_index] or "").strip()
            if text:
                rows.append(PromptRow(number, text, _fields(self.headers, row)))
        return rows

    def read_prompts(self) -> list[tuple[int, str]]:
        return [(r.row_number, r.prompt) for r in self.read_rows()]

    # The runner checks `writable` before calling these; they exist so that a
    # missed check degrades to doing nothing rather than crashing a long run.
    def queue_result(self, *args, **kwargs) -> None:
        return None

    def flush(self, force: bool = False) -> int:
        return 0


def _fields(headers: list[str], row: list[str]) -> dict[str, str]:
    """Every column of a row, keyed by lower-cased header."""
    return {
        header.strip().casefold(): (row[i] or "").strip()
        for i, header in enumerate(headers)
        if header.strip() and i < len(row)
    }
