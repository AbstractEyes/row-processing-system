"""The price table: dated entries with sources, batch and sync cards, later periods and tiers."""

from datetime import date
import json

import pytest

from rps.ledger import PriceError, PriceTable, Usage

MILLION = Usage(input_tokens=1_000_000, output_tokens=1_000_000)


def table(models, **extra):
    return PriceTable.from_dict({"date": "2026-11-07", "models": models, **extra})


def test_the_packaged_table_names_a_source_and_a_check_date_for_every_real_model():
    prices = PriceTable.default()
    real = [name for name in prices.models if name != "fake-model"]
    assert {"claude-haiku-5-5", "claude-sonnet-5-5", "gpt-6-luna", "gpt-6.1-sol", "gemini-3.8-flash", "gemini-4-argon", "deepseek-flash", "deepseek-v4-pro"} <= set(real)
    for name in real:
        price = prices.price(name)
        assert price.source and price.source.startswith("https://"), name
        assert price.checked and date.fromisoformat(price.checked), name
        assert price.provider, name


def test_batch_and_sync_cards_are_separate_and_a_missing_batch_card_takes_the_discount():
    prices = table(
        {
            "own": {"sync": {"input": 2.0, "output": 8.0}, "batch": {"input": 0.9, "output": 3.9}},
            "halved": {"sync": {"input": 2.0, "output": 8.0}},
            "flat": {"input": 2.0, "output": 8.0},
        }
    )
    assert prices.cost("own", MILLION) == pytest.approx(10.0) and prices.cost("own", MILLION, batch=True) == pytest.approx(4.8)
    assert prices.cost("halved", MILLION, batch=True) == pytest.approx(5.0)
    assert prices.cost("flat", MILLION) == pytest.approx(10.0) and prices.cost("flat", MILLION, batch=True) == pytest.approx(5.0)


def test_a_cache_rate_left_out_is_the_plain_input_rate_which_can_only_overstate():
    prices = table({"m": {"input": 2.0, "output": 8.0}})
    usage = Usage(cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)
    assert prices.cost("m", usage) == pytest.approx(4.0)


def test_a_price_that_changes_on_a_date_is_looked_up_by_the_day_of_the_call():
    prices = table(
        {
            "promo": {
                "input": 1.0, "output": 2.0, "until": "2026-12-31",
                "then": {"input": 3.0, "output": 6.0},
            }
        }
    )
    assert prices.cost("promo", MILLION, on=date(2026, 12, 31)) == pytest.approx(3.0)
    assert prices.cost("promo", MILLION, on=date(2027, 1, 1)) == pytest.approx(9.0)
    assert prices.price("promo").valid_until == "2026-12-31" and prices.price("promo").input == 1.0


def test_the_estimate_for_a_day_uses_that_days_rates():
    prices = table({"promo": {"input": 1.0, "output": 2.0, "until": "2026-12-31", "then": {"input": 3.0, "output": 6.0}}})
    assert prices.estimate("promo", 1_000_000, 1_000_000, on=date(2026, 12, 1)) == pytest.approx(3.0)
    assert prices.estimate("promo", 1_000_000, 1_000_000, on=date(2027, 2, 1)) == pytest.approx(9.0)


def test_tiers_price_a_call_by_the_tier_it_ran_in_and_the_estimate_takes_the_dearest():
    prices = table(
        {
            "deep": {
                "tiers": {
                    "offpeak": {"input": 0.5, "output": 1.0},
                    "peak": {"input": 1.0, "output": 2.0},
                }
            }
        }
    )
    assert prices.cost("deep", MILLION, tier="offpeak") == pytest.approx(1.5)
    assert prices.cost("deep", MILLION, tier="peak") == pytest.approx(3.0)
    assert prices.cost("deep", MILLION) == pytest.approx(3.0)  # no tier given: the dearest
    assert prices.cost("deep", MILLION, tier="unknown") == pytest.approx(3.0)
    assert prices.estimate("deep", 1_000_000, 1_000_000) == pytest.approx(3.0)


def test_a_caller_that_knows_the_tier_gets_the_estimate_at_that_tier():
    prices = table(
        {
            "deep": {"tiers": {"offpeak": {"input": 0.5, "output": 1.0}, "peak": {"input": 1.0, "output": 2.0}}},
            "plain": {"input": 1.0, "output": 2.0},
        }
    )
    assert prices.estimate("deep", 1_000_000, 1_000_000, tier="offpeak") == pytest.approx(1.5)
    assert prices.estimate("deep", 1_000_000, 1_000_000, tier="peak") == pytest.approx(3.0)
    assert prices.estimate("deep", 1_000_000, 1_000_000, tier="unknown") == pytest.approx(3.0)  # a tier it lacks: the dearest
    assert prices.estimate("deep", 1_000_000, 1_000_000, tier=None) == pytest.approx(3.0)
    assert prices.estimate("plain", 1_000_000, 1_000_000, tier="offpeak") == pytest.approx(3.0)  # no tiers: the one card
    for tier in ("offpeak", "peak"):
        assert prices.cost("deep", MILLION, tier=tier) <= prices.estimate("deep", 1_000_000, 1_000_000, tier=tier)


def test_the_estimate_prices_every_input_token_at_the_highest_input_side_rate():
    prices = table({"m": {"input": 1.0, "output": 2.0, "cache_write": 1.25, "cache_read": 0.1}})
    assert prices.estimate("m", 1_000_000, 0) == pytest.approx(1.25)
    worst = Usage(cache_write_tokens=1_000_000, output_tokens=500_000)
    assert prices.cost("m", worst) <= prices.estimate("m", 1_000_000, 500_000)


def test_a_price_ending_soon_or_already_ended_is_noted():
    prices = table({"promo": {"input": 1.0, "output": 2.0, "until": "2026-12-01", "then": {"input": 3.0, "output": 6.0}}})
    assert prices.warnings(today=date(2026, 11, 7)) == ["price for promo changes after 2026-12-01"]
    assert prices.warnings(today=date(2026, 11, 7), max_age_days=1) == ["price for promo changes after 2026-12-01"]
    assert prices.warnings(today=date(2026, 10, 1)) == []
    alone = table({"only": {"input": 1.0, "output": 2.0, "until": "2026-11-20"}})
    assert alone.warnings(today=date(2026, 11, 7)) == ["price for only is only given until 2026-11-20"]
    assert alone.warnings(today=date(2026, 12, 1)) == ["price for only was valid until 2026-11-20"]


def test_an_old_table_is_noted_by_its_age():
    prices = table({"m": {"input": 1.0, "output": 2.0}})
    assert prices.warnings(today=date(2027, 1, 7)) == ["price table is 61 days old (dated 2026-11-07)"]


@pytest.mark.parametrize(
    "model",
    [
        {"input": 1.0},  # no output rate
        {"input": -1.0, "output": 1.0},
        {"input": 1.0, "output": 1.0, "surprise": 1},
        {"input": 1.0, "output": 1.0, "until": "soon"},
        {"input": 1.0, "output": 1.0, "then": {"input": 2.0, "output": 2.0}},  # a period before `then` needs an end date
        {"input": 1.0, "output": 1.0, "checked": "yesterday"},
        {"tiers": {"peak": {"input": 1.0}}},
    ],
)
def test_malformed_entries_are_refused(model):
    with pytest.raises(PriceError):
        table({"m": model})


def test_the_legacy_flat_entries_still_load():
    prices = PriceTable.from_dict(json.loads('{"date": "2026-11-01", "models": {"old": {"input": 1.0, "output": 2.0, "valid_until": "2026-11-05"}}}'))
    assert prices.price("old").output == 2.0 and prices.price("old").valid_until == "2026-11-05"
    assert prices.warnings(today=date(2026, 11, 7)) == ["price for old was valid until 2026-11-05"]


# ---------------------------------------------------------------------------- cache storage


def test_the_storage_of_a_cache_costs_its_tokens_times_the_hours_times_the_rate_per_million_tokens_per_hour():
    prices = table({"m": {"sync": {"input": 1.0, "output": 2.0, "cache_storage": 0.5}}})
    assert prices.storage_rate("m") == 0.5
    assert prices.storage_cost("m", 2_000_000, 3.0) == pytest.approx(3.0)  # 2 million tokens, 3 hours, 0.50 each
    assert prices.storage_cost("m", 12_000, 6.0) == pytest.approx(0.036)
    assert prices.storage_cost("m", 0, 6.0) == 0.0 and prices.storage_cost("m", 1_000, 0.0) == 0.0


def test_a_model_with_no_storage_price_has_none_and_charging_for_it_is_an_error():
    prices = table({"m": {"input": 1.0, "output": 2.0}})
    assert prices.storage_rate("m") is None
    with pytest.raises(PriceError, match="no cache storage price"):
        prices.storage_cost("m", 1_000, 1.0)


def test_a_batch_discount_does_not_halve_the_storage_rate():
    prices = table({"m": {"sync": {"input": 2.0, "output": 8.0, "cache_storage": 1.0}}})  # no batch card of its own
    assert prices.cost("m", MILLION, batch=True) == pytest.approx(5.0)  # the discount reaches the tokens ...
    assert prices.storage_rate("m", batch=True) == 1.0  # ... but not the hours a cache is kept


def test_the_storage_rate_changes_with_the_day_like_the_other_rates():
    prices = table(
        {
            "m": {
                "until": "2026-12-31",
                "sync": {"input": 1.0, "output": 2.0, "cache_storage": 0.5},
                "then": {"sync": {"input": 2.0, "output": 4.0, "cache_storage": 1.0}},
            }
        }
    )
    assert prices.storage_rate("m", on=date(2026, 12, 31)) == 0.5 and prices.storage_rate("m", on=date(2027, 1, 1)) == 1.0
    assert prices.storage_cost("m", 1_000_000, 2.0, on=date(2027, 1, 1)) == pytest.approx(2.0)


@pytest.mark.parametrize("rate", [-0.5, "a lot", True])
def test_a_malformed_storage_rate_is_refused(rate):
    with pytest.raises(PriceError):
        table({"m": {"sync": {"input": 1.0, "output": 2.0, "cache_storage": rate}}})


def test_the_packaged_flash_prices_carry_the_storage_rate_read_from_the_page_by_period():
    prices = PriceTable.default()
    flash = prices.price("gemini-3.8-flash")
    assert flash.source == "https://aistudio.google.com/docs/pricing" and flash.checked == "2026-11-07"
    for batch in (False, True):
        assert prices.storage_rate("gemini-3.8-flash", batch=batch, on=date(2026, 12, 31)) == 0.50  # through the introductory period
        assert prices.storage_rate("gemini-3.8-flash", batch=batch, on=date(2027, 1, 1)) == 1.00
    # the cache read rates are the page's own: a tenth of the input rate, and half of that on the batch tab
    now = date(2026, 11, 7)
    assert prices.rates("gemini-3.8-flash", on=now).cache_read == 0.075
    assert prices.rates("gemini-3.8-flash", batch=True, on=now).cache_read == 0.0375
    assert prices.rates("gemini-3.8-flash", on=date(2027, 1, 1)).cache_read == 0.15
    assert prices.storage_cost("gemini-3.8-flash", 12_000, 6.0, batch=True, on=now) == pytest.approx(0.036)


def test_only_a_model_with_explicit_caching_has_a_storage_rate_in_the_packaged_table():
    prices = PriceTable.default()
    with_rate = [name for name in prices.models if prices.storage_rate(name) is not None]
    assert with_rate == ["gemini-3.8-flash"]


# ---------------------------------------------------------------------------- long-prompt thresholds


def test_the_packaged_table_carries_the_long_prompt_thresholds_of_the_pages_it_cites():
    prices = PriceTable.default()
    assert prices.long_prompt_limit("claude-haiku-5-5") == 100_000
    assert prices.long_prompt_limit("gpt-6-luna") == 272_000 and prices.long_prompt_limit("gpt-6.1-sol") == 272_000
    for name in ("claude-sonnet-5-5", "gemini-3.8-flash", "gemini-4-argon", "deepseek-flash", "deepseek-v4-pro", "fake-model"):
        assert prices.long_prompt_limit(name) is None, name  # no such tier on the page


def test_a_long_prompt_threshold_is_read_from_the_model_entry():
    prices = table({"m": {"input": 1.0, "output": 2.0, "long_prompt_tokens": 50_000}, "n": {"input": 1.0, "output": 2.0}})
    assert prices.long_prompt_limit("m") == 50_000 and prices.long_prompt_limit("n") is None


@pytest.mark.parametrize("threshold", [0, -1, "many", 1.5, True])
def test_a_malformed_long_prompt_threshold_is_refused(threshold):
    with pytest.raises(PriceError, match="long_prompt_tokens"):
        table({"m": {"input": 1.0, "output": 2.0, "long_prompt_tokens": threshold}})
