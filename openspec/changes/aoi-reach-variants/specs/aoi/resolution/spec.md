# Spec Delta

## Purpose

Decides which area-of-interest each dataset is processed against, from one declared registry instead of constants scattered across plugins. Keeps the three distinct uses of an AOI separate — what to fetch, what to clip to, and what a statistic is measured over — and keeps derived data attributable to the area it was computed over.

## ADDED Requirements

### Requirement: Dataset to variant bindings are declared in one place

The AOI used for each dataset SHALL be declared in a single registry of dataset-to-variant bindings. A dataset SHALL NOT determine its own AOI through a locally defined constant, table name or distance.

#### Scenario: Resolving a dataset's AOI

- **WHEN** a dataset's AOI is resolved for a reach
- **THEN** the variant named in the registry for that dataset is returned

#### Scenario: Unregistered dataset

- **WHEN** an AOI is resolved for a dataset with no binding in the registry
- **THEN** the request is rejected and the registered dataset names are listed, rather than a default area being supplied

#### Scenario: Changing a dataset's AOI

- **WHEN** a dataset needs a different AOI
- **THEN** the change is made in the registry binding and requires no edit inside that dataset's processing code

### Requirement: Resolution reports what was actually used

Resolving a dataset's AOI SHALL return the geometry together with the reach, the variant requested, the variant actually used, whether the result is degraded, and the reason for any degradation.

#### Scenario: Normal resolution

- **WHEN** a dataset's bound variant is available
- **THEN** the result reports the requested and actual variant as the same and is not marked degraded

#### Scenario: Degraded resolution

- **WHEN** a dataset's bound variant is unavailable and its fallback is substituted
- **THEN** the result names both variants, is marked degraded, and carries the reason

### Requirement: Fetch envelope is distinct from the clip mask

The system SHALL provide a fetch envelope — a rectangular area covering the AOIs needed for a reach — for requesting data from sources that accept only rectangular bounds. The fetch envelope SHALL NOT be used as a clip mask or as the domain of a statistic.

#### Scenario: Requesting data from a bounds-only source

- **WHEN** data is requested from a source that accepts only a rectangular bound
- **THEN** the fetch envelope is used for the request

#### Scenario: Clipping after fetching

- **WHEN** data fetched using the envelope is stored
- **THEN** it is clipped to the dataset's bound variant, not left at the envelope extent

#### Scenario: Envelope is not an analysis domain

- **WHEN** a statistic is computed for a dataset
- **THEN** the dataset's bound variant is its domain and the fetch envelope is not used as the denominator

### Requirement: A statistic declares the single area it is measured over

Each statistic SHALL declare the one AOI it is measured over, and that area SHALL be reported with the result. Statistics measured over different areas SHALL NOT be presented as comparable.

#### Scenario: Statistic reports its domain

- **WHEN** a statistic over an AOI is produced
- **THEN** the reach and variant it was measured over are reported with it

#### Scenario: Statistics over different areas

- **WHEN** two statistics measured over different variants are returned together
- **THEN** each reports its own area and they are not combined into a single ratio

### Requirement: A measured layer is not its own analysis domain

A variant derived from a dataset SHALL NOT be used as the analysis domain for a statistic that measures that same dataset, because the resulting proportion is unity by construction.

#### Scenario: Flood extent as its own denominator

- **WHEN** a statistic measuring the proportion of an area covered by the flood footprint is bound to the flood-footprint variant as its domain
- **THEN** the binding is rejected as invalid

#### Scenario: Flood extent as another dataset's AOI

- **WHEN** a dataset that is not measuring the flood footprint is bound to the flood-footprint variant
- **THEN** the binding is accepted

### Requirement: Raster datasets separate build extent from publish extent

A dataset whose processing depends on terrain beyond the AOI SHALL declare a build extent and a publish extent separately. Its build extent SHALL NOT be narrowed to an AOI that would omit terrain its computation depends on.

#### Scenario: Terrain analysis build extent

- **WHEN** an elevation dataset whose analysis depends on upstream contributing area is resolved for building
- **THEN** an extent that retains that contributing area is returned rather than the narrow corridor

#### Scenario: Publishing the same dataset

- **WHEN** that dataset's outputs are published
- **THEN** they are clipped to its publish variant

#### Scenario: Narrowing a build extent is refused

- **WHEN** a dataset declaring a dependence on surrounding terrain is bound to a build extent that omits it
- **THEN** the binding is rejected

### Requirement: Derived data records the area it was computed over

Every derived dataset row SHALL record the reach and variant it was computed over, and whether that resolution was degraded. Derived data without this attribution SHALL NOT be treated as belonging to any reach.

#### Scenario: Writing derived data

- **WHEN** a derived dataset row is written
- **THEN** it records the reach identifier, the variant used, and whether the resolution was degraded

#### Scenario: Degraded data remains identifiable after the fact

- **WHEN** derived data produced during a degraded resolution is read later
- **THEN** the degraded marker is still present on those rows even though the frontend warning is no longer displayed

#### Scenario: Pre-existing untagged data

- **WHEN** derived data exists without a reach attribution
- **THEN** it is not served as belonging to the current reach until it has been attributed

### Requirement: Cache reuse checks match the reach

A check for whether derived data already exists SHALL match on the reach and variant as well as on the dataset's own keys. Data computed over a different area SHALL NOT satisfy such a check.

#### Scenario: Same period, different reach

- **WHEN** derived data is requested for a time period already computed for a different reach
- **THEN** the check reports it as not available for the current reach and the computation runs

#### Scenario: Same period, same reach

- **WHEN** derived data is requested for a time period already computed for the current reach and variant
- **THEN** the check reports it as available and the computation is skipped

#### Scenario: Same reach, previously degraded

- **WHEN** derived data for the current reach was computed during a degraded resolution and the real source is now available
- **THEN** the check does not report it as available and the computation runs again
