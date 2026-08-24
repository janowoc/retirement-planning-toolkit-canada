# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A config-driven Canadian retirement planning toolkit: one JSON config feeds a
tax engine, an Excel model builder, a Monte Carlo simulation, an HTML dashboard,
and an employer-stock health monitor. Bilingual (EN / Canadian French) in both
product output and documentation.

## Commands

```bash
python3 setup.py --yes --core-only          # bootstrap deps (skip yfinance/edgartools)
python3 -m pip install -r requirements.txt -r requirements-dev.txt

python3 -m pytest tests/ -q                 # full suite (seconds)
python3 -m pytest tests/test_tax_ca.py -q   # one file
python3 -m pytest tests/test_tax_ca.py::test_ontario_marginal_rates -q       # one test
python3 -m pytest tests/test_i18n.py tests/test_docs_parity.py -q            # translation drift only

python3 engine/build_model.py               # -> model/financial_plan.xlsx
python3 engine/quarterly_update.py --sims 2000   # rebuild + Monte Carlo + dashboard
python3 engine/company_health.py            # live market data (network; tolerated to fail in CI)
python3 engine/config_loader.py             # print which config resolved + derived totals

RPT_CONFIG=config/examples/gagnon_config.json python3 engine/build_model.py   # Quebec demo

python3 -m pip install -r requirements-docs.txt && mkdocs serve   # docs site preview
mkdocs build --strict                       # what the Docs workflow runs
```

CI (`.github/workflows/ci.yml`) runs the suite **and** the demo end-to-end on
Python 3.9–3.12. Keep the code 3.9-compatible (no `match`, no `X | Y` unions).

## Architecture

**Config is the only source of truth.** `engine/config_loader.py` is the single
load point; nothing in `engine/` may hardcode a name, balance, ticker, or path.
Resolution order: explicit path → `$RPT_CONFIG` → `config/config.json` (real,
git-ignored) → `config/examples/tremblay_config.json` (demo fallback). Loading
sets `_resolved_path` and `_is_demo` on the dict and runs `validate_config`,
which collects *all* shape errors and raises one `ConfigError`. Derived math
(`investable_total`, `employer_concentration_pct`, …) lives here too — don't
recompute it in callers. `investable_total` deliberately excludes RESP accounts.

**`engine/` is not a package.** Each entry point does
`sys.path.insert(0, <engine dir>)` and then imports siblings flat
(`import tax_ca`, `from i18n import t`). `tests/conftest.py` does the same. Keep
that pattern when adding a module; don't introduce relative imports.

**Data flow.** `config → build_model.build(cfg) → openpyxl Workbook` (8 tabs);
`quarterly_update.py` orchestrates: overlay quarterly input JSON → rebuild
workbook → `monte_carlo()` → `stamp_mc()` writes results back into the Monte
Carlo tab → `refresh_dashboard.render()` emits a self-contained HTML file.

**Tax vs. optimizer split.** `engine/tax_ca.py` is pure tax math — federal +
all 13 province/territory bracket tables, BPA phase-downs, age/pension credits,
Quebec's 16.5% abatement and HSF, `oas_clawback()`, `marginal_rate()`. The
RRSP-meltdown *simulation and grid-search optimizer* live in `build_model.py`
(`_simulate_meltdown`, `_optimize_meltdown`, `build_meltdown`) and call into
`tax_ca`. An unrecognized province code falls back to Ontario with a warning.

**Spreadsheet conventions.** Sheet *names* stay English (they are stable
identifiers — `stamp_mc` and the tests key off them); only cell content is
localized. The `Assumptions` tab is the in-workbook source of truth: other tabs
link to it with cross-sheet formulas rather than repeating values. Font color
encodes provenance — green = cross-sheet link, black = intra-sheet formula,
blue = hardcoded input.

## Bilingual invariants (CI-enforced)

- UI strings are data in `engine/locales/en.json` / `fr.json`, never literals in
  code. `tests/test_i18n.py` fails on any key present in one file but not the
  other, in either direction. Add keys to both.
- Every English doc in `test_docs_parity.py::EN_DOCS` needs a `.fr.md`
  counterpart with a heading count within ±1. Translate in the same change.
- French is Canadian French; `TRANSLATIONS.md` holds the term mapping
  (RRSP=REER, TFSA=CELI, RRIF=FERR, CPP=RPC, OAS=SV, QPP=RRQ, …).
- Language comes from the top-level `"language"` key in config; missing keys
  fall back to English, then to the key itself, so nothing crashes.

## Domain rules

- **Never commit personal data.** Real numbers belong only in
  `config/config.json` and generated `model/` + `dashboard/` artifacts — all
  git-ignored. Check `git status` before committing.
- **Tax/benefit figures must be sourced and dated.** Change a number in
  `tax_ca.py` or a config, and you also update the matching test assertion and
  cite the year + source in `docs/CANADA_RULES.md` (and its `.fr.md`).
- **Determinism.** The Monte Carlo is seeded (`seed=42`) and tests assert exact
  success rates. Don't add unseeded randomness to the engine.
- **No network in tests.** The company-health monitor is exercised separately
  and allowed to fail; it uses yfinance + SEC EDGAR (the EDGAR insider signal is
  US-only and returns empty for Canadian-domiciled issuers).
- Output is illustrative, never presented as personalized financial advice.

## Demo fixtures

`config/examples/tremblay_config.json` (Ontario) and
`gagnon_config.json` (same household in Quebec — QPP, Quebec brackets, the
abatement). Both are fictional and both must keep building cleanly; test changes
against each.
