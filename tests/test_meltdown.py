"""Unit tests for the RRSP-meltdown optimizer and Monte Carlo determinism."""
import copy
import pathlib

import pytest
import config_loader as cl
import build_model as bm
import quarterly_update as qu
import tax_ca

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _cfg(name):
    cfg, _ = cl.load_config(str(ROOT / "config" / "examples" / name))
    return cfg


def _cfg_in(prov):
    """The Ontario demo household relocated to another province/territory."""
    cfg = copy.deepcopy(_cfg("tremblay_config.json"))
    cfg["household"]["province"] = prov
    return cfg


def test_rrif_factors():
    # CORRECTNESS (A6): docs/CANADA_RULES.md §7 prescribed minimum factors, and
    # the "no minimum in the conversion year" rule -- first mandatory withdrawal
    # is for the year you turn 72 (conversion happens by end of the year you
    # turn 71, §6).
    assert bm.rrif_min_factor(60) == 0.0
    assert bm.rrif_min_factor(71) == 0.0           # first mandatory withdrawal is at 72
    assert bm.rrif_min_factor(72) == pytest.approx(0.0540)
    assert bm.rrif_min_factor(94) == pytest.approx(0.1879)
    assert bm.rrif_min_factor(95) == 0.20
    assert bm.rrif_min_factor(110) == 0.20
    # The dead/unreachable 71-entry was removed from the data table (A6 fix):
    # standard conversion never looks up 71 (rrif_min_factor floors it at 0
    # below 72), so keeping a 71 key in RRIF_FACTORS misled a reader into
    # thinking it's used.
    assert 71 not in bm.RRIF_FACTORS


def test_early_rrif_conversion_factor_is_unsupported():
    # DEVIATION (A6, keeping): docs/CANADA_RULES.md §7 -- "for ages below 71
    # (e.g., an early RRIF conversion), the factor is 1/(90 - age)." The engine
    # never models an early conversion, so rrif_min_factor has no notion of it
    # and always returns 0.0 below 72. An early converter at, say, age 65 would
    # have a true CRA minimum of 1/(90-65) = 4.0%, computed here from the
    # documented formula (not by running the implementation) -- the engine
    # silently reports 0 instead. Misleads: anyone who converts before the
    # standard year-you-turn-71 deadline and expects a minimum to be modelled.
    # Pinned as unsupported; do not "fix" -- the model never converts early.
    documented_early_factor_at_65 = 1 / (90 - 65)
    assert documented_early_factor_at_65 == pytest.approx(0.04)
    assert bm.rrif_min_factor(65) == 0.0
    assert bm.rrif_min_factor(65) != documented_early_factor_at_65


def test_simulation_invariants():
    r = bm._simulate_meltdown(_cfg("tremblay_config.json"), "optimal", target=90000.0)
    assert r["total_tax"] > 0
    assert all(row["rrsp"] >= -1e-6 for row in r["schedule"])     # never goes negative
    assert all(row["voluntary"] >= -1e-6 for row in r["schedule"])
    assert r["terminal_tax"] >= 0


@pytest.mark.parametrize("name", ["tremblay_config.json", "gagnon_config.json"])
def test_optimal_beats_do_nothing(name):
    cfg = _cfg(name)
    best = bm._optimize_meltdown(cfg)
    none = bm._simulate_meltdown(cfg, "none")
    assert best["total_tax"] <= none["total_tax"]
    assert best["target"] is not None


def test_optimizer_is_deterministic():
    cfg = _cfg("tremblay_config.json")
    a = bm._optimize_meltdown(cfg)
    b = bm._optimize_meltdown(cfg)
    assert a["target"] == b["target"]
    assert a["total_tax"] == pytest.approx(b["total_tax"])


def test_quebec_costs_more_than_ontario_same_household():
    # gagnon mirrors tremblay's balances but in Quebec -> strictly higher tax.
    on = bm._optimize_meltdown(_cfg("tremblay_config.json"))
    qc = bm._optimize_meltdown(_cfg("gagnon_config.json"))
    assert qc["total_tax"] > on["total_tax"]


@pytest.mark.parametrize("prov", sorted(tax_ca.PROVINCES))
def test_optimizer_runs_for_every_jurisdiction(prov):
    cfg = _cfg_in(prov)
    best = bm._optimize_meltdown(cfg)
    none = bm._simulate_meltdown(cfg, "none")
    assert best["target"] is not None and not best["insolvent"]
    assert best["total_tax"] <= none["total_tax"]
    assert all(row["rrsp"] >= -1e-6 for row in best["schedule"])


def test_cross_province_tax_ordering():
    # Same household: lower-rate jurisdictions cost less lifetime tax than higher.
    def total(prov):
        return bm._optimize_meltdown(_cfg_in(prov))["total_tax"]
    assert total("NU") < total("ON") < total("NS")   # 44.5% < 53.5% < 54.0% top
    assert total("AB") < total("ON")                 # Alberta cheaper than Ontario


def test_clawback_strategy_ceiling_matches_assessment_threshold():
    # CORRECTNESS (A1): docs/CANADA_RULES.md §1 -- the OAS clawback threshold
    # is a single 2025-vintage statutory figure. _simulate_meltdown touches it
    # at TWO call sites: the "clawback" strategy's per-spouse withdrawal
    # ceiling (how much to melt down without crossing the line), and the
    # threshold actually assessed against income when computing clawback. Both
    # must be indexed off tax_ca.BASE_YEAR via tax_ca.index_factor -- indexing
    # either one off "years from today" instead (the original A1 bug) makes
    # the ceiling drift away from the line it's aiming at.
    #
    # Behavioural pin, not just a helper-level check: the "clawback" strategy
    # fills household taxable income exactly up to the indexed ceiling, and
    # the tax step assesses clawback against that same indexed line. If the
    # two call sites agree, a year in which the strategy actually reached its
    # ceiling (not clipped by RRSP exhaustion, not skipped because fixed
    # income alone already exceeds it) must show ZERO clawback -- landing
    # exactly on the line does not cross it. A mismatch between the two sites
    # (whichever direction) breaks this.
    cfg = _cfg("tremblay_config.json")
    a_ret = cfg["household"]["members"][0]["retirement_age"]
    b_ret = cfg["household"]["members"][1]["retirement_age"]
    infl = cfg["assumptions"]["inflation_rate"]
    thr0 = cfg["assumptions"]["oas_clawback_threshold"]

    r = bm._simulate_meltdown(cfg, "clawback")

    # Restrict to years where both spouses are retired: n_active == 2 and
    # spouse B's employment income (b_work) is necessarily 0, so the ceiling
    # collapses to a clean "2 * thr0 * index_factor" with nothing to net out.
    reached = [
        row for row in r["schedule"]
        if row["a_age"] >= a_ret and row["b_age"] >= b_ret
        and row["voluntary"] > 0        # ceiling was actually binding
        and row["rrsp"] > 1.0           # ...and not clipped by RRSP exhaustion
    ]
    assert reached, "test is vacuous: no simulated year reached the clawback ceiling unclipped"

    for row in reached:
        # Pin the ceiling call site explicitly: taxable household income
        # (retirement_fixed + voluntary, net of the already-zero b_work) must
        # equal the same base-year-indexed formula as the threshold call site.
        expected_ceiling = 2 * thr0 * tax_ca.index_factor(row["year"], infl)
        assert row["taxable"] == pytest.approx(expected_ceiling, abs=1.0), (
            f"year {row['year']}: taxable={row['taxable']!r} != expected ceiling "
            f"{expected_ceiling!r} -- the ceiling call site has drifted off BASE_YEAR"
        )
        # And the interaction: landing exactly on the ceiling means each
        # spouse's assessed income lands exactly on the SAME indexed
        # threshold used inside oas_clawback, so clawback must be zero.
        assert row["claw"] == pytest.approx(0.0, abs=1.0), (
            f"year {row['year']}: reached the clawback ceiling but claw={row['claw']!r} "
            "-- the ceiling and the assessment threshold have drifted apart (A1 regression)"
        )


def test_raising_oas_claim_age_alone_reduces_lifetime_oas():
    # DEVIATION (A5, keeping): _simulate_meltdown gates OAS on claim age but
    # pays the configured monthly amount unadjusted -- it does NOT apply the
    # deferral increase (docs/CANADA_RULES.md §1: OAS +0.6%/month, up to +36%
    # at 70). So raising oas_claim_age alone strictly *reduces* lifetime OAS
    # received (fewer years paid, no compensating bump), unless the caller has
    # already re-entered an inflated monthly figure. Misleads: anyone editing
    # oas_claim_age alone to compare claim strategies -- deferral is modelled
    # as pure loss. Isolate the OAS effect with strategy="none" (no voluntary
    # withdrawals) so forced RRIF minimums, which don't depend on OAS claim
    # age, don't confound the comparison.
    cfg_65 = _cfg("tremblay_config.json")
    cfg_70 = copy.deepcopy(cfg_65)
    cfg_70["household"]["members"][0]["oas_claim_age"] = 70   # spouse A: 65 -> 70

    r_65 = bm._simulate_meltdown(cfg_65, "none")
    r_70 = bm._simulate_meltdown(cfg_70, "none")

    taxable_65 = sum(row["taxable"] for row in r_65["schedule"])
    taxable_70 = sum(row["taxable"] for row in r_70["schedule"])
    assert taxable_70 < taxable_65


def test_demo_oas_ratio_matches_documented_deferral_convention():
    # DEVIATION (A5, keeping): the demo config's spouse_b_oas_monthly (990) vs
    # spouse_a_oas_monthly (728) ratio is not computed by the engine -- it is
    # the fixture author pre-applying the OAS deferral-to-70 convention
    # (+0.6%/month x 60 months = +36% at age 70, docs/CANADA_RULES.md §1) before
    # typing the number in, since spouse B's oas_claim_age is 70 (see
    # config/examples/tremblay_config.json). Ties the fixture to the sourced
    # figure instead of just trusting the file.
    cfg = _cfg("tremblay_config.json")
    gb = cfg["government_benefits"]
    assert cfg["household"]["members"][1]["oas_claim_age"] == 70
    assert gb["spouse_b_oas_monthly"] / gb["spouse_a_oas_monthly"] == pytest.approx(1.36, abs=0.002)


def test_monte_carlo_deterministic_and_in_range():
    cfg = _cfg("tremblay_config.json")
    m1 = qu.monte_carlo(cfg, n_sims=500)
    m2 = qu.monte_carlo(cfg, n_sims=500)
    for k in ("conservative", "base", "optimistic"):
        assert 0.0 <= m1[k]["success_rate"] <= 100.0
        assert m1[k]["success_rate"] == m2[k]["success_rate"]   # fixed seed -> reproducible
    assert m1["conservative"]["success_rate"] <= m1["optimistic"]["success_rate"]
