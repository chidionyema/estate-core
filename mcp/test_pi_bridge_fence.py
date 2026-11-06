"""Regression guard for the pi-bridge founder fence.

The fence protects money-rail / identity / contract / migration surface from the
non-interactive executor. It was widened wrong once (a bare `store_platform/` prefix
that refused 414 files to protect ~40) and narrowed wrong once (`\\bcheckout\\b`, whose
trailing word boundary cannot match `CheckoutEndpoints.cs`). Both directions are bugs,
so both directions are pinned here.

Run: python3 ~/.claude/mcp/test_pi_bridge_fence.py     (or: pytest that path)
"""
import importlib.util
import os
import sys

_SPEC = importlib.util.spec_from_file_location(
    "pi_bridge", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pi_bridge.py")
)
pb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pb)


# Work that must reach the cheap executor. Every one of these was refused by the old
# `store_platform/` prefix, which is why design and UI work never got delegated.
ALLOWED = [
    "store_platform/src/Store.Web/scripts/design-audit/audit.mjs",
    "store_platform/src/Store.Web/scripts/design-audit/report.mjs",
    "store_platform/src/Store.Web/src/styles/tokens.css",
    "store_platform/src/Store.Web/src/pages/ideas/[slug].tsx",
    "store_platform/src/Store.Web/src/components/Header.tsx",
    "store_platform/src/Store.Catalog/Domain/Pack.cs",
    "docs/DESIGN_UX_AUDIT_PROGRAM.md",
    "prospector/dossier.py",
]

# Surface that must never leave Claude Code.
REFUSED = [
    "store_platform/src/Store.Api/Payments/StripeProvider.cs",
    "store_platform/src/Store.Api/Payments/PaddleProvider.cs",
    "store_platform/src/Store.Api/Payments/MoneyRailConfigGate.cs",
    "store_platform/src/Store.Api/Endpoints/WebhookEndpoints.cs",
    "store_platform/src/Store.Api/Auth/AuthEndpoints.cs",
    "store_platform/src/Store.Api/Identity/AuthDtos.cs",
    "store_platform/src/Store.Api/Contracts/CheckoutRequest.cs",
    "store_platform/src/Store.Api/Contracts/PricePatchRequest.cs",
    "store_platform/src/Store.Catalog/Domain/Entitlement.cs",
    "store_platform/src/Store.Catalog/Domain/PackPriceHistory.cs",
    "store_platform/src/Store.Catalog/Migrations/20260805201134_AddPackPriceFloorAndHistory.cs",
    "prospector/bridge.py",
    "prospector/pricing.py",
]


# Money-ADJACENT: allowed to be written, impossible to miss in the run report. These are
# the files the commerce-mode work must touch, and refusing them is what sent P1 back as
# `REFUSED ... ['Checkout', 'checkout']` on 2026-08-15.
REVIEW = [
    "store_platform/src/Store.Api/Endpoints/CheckoutEndpoints.cs",
    "store_platform/src/Store.Api/Endpoints/PackEndpoints.cs",
    "store_platform/src/Store.Api/Services/FulfilmentService.cs",
]

# The plans this fence exists to sort, verbatim from docs/SUBSCRIPTION_PROGRAM.md §17.10.
PLAN_P1_COMMERCE_MODE = (
    "Add Store.Api/Commerce/CommerceOptions.cs and CommerceEndpoints.cs implementing "
    "GET /commerce. Modify Endpoints/CheckoutEndpoints.cs to refuse pack checkout with "
    "409 mode_disabled when Commerce.Mode is subscription, and 451 country_not_served "
    "when the billing country is outside Commerce.SellableCountries."
)
PLAN_P3_MONEY_RAIL = (
    "Add Domain/Subscription.cs and a migration AddSubscriptions, wire "
    "Payments/StripeProvider.cs to create mode=subscription sessions and handle "
    "invoice.paid webhooks."
)


def test_design_and_ui_work_is_not_fenced():
    hits = [p for p in ALLOWED if pb.FENCE_RE.search(p)]
    assert not hits, f"fence is over-broad, refuses non-money work: {hits}"


def test_money_surface_is_fenced():
    misses = [p for p in REFUSED if not pb.FENCE_RE.search(p)]
    assert not misses, f"fence has a hole, allows money surface: {misses}"


def test_camelcase_filenames_match():
    """The trailing-\\b bug: `\\bcheckout\\b` never matches `CheckoutEndpoints.cs`."""
    for name in ("StripeProvider.cs", "PaddleProvider.cs",
                 "WebhookEndpoints.cs", "EntitlementService.cs"):
        assert pb.HARD_RE.search(name), f"CamelCase filename slipped the fence: {name}"
    # CheckoutEndpoints.cs is REVIEW now, not HARD — but it must still not be invisible.
    assert pb.REVIEW_RE.search("CheckoutEndpoints.cs")


def test_review_surface_is_writable_but_always_reported():
    hard = [p for p in REVIEW if pb.HARD_RE.search(p)]
    assert not hard, f"REVIEW surface must not be refused outright: {hard}"
    assert pb.review_paths(REVIEW) == REVIEW, "REVIEW surface must be reported after a run"


def test_review_and_hard_tiers_do_not_overlap():
    """A path in both tiers would be reported twice and refused anyway — the report lies."""
    both = [p for p in REVIEW + REFUSED if pb.HARD_RE.search(p) and p in pb.review_paths(REVIEW + REFUSED)]
    assert not both, f"a path is in both tiers: {both}"


def test_commerce_mode_plan_is_dispatchable():
    """The measured regression: `\\bcheckout` is a domain word, not a money surface."""
    hits = pb.fence_violations(PLAN_P1_COMMERCE_MODE)
    assert not hits, f"commerce-mode work refused again over a domain word: {hits}"


def test_money_rail_plan_is_still_refused():
    """The other direction. Dropping `checkout` must not have opened the rail."""
    assert pb.fence_violations(PLAN_P3_MONEY_RAIL), "money-rail plan is no longer refused"


def test_post_run_check_catches_what_the_plan_never_named():
    """The pre-check reads prose and can be walked past without lying; git cannot."""
    plan = "Update the payment provider adapter so line items carry the pack title."
    assert not pb.fence_violations(plan), "test premise stale: this plan should pass the pre-check"
    wrote = ["store_platform/src/Store.Api/Payments/StripeProvider.cs",
             "store_platform/src/Store.Web/src/components/Cart.tsx"]
    assert pb.fenced_paths(wrote) == ["store_platform/src/Store.Api/Payments/StripeProvider.cs"]


# --- 2026-11-06 production-hardening pins -------------------------------------------

def test_model_must_be_provider_slash_name():
    """A bare model name used to mis-split: provider and model both became the string."""
    assert pb.parse_model("minimax/MiniMax-M3") == ("minimax", "MiniMax-M3")
    for bad in ("gpt-5", "", "/noname", "noprovider/"):
        try:
            pb.parse_model(bad)
        except ValueError:
            continue
        raise AssertionError(f"parse_model accepted {bad!r}")


def test_timeout_has_a_ceiling():
    r = pb.tool_pi_execute({"plan": "x", "cwd": "/nonexistent-dir-for-validation-order",
                            "timeout_s": 999999})
    # validation order: cwd check fires before dispatch, but the timeout must parse+clamp
    assert "999999" not in r
    assert pb.MAX_TIMEOUT_S == 3600


def test_porcelain_rename_uses_new_path():
    out = pb.porcelain_paths("R  old/name.py -> new/name.py\n M src/a.py\n?? src/b.py\n")
    assert out["new/name.py"].startswith("R")
    assert "old/name.py" not in out
    assert "src/a.py" in out and "src/b.py" in out


def test_run_log_chain_detects_tamper(tmp_path=None):
    import tempfile
    d = tempfile.mkdtemp()
    log = os.path.join(d, "runs.jsonl")
    pb.RUN_LOG = log
    pb.HMAC_KEY_PATH = os.path.join(d, "key")
    pb.log_run({"kind": "execute", "model": "m/1", "rc": 0})
    pb.log_run({"kind": "execute", "model": "m/1", "rc": 1})
    assert pb.verify_chain(log).startswith("CHAIN OK")
    # tamper: flip an rc in the first row
    with open(log) as fh:
        rows = fh.readlines()
    rows[0] = rows[0].replace('"rc": 0', '"rc": 9')
    with open(log, "w") as fh:
        fh.writelines(rows)
    assert pb.verify_chain(log).startswith("CHAIN FAIL")


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL  {name}\n      {e}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
