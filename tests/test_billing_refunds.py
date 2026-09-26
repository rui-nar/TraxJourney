"""The withdrawal window and the pro-rata refund arithmetic (issue #441).

Pure functions, so every boundary the policy names is pinned exactly. The
window runs to the end of the 14th calendar day after the day the contract
started, in UTC (owner decision, 2026-09-26): open at 23:59:59.999 UTC on that
day, closed at the midnight that follows — whatever time of day it started.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.billing.refunds import (
    prorated_refund_amount,
    withdrawal_checked_at,
    withdrawal_window_closes_at,
    withdrawal_window_open,
)

DAY = 24 * 60 * 60


def _utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


T0 = _utc(2026, 9, 10, 15, 30)  # an arbitrary purchase instant
#: The first instant after the 14th day after 10 September.
CLOSES = _utc(2026, 9, 25)
T0_END_OF_WINDOW = _utc(2026, 9, 24, 23, 59, 59, 999000)


class TestWindow:
    def test_open_at_the_moment_of_purchase(self):
        assert withdrawal_window_open(T0, T0) is True

    def test_open_on_the_last_instant_of_the_fourteenth_day(self):
        assert withdrawal_window_open(T0, T0_END_OF_WINDOW) is True

    def test_closed_at_the_midnight_after_it(self):
        assert withdrawal_window_open(T0, CLOSES) is False

    def test_closed_one_second_later(self):
        assert withdrawal_window_open(T0, CLOSES + 1) is False

    def test_still_open_after_exactly_fourteen_days_of_seconds(self):
        """Counted in calendar days, not in 14 x 86400 s from the purchase."""
        assert withdrawal_window_open(T0, T0 + 14 * DAY + 1) is True

    @pytest.mark.parametrize("start", [
        _utc(2026, 9, 10, 0, 0, 0),              # first instant of the day
        _utc(2026, 9, 10, 23, 59, 59, 999000),   # last instant of the day
    ])
    def test_the_time_of_day_of_the_purchase_does_not_matter(self, start):
        assert withdrawal_window_closes_at(start) == CLOSES

    def test_the_day_is_the_utc_day(self):
        """23:30 on 9 September in UTC-2 is 01:30 on the 10th in UTC."""
        assert withdrawal_window_closes_at(_utc(2026, 9, 10, 1, 30)) == CLOSES

    def test_across_a_month_end(self):
        assert withdrawal_window_closes_at(_utc(2026, 1, 25, 12)) == _utc(2026, 2, 9)

    @pytest.mark.parametrize("start", [0, 0.0, -5.0])
    def test_no_start_on_record_means_no_window(self, start):
        assert withdrawal_window_open(start, T0) is False
        assert withdrawal_window_closes_at(start) == 0.0

    def test_a_subscription_from_before_tracking_is_closed(self):
        """The migrations' backfill value (1.0) must never open a window."""
        assert withdrawal_window_open(1.0, T0) is False


class TestCheckedAt:
    def test_a_request_is_judged_when_it_was_first_made(self):
        assert withdrawal_checked_at(CLOSES + DAY, T0_END_OF_WINDOW) == T0_END_OF_WINDOW

    @pytest.mark.parametrize("requested", [0, 0.0])
    def test_without_one_it_is_now(self, requested):
        assert withdrawal_checked_at(CLOSES, requested) == CLOSES

    def test_a_request_stamped_later_than_now_does_not_move_now(self):
        assert withdrawal_checked_at(T0, T0 + DAY) == T0


class TestProratedRefund:
    START, END = T0, T0 + 30 * DAY

    def test_nothing_used_refunds_everything(self):
        assert prorated_refund_amount(self.START, self.END, 399, self.START) == 399

    def test_before_the_period_refunds_everything_not_more(self):
        assert prorated_refund_amount(self.START, self.END, 399, self.START - DAY) == 399

    def test_a_used_up_period_refunds_nothing(self):
        assert prorated_refund_amount(self.START, self.END, 399, self.END) == 0

    def test_after_the_period_refunds_nothing_not_less(self):
        assert prorated_refund_amount(self.START, self.END, 399, self.END + DAY) == 0

    def test_a_third_used(self):
        # 399 * 20/30 = 266 exactly
        assert prorated_refund_amount(self.START, self.END, 399, self.START + 10 * DAY) == 266

    def test_rounds_down_to_whole_cents(self):
        # 400 * 20/30 = 266.67 -> 266
        assert prorated_refund_amount(self.START, self.END, 400, self.START + 10 * DAY) == 266
        # 99 * 29/30 = 95.7 -> 95
        assert prorated_refund_amount(self.START, self.END, 99, self.START + DAY) == 95

    def test_one_second_used_is_not_a_full_refund(self):
        """Rounding down means a second of use already costs the last cent."""
        assert prorated_refund_amount(self.START, self.END, 999, self.START + 1) == 998

    @pytest.mark.parametrize("paid", [0, -100, None])
    def test_a_free_period_refunds_nothing(self, paid):
        """A trial, a coupon, a 100%-off promotion code."""
        assert prorated_refund_amount(self.START, self.END, paid, self.START) == 0

    @pytest.mark.parametrize("end", [T0, T0 - DAY])
    def test_an_empty_or_inverted_period_refunds_nothing(self, end):
        assert prorated_refund_amount(T0, end, 399, T0) == 0

    @pytest.mark.parametrize("now_offset", [-DAY, 0, 1, 7 * DAY, 30 * DAY, 60 * DAY])
    def test_always_within_zero_and_what_was_paid(self, now_offset):
        amount = prorated_refund_amount(self.START, self.END, 999, self.START + now_offset)
        assert 0 <= amount <= 999
        assert isinstance(amount, int)
