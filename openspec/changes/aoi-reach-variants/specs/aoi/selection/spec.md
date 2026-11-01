# Spec Delta

## Purpose

Lets a user choose the area of interest from the map client by typing a river and a kilometre range, see whether the resulting area is usable, and see when the area served is a degraded substitute for the one requested.

## ADDED Requirements

### Requirement: A reach can be selected from the map client

The map client SHALL offer an input for river, start kilometre and end kilometre, and submitting it SHALL start building the corresponding reach and its AOI variants. The user SHALL NOT need to edit a URL, a configuration file or a geometry file to change the area of interest.

#### Scenario: Selecting a reach

- **WHEN** a user enters a river and a kilometre range in the map client and submits it
- **THEN** the reach and its AOI variants begin building and the user is shown that work has started

#### Scenario: Selecting an already cached reach

- **WHEN** a user selects a reach that is already built and current
- **THEN** the cached reach is used and its AOI is available without rebuilding

#### Scenario: Form is discoverable

- **WHEN** a user opens the map client
- **THEN** the reach selection input is reachable without external instructions

### Requirement: Selection inputs are validated before work starts

The selection SHALL be validated before any build begins, and an invalid selection SHALL be refused with a message naming the problem. An invalid selection SHALL NOT replace the currently served area of interest.

#### Scenario: Reversed kilometre range

- **WHEN** a user submits a start kilometre not less than the end kilometre
- **THEN** the selection is refused with a message naming the problem and the current area of interest is unchanged

#### Scenario: River not available

- **WHEN** a user submits a river that the kilometre-marker source does not contain
- **THEN** the selection is refused and the available rivers are offered

#### Scenario: Kilometres outside the river's range

- **WHEN** a user submits a kilometre range that the selected river's markers do not cover
- **THEN** the selection is refused and the covered range is reported

### Requirement: Build progress and failure are visible

A user who has submitted a selection SHALL be able to see whether the reach is still building, succeeded, or failed, and a failure SHALL show the validating reason rather than a generic error.

#### Scenario: Build in progress

- **WHEN** a user checks a submitted selection that is still building
- **THEN** the client reports that it is still building

#### Scenario: Validation failure after submission

- **WHEN** a reach fails because its markers do not follow the mapped river in order
- **THEN** the client reports that specific reason

#### Scenario: Build succeeded

- **WHEN** a reach finishes building successfully
- **THEN** the client reports success and the reach's AOI layers become available to display

### Requirement: The served area of interest is displayable

The AOI variants for the selected reach SHALL be available as map layers, so a user can see the area their data is being clipped to rather than inferring it.

#### Scenario: Viewing the area of interest

- **WHEN** a reach has been built
- **THEN** its AOI variants are available as layers in the map client

#### Scenario: Reach centerline is viewable

- **WHEN** a user displays the selected reach
- **THEN** the validated centerline is available alongside the AOI polygons

### Requirement: Degraded areas are flagged in the client

Where a dataset's AOI is a degraded substitute for the one requested, the map client SHALL show that, naming the requested and the substituted area. An unflagged layer SHALL mean the requested area was used.

#### Scenario: Watershed substituted for a dataset

- **WHEN** a dataset bound to the watershed variant is served using its fallback
- **THEN** that dataset's layer is flagged in the client with both the requested and the substituted variant

#### Scenario: No degradation

- **WHEN** every dataset's requested variant was available
- **THEN** no degradation flag is shown

#### Scenario: Flag persists while the substitution lasts

- **WHEN** a user reopens the map client while a dataset is still served from a substituted area
- **THEN** the flag is shown again rather than only at the moment of building

### Requirement: Selection is available without a reach already chosen

The map client SHALL present a usable area of interest before any selection has been made in the current deployment, using the configured fallback reach, and SHALL indicate that the fallback is in use.

#### Scenario: First use of a deployment

- **WHEN** a user opens the map client and no reach has been selected
- **THEN** the configured fallback reach is served and the client indicates that it is the fallback

#### Scenario: Selecting after the fallback

- **WHEN** a user selects a reach while the fallback is being served
- **THEN** the selected reach replaces the fallback once it is built
