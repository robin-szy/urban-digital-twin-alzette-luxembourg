# eddie_aoi

The area-of-interest module for the Alzette digital twin.

A **reach** — a river with a start and end kilometre — is the project's single
AOI identity. A reach is *one-dimensional*: a validated centerline with
chainage, not a polygon. Every AOI polygon is derived from a reach by a named
**variant**, and every dataset is bound to the variant it must be processed
against.

```
   river + km_start + km_end
             |
             v
      reach  (centerline + chainage, validated)
             |
             v
      variants  (corridor / bbox / buffer / watersheds / hq100 ...)
             |
             v
      resolver  (dataset -> variant)
```

## The vendored routing

`eddie_aoi/routing.py` contains a **byte-identical copy** of `select_route`
from `users/devika/src/river_aoi.py` (commit `fa5d58b`, authored by Devika),
plus `PreparationError` from `users/devika/src/landuse.py` in
`eddie_aoi/errors.py`, and the chainage-length check extracted from that same
module's `build_corridor`.

### Why it is copied rather than imported

`users/devika/` is not an installable package. It has no `pyproject.toml` or
`setup.py`, and its own scripts reach it like this:

```python
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from devika.src.river_aoi import build_corridor
```

That resolves only because `devika` and `devika.src` happen to work as
implicit namespace packages, with `ROOT` pointing at `users/`. It will not
survive running inside the EDDIE containers, and `users/` also contains a
directory with a space in its name.

The alternatives were considered and rejected in
`openspec/changes/aoi-reach-variants/design.md`: adding packaging metadata
under `users/devika/` restructures a teammate's directory, which is a
conversation to have rather than a change to make unilaterally; rewriting the
routing would discard the only validated river geometry in the repository.

### Do not weaken the validation

The vendored routing is the only thing in this repository that prevents an AOI
being built around the wrong watercourse. Four invariants matter:

| Invariant | What it catches |
| --- | --- |
| Every marker within `marker_tolerance` (20 m) of the route | a path along a parallel or neighbouring watercourse |
| Markers in strictly increasing chainage order along the route | a path that runs the wrong way or doubles back |
| Bridging limited to `join_tolerance` (1 µm) | a real gap in the mapped river being silently joined |
| Route length within 2% of the chainage span | a detour up a tributary and back, which the ordering check alone cannot see |

These are pinned by `tests/test_routing_invariants.py`. That file includes
**mutation checks** which delete a guard from the source, rebuild the function
and assert the bad input is then accepted — so deleting a guard for real makes
the suite fail rather than quietly pass.
`tests/test_vendored_unchanged.py` additionally fails if the copy drifts from
the original at all.

Changes belong upstream in Devika's module, after which this copy should be
refreshed.

### Follow-up: retire this copy

Keeping two copies of the routing is a deliberate, temporary trade-off. The
consolidation — deciding whether Devika's `build_corridor` or this module's
`corridor` variant survives, and retiring the duplicate — is recorded as a
follow-up in `openspec/changes/aoi-reach-variants/tasks.md` and needs a
conversation with Devika rather than a unilateral rewrite.

## Tests

```bash
pytest eddie_aoi            # unit tests
pytest eddie_aoi -m integration   # additionally needs a running PostGIS
```

Routing is tested against synthetic rivers and markers rather than a real
extract, because each fixture must violate exactly one invariant — which a
real extract cannot be made to do.
