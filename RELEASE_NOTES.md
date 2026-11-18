# Release notes

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
