# Instructions for AI build tools

PropRisk checks land for risk before you buy it (Texas has the most sources; other states run the nationwide ones plus any county files). Read `README.md` first.

- **To add or upgrade a county, follow `docs/EXTENDING.md`.** A county is one data
  file in `backend/app/regions/<state>/counties/`. Never edit Python to add a
  county.
- Prove every county change with `scripts/check_region.sh <state> <county>`; it must
  end with PASS. Put its output in the pull request.
- Any other change: see `docs/MAINTENANCE.md`, and run `scripts/test.sh`.
- How to send a change back: `CONTRIBUTING.md`.
