# Rail fixture

`luxembourg-rail.osm.pbf` (365 KB) is a real rail-only extract, standing in for
what Phase 1 of issue #345 publishes per region. Luxembourg is the smallest
European country whose data exercises all three lookups at once: 1,167 railway
ways, 989 further ways carried because a relation references them, 109 route
relations, 101 nodes carrying a `uic_ref` — 66 of them stations — and 8 pairs of
UIC codes that share a relation, which is what strategy A asks about.

It was cut from `https://download.geofabrik.de/europe/luxembourg-latest.osm.pbf`
(2026-09-05) with the selection table in `docs/plans/LOCAL_RAIL_DATA_PLAN.md`, i.e.
what `osmium tags-filter` produces for

    w/railway=rail,narrow_gauge,light_rail  (without a service tag)
    r/route=train,railway,light_rail
    n/uic_ref
    nwr/railway=station,halt + uic_ref

plus the ways those relations reference. Note what the country does *not*
contain: no station is mapped as a way or a relation here, and 8,744 of the
10,669 ways its route relations reference lie across a border and so cannot be
in a Luxembourg extract at all. `tests/test_rail_store.py` covers those shapes
with a synthetic extract it writes itself.

Refresh this file only if the selection changes: a bigger or newer fixture makes
the tests slower without testing anything more, and the counts asserted in
`test_store_holds_the_three_kinds_of_data_the_resolver_asks_for` will move.

## `issue-359-relations.json`

The two route relations behind issue #359 — Paris Gare de l'Est → Strasbourg
resolving 13.9 km south of the station — in the `out geom` shape both sources
return:

* **5920986** `TGV InOui 661A / ICE 83 : Stuttgart → … → Paris Est`, strategy
  A's `elements[0]` for that UIC pair, which the resolver reached for first and
  threw away because a `railway=platform` member captured the endpoint snap;
* **5935098** `TGV InOui 061C : Strasbourg → Aéroport Charles de Gaulle`, what
  shipped instead — a perfectly connected route that does not call at Gare de
  l'Est.

Cut from the `europe/france` store built from the `rail-data-2026-09-08`
extract, keeping every member way the store holds, its role, and every twelfth
vertex plus both ends of each way. Thinning inside a way is safe because
`_build_rail_graph` joins ways on shared *end* vertices, and both ends are
kept — 261 KB against the 6 MB the untrimmed pair costs.

Luxembourg cannot stand in for this: no station in that extract is mapped with
a platform way, which is the entire failure. Do not regenerate this without
re-reading `test_the_fixture_still_contains_the_trap` — it asserts the two
properties that make the file a test rather than a decoration, and a fixture
that loses them would let the regression back in with a green suite.

## `issue-363-montparnasse.json`

Relation **5928800**, `TGV 405 : Paris -- Angoulême -- Bordeaux -- Arcachon` —
one of the three candidates behind issue #363, where Paris Montparnasse →
Bordeaux resolves onto the right line and starts 3.5 km late.

Nothing #359 fixed applies here, which is why France needs a second fixture:
this relation holds every member way (`missing_members = 0`) and has no
platform member. Its graph is in pieces because OSM's own membership does not
join the Montparnasse station throat to the main line, and Overpass returns the
same gap. What closes it is in the file too — the relation's node members, 16
of them, all located, including node **65331500** (`uic_ref` 8739100, Paris
Montparnasse). Those are what `RailStore.relation_geometry` started emitting in
#363, in the shape Overpass's `out geom` has always returned them in.

Cut from the same `europe/france` store built from the `rail-data-2026-09-08`
extract, keeping every member way with its role and every node member whole.
Inside a way it keeps every fortieth vertex, both ends, **and every vertex
another member way also touches** — 297 KB against the 6 MB the untrimmed
relation costs. That last rule is not in the #359 recipe above and is needed
here: thinning on ends alone shattered this graph into 25 components instead of
4, because a route relation does join mid-way at junctions. With the junctions
kept, the number the tests turn on survives exactly — the throat island still
stops **100.24 m** short of the main line.

What the trim does not preserve is the component sizes the issue quotes
(20745 / 322 / 84 / 4 become 2226 / 57 / 3) or the full 532 km, which measures
531 km here. Assert on the gap, the endpoint offsets and the stop node, never
on those.

Regenerating it is a `RailStore.relation_geometry([5928800])` away, but read
`test_the_fixture_still_contains_the_trap` in `tests/test_rail_issue_363.py`
first: it pins the facts that make this a test — no platform member, no missing
member, a graph in pieces with the throat stranded, and a located stop node
naming Montparnasse. A fixture that loses any of them would let the regression
back in with a green suite.
