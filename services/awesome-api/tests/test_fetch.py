from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from awesome_api.config import Config
from awesome_api.daily import DAILY_SOURCE
from awesome_api.sync import SafeRedirect, fetch_daily, fetch_readme


class Response(BytesIO):
    def __init__(self, body, declared_length=None):
        super().__init__(body)
        self.headers = {
            "Content-Length": str(
                declared_length if declared_length is not None else len(body)
            )
        }


def test_fetch_uses_only_raw_host_head_and_uppercase_fallback(monkeypatch):
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, timeout, request.headers))
            if request.full_url.endswith("/readme.md"):
                raise HTTPError(request.full_url, 404, "missing", Message(), None)
            return Response(b"# Markdown")

    monkeypatch.setattr("awesome_api.sync.build_opener", lambda *args: Opener())
    assert (
        fetch_readme("org/awesome-tools", Config(sync_enabled=False)) == b"# Markdown"
    )
    assert [call[0] for call in calls] == [
        "https://raw.githubusercontent.com/org/awesome-tools/HEAD/readme.md",
        "https://raw.githubusercontent.com/org/awesome-tools/HEAD/README.md",
    ]
    assert all(call[1] == 12 and "Authorization" not in call[2] for call in calls)


def test_fetch_rejects_invalid_source_id_without_network(monkeypatch):
    monkeypatch.setattr(
        "awesome_api.sync.build_opener", lambda *args: pytest.fail("network called")
    )
    for source_id in [
        "../bad",
        "org/repo/path",
        "org/repo?x=y",
        "ORG/REPO",
        "https://evil.test",
    ]:
        with pytest.raises(ValueError):
            fetch_readme(source_id, Config(sync_enabled=False))


@pytest.mark.parametrize("body,declared", [(b"12345", 5), (b"abc", 10)])
def test_fetch_rejects_oversize_and_truncated_responses(monkeypatch, body, declared):
    class Opener:
        def open(self, request, timeout):
            return Response(body, declared)

    monkeypatch.setattr("awesome_api.sync.build_opener", lambda *args: Opener())
    with pytest.raises(ValueError):
        fetch_readme("org/repo", Config(max_readme_bytes=4))


def test_redirect_denies_off_allowlist_and_insecure_locations():
    redirect = SafeRedirect()
    request = Request("https://raw.githubusercontent.com/org/repo/HEAD/README.md")
    for url in [
        "https://evil.test/README.md",
        "http://raw.githubusercontent.com/org/repo",
        "https://raw.githubusercontent.com:443/org/repo",
    ]:
        with pytest.raises(ValueError):
            redirect.redirect_request(request, None, 302, "redirect", {}, url)


def test_daily_fetch_uses_fixed_provider_raw_path_and_missing_day_is_neutral(
    monkeypatch,
):
    requests = []

    class Opener:
        def open(self, request, timeout):
            requests.append((request.full_url, request.headers, timeout))
            raise HTTPError(request.full_url, 404, "missing archive", Message(), None)

    monkeypatch.setattr("awesome_api.sync.build_opener", lambda *args: Opener())
    assert fetch_daily(DAILY_SOURCE["id"], "2026-10-04", Config()) is None
    assert (
        requests[0][0]
        == "https://raw.githubusercontent.com/antonkomarev/github-trending-archive/HEAD/archive/repository/2026/2026-10-04/(null).json"
    )
    assert requests[0][2] == 12 and "Authorization" not in requests[0][1]
    for source, day in [
        ("other/provider", "2026-10-04"),
        (DAILY_SOURCE["id"], "../../secret"),
        (DAILY_SOURCE["id"], "2026-02-30"),
    ]:
        with pytest.raises(ValueError):
            fetch_daily(source, day, Config())
    assert len(requests) == 1


@pytest.mark.parametrize("raw,declared", [(b"abcdef", 6), (b"ab", 3)])
def test_daily_fetch_rejects_oversize_or_truncated_response(monkeypatch, raw, declared):
    class Opener:
        def open(self, request, timeout):
            return Response(raw, declared)

    monkeypatch.setattr("awesome_api.sync.build_opener", lambda *args: Opener())
    with pytest.raises(ValueError):
        fetch_daily(DAILY_SOURCE["id"], "2026-10-04", Config(max_readme_bytes=4))
