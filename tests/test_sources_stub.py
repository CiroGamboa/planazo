from datetime import UTC, datetime, timedelta

from planazo.sources.base import error_state, next_run_after
from planazo.sources.models import MediaAsset, RawPost
from planazo.sources.stub import StubEventSource


def _raw_post(url: str) -> RawPost:
    return RawPost(
        source="instagram",
        permalink=url,
        title=None,
        caption="canned",
        posted_at=datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        author_handle="canned_venue",
        media=[MediaAsset(kind="image", url="https://example.com/i.jpg")],
    )


def test_stub_returns_canned_payload_when_url_matches() -> None:
    url = "https://instagram.com/p/CANNED/"
    stub = StubEventSource(payloads={url: _raw_post(url)})

    result = stub.fetch_post(url)

    assert isinstance(result, RawPost)
    assert result.permalink == url


def test_stub_returns_not_found_typed_error_when_url_missing() -> None:
    stub = StubEventSource()

    result = stub.fetch_post("https://instagram.com/p/UNKNOWN/")

    assert isinstance(result, dict)
    assert result["error_type"] == "not_found"
    assert result["url"] == "https://instagram.com/p/UNKNOWN/"


def test_stub_supports_typed_error_payloads() -> None:
    url = "https://instagram.com/p/PRIVATE/"
    stub = StubEventSource(
        payloads={url: {"error_type": "auth_failed", "message": "login required", "url": url}}
    )

    result = stub.fetch_post(url)

    assert isinstance(result, dict)
    assert result["error_type"] == "auth_failed"


def test_stub_targets_iterates_configured_urls() -> None:
    urls = ["https://instagram.com/p/A/", "https://instagram.com/p/B/"]
    stub = StubEventSource(payloads={url: _raw_post(url) for url in urls})

    assert list(stub.targets()) == urls


def test_stub_defaults_expose_the_name_and_cadence_the_protocol_requires() -> None:
    stub = StubEventSource()

    assert stub.name == "stub"
    assert stub.cadence == timedelta(hours=6)


def test_next_run_after_returns_last_run_plus_cadence_when_last_run_is_set() -> None:
    last_run = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    later = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    result = next_run_after(timedelta(hours=6), last_run, now=lambda: later)

    assert result == datetime(2026, 7, 20, 16, 0, tzinfo=UTC)


def test_next_run_after_returns_now_when_last_run_is_none() -> None:
    now_value = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)

    result = next_run_after(timedelta(hours=6), None, now=lambda: now_value)

    assert result == now_value


def test_error_state_carries_the_three_shared_keys() -> None:
    payload = error_state("rate_limited", "429 from source", "https://instagram.com/p/ABC/")

    assert payload == {
        "error_type": "rate_limited",
        "message": "429 from source",
        "url": "https://instagram.com/p/ABC/",
    }
