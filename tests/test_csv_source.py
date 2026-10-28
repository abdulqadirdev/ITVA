"""CSV prompt source: parsing, and the failure modes that actually happen."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from imgen import sheets, sources  # noqa: E402
from imgen.config import SheetsConfig  # noqa: E402


def make_cfg(**over) -> SheetsConfig:
    base = dict(
        mode="csv",
        service_account_file=Path("/nonexistent.json"),
        spreadsheet_id="SHEET123",
        worksheet="Sheet1",
        prompt_column="Prompt",
        status_column="Status",
        paths_column="Local Paths",
        generated_at_column="Generated At",
        error_column="Error",
        write_batch_size=10,
    )
    base.update(over)
    return SheetsConfig(**base)


@pytest.fixture
def serve(monkeypatch):
    """Make the CSV endpoint return a given body."""

    def _serve(body: str):
        class FakeResponse:
            def read(self):
                return body.encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(sheets.urllib.request, "urlopen", lambda *a, **k: FakeResponse())

    return _serve


def test_reads_prompts_with_sheet_row_numbers(serve):
    serve("Prompt,Notes\na cat,x\na dog,y\n")
    source = sheets.CsvSheet.connect(make_cfg())
    # Row 1 is the header, so data starts at sheet row 2.
    assert source.read_prompts() == [(2, "a cat"), (3, "a dog")]


def test_blank_cells_are_skipped_without_shifting_rows(serve):
    serve("Prompt\na cat\n\n   \na dog\n")
    assert sheets.CsvSheet.connect(make_cfg()).read_prompts() == [(2, "a cat"), (5, "a dog")]


def test_extra_columns_are_ignored(serve):
    serve("Status,Prompt,Error\ndone,a cat,\n")
    assert sheets.CsvSheet.connect(make_cfg()).read_prompts() == [(2, "a cat")]


def test_missing_prompt_column_names_what_was_found(serve):
    serve("Idea,Notes\na cat,x\n")
    with pytest.raises(sheets.SheetError, match="Idea"):
        sheets.CsvSheet.connect(make_cfg())


def test_unshared_sheet_is_reported_as_a_sharing_problem(serve):
    # Google answers 200 with a sign-in page rather than an error status.
    serve('<!DOCTYPE html><html><body>Sign in</body></html>')
    with pytest.raises(sheets.SheetError, match="not readable without signing in"):
        sheets.CsvSheet.connect(make_cfg())


def test_missing_spreadsheet_id_is_caught_before_any_request():
    with pytest.raises(sheets.SheetError, match="spreadsheet_id"):
        sheets.CsvSheet.connect(make_cfg(spreadsheet_id=""))


def test_csv_source_is_not_writable(serve):
    serve("Prompt\na cat\n")
    source = sheets.CsvSheet.connect(make_cfg())
    assert source.writable is False
    # No-ops rather than crashes, so a missed check cannot kill a long run.
    source.queue_result(2, "done", paths=["a.jpg"])
    assert source.flush(force=True) == 0


def test_unknown_mode_is_rejected():
    with pytest.raises(sheets.SheetError, match="unknown sheets.mode"):
        sources.build_source(make_cfg(mode="telepathy"))
