import io
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import main
import store
from test_support import make_data_dir

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- helpers

class FakeResponse:
    """Stands in for the object urllib.request.urlopen returns."""

    def __init__(self, lines, status=200):
        self._lines = [line.encode("utf-8") if isinstance(line, str) else line for line in lines]
        self.status = status

    def __iter__(self):
        return iter(self._lines)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return b"".join(self._lines)


def sse_delta(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)


def sse_reasoning(text):
    return "data: " + json.dumps({"choices": [{"delta": {"reasoning_content": text}}]}, ensure_ascii=False)


class FakePlugin:
    """Collects what on_query/on_action send to Win顺."""

    def __init__(self, data_dir="", settings=None):
        self.data_dir = data_dir
        self.settings = settings or {}
        self.partials = []
        self.refreshes = []

    def partial(self, query_id, items, replace_all=False):
        self.partials.append((query_id, list(items), replace_all))

    def refresh(self):
        self.refreshes.append(True)

    def log(self, text, level="info"):
        pass


def settings_with_key(vendor="deepseek", **extra):
    data = {"default_vendor": vendor, "key_" + vendor: "sk-test"}
    data.update(extra)
    return data


# ------------------------------------------------------------------ units

class EndpointTests(unittest.TestCase):
    def test_glm_keeps_its_v4_path(self):
        self.assertEqual(
            main.endpoint(main.VENDORS["glm"]["base"]),
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        )

    def test_trailing_slash_is_not_doubled(self):
        self.assertEqual(
            main.endpoint("https://api.deepseek.com/v1/"),
            "https://api.deepseek.com/v1/chat/completions",
        )

    def test_cloudflare_preset_uses_an_account_id_template(self):
        self.assertEqual(main.VENDORS["cloudflare"]["base"], "")
        self.assertEqual(
            store.CLOUDFLARE_BASE_TEMPLATE,
            "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
        )

    def test_cloudflare_base_is_built_from_the_setting(self):
        base, _model, _key = main.vendor_config(
            {
                "key_cloudflare": "sk-test",
                "model_cloudflare": "cf-model",
                "cloudflare_account_id": "acc123",
            },
            "cloudflare",
        )

        self.assertEqual(
            base, "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1"
        )

    def test_cloudflare_without_account_id_is_an_explicit_error(self):
        with self.assertRaises(main.AnswerError) as caught:
            main.vendor_config(
                {"key_cloudflare": "sk-test", "model_cloudflare": "cf-model"}, "cloudflare"
            )

        self.assertIn("Cloudflare 账号 ID", caught.exception.message)

    def test_every_preset_has_base_and_model(self):
        for key in ("deepseek", "qwen", "kimi", "glm", "openai"):
            with self.subTest(vendor=key):
                self.assertTrue(main.VENDORS[key]["base"].startswith("https://"))
                self.assertTrue(main.VENDORS[key]["model"])


class ParseInputTests(unittest.TestCase):
    def test_prefix_cf(self):
        self.assertEqual(main.parse_input("cf 什么是闭包", "deepseek"), ("cloudflare", "什么是闭包"))

    def test_prefix_ds(self):
        self.assertEqual(main.parse_input("ds 讲个笑话", "openai"), ("deepseek", "讲个笑话"))

    def test_prefix_qw(self):
        self.assertEqual(main.parse_input("qw 你好", "deepseek"), ("qwen", "你好"))

    def test_prefix_kimi(self):
        self.assertEqual(main.parse_input("kimi 你好", "deepseek"), ("kimi", "你好"))

    def test_prefix_glm(self):
        self.assertEqual(main.parse_input("glm 你好", "deepseek"), ("glm", "你好"))

    def test_prefix_gpt(self):
        self.assertEqual(main.parse_input("gpt 你好", "deepseek"), ("openai", "你好"))

    def test_no_prefix_uses_default(self):
        self.assertEqual(main.parse_input("什么是闭包", "qwen"), ("qwen", "什么是闭包"))

    def test_prefix_alone_leaves_empty_question(self):
        self.assertEqual(main.parse_input("cf", "deepseek"), ("cloudflare", ""))

    def test_unknown_prefix_is_part_of_the_question(self):
        self.assertEqual(main.parse_input("xx 你好", "deepseek"), ("deepseek", "xx 你好"))

    def test_only_spaces_is_empty(self):
        self.assertEqual(main.parse_input("   ", "deepseek"), ("deepseek", ""))


class SseTests(unittest.TestCase):
    def test_delta(self):
        self.assertEqual(main.parse_sse_line(sse_delta("你")), ("delta", "你"))

    def test_reasoning_delta(self):
        self.assertEqual(main.parse_sse_line(sse_reasoning("先比较整数")), ("reasoning", "先比较整数"))

    def test_done(self):
        self.assertEqual(main.parse_sse_line("data: [DONE]"), ("done", None))

    def test_usage(self):
        line = 'data: {"choices":[],"usage":{"total_tokens":42}}'
        self.assertEqual(main.parse_sse_line(line), ("usage", {"total_tokens": 42}))

    def test_blank_and_comment_lines_are_ignored(self):
        self.assertIsNone(main.parse_sse_line(""))
        self.assertIsNone(main.parse_sse_line(": keep-alive"))

    def test_broken_json_is_ignored(self):
        self.assertIsNone(main.parse_sse_line("data: {not json"))


class ThrottleTests(unittest.TestCase):
    def test_first_call_is_ready_then_waits(self):
        throttle = main.Throttle(0.15)

        self.assertTrue(throttle.ready(0.0))
        self.assertFalse(throttle.ready(0.05))
        self.assertTrue(throttle.ready(0.16))


class AnswerErrorTests(unittest.TestCase):
    def test_missing_key_names_the_vendor(self):
        with self.assertRaises(main.AnswerError) as caught:
            main.answer({"default_vendor": "kimi"}, "你好", "kimi")

        self.assertEqual(caught.exception.kind, "missing_key")
        self.assertIn("Kimi", caught.exception.message)
        self.assertIn("API Key", caught.exception.message)

    def test_http_error_keeps_status_and_reason(self):
        error = urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)
        with mock.patch("main._urlopen", side_effect=error):
            with self.assertRaises(main.AnswerError) as caught:
                main.answer(settings_with_key(), "你好", "deepseek")

        self.assertEqual(caught.exception.kind, "http")
        self.assertIn("401", caught.exception.message)
        self.assertIn("Unauthorized", caught.exception.message)

    def test_http_error_reads_openai_body_message(self):
        body = io.BytesIO(json.dumps({"error": {"message": "Incorrect API key provided"}}).encode("utf-8"))
        error = urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, body)

        with mock.patch("main._urlopen", side_effect=error):
            with self.assertRaises(main.AnswerError) as caught:
                main.answer(settings_with_key(), "你好", "deepseek")

        self.assertIn("Incorrect API key provided", caught.exception.message)

    def test_http_error_reads_cloudflare_errors_message(self):
        body = io.BytesIO(json.dumps({"errors": [{"message": "Account quota exceeded"}]}).encode("utf-8"))
        error = urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, body)

        with mock.patch("main._urlopen", side_effect=error):
            with self.assertRaises(main.AnswerError) as caught:
                main.answer(
                    settings_with_key("cloudflare", cloudflare_account_id="acc-1"),
                    "你好",
                    "cloudflare",
                )

        self.assertIn("Account quota exceeded", caught.exception.message)

    def test_http_error_body_message_is_cut_to_80_characters(self):
        body = io.BytesIO(json.dumps({"error": {"message": "x" * 200}}).encode("utf-8"))
        error = urllib.error.HTTPError("https://x", 500, "Server Error", {}, body)

        with mock.patch("main._urlopen", side_effect=error):
            with self.assertRaises(main.AnswerError) as caught:
                main.answer(settings_with_key(), "你好", "deepseek")

        detail = caught.exception.message.split("：", 1)[1]
        self.assertLessEqual(len(detail), 80)

    def test_socket_timeout_becomes_timeout_kind(self):
        with mock.patch("main._urlopen", side_effect=socket.timeout("timed out")):
            with self.assertRaises(main.AnswerError) as caught:
                main.answer(settings_with_key(), "你好", "deepseek")

        self.assertEqual(caught.exception.kind, "timeout")
        self.assertEqual(caught.exception.message, "回答超时，请重试")

    def test_refused_connection_is_a_network_error(self):
        reason = ConnectionRefusedError(10061, "拒绝连接")
        with mock.patch("main._urlopen", side_effect=urllib.error.URLError(reason)):
            with self.assertRaises(main.AnswerError) as caught:
                main.answer(settings_with_key(), "你好", "deepseek")

        self.assertEqual(caught.exception.kind, "network")


class StreamingTests(unittest.TestCase):
    def test_text_usage_and_model_are_collected(self):
        lines = [
            sse_delta("你"),
            sse_delta("好"),
            "",
            'data: {"choices":[],"usage":{"total_tokens":42}}',
            "data: [DONE]",
        ]
        with mock.patch("main._urlopen", return_value=FakeResponse(lines)):
            result = main.answer(settings_with_key(), "你好", "deepseek")

        self.assertEqual(result["text"], "你好")
        self.assertEqual(result["tokens"], 42)
        self.assertEqual(result["model"], "deepseek-chat")
        self.assertGreaterEqual(result["elapsed"], 0.0)

    def test_on_delta_receives_growing_text(self):
        seen = []
        lines = [sse_delta("a"), sse_delta("b"), "data: [DONE]"]
        with mock.patch("main._urlopen", return_value=FakeResponse(lines)):
            main.answer(settings_with_key(), "q", "deepseek", on_delta=lambda text, thinking: seen.append((text, thinking)))

        self.assertEqual(seen[-1], ("ab", False))

    def test_model_setting_overrides_the_preset(self):
        lines = [sse_delta("x"), "data: [DONE]"]
        settings = settings_with_key(model_deepseek="deepseek-reasoner")
        with mock.patch("main._urlopen", return_value=FakeResponse(lines)):
            result = main.answer(settings, "q", "deepseek")

        self.assertEqual(result["model"], "deepseek-reasoner")

    def test_request_goes_to_the_configured_base_url(self):
        lines = [sse_delta("x"), "data: [DONE]"]
        settings = {
            "default_vendor": "custom",
            "key_custom": "k",
            "base_url_custom": "http://127.0.0.1:9/v1",
            "model_custom": "m",
        }
        with mock.patch("main._urlopen", return_value=FakeResponse(lines)) as fake:
            main.answer(settings, "q", "custom")

        request = fake.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:9/v1/chat/completions")
        self.assertEqual(fake.call_args.kwargs["timeout"], main.HTTP_TIMEOUT)


class SlowResponse:
    """Streams one chunk, then stalls for `delay` seconds."""

    def __init__(self, delay):
        self.delay = delay

    def __iter__(self):
        yield sse_delta("a").encode("utf-8")
        time.sleep(self.delay)
        yield sse_delta("b").encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class PacedResponse:
    """Streams every chunk after `delay` seconds: slow, but never idle."""

    def __init__(self, chunks, delay):
        self.chunks = chunks
        self.delay = delay

    def __iter__(self):
        for chunk in self.chunks:
            time.sleep(self.delay)
            yield chunk.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class IdleTimeoutTests(unittest.TestCase):
    def test_stalled_stream_times_out(self):
        with mock.patch("main._urlopen", return_value=SlowResponse(0.4)), mock.patch("main.HTTP_TIMEOUT", 0.1):
            with self.assertRaises(main.AnswerError) as caught:
                main.answer(settings_with_key(), "q", "deepseek")

        self.assertEqual(caught.exception.kind, "timeout")

    def test_slow_but_steady_stream_does_not_time_out(self):
        chunks = [sse_delta("a"), sse_delta("b"), sse_delta("c"), sse_delta("d"), "data: [DONE]"]
        response = PacedResponse(chunks, delay=0.06)

        with mock.patch("main._urlopen", return_value=response), mock.patch("main.HTTP_TIMEOUT", 0.15):
            result = main.answer(settings_with_key(), "q", "deepseek")

        self.assertEqual(result["text"], "abcd")


class ReasonerTests(unittest.TestCase):
    def setUp(self):
        main._pending = None
        self.data_dir = make_data_dir()
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        store.save_config(self.data_dir, {"default_vendor": "deepseek", "model_deepseek": "deepseek-reasoner"})

    def test_reasoning_is_thinking_but_never_part_of_the_answer(self):
        plugin = FakePlugin(data_dir=str(self.data_dir))
        query = main.winshun.Query(text="9.11 和 9.9 谁大", command="ai", trigger="keyword", keyword="ai", query_id=3)
        main._pending = {"vendor": "deepseek", "question": "9.11 和 9.9 谁大"}
        lines = [
            sse_reasoning("先比较整数部分"),
            sse_reasoning("再比较小数部分"),
            sse_delta("9.9 更大"),
            "data: [DONE]",
        ]

        with mock.patch("main._urlopen", return_value=FakeResponse(lines)), \
                mock.patch("store.read_secret", return_value="sk-test"):
            items = main.on_query(query, plugin)

        self.assertEqual(items[0]["title"], "9.9 更大")
        self.assertNotIn("先比较", items[0]["title"])
        self.assertNotIn("先比较", items[0]["copy"])
        infos = [call[1][1]["title"] for call in plugin.partials]
        self.assertIn("思考中…", infos)


class QueryRowTests(unittest.TestCase):
    def setUp(self):
        main._pending = None
        self.data_dir = make_data_dir()
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        store.save_config(self.data_dir, {"default_vendor": "deepseek"})

    def plugin(self):
        return FakePlugin(data_dir=str(self.data_dir))

    def query(self, text):
        return main.winshun.Query(text=text, command="ai", trigger="keyword", keyword="ai", query_id=1)

    def run_query(self, query, key="sk-test", plugin=None):
        plugin = plugin or self.plugin()
        with mock.patch("store.read_secret", return_value=key):
            return main.on_query(query, plugin)

    def answered_query(self, query, key="sk-test", plugin=None):
        """Placeholder -> Enter action -> refreshed query, like Win顺 does."""
        plugin = plugin or self.plugin()
        first = self.run_query(query, key=key, plugin=plugin)
        if isinstance(first, dict):
            main._pending = first["items"][0]["data"]
            return self.run_query(query, key=key, plugin=plugin), plugin
        return first, plugin

    def test_hint_row_when_nothing_was_typed(self):
        items = self.run_query(self.query(""))

        self.assertEqual(len(items), 1)
        self.assertFalse(items[0]["valid"])

    def test_hint_row_describes_enter_to_ask(self):
        row = main.hint_row()

        self.assertIn("回车提问", row["subtitle"])
        self.assertNotIn("自动提问", row["subtitle"])
        self.assertNotIn("1.2", row["subtitle"])

    def test_non_empty_query_returns_only_a_placeholder(self):
        with mock.patch("main._urlopen") as fake:
            result = self.run_query(self.query("你好"))

        fake.assert_not_called()
        self.assertEqual(result["rerun"], 2)
        item = result["items"][0]
        self.assertEqual(item["id"], "ask")
        self.assertNotEqual(item.get("valid"), False)
        self.assertEqual(item["data"], {"vendor": "deepseek", "question": "你好"})

    def test_answer_row_then_info_row(self):
        query = self.query("你好")
        lines = [sse_delta("你好呀"), "data: [DONE]"]

        with mock.patch("main._urlopen", return_value=FakeResponse(lines)):
            items, _plugin = self.answered_query(query)

        self.assertEqual(items[0]["title"], "你好呀")
        self.assertEqual(items[0]["copy"], "你好呀")
        self.assertIsNone(items[0].get("valid"))
        self.assertFalse(items[1]["valid"])
        self.assertIn("deepseek-chat", items[1]["title"])

    def test_missing_key_becomes_a_valid_false_hint(self):
        query = self.query("你好")

        with mock.patch("main._urlopen") as fake:
            items, _plugin = self.answered_query(query, key="")

        fake.assert_not_called()
        self.assertFalse(items[0]["valid"])
        self.assertIn("API Key", items[0]["title"])

    def test_http_error_becomes_a_valid_false_row(self):
        query = self.query("你好")
        error = urllib.error.HTTPError("https://x", 429, "Too Many Requests", {}, None)

        with mock.patch("main._urlopen", side_effect=error):
            items, _plugin = self.answered_query(query)

        self.assertFalse(items[0]["valid"])
        self.assertIn("429", items[0]["title"])

    def test_streaming_sends_partials(self):
        plugin = self.plugin()
        query = main.winshun.Query(text="你好", command="ai", trigger="keyword", keyword="ai", query_id=7)
        lines = [sse_delta("一"), sse_delta("二"), "data: [DONE]"]

        with mock.patch("main._urlopen", return_value=FakeResponse(lines)):
            self.answered_query(query, plugin=plugin)

        self.assertTrue(plugin.partials)
        self.assertTrue(all(call[0] == 7 for call in plugin.partials))

    def test_inline_prefix_picks_the_vendor(self):
        query = self.query("cf 你好")
        store.save_config(
            self.data_dir,
            {"default_vendor": "deepseek", "cloudflare_account_id": "acc-9"},
        )

        with mock.patch("main._urlopen", return_value=FakeResponse([sse_delta("x"), "data: [DONE]"])) as fake:
            self.answered_query(query)

        request = fake.call_args.args[0]
        self.assertIn("api.cloudflare.com", request.full_url)
        self.assertIn("/accounts/acc-9/", request.full_url)

    def test_cloudflare_without_account_id_makes_no_request(self):
        query = self.query("cf 你好")

        with mock.patch("main._urlopen") as fake:
            items, _plugin = self.answered_query(query)

        fake.assert_not_called()
        self.assertFalse(items[0]["valid"])
        self.assertIn("Cloudflare 账号 ID", items[0]["title"])

    def test_action_copies_the_answer(self):
        request = main.winshun.ActionRequest(
            action="copy", item={"id": "answer", "title": "答案", "copy": "答案全文"}, text="你好"
        )

        effects = main.on_action(request, FakePlugin())

        self.assertEqual(effects[0], main.winshun.copy("答案全文"))
        self.assertIn({"type": "hide"}, effects)


class EnterAskTests(QueryRowTests):
    def ask_action(self, vendor="deepseek", question="你好", text=""):
        return main.winshun.ActionRequest(
            action="default",
            item={"id": "ask", "data": {"vendor": vendor, "question": question}},
            text=text,
        )

    def test_enter_stores_the_question_and_calls_refresh(self):
        plugin = self.plugin()

        effects = main.on_action(self.ask_action(question="你好"), plugin)

        self.assertIn({"type": "keep"}, effects)
        self.assertEqual(main._pending, {"vendor": "deepseek", "question": "你好"})
        self.assertEqual(plugin.refreshes, [True])

    def test_pending_query_answers_once_then_returns_to_placeholder(self):
        plugin = self.plugin()
        query = self.query("你好")
        with mock.patch("store.read_secret", return_value="sk-test"):
            main.on_action(self.ask_action(question="你好"), plugin)
        lines = [sse_delta("ok"), "data: [DONE]"]

        with mock.patch("main._urlopen", return_value=FakeResponse(lines)) as fake:
            items = self.run_query(query, plugin=plugin)

        self.assertEqual(len(fake.call_args_list), 1)
        self.assertEqual(items[0]["title"], "ok")

        with mock.patch("main._urlopen") as again:
            result = self.run_query(query, plugin=plugin)

        again.assert_not_called()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["items"][0]["id"], "ask")

    def test_typing_without_enter_never_sends_a_request(self):
        plugin = self.plugin()

        for text in ("你", "你好", "你好呀"):
            with mock.patch("main._urlopen") as fake:
                result = self.run_query(self.query(text), plugin=plugin)
            fake.assert_not_called()
            self.assertIsInstance(result, dict)

    def test_enter_reparses_the_latest_input_text(self):
        plugin = self.plugin()

        main.on_action(self.ask_action(question="旧问题", text="ai 新问题"), plugin)

        self.assertEqual(main._pending, {"vendor": "deepseek", "question": "新问题"})

    def test_enter_text_can_switch_the_vendor(self):
        plugin = self.plugin()

        main.on_action(self.ask_action(question="旧问题", text="ai cf 新问题"), plugin)

        self.assertEqual(main._pending, {"vendor": "cloudflare", "question": "新问题"})

    def test_enter_with_empty_text_falls_back_to_item_data(self):
        plugin = self.plugin()

        main.on_action(self.ask_action(question="来自占位", text=""), plugin)

        self.assertEqual(main._pending, {"vendor": "deepseek", "question": "来自占位"})

    def test_a_new_question_drops_the_pending_one_without_http(self):
        plugin = self.plugin()
        main._pending = {"vendor": "deepseek", "question": "旧问题"}

        with mock.patch("main._urlopen") as fake:
            result = self.run_query(self.query("新问题"), plugin=plugin)

        fake.assert_not_called()
        self.assertIsNone(main._pending)
        self.assertEqual(result["items"][0]["data"]["question"], "新问题")


class SettingsCacheTests(unittest.TestCase):
    def setUp(self):
        main.refresh_settings_cache()
        self.dir = make_data_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        store.save_config(self.dir, {"default_vendor": "deepseek"})

    def test_settings_are_read_once_until_refreshed(self):
        with mock.patch("store.read_secret", return_value="sk-test") as read:
            main.cached_settings(self.dir)
            main.cached_settings(self.dir)

        self.assertEqual(read.call_count, len(store.VENDORS))

        with mock.patch("store.read_secret", return_value="sk-test") as read:
            main.refresh_settings_cache()
            main.cached_settings(self.dir)

        self.assertEqual(read.call_count, len(store.VENDORS))


class LazyTkTests(unittest.TestCase):
    def test_importing_main_does_not_load_tkinter(self):
        code = "import sys, main; print('tkinter' in sys.modules)"
        proc = subprocess.run(
            [sys.executable, "-c", code], cwd=HERE, capture_output=True, text=True
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")


class ProbeTests(unittest.TestCase):
    def test_probe_reports_the_served_model(self):
        response = FakeResponse([json.dumps({"model": "deepseek-chat", "choices": []}).encode("utf-8")])

        with mock.patch("main._urlopen", return_value=response) as fake:
            result = main.probe(settings_with_key(), "deepseek")

        self.assertEqual(result["model"], "deepseek-chat")
        request = fake.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(json.loads(request.data.decode("utf-8"))["max_tokens"], 1)

    def test_probe_maps_http_errors(self):
        error = urllib.error.HTTPError("https://x", 401, "Unauthorized", {}, None)

        with mock.patch("main._urlopen", side_effect=error):
            with self.assertRaises(main.AnswerError) as caught:
                main.probe(settings_with_key(), "deepseek")

        self.assertEqual(caught.exception.kind, "http")
        self.assertIn("401", caught.exception.message)

    def test_probe_uses_the_cloudflare_account_id(self):
        response = FakeResponse([json.dumps({"model": "cf-model", "choices": []}).encode("utf-8")])
        settings = settings_with_key("cloudflare", cloudflare_account_id="acc-42")

        with mock.patch("main._urlopen", return_value=response) as fake:
            main.probe(settings, "cloudflare")

        self.assertIn("/accounts/acc-42/ai/v1", fake.call_args.args[0].full_url)

    def test_probe_without_cloudflare_account_id_makes_no_request(self):
        with mock.patch("main._urlopen") as fake:
            with self.assertRaises(main.AnswerError) as caught:
                main.probe(settings_with_key("cloudflare"), "cloudflare")

        fake.assert_not_called()
        self.assertIn("Cloudflare 账号 ID", caught.exception.message)

    def test_probe_maps_timeout(self):
        with mock.patch("main._urlopen", side_effect=socket.timeout()):
            with self.assertRaises(main.AnswerError) as caught:
                main.probe(settings_with_key(), "deepseek")

        self.assertEqual(caught.exception.kind, "timeout")


class ManifestTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "plugin.json"), encoding="utf-8") as handle:
            self.manifest = json.load(handle)

    def test_id_matches_the_folder(self):
        self.assertEqual(self.manifest["id"], "me.ai-ask")
        self.assertEqual(self.manifest["id"], os.path.basename(HERE))

    def test_identity(self):
        self.assertEqual(self.manifest["id"], "me.ai-ask")
        self.assertEqual(self.manifest["id"], os.path.basename(HERE))
        self.assertEqual(self.manifest["version"], "1.2.4")

    def test_permissions_are_network_ui_and_clipboard_read(self):
        self.assertEqual(sorted(self.manifest["permissions"]), ["clipboard:read", "network", "ui"])

    def test_runtime_limits(self):
        runtime = self.manifest["runtime"]
        self.assertEqual(runtime["mode"], "resident")
        self.assertEqual(runtime["timeoutMs"], 10000)
        self.assertEqual(runtime["debounceMs"], 300)
        self.assertEqual(runtime["start"], "eager")

    def test_copy_matches_enter_to_ask(self):
        description = self.manifest["description"]
        hint = self.manifest["commands"][0]["hint"]
        for text in (description["zh"], description["en"], hint["zh"], hint["en"]):
            self.assertNotIn("自动提问", text)
            self.assertNotIn("停顿 1.2 秒", text)
        self.assertIn("回车", description["zh"])
        self.assertIn("回车", hint["zh"])

    def test_declares_the_ai_keyword(self):
        self.assertEqual(self.manifest["commands"][0]["keyword"], "ai")

    def test_top_level_ai_field_is_not_used(self):
        self.assertNotIn("ai", self.manifest)

    def test_custom_settings_window_is_enabled(self):
        self.assertTrue(self.manifest["settingsWindow"])

    def test_builtin_settings_are_gone(self):
        self.assertNotIn("settings", self.manifest)


# -------------------------------------------------------------- end to end

class _SseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for piece in ("你好", "，世界"):
            payload = {"choices": [{"delta": {"content": piece}}]}
            self.wfile.write(("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()
        usage = {"choices": [], "usage": {"total_tokens": 7}}
        self.wfile.write(("data: " + json.dumps(usage) + "\n\n").encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, *args):
        pass


class ResidentProcess:
    def __init__(self):
        self.process = subprocess.Popen(
            [sys.executable, "-u", os.path.join(HERE, "main.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=HERE,
        )
        self.lines = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for raw in self.process.stdout:
            self.lines.put(raw.decode("utf-8", "replace"))

    def send(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def receive(self, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                return json.loads(self.lines.get(timeout=0.2))
            except queue.Empty:
                continue
        self.stop()
        raise AssertionError("插件没有在 %s 秒内回应" % timeout)

    def receive_until(self, message_id, timeout=20):
        seen = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.receive(timeout=max(0.1, deadline - time.time()))
            if message.get("id") == message_id:
                return message, seen
            seen.append(message)
        self.stop()
        raise AssertionError("没等到 id=%s 的回应" % message_id)

    def stop(self):
        try:
            self.process.stdin.close()
        except Exception:
            pass
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except Exception:
                pass


class ResidentEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _SseHandler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

        self.data_dir = make_data_dir()
        self.addCleanup(shutil.rmtree, self.data_dir, True)
        store.save_config(self.data_dir, {
            "default_vendor": "custom",
            "base_url_custom": "http://127.0.0.1:%d/v1" % self.port,
            "model_custom": "test-model",
        })
        self._install_test_secret()
        self.plugin = ResidentProcess()

    def _install_test_secret(self):
        if os.name != "nt":
            self.skipTest("凭据管理器只在 Windows 上有")
        if store.read_secret("custom"):
            self.skipTest("已有 custom 厂商凭据，跳过以免覆盖")
        try:
            store.write_secret("custom", "test-key")
        except OSError:
            self.skipTest("写不进测试凭据")
        self.addCleanup(store.delete_secret, "custom")

    def tearDown(self):
        self.plugin.stop()
        self.server.shutdown()
        self.server.server_close()

    def initialize(self):
        self.plugin.send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "1",
                    "pluginDir": HERE,
                    "dataDir": str(self.data_dir),
                    "locale": "zh",
                    "theme": "light",
                },
            }
        )
        reply = self.plugin.receive()
        self.assertEqual(reply["result"]["protocolVersion"], "1")

    def test_query_waits_for_enter_then_streams_the_answer(self):
        self.initialize()
        query_message = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "query",
            "params": {"queryId": 5, "command": "ai", "trigger": "keyword", "keyword": "ai", "text": "你好", "raw": "ai 你好"},
        }

        self.plugin.send(query_message)
        first, _seen = self.plugin.receive_until(2)
        placeholder = first["result"]["items"][0]
        self.assertEqual(placeholder["id"], "ask")
        self.assertEqual(first["result"]["rerun"], 2)

        self.plugin.send(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "action",
                "params": {"action": "default", "item": placeholder, "text": "ai 你好"},
            }
        )
        action_reply, action_seen = self.plugin.receive_until(3)
        self.assertIn({"type": "keep"}, action_reply["result"]["effects"])
        self.assertTrue([m for m in action_seen if m.get("method") == "winshun/refresh"])

        refreshed = dict(query_message, id=4)
        self.plugin.send(refreshed)
        reply, seen = self.plugin.receive_until(4)

        items = reply["result"]["items"]
        self.assertEqual(items[0]["title"], "你好，世界")
        self.assertEqual(items[0]["copy"], "你好，世界")
        self.assertFalse(items[1]["valid"])
        self.assertIn("test-model", items[1]["title"])
        self.assertIn("7", items[1]["title"])
        self.assertTrue([message for message in seen if message.get("method") == "query/partial"])

    def test_shutdown_is_acknowledged(self):
        self.initialize()

        self.plugin.send({"jsonrpc": "2.0", "id": 9, "method": "shutdown", "params": {}})
        reply = self.plugin.receive()

        self.assertEqual(reply["id"], 9)
        self.plugin.process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
