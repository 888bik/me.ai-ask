import io
import json
import queue
import threading
import time
import unittest

import winshun


class _PipeInput:
    """Stands in for sys.stdin.buffer: readline blocks until a line is pushed."""

    def __init__(self):
        self.lines = queue.Queue()

    def readline(self):
        return self.lines.get()


def frame(method, message_id=1, params=None):
    payload = {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}}
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


class ShowSettingsTests(unittest.TestCase):
    def setUp(self):
        self._saved_out = winshun._out
        self.out = io.BytesIO()
        winshun._out = self.out
        self.addCleanup(self._restore)

    def _restore(self):
        winshun._out = self._saved_out

    def run_loop(self, messages, on_query, on_show_settings=None):
        plugin = winshun.Plugin(on_query, name="me.ai-ask", on_show_settings=on_show_settings)
        plugin._in = io.BytesIO(b"".join(messages))
        plugin.serve()
        return [json.loads(line) for line in self.out.getvalue().decode("utf-8").splitlines() if line.strip()]

    def test_replies_empty_result_and_keeps_answering(self):
        seen = []

        def on_show(plugin):
            seen.append("show")

        def on_query(query, plugin):
            seen.append("query")
            return [winshun.item("a", "答案")]

        messages = self.run_loop(
            [
                frame("initialize"),
                frame("showSettings", 2),
                frame("query", 3, {"queryId": 1, "text": "x", "command": "ai"}),
            ],
            on_query,
            on_show,
        )

        self.assertEqual(seen, ["show", "query"])
        by_id = {message.get("id"): message for message in messages}
        self.assertEqual(by_id[2]["result"], {})
        self.assertEqual(by_id[3]["result"]["items"][0]["title"], "答案")

    def test_without_handler_the_request_still_gets_an_empty_result(self):
        messages = self.run_loop([frame("initialize"), frame("showSettings", 2)], lambda query, plugin: [])

        by_id = {message.get("id"): message for message in messages}
        self.assertEqual(by_id[2]["result"], {})

    def test_query_is_answered_while_a_window_thread_is_busy(self):
        def on_show(plugin):
            threading.Thread(target=time.sleep, args=(0.5,), daemon=True).start()

        started = time.monotonic()
        messages = self.run_loop(
            [frame("initialize"), frame("showSettings", 2), frame("query", 3, {"queryId": 1})],
            lambda query, plugin: [winshun.item("a", "答案")],
            on_show,
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.3)
        by_id = {message.get("id"): message for message in messages}
        self.assertEqual(by_id[3]["result"]["items"][0]["title"], "答案")


class CrossThreadToolTests(unittest.TestCase):
    def setUp(self):
        self._saved_out = winshun._out
        self.out = io.BytesIO()
        winshun._out = self.out
        self.addCleanup(self._restore)

    def _restore(self):
        winshun._out = self._saved_out

    def _wait_for_request(self, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for line in self.out.getvalue().decode("utf-8").splitlines():
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if message.get("method") == "winshun/tool":
                    return message
            time.sleep(0.01)
        return None

    def test_tool_from_another_thread_gets_the_routed_answer(self):
        plugin = winshun.Plugin(lambda query, plugin: [])
        plugin._in = _PipeInput()
        threading.Thread(target=plugin.serve, daemon=True).start()

        result = {}

        def call():
            result["value"] = plugin.tool("clipboard_history", limit=1)

        caller = threading.Thread(target=call, daemon=True)
        caller.start()

        request = self._wait_for_request()
        self.assertIsNotNone(request, "插件没有发出 winshun/tool 请求")

        plugin._in.lines.put(
            (
                json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"items": [{"text": "sk"}]}})
                + "\n"
            ).encode("utf-8")
        )
        caller.join(5)
        plugin._in.lines.put(b"")

        self.assertEqual(result.get("value"), {"items": [{"text": "sk"}]})

    def test_tool_error_from_another_thread_is_returned(self):
        plugin = winshun.Plugin(lambda query, plugin: [])
        plugin._in = _PipeInput()
        threading.Thread(target=plugin.serve, daemon=True).start()

        result = {}

        def call():
            result["value"] = plugin.tool("clipboard_history")

        caller = threading.Thread(target=call, daemon=True)
        caller.start()

        request = self._wait_for_request()
        self.assertIsNotNone(request)

        plugin._in.lines.put(
            (
                json.dumps({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32000, "message": "denied"}})
                + "\n"
            ).encode("utf-8")
        )
        caller.join(5)
        plugin._in.lines.put(b"")

        self.assertEqual(result.get("value"), {"error": {"code": -32000, "message": "denied"}})


if __name__ == "__main__":
    unittest.main()
