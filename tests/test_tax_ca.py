"""Unit tests for the Canadian tax engine. Expected values are anchored to the
sourced 2025 figures in docs/CANADA_RULES.md (verified vs CRA / Revenu Quebec)."""
import datetime
import inspect

import pytest
import tax_ca


def test_zero_and_negative_income():
    assert tax_ca.income_tax(0, "ON") == 0
    assert tax_ca.income_tax(-5000, "ON") == 0


ALL = sorted(tax_ca.PROVINCES)


@pytest.mark.parametrize("prov", ALL)
def test_monotonic_increasing(prov):
    prev = -1.0
    for inc in range(0, 600001, 20000):
        t = tax_ca.income_tax(inc, prov)
        assert t >= prev - 1e-6, f"{prov} not monotonic at {inc}"
        prev = t


@pytest.mark.parametrize("prov", ALL)
def test_bracket_tables_well_formed(prov):
    """Structural guard against data-entry slips in any jurisdiction's brackets:
    open-ended top, rates in (0,1) and non-decreasing, thresholds strictly ascending."""
    br = tax_ca.PROVINCES[prov]["brackets"]
    assert br[-1][1] is None, f"{prov}: top bracket must be open-ended (None)"
    rates = [r for r, _ in br]
    uppers = [u for _, u in br[:-1]]
    assert all(0 < r < 1 for r in rates), f"{prov}: rate out of range"
    assert rates == sorted(rates), f"{prov}: rates must be non-decreasing (progressive)"
    assert uppers == sorted(uppers) and len(set(uppers)) == len(uppers), \
        f"{prov}: thresholds must be strictly ascending"
    assert all(u > 0 for u in uppers)
    assert tax_ca.PROVINCES[prov]["bpa"] > 0


@pytest.mark.parametrize("prov", ALL)
def test_marginal_rate_within_bounds(prov):
    # Combined marginal rate must stay sane across the income range (catches a
    # rate typo like 0.18 -> 1.8 or a sign error). Top combined rates are <55%.
    for inc in (30000, 75000, 150000, 400000):
        m = tax_ca.marginal_rate(inc, prov)
        assert 0.0 <= m <= 0.56, f"{prov}: marginal {m:.3f} at {inc} out of bounds"


def test_bpa_shelters_low_income():
    # Below the federal + Ontario Basic Personal Amounts, tax is ~0.
    assert tax_ca.income_tax(10000, "ON") == pytest.approx(0, abs=1)


def test_ontario_marginal_rates():
    # 50k: fed 14.5% + ON 5.05% = 19.55%; 90k: fed 20.5% + ON 9.15% = 29.65%
    assert tax_ca.marginal_rate(50000, "ON") == pytest.approx(0.1955, abs=0.01)
    assert tax_ca.marginal_rate(90000, "ON") == pytest.approx(0.2965, abs=0.01)


def test_quebec_top_marginal_includes_abatement():
    # Top: fed 33% x (1 - 0.165 abatement) + QC 25.75% = 53.305%
    assert tax_ca.marginal_rate(300000, "QC") == pytest.approx(0.533, abs=0.006)
    # ...and must be well below the un-abated 33% + 25.75% = 58.75%
    assert tax_ca.marginal_rate(300000, "QC") < 0.56


# Top combined marginal rate (federal top 33%, abated 16.5% only in QC, + provincial
# top) at an income above every top-bracket threshold. Sourced per jurisdiction in
# docs/CANADA_RULES.md. Extended one phase at a time as provinces are encoded.
TOP_MARGINAL = {
    "ON": 0.5353, "QC": 0.5331, "BC": 0.5350, "AB": 0.4800,
    "MB": 0.5040, "SK": 0.4750, "NS": 0.5400, "NB": 0.5250,
    "NL": 0.5480, "PE": 0.5200,
    "YT": 0.4800, "NT": 0.4705, "NU": 0.4450,
}


def test_all_thirteen_jurisdictions_encoded():
    # 10 provinces + 3 territories; QC carries the special abatement field.
    assert len(tax_ca.PROVINCES) == 13
    assert tax_ca.PROVINCES["QC"]["abatement"] == 0.165


@pytest.mark.parametrize("prov,expected", sorted(TOP_MARGINAL.items()))
def test_top_combined_marginal_rate(prov, expected):
    # $1.5M clears every jurisdiction's top-bracket threshold -- incl. Yukon's $500k
    # split and Newfoundland's top bracket at ~$1.13M -- so this checks the true top rate.
    assert tax_ca.marginal_rate(1_500_000, prov) == pytest.approx(expected, abs=0.004)


def test_oas_clawback():
    assert tax_ca.oas_clawback(50000, 8000, 93454) == 0          # below threshold
    assert tax_ca.oas_clawback(100000, 8000, 93454) == pytest.approx(981.9, abs=0.5)
    assert tax_ca.oas_clawback(1_000_000, 8000, 93454) == 8000   # capped at OAS received


def test_quebec_hsf_bands():
    f = tax_ca.quebec_hsf
    assert f(18130) == 0                                  # at exemption
    assert f(20000) == pytest.approx(18.70, abs=0.05)     # ramp 1
    assert f(33130) == pytest.approx(150, abs=0.5)        # top of ramp 1
    assert f(50000) == 150                                # flat band
    assert f(100000) == pytest.approx(519.4, abs=1)       # ramp 2
    assert f(200000) == 1000                              # cap


@pytest.mark.parametrize("prov", ["ON", "QC"])
def test_retirement_credits_reduce_tax(prov):
    working = tax_ca.income_tax(40000, prov)
    retiree = tax_ca.income_tax(40000, prov, age=70, pension_income=40000, hsf_base=40000)
    assert retiree < working   # age amount + pension credit apply at 65+


def test_age_amount_phases_out_at_high_income():
    # At $90k the federal age amount is largely phased out, so the retiree
    # discount is much smaller than at $40k.
    d_low = tax_ca.income_tax(40000, "ON") - tax_ca.income_tax(40000, "ON", age=70, pension_income=40000)
    d_high = tax_ca.income_tax(90000, "ON") - tax_ca.income_tax(90000, "ON", age=70, pension_income=90000)
    assert d_low > d_high > 0


def test_federal_fourth_bracket_is_statutory():
    # Must be the statutory 29%, NOT the effective 29.31% that bakes in the BPA grind.
    rates = [r for r, _ in tax_ca.FEDERAL_BRACKETS]
    assert 0.29 in rates and 0.2931 not in rates


def test_bpa_phasedown_helper():
    f = tax_ca._effective_bpa
    yr, infl = tax_ca.BASE_YEAR, 0.0
    assert f(16129, 14538, (177882, 253414), 100000, yr, infl) == pytest.approx(16129)  # full below
    assert f(16129, 14538, (177882, 253414), 300000, yr, infl) == pytest.approx(14538)  # floor above
    mid = (177882 + 253414) / 2
    assert f(16129, 14538, (177882, 253414), mid, yr, infl) == pytest.approx((16129 + 14538) / 2)


def test_high_income_matches_external_calculator():
    # Anchored to first-principles CRA 2025 ground truth (statutory 29% + federal
    # BPA phase-down + provincial schedules), independently cross-checked by external
    # calculator verifiers. MB also exercises the provincial BPA phase-down ($200k-$400k).
    assert tax_ca.income_tax(250000, "ON", hsf_base=0) == pytest.approx(89367, abs=3)
    assert tax_ca.income_tax(250000, "MB", hsf_base=0) == pytest.approx(91947, abs=3)


def test_unknown_province_falls_back_to_ontario():
    # "ZZ" is not a real jurisdiction -> engine warns and uses Ontario.
    assert tax_ca.income_tax(80000, "ZZ") == tax_ca.income_tax(80000, "ON")


# ---- A1: index_factor / BASE_YEAR indexation consistency -------------------

def test_base_year_is_2025():
    # CORRECTNESS: BASE_YEAR is not a free parameter -- every statutory figure
    # in this module (federal brackets/BPA per docs/CANADA_RULES.md §4a, the
    # provincial tables per §5, and the OAS clawback threshold per §1) is
    # sourced to the 2025 tax year. Written against the literal 2025, not the
    # symbol, so a change to BASE_YEAR itself fails loudly here instead of
    # silently re-anchoring every test that's written relative to the symbol.
    assert tax_ca.BASE_YEAR == 2025


def test_index_factor_base_year():
    # CORRECTNESS: index_factor(BASE_YEAR, infl) is 1.0 by definition (no years
    # have elapsed from the base year), and one year of indexation at 2% is
    # exactly a 1.02x multiplier. Both are definitional, not sourced figures.
    # Uses the literal 2025 (not the tax_ca.BASE_YEAR symbol) so a regression
    # that changes BASE_YEAR without updating the vintage of the underlying
    # figures fails this assertion instead of silently moving with it.
    assert tax_ca.index_factor(2025, 0.021) == 1.0
    assert tax_ca.index_factor(tax_ca.BASE_YEAR, 0.021) == 1.0
    assert tax_ca.index_factor(tax_ca.BASE_YEAR + 1, 0.02) == pytest.approx(1.02)


def test_index_factor_matches_bracket_indexation():
    # CORRECTNESS (A1 fix): the OAS clawback threshold and the federal bracket
    # ceilings must be indexed off the SAME base year by the SAME factor for a
    # given simulation year -- otherwise the threshold silently drifts against
    # the brackets it interacts with (docs/CANADA_RULES.md §1; the bug this
    # fixes is described in .claude/plans/canada-tax-characterization.md A1).
    # Derived from today's date, not hardcoded to 2026, so this doesn't rot.
    year = datetime.date.today().year
    infl = 0.021
    # `_f` (used internally by federal_tax/_bracket_tax to project bracket
    # ceilings) must delegate to the same public `index_factor` used to project
    # the OAS threshold in build_model -- i.e. there is exactly one
    # implementation, so they can never diverge again.
    assert tax_ca._f(year, infl) == tax_ca.index_factor(year, infl)


# ---- A2: OAS full-clawback ceiling identity ---------------------------------

def test_oas_full_clawback_ceiling():
    # CORRECTNESS: docs/CANADA_RULES.md §1 -- "full clawback ceiling, 65-74" is
    # threshold + annual_OAS/0.15, computed dynamically (not hardcoded). With
    # the 2025 threshold $93,454 and OAS $727.67/mo (=$8,732.04/yr):
    # 93,454 + 8,732.04/0.15 = $151,667.6, matching the documented $151,668.
    threshold = 93454
    annual_oas = 727.67 * 12
    ceiling = threshold + annual_oas / 0.15
    assert ceiling == pytest.approx(151667.6, abs=0.1)
    # At the ceiling, the FULL annual OAS is clawed back...
    assert tax_ca.oas_clawback(ceiling, annual_oas, threshold) == pytest.approx(annual_oas, abs=1e-6)
    # ...and strictly less just below it.
    assert tax_ca.oas_clawback(ceiling - 100, annual_oas, threshold) < annual_oas


# ---- A3: clawback is assessed on individual income, not household ----------

def test_oas_clawback_is_individual_not_household():
    # CORRECTNESS: docs/CANADA_RULES.md §1 -- "assessed on individual net
    # income (line 23400), not household." Two spouses each at $60k (household
    # total $120k) individually owe nothing; one spouse alone at $120k owes tax.
    threshold = 93454
    annual_oas = 727.67 * 12
    assert tax_ca.oas_clawback(60000, annual_oas, threshold) == 0
    assert tax_ca.oas_clawback(120000, annual_oas, threshold) > 0


# ---- A4: clawback cash-flow timing (deviation, do not "fix") ---------------

def test_oas_clawback_has_no_benefit_period_parameter():
    # DEVIATION (keeping, not a bug): oas_clawback() assesses the SAME year's
    # net income against that SAME year's OAS -- which is the correct
    # lifetime-tax treatment. docs/CANADA_RULES.md §1's July-June period is the
    # WITHHOLDING schedule (Service Canada collects installments against the
    # following benefit year; the return then reconciles). The function has no
    # year/benefit-period parameter, so a caller cannot express that ~1-year
    # lag. The residual deviation is cash-flow only: the money actually leaves
    # ~1 year later than modelled, so discounting it at year n slightly
    # overstates its present value and spendable cash is reduced a year early.
    # Misleads: anyone reading a modelled year-n clawback as when Service Canada
    # actually withholds the money. Do NOT "fix" this -- same-year assessment
    # is correct for lifetime-tax purposes; only a cash-flow-timing model would
    # need the lag, and none exists.
    params = list(inspect.signature(tax_ca.oas_clawback).parameters)
    assert params == ["net_income", "oas_received", "threshold", "recovery_rate"]
    assert "year" not in params
    assert "benefit_period" not in params
