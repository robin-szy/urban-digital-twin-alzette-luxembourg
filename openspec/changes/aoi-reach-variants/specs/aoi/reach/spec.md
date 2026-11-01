# Spec Delta

## Purpose

Defines a stretch of river as the project's single area-of-interest identity: a river name with a start and end kilometre, resolved to a validated centerline with chainage. Every AOI polygon in the system is derived from a reach rather than defined independently.

## ADDED Requirements

### Requirement: Reach identity

A reach SHALL be identified by river name, start kilometre and end kilometre, and SHALL carry a canonical identifier derived from those three values. Two requests naming the same river and the same kilometres SHALL resolve to the same identifier regardless of numeric formatting.

#### Scenario: Equivalent kilometre formatting

- **WHEN** reaches are requested for Alzette km `35`, km `35.0` and km `35.00` with the same end kilometre
- **THEN** all three resolve to one reach identifier and one cache entry

#### Scenario: Identifier is stable across restarts

- **WHEN** the same reach is requested after the service restarts
- **THEN** the identifier is unchanged and the existing cached reach is reused

#### Scenario: Kilometres below source precision

- **WHEN** two requested kilometre values differ by less than the canonical precision
- **THEN** they resolve to the same reach identifier

### Requirement: Reach bounds validation

A reach SHALL be rejected when its start kilometre is not strictly less than its end kilometre, when either value is not finite, or when the requested river is absent from the kilometre-marker source. Rejection SHALL name which condition failed and SHALL list the available rivers when the river is unknown.

#### Scenario: Reversed kilometres

- **WHEN** a reach is requested with start kilometre greater than or equal to the end kilometre
- **THEN** the request is rejected and the message states that an increasing range is required

#### Scenario: Unknown river

- **WHEN** a reach is requested for a river name absent from the marker source
- **THEN** the request is rejected and the available river names are listed

#### Scenario: Endpoint markers missing

- **WHEN** the marker source has no marker at the requested start or end kilometre
- **THEN** the request is rejected and the message identifies the missing endpoint

### Requirement: Centerline follows the mapped river

A reach SHALL resolve to a single continuous centerline along the mapped watercourse between its endpoint kilometres. The centerline SHALL be accepted only when every kilometre marker in range lies within the marker tolerance of it and the markers appear along it in increasing kilometre order.

#### Scenario: Markers out of order

- **WHEN** the resolved path passes the kilometre markers in an order inconsistent with their kilometre values
- **THEN** the reach is rejected rather than returned

#### Scenario: Marker too far from the path

- **WHEN** any kilometre marker in range lies farther from the resolved path than the marker tolerance
- **THEN** the reach is rejected and the maximum marker offset is reported

#### Scenario: Mapped river is disconnected

- **WHEN** no continuous path exists along the mapped watercourse between the endpoint markers
- **THEN** the reach is rejected and the message states that the mapped river is disconnected

### Requirement: A searched centerline's length agrees with chainage

A centerline obtained by searching for a path through mapped watercourse geometry SHALL be accepted only when its length agrees with the requested kilometre span within a declared tolerance. This prevents a path that diverts along a tributary and returns from being accepted as the reach.

#### Scenario: Path diverts along a tributary

- **WHEN** a searched path's length differs from the requested kilometre span by more than the tolerance
- **THEN** the reach is rejected and both the path length and the expected span are reported

#### Scenario: Length within tolerance

- **WHEN** a searched path's length agrees with the requested kilometre span within the tolerance
- **THEN** the reach is accepted and its measured length is recorded

#### Scenario: The gate does not apply to a centerline that cannot divert

- **WHEN** a centerline is constructed directly from the ordered kilometre markers, so no path search occurs
- **THEN** a length shorter than the kilometre span does not reject the reach

### Requirement: Centerline length deviation is always recorded

Every reach SHALL record how far its centerline's length deviates from the requested kilometre span, whatever method produced the centerline. A deviation that is not treated as a failure SHALL still be visible to a consumer.

#### Scenario: Deviation recorded for a searched centerline

- **WHEN** a reach built by searching mapped geometry is accepted
- **THEN** its length deviation from the kilometre span is recorded in its provenance

#### Scenario: Deviation recorded for a marker-constructed centerline

- **WHEN** a reach built directly from the ordered markers is accepted
- **THEN** its length deviation is recorded in its provenance even though it did not gate acceptance

### Requirement: A centerline declares how it was produced

Every reach SHALL record which method produced its centerline, so a consumer can tell a centerline that follows the mapped watercourse from one approximated between markers.

#### Scenario: Marker-constructed centerline is identifiable

- **WHEN** a reach is built directly from the ordered kilometre markers
- **THEN** its provenance states that it does not follow the mapped watercourse

#### Scenario: Searched centerline is identifiable

- **WHEN** a reach is built by searching the mapped watercourse geometry
- **THEN** its provenance states that it follows the mapped watercourse

### Requirement: Reach exposes chainage

An accepted reach SHALL expose a mapping between river kilometre and distance along its centerline, so that consumers needing a position along the river do not re-derive it from the marker source.

#### Scenario: Position along the river

- **WHEN** a consumer requests the centerline position for a kilometre inside the reach
- **THEN** the reach returns the corresponding distance along the centerline

#### Scenario: Kilometre outside the reach

- **WHEN** a consumer requests a position for a kilometre outside the reach bounds
- **THEN** the request is rejected rather than extrapolated

### Requirement: Reach geometry is stored in the project metric CRS

A reach centerline SHALL be computed and stored in the project's metric coordinate reference system. Geographic coordinates SHALL be produced only when publishing to a map client, and SHALL never be read back as the source for a measurement.

#### Scenario: Stored geometry

- **WHEN** a reach is persisted
- **THEN** its centerline is stored in the project metric CRS with that CRS recorded alongside it

#### Scenario: Length and distance measurements

- **WHEN** any length, distance or tolerance for a reach is measured
- **THEN** it is measured in metres in the metric CRS, not in degrees

### Requirement: Reach build status is observable

A reach SHALL report whether it is building, ready or failed, and a failed reach SHALL retain the reason it failed. A reach that is not ready SHALL NOT be served as if it were.

#### Scenario: Build in progress

- **WHEN** a reach is requested while its centerline is still being resolved
- **THEN** its status reports that it is building and no geometry is served

#### Scenario: Failed reach is not retried silently

- **WHEN** a reach failed validation and the same reach is requested again
- **THEN** the recorded failure reason is returned rather than an empty or partial result

### Requirement: Reaches are cached and reusable

An accepted reach SHALL be persisted so that a later request for the same reach reuses it instead of re-resolving the centerline. Each reach SHALL record when it was last used.

#### Scenario: Repeat request

- **WHEN** a reach that is already persisted and ready is requested again
- **THEN** the stored reach is returned without re-resolving the centerline, and its last-used time is updated

#### Scenario: Source data changed

- **WHEN** the kilometre-marker or mapped-river source has changed since a reach was built
- **THEN** that reach is treated as stale and rebuilt rather than served from cache

### Requirement: A fallback reach is configured and pinned

The system SHALL have a fallback reach supplied by configuration rather than hardcoded, used when no reach has been selected. The fallback reach SHALL be pinned so that cache eviction cannot remove it.

#### Scenario: No selection made

- **WHEN** a consumer resolves an AOI and no reach has been selected
- **THEN** the configured fallback reach is used and the response states that the fallback was used

#### Scenario: Eviction cannot remove the fallback

- **WHEN** cache eviction runs and the fallback reach is the least recently used
- **THEN** the fallback reach is retained

#### Scenario: Fallback misconfigured

- **WHEN** the configured fallback reach fails validation at startup
- **THEN** startup fails with the validation reason rather than falling back to an unvalidated area

### Requirement: Unused reaches can be evicted

The system SHALL support removing cached reaches and their derived AOI polygons based on how recently they were used. Eviction SHALL NOT remove a pinned reach, nor a reach that derived data still references.

#### Scenario: Eviction of an unused reach

- **WHEN** eviction runs for reaches unused beyond the requested age
- **THEN** those reaches and their derived AOI polygons are removed and the removed identifiers are reported

#### Scenario: Reach still referenced by derived data

- **WHEN** eviction would remove a reach that derived data rows still reference
- **THEN** that reach is retained and reported as retained with the reason

#### Scenario: Eviction is explicitly invoked

- **WHEN** no eviction request has been made
- **THEN** no cached reach is removed
