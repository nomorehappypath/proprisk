# Release notes

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
