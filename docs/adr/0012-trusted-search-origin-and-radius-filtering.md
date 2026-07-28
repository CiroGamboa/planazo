# ADR 0012: Trusted search origin and radius filtering

## Status

Accepted

## Context

`radius_km` narrows a search by distance. An LLM, prose location, event text, or an
implicit city name is not a trustworthy geographic origin. Treating one as an origin
would silently turn an uncertain interpretation into a location claim.

## Decision

`SearchOrigin` is attached only by application-owned code after validation. Valid
sources are an explicit Telegram location validated at its boundary or a deterministic
application-code city centre. Live locations are not persisted.

The interpreter and its LLM tool schema remain coordinate-free. A radius request
without an attached origin returns `missing_search_origin` before a model call or run
trace. Radius filtering uses one fixed Earth-radius Haversine calculation, includes
events at the exact boundary, preserves source order, and excludes coordinate-less
events only while a radius is active.

Catalog tools remain coordinate-free; the application composition filters their
validated results. This supersedes only ADR 0011 Telegram interface's conditional
reservation of 0012; conditional event-source work moves to 0013.

