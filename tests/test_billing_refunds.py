"""The withdrawal window and the pro-rata refund arithmetic (issue #441).

Pure functions, so every boundary the policy names is pinned exactly: open on
day 0, open one second before 14 days, closed at exactly 14 days and after.
"""
from __future__ import annotations

import pytest

from src.billing.refunds import (
    WITHDRAWAL_WINDOW_SECONDS,
    prorated_refund_amount,
    withdrawal_window_closes_at,
    withdrawal_window_open,
)

DAY = 24 * 60 * 60
T0 = 1_780_000_000.0  # an arbitrary purchase instant


class TestWindow:
    def test_is_fourteen_days(self):
        assert WITHDRAWAL_WINDOW_SECONDS == 14 * DAY

    def test_open_at_the_moment_of_purchase(self):
        assert withdrawal_window_open(T0, T0) is True

    def test_open_one_second_before_fourteen_days(self):
        assert withdrawal_window_open(T0, T0 + 14 * DAY - 1) is True

    def test_closed_at_exactly_fourteen_days(self):
        assert withdrawal_window_open(T0, T0 + 14 * DAY) is False

    def test_closed_one_second_after_fourteen_days(self):
        assert withdrawal_window_open(T0, T0 + 14 * DAY + 1) is False

    @pytest.mark.parametrize("initial", [0, 0.0, -5.0])
    def test_no_purchase_on_record_means_no_window(self, initial):
        assert withdrawal_window_open(initial, T0) is False
        assert withdrawal_window_closes_at(initial) == 0.0

    def test_a_purchase_from_before_tracking_is_closed(self):
        """The migration's backfill value (1.0) must never open a window."""
        assert withdrawal_window_open(1.0, T0) is False

    def test_closes_at(self):
        assert withdrawal_window_closes_at(T0) == T0 + 14 * DAY


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
