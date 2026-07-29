"""LLM-facing curator tools — six functions bound to the run_loop tool bus.

Two-tier discipline (ADR 0003, matches `catalog/tools.py`): each tool opens
its own connection, takes flat scalar arguments, returns typed error dicts,
lets no exception escape. Docstrings ARE the LLM prompt — an LLM reading
the tool schema learns when to call each one and what shape the response
takes.

Three read tools surface curator-actionable rows from the catalog:

- `list_stale_events` — events past `end_utc`.
- `list_duplicate_candidates` — groups by normalized title + start date +
  venue name where more than one live row shares the key.
- `list_low_confidence_events` — low-confidence extractions the curator
  should consider archiving or re-categorizing.

Three write tools mutate the catalog. Every write goes through the T1
primitives (`soft_delete_event`, `update_event_category`) so archived rows
stay reversible and category writes stay Literal-safe.

- `archive_event` — soft-delete one event with a rationale.
- `merge_events` — soft-delete N duplicate rows in favor of one kept row.
- `update_event_category` — correct one event's `EventCategory` Literal.

`dry_run` is not an LLM-exposed argument (the LLM should not decide
whether to write). Instead, `build_curator_tools(dry_run=...)` returns
a fresh registry whose write tools carry the flag closed-over — matches
the factory pattern `agents/event_agent.py::build_memory_tools` uses.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Final, get_args

from pydantic import ValidationError

from planazo.catalog.repository import (
    _event_from_row,
    soft_delete_event,
)
from planazo.catalog.repository import (
    update_event_category as _update_event_category_primitive,
)
from planazo.query.models import EventCategory
from planazo.storage import db

REASON_CAP: Final[int] = 500
"""Max length of a curator-tool `reason` argument.

Matches the `RATIONALE_CAP` `llm_decisions.rationale` uses at the DB
boundary. Longer reasons truncate to this many characters before Pydantic
validation at the observability writer; the tool tier refuses over-cap
input with a typed error rather than silently truncating.
"""


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


def list_stale_events(limit: int = 50) -> dict[str, object]:
    """List live events whose `end_utc` is in the past — candidates for archive.

    Call this to find events that already happened. An event is stale when
    its stored `end_utc` is earlier than the current time AND it has not
    already been archived (`archived_at IS NULL`). Returns rows ordered by
    how long ago they ended (most recent first), so the LLM can decide
    whether to archive them one at a time via `archive_event`.

    `limit` bounds the response size; the default of 50 matches typical
    curator-tick budgets. A `limit` of 0 or below is an
    `invalid_search_filter` error.

    Return shape:
      {"events": [{"event_id", "title", "end_utc", "days_past"}, ...],
       "total": <count returned>}
    """
    if limit < 1:
        return {
            "error_type": "invalid_search_filter",
            "message": f"limit must be >= 1, got {limit}",
        }
    now_utc = datetime.now(UTC)
    try:
        conn = db.connect()
    except (OSError, sqlite3.Error) as exc:
        return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    try:
        try:
            rows = conn.execute(
                "SELECT id, title, end_utc FROM events"
                " WHERE archived_at IS NULL AND end_utc < ?"
                " ORDER BY end_utc DESC LIMIT ?",
                (now_utc.isoformat(), limit),
            ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    finally:
        conn.close()

    events: list[dict[str, object]] = []
    for row in rows:
        end = datetime.fromisoformat(row["end_utc"])
        events.append(
            {
                "event_id": int(row["id"]),
                "title": row["title"],
                "end_utc": row["end_utc"],
                "days_past": max(0, (now_utc - end).days),
            }
        )
    return {"events": events, "total": len(events)}


def list_duplicate_candidates(limit: int = 50) -> dict[str, object]:
    """List groups of live events sharing normalized title + start date + venue.

    Call this to find probable duplicates — the same event announced by
    two accounts on the same day at the same venue. Groups are formed by
    the composite key
    `(lowercase-trimmed(title), start_utc::date, coalesce(venue_name, ""))`.
    Only groups with more than one row are returned. Rows are always
    live (`archived_at IS NULL`).

    Use this to pick which id to keep and which to archive via
    `merge_events`. Prefer the row with the highest `confidence`, or with
    the more specific venue, or the more official-looking source_account
    — the LLM decides.

    `limit` bounds the number of GROUPS returned (not the total row
    count). Each group carries up to 20 rows for reasonable prompts.

    Return shape:
      {"groups": [{"group_key",
                   "events": [{"event_id", "title", "start_utc",
                               "venue_name", "source_url", "confidence"}, ...]},
                  ...],
       "total": <group count>}
    """
    if limit < 1:
        return {
            "error_type": "invalid_search_filter",
            "message": f"limit must be >= 1, got {limit}",
        }
    try:
        conn = db.connect()
    except (OSError, sqlite3.Error) as exc:
        return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    try:
        try:
            group_rows = conn.execute(
                "SELECT LOWER(TRIM(title)) AS norm_title,"
                " substr(start_utc, 1, 10) AS start_date,"
                " COALESCE(venue_name, '') AS venue,"
                " COUNT(*) AS n"
                " FROM events"
                " WHERE archived_at IS NULL"
                " GROUP BY norm_title, start_date, venue"
                " HAVING n > 1"
                " ORDER BY n DESC LIMIT ?",
                (limit,),
            ).fetchall()
            groups: list[dict[str, object]] = []
            for group_row in group_rows:
                members = conn.execute(
                    "SELECT id, title, start_utc, venue_name, source_url, confidence"
                    " FROM events"
                    " WHERE archived_at IS NULL"
                    " AND LOWER(TRIM(title)) = ?"
                    " AND substr(start_utc, 1, 10) = ?"
                    " AND COALESCE(venue_name, '') = ?"
                    " ORDER BY confidence DESC, id ASC LIMIT 20",
                    (group_row["norm_title"], group_row["start_date"], group_row["venue"]),
                ).fetchall()
                groups.append(
                    {
                        "group_key": (
                            f"{group_row['norm_title']}|{group_row['start_date']}|"
                            f"{group_row['venue']}"
                        ),
                        "events": [
                            {
                                "event_id": int(m["id"]),
                                "title": m["title"],
                                "start_utc": m["start_utc"],
                                "venue_name": m["venue_name"],
                                "source_url": m["source_url"],
                                "confidence": m["confidence"],
                            }
                            for m in members
                        ],
                    }
                )
        except (OSError, sqlite3.Error) as exc:
            return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    finally:
        conn.close()

    return {"groups": groups, "total": len(groups)}


def _tokenize_title(title: str) -> frozenset[str]:
    """Lowercase-split-strip-punct tokenization for Jaccard similarity.

    Simple and dependency-free: lowercase, split on whitespace, strip a
    conservative punctuation set from each token, drop empty tokens.
    Not language-aware — matches the ADR 0020 §Out of scope note that
    fuzzy dedup starts with exact-Python tokens and defers full NLP.
    """
    stripped: set[str] = set()
    for token in title.lower().split():
        clean = token.strip(".,!?()[]{}\"'`:;@-—…")
        if clean:
            stripped.add(clean)
    return frozenset(stripped)


def _jaccard(a: str, b: str) -> float:
    """Token-set Jaccard similarity between two titles in `[0.0, 1.0]`.

    Returns `0.0` when either title tokenizes to the empty set — the
    caller treats that as "no similarity signal".
    """
    a_tokens = _tokenize_title(a)
    b_tokens = _tokenize_title(b)
    if not a_tokens or not b_tokens:
        return 0.0
    intersection = a_tokens & b_tokens
    union = a_tokens | b_tokens
    return len(intersection) / len(union)


def list_fuzzy_duplicate_candidates(
    similarity_threshold: float = 0.6,
    limit: int = 50,
) -> dict[str, object]:
    """List groups sharing venue + date whose titles are similar-but-not-identical.

    Complementary to `list_duplicate_candidates`, which requires titles
    to match after `lower(trim(...))`. This tool covers the softer case
    where two accounts announce the same event with slightly different
    wording — a common Instagram pattern where the venue account posts
    "Techno Night @ Sala Apolo" and the promoter posts "🔥 Techno Night
    - Live DJ Set". Same date, same venue, different-but-similar
    titles.

    Grouping key is `(start_utc::date, coalesce(venue_name, ""))`. Two
    events land in the same fuzzy-duplicate group if their titles have
    token-set Jaccard similarity `>= similarity_threshold`. The
    tokenizer is dependency-free: lowercase, split on whitespace, strip
    conservative punctuation.

    Groups where every title pair also matches exactly are excluded —
    those are already covered by `list_duplicate_candidates`. Use this
    tool AFTER `list_duplicate_candidates` to catch the softer cases;
    then decide with `merge_events` which id to keep. When in doubt,
    prefer `archive_event` on one row over `merge_events` — a bad
    merge affects two rows, a bad archive only one.

    `similarity_threshold` in `[0.0, 1.0]`; default 0.6 is roughly
    "same event, different marketing". Values outside range are
    `invalid_search_filter`. `limit` bounds returned groups (up to 20
    rows per group).

    Return shape:
      {"groups": [{"group_key",
                   "events": [{"event_id", "title", "start_utc",
                               "venue_name", "source_url", "confidence"}, ...],
                   "max_similarity": <float>}, ...],
       "total": <group count>}
    """
    if not 0.0 <= similarity_threshold <= 1.0:
        return {
            "error_type": "invalid_search_filter",
            "message": f"similarity_threshold must be in [0.0, 1.0], got {similarity_threshold}",
        }
    if limit < 1:
        return {
            "error_type": "invalid_search_filter",
            "message": f"limit must be >= 1, got {limit}",
        }
    try:
        conn = db.connect()
    except (OSError, sqlite3.Error) as exc:
        return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    try:
        try:
            # Fetch all live events grouped by (start_date, venue) where the
            # bucket has more than one row — those are the fuzzy-match
            # candidates. Sort by bucket size DESC so the LLM sees the
            # biggest clusters first.
            bucket_rows = conn.execute(
                "SELECT substr(start_utc, 1, 10) AS start_date,"
                " COALESCE(venue_name, '') AS venue,"
                " COUNT(*) AS n"
                " FROM events"
                " WHERE archived_at IS NULL"
                " GROUP BY start_date, venue"
                " HAVING n > 1"
                " ORDER BY n DESC"
            ).fetchall()
            groups: list[dict[str, object]] = []
            for bucket in bucket_rows:
                if len(groups) >= limit:
                    break
                members = conn.execute(
                    "SELECT id, title, start_utc, venue_name, source_url, confidence"
                    " FROM events"
                    " WHERE archived_at IS NULL"
                    " AND substr(start_utc, 1, 10) = ?"
                    " AND COALESCE(venue_name, '') = ?"
                    " ORDER BY confidence DESC, id ASC LIMIT 20",
                    (bucket["start_date"], bucket["venue"]),
                ).fetchall()
                titles = [row["title"] for row in members]
                if not _bucket_has_fuzzy_pair(titles, similarity_threshold):
                    continue
                if _bucket_is_all_exact_matches(titles):
                    # Already covered by list_duplicate_candidates.
                    continue
                max_similarity = _bucket_max_similarity(titles)
                groups.append(
                    {
                        "group_key": f"{bucket['start_date']}|{bucket['venue']}",
                        "events": [
                            {
                                "event_id": int(m["id"]),
                                "title": m["title"],
                                "start_utc": m["start_utc"],
                                "venue_name": m["venue_name"],
                                "source_url": m["source_url"],
                                "confidence": m["confidence"],
                            }
                            for m in members
                        ],
                        "max_similarity": round(max_similarity, 3),
                    }
                )
        except (OSError, sqlite3.Error) as exc:
            return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    finally:
        conn.close()

    return {"groups": groups, "total": len(groups)}


def _bucket_has_fuzzy_pair(titles: list[str], threshold: float) -> bool:
    """True iff at least one title pair in `titles` has Jaccard >= threshold."""
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            if _jaccard(titles[i], titles[j]) >= threshold:
                return True
    return False


def _bucket_is_all_exact_matches(titles: list[str]) -> bool:
    """True iff every title in `titles` normalizes to the same string.

    Matches the exact-match key `list_duplicate_candidates` uses:
    `lower(trim(title))`.
    """
    if not titles:
        return False
    normalized = {t.strip().lower() for t in titles}
    return len(normalized) == 1


def _bucket_max_similarity(titles: list[str]) -> float:
    """Maximum pairwise Jaccard across `titles`. Empty or one-item lists → 0."""
    best = 0.0
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            score = _jaccard(titles[i], titles[j])
            if score > best:
                best = score
    return best


def list_low_confidence_events(threshold: float = 0.4, limit: int = 50) -> dict[str, object]:
    """List live events with `confidence < threshold` — candidates for review.

    Call this to find events the Extractor was unsure about. Low-confidence
    rows often have mis-categorized `EventCategory`, missing venue/date, or
    incoherent title/description pairing. Use this list to feed either
    `update_event_category` (if the fix is a Literal correction) or
    `archive_event` (if the row shouldn't be surfaced at all).

    `threshold` is inclusive-of-lower: rows with exactly `threshold` are
    NOT returned. Default 0.4 catches roughly the bottom quartile of a
    typical extraction distribution. Values outside `[0, 1]` are
    `invalid_search_filter`.

    Return shape:
      {"events": [{"event_id", "title", "confidence", "category",
                   "description"}, ...],
       "total": <count>}
    """
    if not 0.0 <= threshold <= 1.0:
        return {
            "error_type": "invalid_search_filter",
            "message": f"threshold must be in [0.0, 1.0], got {threshold}",
        }
    if limit < 1:
        return {
            "error_type": "invalid_search_filter",
            "message": f"limit must be >= 1, got {limit}",
        }
    try:
        conn = db.connect()
    except (OSError, sqlite3.Error) as exc:
        return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    try:
        try:
            rows = conn.execute(
                "SELECT id, title, confidence, category, description"
                " FROM events"
                " WHERE archived_at IS NULL AND confidence < ?"
                " ORDER BY confidence ASC LIMIT ?",
                (threshold, limit),
            ).fetchall()
        except (OSError, sqlite3.Error) as exc:
            return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    finally:
        conn.close()

    events = [
        {
            "event_id": int(row["id"]),
            "title": row["title"],
            "confidence": row["confidence"],
            "category": row["category"],
            "description": row["description"],
        }
        for row in rows
    ]
    return {"events": events, "total": len(events)}


# ---------------------------------------------------------------------------
# Write tool implementations (dry_run-aware; exposed via the factory below)
# ---------------------------------------------------------------------------


def _validate_reason(reason: str) -> str | None:
    """Return `None` if `reason` is well-formed, else an error message."""
    if not reason or not reason.strip():
        return "reason must be a non-empty string"
    if len(reason) > REASON_CAP:
        return f"reason must be <= {REASON_CAP} chars, got {len(reason)}"
    return None


def _archive_event_impl(event_id: int, reason: str, *, dry_run: bool) -> dict[str, object]:
    error_message = _validate_reason(reason)
    if error_message is not None:
        return {"error_type": "invalid_reason", "message": error_message}
    if event_id < 1:
        return {
            "error_type": "invalid_event_id",
            "message": f"event_id must be >= 1, got {event_id}",
        }

    try:
        conn = db.connect()
    except (OSError, sqlite3.Error) as exc:
        return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    try:
        row = conn.execute(
            "SELECT id, archived_at FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return {"error_type": "not_found", "message": f"no event with id={event_id}"}
        if row["archived_at"] is not None:
            return {
                "error_type": "already_archived",
                "message": f"event id={event_id} is already archived",
            }
        if dry_run:
            return {"status": "dry_run", "archived_event_id": event_id}
        try:
            outcome = soft_delete_event(conn, event_id)
        except (OSError, sqlite3.Error) as exc:
            return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
        if not outcome:
            # Race: another writer archived the row between our SELECT and
            # UPDATE. Treat as `already_archived` so the LLM sees a typed
            # branch instead of a mysterious False.
            return {
                "error_type": "already_archived",
                "message": f"event id={event_id} was archived concurrently",
            }
    finally:
        conn.close()

    return {"status": "ok", "archived_event_id": event_id}


def _coerce_archive_event_ids(value: object) -> list[int] | dict[str, object]:
    """Normalize LLM-provided `archive_event_ids` into a `list[int]` or return an error dict.

    LLM providers commonly auto-unbox a single-element list, sending
    `archive_event_ids='26'` (a string) or `archive_event_ids=26` (a
    bare int) instead of the schema-correct `[26]`. Coerce those into
    `[26]` here so a well-formed merge intent isn't lost to marshalling
    quirks. Anything genuinely malformed (a dict, a nested list, a
    non-numeric string) still returns a typed `invalid_event_id` error.
    """
    if isinstance(value, list):
        coerced: list[int] = []
        for entry in value:
            try:
                coerced.append(int(entry))
            except (TypeError, ValueError):
                return {
                    "error_type": "invalid_event_id",
                    "message": (f"archive_event_ids entries must be integers, got {entry!r}"),
                }
        return coerced
    if isinstance(value, int) and not isinstance(value, bool):
        return [value]
    if isinstance(value, str):
        try:
            return [int(value.strip())]
        except ValueError:
            return {
                "error_type": "invalid_event_id",
                "message": f"archive_event_ids string {value!r} is not an integer",
            }
    return {
        "error_type": "invalid_event_id",
        "message": (f"archive_event_ids must be list[int] or an int, got {type(value).__name__}"),
    }


def _merge_events_impl(
    keep_event_id: int,
    archive_event_ids: list[int] | object,
    reason: str,
    *,
    dry_run: bool,
) -> dict[str, object]:
    error_message = _validate_reason(reason)
    if error_message is not None:
        return {"error_type": "invalid_reason", "message": error_message}
    if keep_event_id < 1:
        return {
            "error_type": "invalid_event_id",
            "message": f"keep_event_id must be >= 1, got {keep_event_id}",
        }
    coerced = _coerce_archive_event_ids(archive_event_ids)
    if isinstance(coerced, dict):
        return coerced
    archive_event_ids = coerced
    if not archive_event_ids:
        return {
            "error_type": "invalid_merge_group",
            "message": "archive_event_ids must contain at least one id",
        }
    if keep_event_id in archive_event_ids:
        return {
            "error_type": "invalid_merge_group",
            "message": f"keep_event_id={keep_event_id} cannot appear in archive_event_ids",
        }
    if any(eid < 1 for eid in archive_event_ids):
        return {
            "error_type": "invalid_event_id",
            "message": f"every archive_event_ids entry must be >= 1, got {archive_event_ids}",
        }

    try:
        conn = db.connect()
    except (OSError, sqlite3.Error) as exc:
        return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    try:
        kept = conn.execute(
            "SELECT id, archived_at FROM events WHERE id = ?", (keep_event_id,)
        ).fetchone()
        if kept is None or kept["archived_at"] is not None:
            return {
                "error_type": "invalid_merge_group",
                "message": f"keep_event_id={keep_event_id} is missing or archived",
            }
        # Every id to archive must exist and be live. Refuse the whole call
        # if any id fails — a partial merge is a Rule-4 audit-trail hazard.
        for aid in archive_event_ids:
            row = conn.execute("SELECT id, archived_at FROM events WHERE id = ?", (aid,)).fetchone()
            if row is None:
                return {"error_type": "not_found", "message": f"no event with id={aid}"}
            if row["archived_at"] is not None:
                return {
                    "error_type": "already_archived",
                    "message": f"event id={aid} is already archived",
                }
        if dry_run:
            return {
                "status": "dry_run",
                "kept_event_id": keep_event_id,
                "archived_event_ids": list(archive_event_ids),
            }
        archived_now: list[int] = []
        for aid in archive_event_ids:
            try:
                if soft_delete_event(conn, aid):
                    archived_now.append(aid)
            except (OSError, sqlite3.Error) as exc:
                return {
                    "error_type": "curator_store_unavailable",
                    "message": type(exc).__name__,
                }
    finally:
        conn.close()

    return {
        "status": "ok",
        "kept_event_id": keep_event_id,
        "archived_event_ids": archived_now,
    }


def _update_event_category_impl(
    event_id: int, new_category: str, reason: str, *, dry_run: bool
) -> dict[str, object]:
    error_message = _validate_reason(reason)
    if error_message is not None:
        return {"error_type": "invalid_reason", "message": error_message}
    if event_id < 1:
        return {
            "error_type": "invalid_event_id",
            "message": f"event_id must be >= 1, got {event_id}",
        }

    valid_categories = set(get_args(EventCategory))
    if new_category not in valid_categories:
        return {
            "error_type": "invalid_category",
            "message": (
                f"new_category={new_category!r} is not a valid EventCategory; "
                f"expected one of {sorted(valid_categories)}"
            ),
        }

    try:
        conn = db.connect()
    except (OSError, sqlite3.Error) as exc:
        return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
    try:
        row = conn.execute(
            "SELECT id, category, archived_at FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return {"error_type": "not_found", "message": f"no event with id={event_id}"}
        if row["archived_at"] is not None:
            return {
                "error_type": "already_archived",
                "message": f"event id={event_id} is already archived",
            }
        old_category = row["category"]
        if old_category == new_category:
            return {
                "error_type": "no_change_needed",
                "message": f"event id={event_id} is already category={new_category!r}",
            }
        if dry_run:
            return {
                "status": "dry_run",
                "event_id": event_id,
                "old_category": old_category,
                "new_category": new_category,
            }
        try:
            outcome = _update_event_category_primitive(conn, event_id, new_category)
        except (OSError, sqlite3.Error) as exc:
            return {"error_type": "curator_store_unavailable", "message": type(exc).__name__}
        if not outcome:
            return {
                "error_type": "already_archived",
                "message": f"event id={event_id} was archived concurrently",
            }
        # VERIFY: read back rather than trust the write.
        try:
            persisted_event = _event_from_row(
                conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            )
        except ValidationError as exc:
            return {"error_type": "invalid_event_data", "message": str(exc)}
    finally:
        conn.close()

    return {
        "status": "ok",
        "event_id": event_id,
        "old_category": old_category,
        "new_category": persisted_event.category,
    }


# ---------------------------------------------------------------------------
# LLM-facing write-tool factories (docstrings ARE the LLM prompt)
# ---------------------------------------------------------------------------


def _make_archive_event(dry_run: bool) -> Callable[[int, str], dict[str, object]]:
    def archive_event(event_id: int, reason: str) -> dict[str, object]:
        """Soft-delete one event with a rationale.

        Call this to retire an event the catalog should no longer surface —
        it has passed, is a stale duplicate, or is otherwise unfit for
        recommendations. `event_id` must reference a live row (an
        `archived_at IS NULL` row); already-archived ids come back as the
        typed `already_archived` branch, not as a silent success.

        `reason` is a short justification (<= 500 chars). It is
        DB-inside per Rule 2: full LLM reasoning is allowed subject to
        length + control-char sanitization at the observability writer.
        This tool does not itself write to `llm_decisions` — the
        composition root's observer sees your `reason` argument and
        writes the audit row.

        The operation is reversible: `sqlite3 var/planazo.db "UPDATE
        events SET archived_at = NULL WHERE id = <id>"` restores the row.

        Error branches: `not_found`, `already_archived`, `invalid_reason`,
        `invalid_event_id`, `curator_store_unavailable`.
        """
        return _archive_event_impl(event_id, reason, dry_run=dry_run)

    return archive_event


def _make_merge_events(
    dry_run: bool,
) -> Callable[[int, list[int], str], dict[str, object]]:
    def merge_events(
        keep_event_id: int, archive_event_ids: list[int], reason: str
    ) -> dict[str, object]:
        """Soft-delete N duplicate events, keeping one as canonical.

        Call this when `list_duplicate_candidates` surfaces a group and
        you have decided which id is the canonical one. Every id in
        `archive_event_ids` is soft-deleted; `keep_event_id` remains live.
        The whole call is refused if any id is missing, already archived,
        or if `keep_event_id` appears in `archive_event_ids` — a partial
        merge would leave a confusing audit trail.

        `reason` is a short justification (<= 500 chars) applied to every
        archived row in the group. Composition root's observer writes one
        `llm_decisions` row per archived id with `decision_kind="merge"`.

        Error branches: `invalid_merge_group`, `not_found`,
        `already_archived`, `invalid_reason`, `invalid_event_id`,
        `curator_store_unavailable`.
        """
        return _merge_events_impl(keep_event_id, archive_event_ids, reason, dry_run=dry_run)

    return merge_events


def _make_update_event_category(
    dry_run: bool,
) -> Callable[[int, str, str], dict[str, object]]:
    def update_event_category(event_id: int, new_category: str, reason: str) -> dict[str, object]:
        """Correct one event's `EventCategory` on a live row.

        Call this when `list_low_confidence_events` (or a spot check)
        reveals a mis-classified event. `new_category` MUST be one of
        the six `EventCategory` Literal values: `"tech"`, `"cultural"`,
        `"music"`, `"networking"`, `"sports"`, `"other"`. Anything else
        comes back as `invalid_category`.

        A no-op call (the row already has `new_category`) returns
        `no_change_needed` so the LLM sees the state truthfully rather
        than "success" on a nothing-happened.

        `reason` is a short justification (<= 500 chars). Composition
        root's observer writes it to `llm_decisions` with
        `decision_kind="update_category"`.

        Error branches: `not_found`, `already_archived`,
        `invalid_category`, `no_change_needed`, `invalid_reason`,
        `invalid_event_id`, `curator_store_unavailable`.
        """
        return _update_event_category_impl(event_id, new_category, reason, dry_run=dry_run)

    return update_event_category


def build_curator_tools(*, dry_run: bool = False) -> dict[str, Callable[..., dict[str, object]]]:
    """Return the six curator tools with `dry_run` closed over the writes.

    `dry_run=True` flips every write tool to a no-op that still validates
    input and returns a `{"status": "dry_run", ...}` payload so the LLM
    sees the same shape and the audit trail records what would have
    happened. Read tools are unaffected.

    Registry keys match the tool names the composition root will pass to
    `agentlib.run_loop` — do not rename without updating the T4 agent.
    """
    return {
        "list_stale_events": list_stale_events,
        "list_duplicate_candidates": list_duplicate_candidates,
        "list_fuzzy_duplicate_candidates": list_fuzzy_duplicate_candidates,
        "list_low_confidence_events": list_low_confidence_events,
        "archive_event": _make_archive_event(dry_run),
        "merge_events": _make_merge_events(dry_run),
        "update_event_category": _make_update_event_category(dry_run),
    }
