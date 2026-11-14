# Limits

Stated plainly, because an unstated limit is a claim. Referenced from the [README](../README.md).

- **The matching numbers are unmeasured.** `GET /reports/matching` computes the auto-match rate and
  suggestion accuracy from an institution's own recorded decisions, and `tools/tune_weights.py` tunes the
  weights against them with 5-fold cross-validation, refusing to propose anything from fewer than 50
  decisions. No real institution has used this, so there is no real-world rate to quote and none is
  invented here. The current weights are informed guesses; `tools/pilot.py` is the path to a measured one.
- **The bank adapters are written against published export layouts**, and the golden statements in
  `tests/golden/` are synthetic. Each should be re-checked against a real anonymised export before it is
  trusted.
- **SBI and Canara print UPI IDs truncated or masked, and Axis prints none**, so for those banks matching
  relies on reference codes or review. A truncated ID is never stored as if it were the payer's.
- **A change to a statement file's very first line** is visible only against the previous statement, not
  within the file. There is a test that says so.
- **A single upload must fit in memory** for the grouping pass (10 MB cap at the API).
- **No web UI.** The API and `/docs` are the interface.
