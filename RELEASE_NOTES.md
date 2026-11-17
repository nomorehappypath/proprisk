# Release notes

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
