# Release notes

## Unreleased: every card for Denver, from Colorado's own sources (Phase C2)

- **A Denver lookup now fills every card a Texas lookup shows**, from Colorado's and
  Denver's own public data:
  - **Roads:** traffic counts (federal highway data for Colorado, and Denver regional
    counts), traffic history (CDOT, 1997–2025), roads, bridges with CDOT's condition
    ratings and posted weight limits, and road projects (CDOT).
  - **Denver:** zoning, zoning standards (written from the Denver Zoning Code),
    future land use (Blueprint Denver), rezonings, site plans and permits, historic
    districts and landmarks, and RTD transit stops and stations.
  - **Colorado:** water (Denver Water), sewer (Metro Water Recovery and the city's
    sewer mains), water, sanitation and metro districts (DOLA), schools (CDE),
    property tax levies (DOLA), state storage-tank and cleanup records (OPS and
    CDPHE), and 2022 aerial photos (DRCOG).
- **Each card names its own agency** ("Schools (CDE)", "Road Projects (CDOT)"), and
  says plainly what that agency does not publish, such as project cost (shown as the
  published range), contract awards, or whether a bridge is weight-posted.
- **The Edwards Aquifer row says "does not apply in Colorado"**, since that aquifer
  is in Texas only.
- **Any state or county can now connect its own sources, card by card.**
  `docs/EXTENDING.md`, Job D, is the step-by-step recipe, and
  `scripts/check_region.sh` now prints one line per connected source.
- **Texas lookups are unchanged**, except one display fix everywhere: years in the
  traffic and development tables no longer show a comma ("2026", not "2,026").
- **Updating:** run `scripts/stop.sh`, update, then `scripts/launch.sh`.

## Unreleased: states (Phase C), with Denver, Colorado

- **PropRisk now knows which state an address is in.** Put the state in the address
  ("Denver, CO 80202"). With no state named, it still assumes Texas, as before.
- **Denver County, Colorado** is the first county outside Texas: its city and county
  boundary, parcels, metropolitan districts, fire stations, subdivisions and clerk
  records page, plus the nationwide sources and Colorado census figures.
- **Outside Texas, Texas-only sources are switched off** and their cards say
  "Not connected for <State> yet", never "nothing found". The environmental card
  shows only the federal EPA check and says state records are not connected.
- **If the address and the map disagree on the state**, the lookup stops and asks
  you to check the address.
- **Settings → "Where PropRisk works"** now has a State column.
- **Updating from an earlier version:** run `scripts/stop.sh`, update, then
  `scripts/launch.sh`. The launch upgrades your saved lookups (all kept, marked as
  Texas). If PropRisk is still running, the launch asks you to stop it first.

## Unreleased: Disclaimer page

- **New "Disclaimer" link in the top bar.** It says plainly that PropRisk is a free,
  open-source project built to show that NoMoreHappyPath can build commercial-grade
  software. It comes with no warranty, its makers bear no responsibility for bugs or
  errors, and you use it at your own risk.

## Unreleased: Comal County, and how to add counties with NoMoreHappyPath

- **Comal County, Texas (Partial).** Its parcels, school and emergency-service
  districts, fire stations and subdivisions now come from Comal's own public maps.
  NoMoreHappyPath added it by following `docs/EXTENDING.md`, with no code change.
- **The README now explains, step by step, how to add a county** (or upgrade one to
  Full) with NoMoreHappyPath, and says plainly that adding a new state needs a later
  release.

## Unreleased: add your county (Phase B)

- **New licence: Apache License 2.0** (was AGPL-3.0). You may use, change and share
  PropRisk, including commercially, as long as you keep the licence and notice. This is
  the same licence as NoMoreHappyPath.
- **Anyone can add a county.** `docs/EXTENDING.md` is the step-by-step recipe, and
  `CONTRIBUTING.md` explains how to send it back. They are written so an AI build tool
  (NoMoreHappyPath or another) can do it for you, with no programming.
- **One command proves a county works:** `scripts/check_region.sh tx <county>`. It
  checks the file, asks each of the county's map services about a real address, runs a
  real lookup, and says PASS or FAIL in plain lines. It uses none of your keys and
  spends no AI money.
- **More honest district answers.** When a county's district map is down, the district
  card now says the answer is partial instead of showing it as complete.
- **New counties are careful by default.** For a newly added county, a point outside
  every city and ETJ it knows is reported as "not confirmed" unless the county file says
  its maps list every city. The seven existing counties are unchanged.

## Unreleased: counties live in data files (Phase A)

- Each county PropRisk covers is now one plain file in `backend/app/regions/tx/counties/`,
  and Texas itself is `backend/app/regions/tx/state.yaml`. This is the first step toward
  adding counties and states (see `docs/specs/add_counties_and_states.md`).
- Lookups give the same results as before: checked against the old code on a real
  address in each of the seven counties.
- Settings → "Where PropRisk works" now lists Full counties first, then Partial ones,
  each in alphabetical order.
- If a county file has a mistake, PropRisk does not start and says which file and which
  line to fix.
