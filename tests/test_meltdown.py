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


def test_pension_split_capped_below_65_when_only_pension_eligible():
    # CORRECTNESS (B1): docs/CANADA_RULES.md §13 -- under 65 the "eligible for
    # splitting" list is (in this model) just the DB pension annuity; RRIF /
    # registered withdrawals do NOT qualify until 65. So even though T1032
    # permits reallocating up to 50% of ELIGIBLE income, it does not permit
    # equalizing TOTAL income once some of that income (here, the voluntary
    # RRSP melt) is not eligible. Use "optimal" with a fixed target so a
    # voluntary, pre-65-ineligible withdrawal exists alongside the pension --
    # target=90000 is the same value already used by test_simulation_invariants.
    cfg = _cfg("tremblay_config.json")
    a_ret = cfg["household"]["members"][0]["retirement_age"]
    b_ret = cfg["household"]["members"][1]["retirement_age"]
    pension_m = cfg["income"]["pension_monthly_at_retirement"]
    cola = cfg["income"]["pension_cola"]

    r = bm._simulate_meltdown(cfg, "optimal", target=90000.0)
    rows = [row for row in r["schedule"]
            if a_ret <= row["a_age"] < 65 and b_ret <= row["b_age"] < 65]
    assert rows, "test is vacuous: no simulated year has both spouses retired and under 65"

    for row in rows:
        # Spouse A's eligible income pre-65 is exactly the DB pension annuity
        # for that year -- computed here from config (pension_monthly x 12 x
        # cola compounding), independently of the simulator's own field.
        expected_pension = pension_m * 12 * (1 + cola) ** (row["a_age"] - a_ret)
        assert row["eligible_a"] == pytest.approx(expected_pension, abs=1.0)
        assert row["taxed_a"] != pytest.approx(row["taxed_b"], abs=1.0), (
            "gross incomes differ (pension + ineligible RRSP melt on A vs a "
            "smaller registered-withdrawal share on B) but taxed incomes came "
            "out equal -- income appears to have been split past the s13 cap"
        )
        # The transfer itself is capped at exactly 50% of that eligible pension.
        assert row["split_transfer"] == pytest.approx(0.5 * expected_pension, abs=1.0)


def test_registered_withdrawal_split_expands_at_65():
    # CORRECTNESS (B1): docs/CANADA_RULES.md §13 -- "at 65+ the list broadens
    # to include RRIF withdrawals... so RRIF income becomes splittable at 65."
    # Eligibility is gated on that spouse's OWN age (not the household's, and
    # not "the older spouse" -- in this demo spouse B is chronologically older
    # but spouse A holds the pension and is the transferor throughout this
    # stretch, so it is A's own 65th birthday that newly makes A's share of
    # the registered withdrawal eligible). Compare the year before vs the year
    # A turns 65, strategy and target held constant.
    cfg = _cfg("tremblay_config.json")
    r = bm._simulate_meltdown(cfg, "optimal", target=90000.0)
    before = next(row for row in r["schedule"] if row["a_age"] == 64)
    at65 = next(row for row in r["schedule"] if row["a_age"] == 65)
    assert before["eligible_a"] < at65["eligible_a"]   # reg. withdrawal share newly counted
    assert before["split_transfer"] < at65["split_transfer"]


def test_split_transfer_never_exceeds_half_of_transferors_eligible_income():
    # CORRECTNESS (B1): docs/CANADA_RULES.md §13's hard cap -- "up to 50% of
    # eligible pension income." Check every simulated year across all three
    # strategies: the transfer never exceeds 50% of the larger of the two
    # spouses' eligible income (the transferor is always the higher-income
    # spouse, so its eligible income is whichever of eligible_a/eligible_b is
    # relevant that year -- taking the max is a safe upper bound either way).
    cfg = _cfg("tremblay_config.json")
    for strategy, kwargs in (("none", {}), ("clawback", {}), ("optimal", {"target": 90000.0})):
        r = bm._simulate_meltdown(cfg, strategy, **kwargs)
        for row in r["schedule"]:
            cap = 0.5 * max(row["eligible_a"], row["eligible_b"])
            assert row["split_transfer"] <= cap + 1.0, (
                f"{strategy} {row['year']}: transfer {row['split_transfer']} exceeds "
                f"50% of eligible income {cap}"
            )


@pytest.mark.parametrize("name", ["tremblay_config.json", "gagnon_config.json"])
def test_only_pension_and_registered_income_is_ever_eligible(name):
    # CORRECTNESS (B1): docs/CANADA_RULES.md §13 -- the eligible list is the RPP
    # lifetime annuity (the DB pension) plus, once THAT spouse turns 65, their
    # registered/RRIF withdrawals. CPP, OAS, passive and employment income are
    # never splittable under T1032 (CPP *sharing* is a separate mechanism).
    #
    # The bound is rebuilt here from config + the schedule's own withdrawal
    # figures rather than read back from eligible_a/eligible_b, so this fails if
    # any non-eligible stream is ever folded into the eligible pool. Both demos
    # are exercised: the Ontario fixture has passive_income_annual == 0, which
    # makes an inflated eligible pool invisible there, so Quebec carries the
    # real coverage.
    cfg = _cfg(name)
    inc = cfg["income"]
    acct = cfg["accounts"]
    a_ret = cfg["household"]["members"][0]["retirement_age"]
    pension_m, cola = inc["pension_monthly_at_retirement"], inc["pension_cola"]
    a_rrsp = float(acct.get("spouse_a_rrsp", 0))
    b_rrsp = float(acct.get("spouse_b_rrsp", 0))
    total_rrsp = a_rrsp + b_rrsp
    a_share = (a_rrsp / total_rrsp) if total_rrsp > 0 else 0.5

    for strategy, kwargs in (("none", {}), ("clawback", {}), ("optimal", {"target": 90000.0})):
        r = bm._simulate_meltdown(cfg, strategy, **kwargs)
        for row in r["schedule"]:
            reg = row["forced"] + row["voluntary"]
            pension = pension_m * 12 * ((1 + cola) ** max(0, row["a_age"] - a_ret))
            # Spouse A: DB pension at any age, plus A's share of the registered
            # draw only from A's own 65th birthday.
            cap_a = pension + (reg * a_share if row["a_age"] >= 65 else 0.0)
            # Spouse B holds no DB pension in either fixture, so B's eligible
            # income is their registered share alone, gated on B's own age.
            cap_b = reg * (1.0 - a_share) if row["b_age"] >= 65 else 0.0
            assert row["eligible_a"] <= cap_a + 1.0, (
                f"{name} {strategy} {row['year']}: eligible_a {row['eligible_a']} "
                f"exceeds pension + A's registered share {cap_a} -- a non-eligible "
                f"stream (CPP/OAS/passive) has leaked into the splittable pool"
            )
            assert row["eligible_b"] <= cap_b + 1.0, (
                f"{name} {strategy} {row['year']}: eligible_b {row['eligible_b']} "
                f"exceeds B's registered share {cap_b}"
            )


def test_pension_income_splitting_flag_is_live():
    # CORRECTNESS (B1): household.pension_income_splitting was declared in
    # every config and documented in ARCHITECTURE.md but read nowhere in
    # engine/ -- setting it false changed nothing (dead flag). After the fix,
    # disabling the T1032 election must strictly raise lifetime tax for a
    # household with an income gap between spouses -- reallocating income into
    # a lower bracket is never worse under a progressive schedule (§13).
    cfg_on = _cfg("tremblay_config.json")
    cfg_off = copy.deepcopy(cfg_on)
    cfg_off["household"]["pension_income_splitting"] = False

    on = bm._simulate_meltdown(cfg_on, "clawback")
    off = bm._simulate_meltdown(cfg_off, "clawback")
    assert off["total_tax"] > on["total_tax"]


def test_pension_income_splitting_defaults_true_when_key_absent():
    # CORRECTNESS (B1): "default to True when the key is absent so existing
    # configs behave as before." Remove the key entirely (as opposed to
    # setting it False) and confirm behaviour matches it being explicitly True.
    cfg_explicit = _cfg("tremblay_config.json")
    cfg_absent = copy.deepcopy(cfg_explicit)
    del cfg_absent["household"]["pension_income_splitting"]

    a = bm._simulate_meltdown(cfg_explicit, "clawback")
    b = bm._simulate_meltdown(cfg_absent, "clawback")
    assert a["total_tax"] == pytest.approx(b["total_tax"])


def test_oas_steps_up_ten_percent_at_75():
    # CORRECTNESS (B2): docs/CANADA_RULES.md §1 -- max monthly OAS is
    # $727.67/mo at 65-74 and $800.44/mo at 75+, a permanent increase
    # effective July 2022. Derive the ratio from those two sourced figures
    # (not hardcoded blind) and confirm the simulated OAS schedule steps up by
    # that ratio, ON TOP OF the ordinary inflation indexation, across the
    # 74->75 boundary.
    documented_ratio = 800.44 / 727.67
    assert documented_ratio == pytest.approx(1.10, abs=0.001)

    cfg = _cfg("tremblay_config.json")
    r = bm._simulate_meltdown(cfg, "none")   # isolate: no voluntary withdrawals to confound
    infl = cfg["assumptions"]["inflation_rate"]
    row74 = next(row for row in r["schedule"] if row["a_age"] == 74)
    row75 = next(row for row in r["schedule"] if row["a_age"] == 75)
    expected = row74["oas_a"] * (1 + infl) * documented_ratio   # one more inflation year, plus the uplift
    assert row75["oas_a"] == pytest.approx(expected, rel=0.01)


def test_monte_carlo_deterministic_and_in_range():
    cfg = _cfg("tremblay_config.json")
    m1 = qu.monte_carlo(cfg, n_sims=500)
    m2 = qu.monte_carlo(cfg, n_sims=500)
    for k in ("conservative", "base", "optimistic"):
        assert 0.0 <= m1[k]["success_rate"] <= 100.0
        assert m1[k]["success_rate"] == m2[k]["success_rate"]   # fixed seed -> reproducible
    assert m1["conservative"]["success_rate"] <= m1["optimistic"]["success_rate"]
