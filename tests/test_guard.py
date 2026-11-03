import contextlib
import copy
import io
import json
from pathlib import Path
import runpy
import unittest
from concurrent.futures import ThreadPoolExecutor

from proxy_loop_guard import DEFAULT_CONFIG, LoopDetected, LoopGuard


def request(count=0, *, tool="Read", varying=False, result=None):
    messages = [{"role": "user", "content": "Inspect the project"}]
    for i in range(count):
        arguments = {"file_path": f"/project/{i if varying else 'same'}.py"}
        messages.append({
            "role": "assistant",
            "tool_calls": [{"id": str(i), "type": "function", "function": {
                "name": tool, "arguments": json.dumps(arguments),
            }}],
        })
        if result is not None:
            messages.append({"role": "tool", "tool_call_id": str(i), "content": result})
    return {"model": "demo-model", "messages": messages}


class PublicApiTests(unittest.TestCase):
    def test_below_threshold_is_unchanged(self):
        req = request(4)
        original = copy.deepcopy(req)
        self.assertIs(LoopGuard().check(req, session_id="one"), req)
        self.assertEqual(req, original)

    def test_warning_and_deduplication(self):
        guard = LoopGuard()
        req = request(5)
        before = len(req["messages"])
        guard.check(req, session_id="one")
        self.assertEqual(len(req["messages"]), before + 1)
        self.assertIn("[proxy loop-guard]", req["messages"][-1]["content"])
        guard.check(req, session_id="one")
        self.assertEqual(len(req["messages"]), before + 1)
        self.assertNotIn("_session_hint", req)

    def test_block_and_hint_cleanup(self):
        req = request(12)
        with self.assertRaises(LoopDetected) as caught:
            LoopGuard().check(req, session_id="one")
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("12x", caught.exception.detail)
        self.assertNotIn("_session_hint", req)

    def test_blocked_request_never_reaches_upstream(self):
        forwarded = []
        guard = LoopGuard()

        def proxy(req):
            try:
                guard.check(req, session_id="one")
            except LoopDetected:
                return "blocked"
            forwarded.append(req)
            return "forwarded"

        self.assertEqual(proxy(request(12)), "blocked")
        self.assertEqual(forwarded, [])
        self.assertEqual(proxy(request(1)), "forwarded")
        self.assertEqual(len(forwarded), 1)

    def test_instances_and_configs_are_isolated(self):
        first = LoopGuard({"generic_warn": 2})
        second = LoopGuard()
        self.assertEqual(DEFAULT_CONFIG["generic_warn"], 5)
        req_a, req_b = request(2), request(2)
        first.check(req_a, session_id="same")
        second.check(req_b, session_id="same")
        self.assertEqual(req_a["messages"][-1]["role"], "user")
        self.assertEqual(req_b["messages"][-1]["role"], "assistant")
        first.reset()
        self.assertTrue(second._loopguard_sessions)

    def test_invalid_configs_are_rejected(self):
        for cfg in ({"typo": 1}, {"generic_warn": 0}, {"generic_warn": True},
                    {"generic_warn": 1.5}, {"generic_warn": 13}, {"window_calls": 10}):
            with self.subTest(config=cfg), self.assertRaises(ValueError):
                LoopGuard(cfg)

    def test_invalid_sessions_and_requests_are_rejected(self):
        guard = LoopGuard()
        for session_id in ("", " ", None, 1):
            with self.subTest(session=session_id), self.assertRaises(ValueError):
                guard.check(request(), session_id=session_id)
        for req in ({}, {"messages": "invalid"}, None):
            with self.subTest(request=req), self.assertRaises(ValueError):
                guard.check(req, session_id="one")

    def test_snapshot_is_detached_and_reset_clears_it(self):
        guard = LoopGuard()
        guard.check(request(5), session_id="one")
        snapshot = guard.snapshot()
        self.assertTrue(snapshot["events"])
        snapshot["events"].clear()
        self.assertTrue(guard.snapshot()["events"])
        guard.reset()
        self.assertEqual(guard.snapshot(), {"events": [], "counters": {}, "updated": None})

    def test_concurrent_sessions_do_not_share_stagnation(self):
        guard = LoopGuard()

        def check_one(i):
            guard.check(request(3, varying=True), session_id=f"session-{i}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(check_one, range(24)))
        self.assertEqual(len(guard._loopguard_sessions), 24)
        self.assertTrue(all(s["stagnant_turns"] == 0 for s in guard._loopguard_sessions.values()))

    def test_stagnation_accumulates_across_requests(self):
        guard = LoopGuard({"generic_warn": 90, "generic_kill": 100})
        for i in range(1, 6):
            req = request(i)
            guard.check(req, session_id="one")
        with self.assertRaises(LoopDetected) as caught:
            guard.check(request(6), session_id="one")
        self.assertIn("stagnation", caught.exception.detail)

    def test_idle_cleanup_preserves_active_sessions(self):
        guard = LoopGuard()
        for i in range(51):
            guard._loopguard_sessions[str(i)] = {"last_seen": 0}
        guard.check(request(), session_id="active")
        self.assertEqual(len(guard._loopguard_sessions), 1)

    def test_original_task_handles_text_blocks(self):
        req = request(5)
        req["messages"][0]["content"] = [{"type": "text", "text": "Inspect the project"}]
        LoopGuard().check(req, session_id="one")
        self.assertIn("Original task: Inspect the project", req["messages"][-1]["content"])

    def test_progress_confirmations_do_not_count_as_no_progress(self):
        for tool in ("Edit", "Write", "update_plan", "TodoWrite"):
            req = request(12, tool=tool, varying=True, result="Updated successfully")
            before = len(req["messages"])
            LoopGuard().check(req, session_id=tool)
            self.assertEqual(len(req["messages"]), before)

    def test_varied_queries_with_identical_empty_results_are_blocked(self):
        req = request(12, tool="Glob", varying=True, result="[]")
        # Glob scopes by path; use one directory with distinct patterns.
        for message in req["messages"]:
            if message.get("tool_calls"):
                call = message["tool_calls"][0]
                call["function"]["arguments"] = json.dumps({"path": "/project", "pattern": call["id"]})
        with self.assertRaises(LoopDetected) as caught:
            LoopGuard().check(req, session_id="one")
        self.assertIn("zero-progress", caught.exception.detail)


class ExtractedRegressionTests(unittest.TestCase):
    def test_production_detector_scenarios(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            runpy.run_path(str(Path(__file__).with_name("regression_scenarios.py")))
        self.assertIn("all extracted loop-guard regression scenarios passed", output.getvalue())


if __name__ == "__main__":
    unittest.main()
