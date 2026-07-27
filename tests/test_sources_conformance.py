"""Structural-conformance assertions between the concrete Pydantic models and
the `interfaces.sources` Protocols they satisfy.

The assertions below are typed helpers that return the value under
`interfaces.sources`'s Protocol annotation. They exist to *type-check*, not to
run — a mypy-strict pass over `src/` (plus the type-check that pytest applies
when collecting the file) is what verifies the conformance. Each helper is
called once so pytest reports a green line for the assertion having been
exercised, but the load-bearing check is the type annotation itself.
"""

from datetime import UTC, datetime, timedelta

from planazo.interfaces.sources import EventSource as EventSourceProtocol
from planazo.interfaces.sources import MediaAsset as MediaAssetProtocol
from planazo.interfaces.sources import RawPost as RawPostProtocol
from planazo.sources.models import MediaAsset, RawPost
from planazo.sources.stub import StubEventSource


def _conforms_media_asset(m: MediaAsset) -> MediaAssetProtocol:
    return m


def _conforms_raw_post(p: RawPost) -> RawPostProtocol:
    return p


def _conforms_stub(s: StubEventSource) -> EventSourceProtocol:
    return s


def test_media_asset_conforms_to_interface_protocol() -> None:
    asset = MediaAsset(kind="image", url="https://example.com/i.jpg")

    assert _conforms_media_asset(asset) is asset


def test_raw_post_conforms_to_interface_protocol() -> None:
    post = RawPost(
        source="instagram",
        permalink="https://instagram.com/p/ABC/",
        title=None,
        caption="hello",
        posted_at=datetime(2026, 7, 20, tzinfo=UTC),
        author_handle="venue",
        media=[MediaAsset(kind="image", url="https://example.com/i.jpg")],
    )

    assert _conforms_raw_post(post) is post


def test_stub_event_source_conforms_to_interface_protocol() -> None:
    stub = StubEventSource(name="stub", cadence=timedelta(hours=6))

    assert _conforms_stub(stub) is stub
