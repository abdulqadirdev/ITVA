"""Finding and installing a newer release."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from imgen import update  # noqa: E402


def test_versions_compare_as_numbers_not_text():
    """The classic way an updater quietly stops working: as text,
    "1.10.0" sorts before "1.9.0", so every release after .9 looks old."""
    assert update.parse_version("v1.10.2") == (1, 10, 2)
    assert update.is_newer("1.10.0", "1.9.0")
    assert not update.is_newer("1.9.0", "1.10.0")
    assert update.is_newer("v2.0.0", "1.99.99")
    assert not update.is_newer("1.1.1", "1.1.1"), "the same version is not newer"
    assert not update.is_newer("1.0.0", "1.1.1")
    # Junk must not read as a newer release.
    assert not update.is_newer("", "1.1.1")
    assert not update.is_newer("latest", "1.1.1")


def test_an_unconfigured_repo_is_a_clear_message_not_a_crash():
    with pytest.raises(update.UpdateError, match="no GitHub repository"):
        update.check("")
    with pytest.raises(update.UpdateError, match="no GitHub repository"):
        update.check("not-a-repo-path")


def _fake_github(monkeypatch, payload, status=200):
    """Answer the API without touching the network."""
    import io
    import urllib.error

    class Response(io.BytesIO):
        headers = {"Content-Length": "0"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_open(request, timeout=0):
        if status != 200:
            raise urllib.error.HTTPError(
                request.full_url, status, "nope", {}, None
            )
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr(update.urllib.request, "urlopen", fake_open)


def test_a_newer_release_with_a_deb_is_offered(monkeypatch):
    _fake_github(monkeypatch, {
        "tag_name": "v9.9.9",
        "body": "  faster thumbnails  ",
        "assets": [
            {"name": "notes.txt", "url": "https://x/1", "size": 10},
            {"name": "flow-bulk-generator_9.9.9_amd64.deb",
             "url": "https://x/2", "size": 1_000_000},
        ],
    })
    found = update.check("owner/name")
    assert found is not None
    assert found.version == "9.9.9"
    assert found.name.endswith(".deb"), "the .deb is the asset, not the notes"
    assert found.url == "https://x/2"
    assert found.notes == "faster thumbnails"
    assert found.megabytes == pytest.approx(1.0)


def test_the_current_version_is_not_offered_as_an_update(monkeypatch):
    _fake_github(monkeypatch, {
        "tag_name": f"v{update.__version__}",
        "assets": [{"name": "a.deb", "url": "https://x/a", "size": 1}],
    })
    assert update.check("owner/name") is None


def test_a_release_without_a_deb_says_so(monkeypatch):
    _fake_github(monkeypatch, {
        "tag_name": "v9.9.9",
        "assets": [{"name": "source.zip", "url": "https://x/s", "size": 1}],
    })
    with pytest.raises(update.UpdateError, match="no .deb"):
        update.check("owner/name")


def test_a_private_repo_is_explained_rather_than_reported_as_missing(monkeypatch):
    _fake_github(monkeypatch, {}, status=404)
    with pytest.raises(update.UpdateError, match="private repository"):
        update.check("owner/name")


def test_being_rate_limited_is_explained(monkeypatch):
    _fake_github(monkeypatch, {}, status=403)
    with pytest.raises(update.UpdateError, match="hourly limit"):
        update.check("owner/name")


def test_the_download_asks_for_the_file_not_its_metadata(monkeypatch):
    """The asset API URL serves JSON unless octet-stream is requested;
    without the header the 'package' is a few hundred bytes of JSON that
    dpkg then rejects as a corrupt archive."""
    request = update._request("https://x/2", accept="application/octet-stream")
    assert request.get_header("Accept") == "application/octet-stream"


def test_a_token_is_sent_only_when_there_is_one(monkeypatch):
    monkeypatch.delenv("FLOW_UPDATE_TOKEN", raising=False)
    assert update._request("https://x").get_header("Authorization") is None
    assert update._request("https://x", "abc").get_header("Authorization") == (
        "Bearer abc"
    )
    monkeypatch.setenv("FLOW_UPDATE_TOKEN", "from-env")
    assert update._request("https://x").get_header("Authorization") == (
        "Bearer from-env"
    )


def test_a_truncated_download_is_refused(monkeypatch, tmp_path):
    import io

    class Response(io.BytesIO):
        headers = {"Content-Length": "4"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        update.urllib.request, "urlopen",
        lambda request, timeout=0: Response(b"ab"),
    )
    release = update.Release(
        "9.9.9", "v9.9.9", "", "https://example.invalid/a.deb", "a.deb", size=4
    )
    with pytest.raises(update.UpdateError, match="expected 4"):
        update.download(release, tmp_path)


def test_the_cache_stops_a_check_on_every_launch(tmp_path):
    """Several laptops share one address, and GitHub allows 60 requests
    an hour to it."""
    assert update.read_cache(tmp_path) is None
    update.write_cache(tmp_path, None)
    assert update.read_cache(tmp_path) is not None
    assert update.read_cache(tmp_path, max_age=-1) is None, "stale is ignored"
