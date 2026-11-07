# Spec Delta

## Purpose

Derives a precipitation-based layer from already-ingested weather observations and publishes it as a discoverable map layer, starting with a placeholder transform that validates the derive-and-publish path end to end.

## ADDED Requirements

### Requirement: Derived precipitation values are computed from stored observations
The system SHALL compute a derived value for every stored precipitation observation (variable `sum_nn050`) by applying a transform function to its `value`, and persist the result together with its station's geometry in a dedicated table.

#### Scenario: Observations exist
- **WHEN** the precipitation-derivation task is triggered against a database containing `weather_observations` rows for variable `sum_nn050`
- **THEN** every such row has a corresponding row in the derived table, with the derived value equal to the transform applied to the original value, and the originating station's geometry attached

#### Scenario: No observations exist
- **WHEN** the precipitation-derivation task is triggered against a database with zero `sum_nn050` observations
- **THEN** the task completes without error and the derived table ends up empty

### Requirement: Derived layer is published and discoverable without frontend changes
The system SHALL publish the derived precipitation table as a GeoServer vector layer in the `input_layers` workspace, so that it becomes discoverable through the existing Terria catalog endpoint with no change to frontend code.

#### Scenario: Layer appears in the Terria catalog after a run
- **WHEN** the derived table has been populated and the task has run to completion
- **THEN** `GET /terria-catalog.json` includes a catalog entry for the derived layer under the `input_layers` group

#### Scenario: Re-running the task is safe
- **WHEN** the task is triggered again after the derived layer is already registered in GeoServer
- **THEN** the task does not fail, and the existing layer registration remains valid and queryable

### Requirement: Task runs over all stored data, not a selected area or schedule
The task SHALL be invoked manually and SHALL process all currently stored precipitation observations, without filtering by an area of interest and without any automatic scheduling.

#### Scenario: Manual trigger processes all precipitation observations
- **WHEN** an operator invokes the task by name with no area-of-interest or schedule argument
- **THEN** every `sum_nn050` observation currently in `weather_observations` is considered for derivation
