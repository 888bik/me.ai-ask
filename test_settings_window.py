import io
import shutil
import time
import unittest
from contextlib import redirect_stderr
from unittest import mock

import main
import settings_window
import store
from test_support import make_data_dir


class _Plugin:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class WindowThreadTests(unittest.TestCase):
    def setUp(self):
        settings_window._state.update(thread=None, root=None, window=None)
        self.dir = make_data_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_second_call_raises_the_open_window_instead_of_opening_another(self):
        calls = []

        def fake_run(data_dir, on_saved=None, clipboard_text="", clipboard_reader=None):
            calls.append(data_dir)
            time.sleep(0.3)

        with mock.patch.object(settings_window, "_run", fake_run):
            first = settings_window.show_window(str(self.dir))
            second = settings_window.show_window(str(self.dir))

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(calls, [str(self.dir)])

    def test_returns_before_the_window_blocks(self):
        with mock.patch.object(
            settings_window,
            "_run",
            lambda data_dir, on_saved=None, clipboard_text="", clipboard_reader=None: time.sleep(0.5),
        ):
            started = time.monotonic()
            settings_window.show_window(str(self.dir))
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)

    def test_main_handler_returns_fast(self):
        with mock.patch.object(
            settings_window,
            "_run",
            lambda data_dir, on_saved=None, clipboard_text="", clipboard_reader=None: time.sleep(0.5),
        ):
            started = time.monotonic()
            main.on_show_settings(_Plugin(str(self.dir)))
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)

    def test_main_handler_opens_the_window_and_refreshes_the_cache(self):
        plugin = _Plugin(str(self.dir))

        with mock.patch.object(settings_window, "show_window", return_value=True) as show:
            main.on_show_settings(plugin)

        show.assert_called_once()
        args = show.call_args.args
        self.assertEqual(args[0], str(self.dir))
        self.assertIs(args[1], main.refresh_settings_cache)
        self.assertEqual(args[2], "")
        self.assertTrue(callable(args[3]))

    def test_main_handler_passes_the_host_clipboard(self):
        plugin = _Plugin(str(self.dir))
        plugin.tool = mock.Mock(return_value={"items": [{"text": "sk-host", "kind": "text"}]})

        with mock.patch.object(settings_window, "show_window", return_value=True) as show:
            main.on_show_settings(plugin)

        show.assert_called_once()
        args = show.call_args.args
        self.assertEqual(args[0], str(self.dir))
        self.assertIs(args[1], main.refresh_settings_cache)
        self.assertEqual(args[2], "sk-host")
        self.assertTrue(callable(args[3]))
        plugin.tool.assert_called_once_with("clipboard_history", limit=1, kind="text", max_chars=20000)


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.dir = make_data_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        store.save_config(self.dir, {"default_vendor": "deepseek", "model_deepseek": "deepseek-chat"})

    def make_draft(self):
        with mock.patch("store.read_secret", return_value=""):
            return settings_window.Draft(self.dir)

    def test_fresh_draft_is_clean(self):
        self.assertFalse(self.make_draft().dirty())

    def test_edits_mark_it_dirty(self):
        draft = self.make_draft()
        draft.keys["deepseek"] = "sk-new"

        self.assertTrue(draft.dirty())

    def test_save_writes_every_pages_edits(self):
        draft = self.make_draft()
        draft.keys["deepseek"] = "sk-new"
        draft.config["system_prompt"] = "短答"

        with mock.patch("store.write_secret") as write_secret:
            draft.save()

        self.assertEqual(store.load_config(self.dir)["system_prompt"], "短答")
        write_secret.assert_called_once_with("deepseek", "sk-new")
        self.assertFalse(draft.dirty())

    def test_save_does_not_touch_unchanged_keys(self):
        draft = self.make_draft()

        with mock.patch("store.write_secret") as write_secret:
            draft.save()

        write_secret.assert_not_called()


class _AliveThread:
    """Stands in for the window thread so show_window takes the raise path."""

    def is_alive(self):
        return True


def _tk_root():
    try:
        settings_window._load_tk()
        root = settings_window.tk.Tk()
    except Exception as exc:  # depends on the machine having a display
        raise unittest.SkipTest("没有可用的 Tk：%s" % exc)
    root.withdraw()
    return root


class PageSwapTests(unittest.TestCase):
    def setUp(self):
        self.root = _tk_root()
        self.addCleanup(self.root.destroy)
        self.dir = make_data_dir()
        self.addCleanup(shutil.rmtree, self.dir, True)
        store.save_config(self.dir, {"default_vendor": "deepseek", "model_deepseek": "deepseek-chat"})

    def make_window(self, stored="", on_saved=None, clipboard_text="", clipboard_reader=None):
        with mock.patch("store.read_secret", return_value=stored), \
                mock.patch.object(settings_window, "_probe_clipboard"):
            return settings_window._Window(
                self.root, str(self.dir), on_saved, clipboard_text, clipboard_reader
            )

    def test_stored_key_is_not_prefilled(self):
        window = self.make_window(stored="sk-stored")
        window.select(1)

        self.assertEqual(window.ui["key"].get(), "")
        self.assertIn("已保存", window.ui["key_state"]["text"])

    def test_leaving_the_key_blank_keeps_the_stored_one(self):
        window = self.make_window(stored="sk-stored")
        window.select(1)

        with mock.patch.object(settings_window.messagebox, "showinfo"), \
                mock.patch("store.write_secret") as write_secret:
            window.save()

        write_secret.assert_not_called()

    def test_clear_button_marks_the_key_for_deletion(self):
        window = self.make_window(stored="sk-stored")
        window.select(1)
        window._clear_key("deepseek", window.ui["key"])

        with mock.patch.object(settings_window.messagebox, "showinfo"), \
                mock.patch("store.write_secret") as write_secret:
            window.save()

        write_secret.assert_called_once_with("deepseek", "")

    def test_model_combo_offers_presets_and_stays_editable(self):
        window = self.make_window()
        window.select(1)

        combo = window.ui["model_combo"]
        self.assertIn("deepseek-chat", combo["values"])
        self.assertNotEqual(str(combo["state"]), "readonly")

        window.ui["model"].set("my-custom-model")
        window._flush()
        self.assertEqual(window.draft.config["model_deepseek"], "my-custom-model")

    def test_key_survives_switching_pages_without_saving(self):
        window = self.make_window()
        window.select(1)  # DeepSeek
        window.ui["key"].set("sk-unsaved")

        window.select(2)  # Cloudflare
        window.select(1)  # back to DeepSeek

        self.assertEqual(window.ui["key"].get(), "sk-unsaved")
        self.assertEqual(window.draft.keys["deepseek"], "sk-unsaved")
        self.assertTrue(window.draft.dirty())

    def test_switching_pages_does_not_write_to_disk(self):
        window = self.make_window()
        window.select(1)
        window.ui["key"].set("sk-unsaved")

        window.select(2)

        self.assertEqual(store.load_config(self.dir)["default_vendor"], "deepseek")
        with mock.patch("store.read_secret", return_value=""):
            self.assertFalse(settings_window.Draft(self.dir).dirty())

    def test_close_asks_before_dropping_unsaved_edits(self):
        window = self.make_window()
        window.select(1)
        window.ui["key"].set("sk-unsaved")

        with mock.patch.object(settings_window.messagebox, "askyesnocancel", return_value=False) as ask, \
                mock.patch.object(window.root, "destroy") as destroy:
            window.close()

        ask.assert_called_once()
        destroy.assert_called_once()

    def test_close_without_edits_does_not_ask(self):
        window = self.make_window()

        with mock.patch.object(settings_window.messagebox, "askyesnocancel") as ask, \
                mock.patch.object(window.root, "destroy") as destroy:
            window.close()

        ask.assert_not_called()
        destroy.assert_called_once()

    def test_save_calls_on_saved(self):
        calls = []
        with mock.patch("store.read_secret", return_value=""), \
                mock.patch.object(settings_window, "_probe_clipboard"):
            window = settings_window._Window(self.root, str(self.dir), on_saved=lambda: calls.append("saved"))
        window.select(1)
        window.ui["key"].set("sk-unsaved")

        with mock.patch.object(settings_window.messagebox, "showinfo"):
            window.save()

        self.assertEqual(calls, ["saved"])

    def test_save_button_tells_then_closes(self):
        window = self.make_window()
        window.select(1)
        window.ui["key"].set("sk-unsaved")

        with mock.patch.object(settings_window.messagebox, "showinfo") as told, \
                mock.patch.object(window.root, "destroy") as destroy:
            window._save_and_close()

        told.assert_called_once()
        destroy.assert_called_once()

    def test_failed_save_keeps_the_window_open(self):
        window = self.make_window()
        window.select(1)
        window.ui["key"].set("sk-unsaved")

        with mock.patch.object(settings_window.Draft, "save", side_effect=OSError("disk full")), \
                mock.patch.object(settings_window.messagebox, "showerror") as error, \
                mock.patch.object(window.root, "destroy") as destroy:
            window._save_and_close()

        error.assert_called_once()
        destroy.assert_not_called()

    def test_paste_button_fills_the_key(self):
        window = self.make_window()
        window.select(1)
        window.root.clipboard_get = lambda *args, **kwargs: "sk-from-clipboard"

        window._paste_into_key(window.ui["key"])

        self.assertEqual(window.ui["key"].get(), "sk-from-clipboard")

    def test_paste_failure_warns_instead_of_failing_silently(self):
        window = self.make_window()
        window.select(1)
        window.root.clipboard_get = mock.Mock(side_effect=RuntimeError("no clipboard"))

        with mock.patch.object(settings_window.messagebox, "showwarning") as warned:
            window._paste_into_key(window.ui["key"])

        warned.assert_called_once()
        self.assertEqual(window.ui["key"].get(), "")

    def test_paste_fetches_a_fresh_host_clipboard_on_click(self):
        calls = []

        def reader():
            calls.append(1)
            return "sk-fresh"

        with mock.patch("store.read_secret", return_value=""), \
                mock.patch.object(settings_window, "_probe_clipboard"):
            window = settings_window._Window(self.root, str(self.dir), None, "", reader)
        window.select(1)
        window.root.clipboard_get = mock.Mock(side_effect=RuntimeError("blocked"))

        window._paste_into_key(window.ui["key"])

        self.assertEqual(window.ui["key"].get(), "sk-fresh")
        self.assertEqual(len(calls), 1)

    def test_paste_uses_host_clipboard_when_windows_clipboard_is_blocked(self):
        with mock.patch("store.read_secret", return_value=""), \
                mock.patch.object(settings_window, "_probe_clipboard"):
            window = settings_window._Window(self.root, str(self.dir), None, "sk-host")
        window.select(1)
        window.root.clipboard_get = mock.Mock(side_effect=RuntimeError("blocked"))

        window._paste_into_key(window.ui["key"])

        self.assertEqual(window.ui["key"].get(), "sk-host")

    def test_host_clipboard_that_does_not_look_like_a_key_is_rejected(self):
        with mock.patch("store.read_secret", return_value=""), \
                mock.patch.object(settings_window, "_probe_clipboard"):
            window = settings_window._Window(self.root, str(self.dir), None, "这是刚才复制的一段话")
        window.select(1)
        window.root.clipboard_get = mock.Mock(side_effect=RuntimeError("blocked"))

        with mock.patch.object(settings_window.messagebox, "showwarning") as warned:
            window._paste_into_key(window.ui["key"])

        warned.assert_called_once()
        self.assertEqual(window.ui["key"].get(), "")

    def test_test_connection_reports_success(self):
        window = self.make_window()
        window.select(1)
        window.ui["key"].set("sk-x")

        with mock.patch("main.probe", return_value={"model": "deepseek-chat"}), \
                mock.patch.object(settings_window.messagebox, "showinfo") as told:
            window._run_probe("deepseek", window._probe_settings("deepseek"))
            window.root.update()

        told.assert_called_once()
        self.assertIn("连接成功", told.call_args.args[1])

    def test_test_connection_reports_failure(self):
        window = self.make_window()
        window.select(1)
        window.ui["key"].set("sk-x")

        with mock.patch("main.probe", side_effect=main.AnswerError("http", "HTTP 401：bad key")), \
                mock.patch.object(settings_window.messagebox, "showerror") as error:
            window._run_probe("deepseek", window._probe_settings("deepseek"))
            window.root.update()

        error.assert_called_once()
        self.assertIn("401", error.call_args.args[1])

    def test_test_connection_requires_a_key(self):
        window = self.make_window()
        window.select(1)

        with mock.patch("main.probe") as probe, \
                mock.patch.object(settings_window.messagebox, "showwarning") as warned:
            window._test_connection()

        warned.assert_called_once()
        probe.assert_not_called()

    def test_opening_the_window_logs_clipboard_state(self):
        errors = io.StringIO()

        with mock.patch("store.read_secret", return_value=""), redirect_stderr(errors):
            settings_window._Window(self.root, str(self.dir))

        self.assertIn("me.ai-ask: clipboard", errors.getvalue())

    def test_reopening_refreshes_the_snapshot_of_the_open_window(self):
        window = self.make_window()
        self.addCleanup(settings_window._state.update, thread=None, root=None, window=None)
        settings_window._state.update(thread=_AliveThread(), root=window.root, window=window)

        settings_window.show_window(str(self.dir), None, "sk-new")
        window.root.update()

        self.assertEqual(window.clipboard_text, "sk-new")

    def test_window_is_topmost_and_stays_topmost_when_raised_again(self):
        window = self.make_window()
        self.assertEqual(window.root.attributes("-topmost"), 1)

        self.addCleanup(settings_window._state.update, thread=None, root=None)
        settings_window._state.update(thread=_AliveThread(), root=window.root)

        settings_window.show_window(str(self.dir))
        window.root.update()

        self.assertEqual(window.root.attributes("-topmost"), 1)


if __name__ == "__main__":
    unittest.main()
