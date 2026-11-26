# Spec Delta

## Purpose

Derives area-of-interest polygons from a reach through interchangeable named variants, so that a corridor, a bounding box, a watershed selection or a flood footprint are all obtained the same way. Covers how variants are produced, cached, invalidated, and how an unavailable variant degrades visibly instead of silently.

## ADDED Requirements

### Requirement: Variants share one interface

Every AOI variant SHALL be obtainable through the same call shape, taking a reach and the variant's parameters and returning a single polygon. A consumer SHALL NOT need to know whether a variant is computed from the reach or selected from an external layer.

#### Scenario: Computed and lookup variants are interchangeable

- **WHEN** a consumer requests a computed variant and then a lookup variant for the same reach
- **THEN** both return a single polygon through the same interface, with the same accompanying fields

#### Scenario: Unknown variant name

- **WHEN** a variant is requested by a name that is not registered
- **THEN** the request is rejected and the registered variant names are listed

### Requirement: Variant geometry is a single polygon in the metric CRS

A variant SHALL return exactly one polygonal geometry in the project's metric coordinate reference system. Where a variant derives from several source features, those features SHALL be dissolved into one geometry before being returned.

#### Scenario: Multiple source features

- **WHEN** a lookup variant selects several intersecting features from its source layer
- **THEN** they are dissolved into one geometry and returned as a single polygon

#### Scenario: Geometry CRS

- **WHEN** any variant returns a polygon
- **THEN** the polygon is in the project metric CRS and all its parameters were applied in metres

### Requirement: Variant results carry provenance

Every variant result SHALL record the variant name, the parameters used, the reach it was derived from, and an identifier of the source data it was built from. This record SHALL be persisted with the geometry.

#### Scenario: Provenance accompanies the geometry

- **WHEN** a variant is built and persisted
- **THEN** its variant name, parameters, reach identifier and source identifier are stored with the geometry

#### Scenario: Reproducing a stored variant

- **WHEN** a stored variant's recorded parameters and source are reapplied to its recorded reach
- **THEN** the same geometry is produced

### Requirement: Computed variants are derived only from the reach

Variants computed from the reach — a corridor of given half-width, a bounding box of given margin, and a buffer of given distance — SHALL depend on nothing but the reach centerline and their own parameters, and SHALL always be available.

#### Scenario: Corridor ends at the reach endpoints

- **WHEN** a corridor variant is built for a reach
- **THEN** the corridor extends the requested half-width to each side of the centerline and terminates at the reach endpoints without extending beyond them

#### Scenario: Computed variant needs no external data

- **WHEN** a computed variant is requested and no external source layers are reachable
- **THEN** the variant is still produced successfully

#### Scenario: Invalid parameter

- **WHEN** a computed variant is requested with a non-positive or non-finite distance parameter
- **THEN** the request is rejected rather than producing a degenerate polygon

### Requirement: Lookup variants derive from an external source

Variants that depend on externally supplied data — watershed areas, the HQ100 flood footprint, and extreme-flood extents — SHALL derive one geometry from that source for the reach, excluding areas unrelated to the reach. Their source layer SHALL be supplied by configuration rather than hardcoded.

#### Scenario: Areas unrelated to the reach are excluded

- **WHEN** a lookup variant is built for a reach
- **THEN** the result covers only the part of the source related to that reach, and areas elsewhere in the source layer are excluded

#### Scenario: Source layer relocated

- **WHEN** a lookup variant's source layer path changes in configuration
- **THEN** the variant builds from the new location without a code change

#### Scenario: Source present but yields nothing

- **WHEN** a lookup variant's source layer is available but contains nothing related to the reach
- **THEN** the variant reports an empty result rather than returning an empty polygon as valid

### Requirement: A lookup variant declares its selection mode

Each lookup variant SHALL declare whether it takes whole source features that relate to the reach, or restricts the source to the reach's own neighbourhood. A variant whose source is one continuous extent spanning unrelated areas SHALL NOT take whole features, because feature boundaries then carry no information about which areas relate to the reach.

#### Scenario: Whole features where a feature is a meaningful unit

- **WHEN** a variant whose source features are discrete units related to the reach is built
- **THEN** each related feature is included in full rather than cut at the reach's neighbourhood

#### Scenario: Restricted where the source is one continuous extent

- **WHEN** a variant whose source is a single extent spanning many unrelated areas is built
- **THEN** the result is restricted to the reach's neighbourhood rather than returning the whole extent

#### Scenario: Mode is declared, not inferred per request

- **WHEN** a lookup variant is registered
- **THEN** its selection mode is part of its declaration and does not vary between requests for different reaches

### Requirement: Invalid source geometry is repaired after restriction

A lookup variant SHALL tolerate invalid geometry in an external source by repairing it, and SHALL repair only the geometry it retains rather than the whole source layer.

#### Scenario: Invalid source geometry

- **WHEN** a lookup variant's source layer contains invalid geometry overlapping the reach
- **THEN** the variant produces a valid result rather than failing or returning invalid geometry

#### Scenario: Repair follows restriction

- **WHEN** a variant restricts a large source extent to the reach's neighbourhood and that extent is invalid
- **THEN** the repair is applied to the restricted geometry, not to the full source extent

### Requirement: Variant availability is declared

Each variant SHALL declare whether its source data is available. A variant whose source has not been supplied SHALL report itself unavailable rather than failing only at the moment it is first built.

#### Scenario: Listing variants

- **WHEN** the available variants are listed
- **THEN** each is reported as available or unavailable, with the reason for any that are unavailable

#### Scenario: Source supplied later

- **WHEN** a previously unavailable variant's source layer becomes available
- **THEN** the variant reports itself available without a code change

### Requirement: An unavailable variant degrades to a declared substitute

When a requested variant is unavailable, the system SHALL substitute that variant's configured fallback rather than failing, and SHALL mark the result as degraded. A variant without a configured fallback SHALL fail rather than substitute an undeclared area.

#### Scenario: Watershed source not yet supplied

- **WHEN** the watershed variant is requested while its source layer has not been supplied
- **THEN** its configured fallback is used and the result is marked degraded with the reason

#### Scenario: No fallback configured

- **WHEN** an unavailable variant has no configured fallback
- **THEN** the request fails with the reason rather than substituting any other variant

#### Scenario: Degradation is never silent

- **WHEN** any degraded result is returned
- **THEN** it carries the requested variant, the variant actually used, a degraded marker and the reason

### Requirement: Variants are cached per reach

A built variant SHALL be persisted against its reach and variant key, so that repeat requests reuse it. Each cached variant SHALL record when it was last used.

#### Scenario: Repeat request for the same variant

- **WHEN** a variant already cached for a reach is requested again
- **THEN** the cached geometry is returned without rebuilding, and its last-used time is updated

#### Scenario: Same variant, different parameters

- **WHEN** a variant is requested with parameters differing from a cached entry
- **THEN** it is treated as a distinct variant key and built rather than served from the existing entry

### Requirement: Cached variants are invalidated when their source changes

A cached variant SHALL be invalidated when the source data it was built from has changed. A changed external layer SHALL NOT continue to be served from an entry built against the previous version.

#### Scenario: External flood layer republished

- **WHEN** a lookup variant's source layer changes and that variant is requested
- **THEN** the cached entry is treated as stale and the variant is rebuilt from the new source

#### Scenario: Source unchanged

- **WHEN** a cached variant's source data is unchanged
- **THEN** the cached geometry is served without rebuilding

#### Scenario: Degraded entry is reconsidered

- **WHEN** a variant cached as degraded is requested after its real source becomes available
- **THEN** the degraded entry is not served and the variant is rebuilt from the real source
