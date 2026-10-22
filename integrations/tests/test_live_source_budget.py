"""``RequestBudget`` (integrations/live_source_guard.py): the per-call request bound.

The real ``GuardedTransport`` / ``SourceApi`` over the adapter tests' in-process
``FakeSender``: token POSTs count, a request past the bound is refused before I/O and
never retried, nested budgets restore the outer one, and transports outside a budget
are not touched by it.

Evidence label: ``injected-transport``. No network, no credentials.
"""

from __future__ import annotations

import unittest

from integrations import graph_read
from integrations.live_source_guard import (BoundedBackoff, GuardedTransport,
                                            ReadOnlyGuardError, RequestBudget,
                                            RequestBudgetExhausted, RequestDeadline,
                                            RequestDeadlinePassed, unavailable_detail)
from integrations.tests.test_live_source_adapters import ENV, MSG_PATH, FakeSender, j

URL = "https://graph.microsoft.com" + MSG_PATH + "/m1"


class RequestBudgetTests(unittest.TestCase):
    def api(self, sender: FakeSender, *, retry: bool = False):
        return graph_read.build_graph(ENV, sender=sender, retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0) if retry else None)

    def test_the_token_post_counts_and_the_get_past_the_bound_is_never_sent(self):
        sender = FakeSender({MSG_PATH + "/m1": j(200, {"id": "m1"})})
        api, transport = self.api(sender)
        with RequestBudget(transport, 1).active() as budget:
            with self.assertRaises(RequestBudgetExhausted) as caught:
                api.get_json(URL)
        self.assertEqual([c[0] for c in sender.calls], ["POST"])   # the token only
        self.assertEqual((budget.used, budget.refused), (1, 1))
        self.assertEqual(unavailable_detail(caught.exception), "request_budget")
        # Within the bound the same read goes through: token POST + GET.
        api, transport = self.api(FakeSender({MSG_PATH + "/m1": j(200, {"id": "m1"})}))
        with RequestBudget(transport, 2).active() as budget:
            self.assertEqual(api.get_json(URL)["id"], "m1")
        self.assertEqual((budget.used, budget.refused), (2, 0))

    def test_a_retry_after_a_429_is_refused_before_io_and_not_retried_again(self):
        sender = FakeSender({MSG_PATH + "/m1": j(429, {"error": {"code": "throttled"}})})
        api, transport = self.api(sender, retry=True)
        with RequestBudget(transport, 2).active() as budget:
            with self.assertRaises(RequestBudgetExhausted):
                api.get_json(URL)
        # Token POST + one GET answered 429; its retry was refused, and nothing after.
        self.assertEqual([c[0] for c in sender.calls], ["POST", "GET"])
        self.assertEqual((transport.requests_sent, budget.refused), (2, 1))
        # Unbounded, the same api retries the 429 (the bound, not the backoff, stopped it).
        with self.assertRaises(Exception) as caught:
            api.get_json(URL)
        self.assertNotIsInstance(caught.exception, RequestBudgetExhausted)
        self.assertGreater(len(sender.api_calls()), 2)

    def test_a_nested_budget_applies_inside_its_block_and_the_outer_one_after(self):
        sender = FakeSender({MSG_PATH + "/m1": j(200, {"id": "m1"})})
        api, transport = self.api(sender)
        outer = RequestBudget(transport, 3)
        with outer.active():
            api.get_json(URL)                       # token POST + GET: 2 of 3
            with RequestBudget(transport, 0).active() as inner:
                with self.assertRaises(RequestBudgetExhausted):
                    api.get_json(URL)
            self.assertEqual((inner.refused, outer.refused), (1, 0))
            api.get_json(URL)                       # the outer bound again: 3 of 3
            with self.assertRaises(RequestBudgetExhausted):
                api.get_json(URL)
        self.assertEqual((outer.used, outer.refused), (3, 1))
        # Outside any active budget nothing is enforced (the count still runs).
        api.get_json(URL)
        self.assertEqual(outer.used, 4)

    def test_invalid_limits_and_transports_are_rejected(self):
        transport = GuardedTransport(FakeSender(), environ=ENV)
        for limit in (-1, True, 1.5, "3", None):
            with self.assertRaises(ValueError):
                RequestBudget(transport, limit)
        for transports in ((), [object()], [transport, "x"]):
            with self.assertRaises(ValueError):
                RequestBudget(transports, 1)
        self.assertEqual(RequestBudget(transport, 0).limit, 0)

    def test_other_transports_are_unaffected_and_shared_transports_share_the_bound(self):
        graph = FakeSender({MSG_PATH + "/m1": j(200, {"id": "m1"})})
        other = FakeSender({MSG_PATH + "/m1": j(200, {"id": "m1"})})
        (api, transport), (other_api, other_transport) = self.api(graph), self.api(other)
        with RequestBudget(transport, 0).active() as budget:
            self.assertEqual(other_api.get_json(URL)["id"], "m1")
            with self.assertRaises(RequestBudgetExhausted):
                api.get_json(URL)
        self.assertEqual((graph.calls, len(other.calls), budget.refused), ([], 2, 1))
        # One budget over both: their requests share one bound.
        with RequestBudget([transport, other_transport], 3).active() as shared:
            api.get_json(URL)                       # 2: token + GET
            other_api.get_json(URL)                 # 3: GET (token held)
            with self.assertRaises(RequestBudgetExhausted):
                api.get_json(URL)
            with self.assertRaises(RequestBudgetExhausted):
                other_api.get_json(URL)
        self.assertEqual((shared.used, shared.refused), (3, 2))

    def test_the_read_only_guard_still_refuses_first_and_counts_nothing(self):
        sender = FakeSender()
        transport = GuardedTransport(sender, environ=ENV)
        with RequestBudget(transport, 0).active() as budget:
            with self.assertRaises(ReadOnlyGuardError):
                transport.request("DELETE", URL, {})
        self.assertEqual((sender.calls, budget.used, budget.refused), ([], 0, 0))


class RequestDeadlineTests(unittest.TestCase):
    """``RequestDeadline``: one wall clock over every request sent in its context (the
    case pick's ``PICK_BUDGET_SECONDS``); each request waits at most the time left, one
    with too little left is refused before I/O and never retried, and deadlines nest by
    intersection."""

    def setUp(self):
        self.now = [0.0]
        self.sender = FakeSender({MSG_PATH + "/m1": j(200, {"id": "m1"})})
        self.api, self.transport = graph_read.build_graph(ENV, sender=self.sender,
                                                          retry=BoundedBackoff(
            sleep=lambda s: None, jitter=lambda: 0.0))

    def deadline(self, seconds):
        return RequestDeadline(seconds, clock=lambda: self.now[0])

    def test_each_request_waits_at_most_the_time_left_and_none_past_it_is_sent(self):
        with self.deadline(10).active() as deadline:
            self.api.get_json(URL)              # token POST + GET, both within 10 s
            self.assertTrue(all(t <= 10 for t in self.sender.timeouts), self.sender.timeouts)
            self.now[0] = 7.5
            self.api.get_json(URL)
            self.assertEqual(self.sender.timeouts[-1], 2.5)
            sent = len(self.sender.calls)
            self.now[0] = 9.5                   # under MIN_SECONDS left: refused
            with self.assertRaises(RequestDeadlinePassed) as caught:
                self.api.get_json(URL)
            self.assertIsInstance(caught.exception, RequestBudgetExhausted)
        # Refused before I/O and not retried by the backoff.
        self.assertEqual((len(self.sender.calls), deadline.refused, deadline.passed),
                         (sent, 1, True))
        # Outside the block nothing is enforced.
        self.api.get_json(URL)
        self.assertEqual(len(self.sender.calls), sent + 1)

    def test_deadlines_nest_by_intersection(self):
        outer = self.deadline(5)
        with outer.active():
            with self.deadline(100).active() as inner:
                self.api.get_json(URL)          # the inner one never lifts the outer
                self.assertTrue(all(t <= 5 for t in self.sender.timeouts))
                self.now[0] = 4.5
                with self.assertRaises(RequestDeadlinePassed):
                    self.api.get_json(URL)
        self.assertEqual((outer.refused, inner.refused), (1, 0))

    def test_invalid_seconds_are_rejected(self):
        for seconds in (-1, True, "3", None):
            with self.assertRaises(ValueError):
                RequestDeadline(seconds)
        self.assertTrue(RequestDeadline(0).passed)


if __name__ == "__main__":
    unittest.main()
