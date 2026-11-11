"""Custom settings window (tkinter) for me.ai-ask.

Edits live in an in-memory draft: switching pages only rebuilds the widgets,
so nothing typed is lost before 保存. 保存 writes config.json and the changed
credentials in one go, and closing with unsaved edits asks first.

WinShun sends ``showSettings`` and expects an answer within 10 seconds, so the
JSON-RPC loop must not build the UI itself. ``show_window()`` starts a thread
and returns at once; a second request raises the open window.
"""

import sys
import threading

import store

# Imported on demand by _load_tk(), inside the window thread only: a resident
# plugin that never opens its settings must not pay for tkinter.
tk = None
ttk = None
messagebox = None

_TITLES = [(vendor, store.VENDORS[vendor]["name"]) for vendor in store.VENDOR_ORDER]

_state = {"thread": None, "root": None, "window": None}
_lock = threading.Lock()


def _load_tk():
    """Import tkinter once; every Tk call happens after this in the window thread."""
    global tk, ttk, messagebox
    if tk is not None:
        return
    import tkinter as _tk
    from tkinter import messagebox as _messagebox
    from tkinter import ttk as _ttk

    tk = _tk
    ttk = _ttk
    messagebox = _messagebox


class Draft:
    """Everything the window has edited but not saved yet."""

    def __init__(self, data_dir, config=None, keys=None):
        self.data_dir = str(data_dir)
        self.config = dict(config if config is not None else store.load_config(data_dir))
        self.keys = dict(
            keys if keys is not None else {vendor: store.read_secret(vendor) for vendor in store.VENDOR_ORDER}
        )
        self.original_config = dict(self.config)
        self.original_keys = dict(self.keys)

    def dirty(self):
        return self.config != self.original_config or self.keys != self.original_keys

    def save(self):
        store.save_config(self.data_dir, self.config)
        for vendor, secret in self.keys.items():
            if secret != self.original_keys.get(vendor):
                store.write_secret(vendor, secret)
        self.original_config = dict(self.config)
        self.original_keys = dict(self.keys)


def show_window(data_dir, on_saved=None, clipboard_text="", clipboard_reader=None):
    """Start the window, or raise the one already open. True when it started one."""
    with _lock:
        thread = _state["thread"]
        if thread is not None and thread.is_alive():
            window = _state.get("window")
            if window is not None:
                window.clipboard_reader = clipboard_reader
                try:
                    window.root.after(0, window.set_clipboard_text, clipboard_text or "")
                except Exception:
                    pass
            root = _state["root"]
            if root is not None:
                try:
                    root.after(0, _raise, root)
                except Exception:
                    pass
            return False

        thread = threading.Thread(
            target=_run,
            args=(str(data_dir), on_saved, clipboard_text or "", clipboard_reader),
            name="me.ai-ask-settings",
            daemon=True,
        )
        _state["thread"] = thread
        thread.start()
        return True


def _raise(root):
    try:
        # Bring it forward once, but do not pin it above other apps.
        root.attributes("-topmost", False)
        root.deiconify()
        root.lift()
        root.focus_force()
    except Exception:
        pass


def _job_ui_flags():
    """JOBOBJECT_BASIC_UI_RESTRICTIONS of this process's job, as hex, or "?"."""
    try:
        import ctypes
        from ctypes import wintypes

        class _UI(ctypes.Structure):
            _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]

        info = _UI()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        ok = kernel32.QueryInformationJobObject(None, 4, ctypes.byref(info), ctypes.sizeof(info), None)
        return "0x%x" % info.UIRestrictionsClass if ok else "?"
    except Exception:
        return "?"


def _log_clipboard(ok, error=None, note=""):
    """One stderr line (plugins.log): clipboard state, never its content."""
    try:
        extra = "" if ok else " error=%r" % (error,)
        if note:
            extra += " " + note
        sys.stderr.write(
            "me.ai-ask: clipboard %s job_ui_flags=%s%s\n" % ("ok" if ok else "blocked", _job_ui_flags(), extra)
        )
        sys.stderr.flush()
    except Exception:
        pass


def _probe_clipboard(root, note=""):
    try:
        root.clipboard_get()
        _log_clipboard(True, None, note)
    except Exception as exc:
        _log_clipboard(False, exc, note)


def _run(data_dir, on_saved=None, clipboard_text="", clipboard_reader=None):
    _load_tk()
    root = tk.Tk()
    _state["root"] = root
    try:
        window = _Window(root, data_dir, on_saved, clipboard_text, clipboard_reader)
        _state["window"] = window
        root.protocol("WM_DELETE_WINDOW", window.close)
        root.mainloop()
    finally:
        _state["window"] = None
        _state["root"] = None


def _title_for_vendor(vendor):
    for key, title in _TITLES:
        if key == vendor:
            return title
    return _TITLES[0][1]


def _vendor_for_title(title):
    for key, name in _TITLES:
        if name == title:
            return key
    return _TITLES[0][0]


class _Window:
    def __init__(self, root, data_dir, on_saved=None, clipboard_text="", clipboard_reader=None):
        self.root = root
        self.data_dir = str(data_dir)
        self.on_saved = on_saved
        self.clipboard_text = clipboard_text or ""
        self.clipboard_reader = clipboard_reader
        self.draft = Draft(self.data_dir)
        self.ui = _blank_ui()
        self.names = None
        self.right = None
        self._build()

    def set_clipboard_text(self, text):
        """A newer snapshot from WinShun, for a window that is already open."""
        self.clipboard_text = text or ""

    # -- construction ---------------------------------------------------
    def _build(self):
        # Stay above Win顺's always-on-top launcher window.
        self.root.title("问 AI · 设置")
        self.root.geometry("780x470")
        self.root.minsize(660, 400)

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        buttons = ttk.Frame(outer)
        buttons.pack(side="bottom", fill="x", pady=(10, 0))

        content = ttk.Frame(outer)
        content.pack(side="top", fill="both", expand=True)

        left = ttk.Frame(content, width=190)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        self.right = ttk.Frame(content, padding=(16, 0, 0, 0))
        self.right.pack(side="left", fill="both", expand=True)

        self.names = tk.Listbox(left, exportselection=False, activestyle="none")
        self.names.pack(fill="both", expand=True)
        self.names.insert("end", "通用")
        for _vendor, title in _TITLES:
            self.names.insert("end", title)
        self.names.bind("<<ListboxSelect>>", self._on_list_select)

        ttk.Button(buttons, text="关闭", command=self.close).pack(side="right")
        ttk.Button(buttons, text="保存", command=self._save_and_close).pack(side="right", padx=(0, 8))

        _probe_clipboard(self.root, "host-clipboard=%s" % ("yes" if self.clipboard_text else "no"))
        self.select(0)

    def _on_list_select(self, _event=None):
        selection = self.names.curselection()
        self.select(selection[0] if selection else 0)

    # -- pages ----------------------------------------------------------
    def select(self, index):
        """Show page `index`; edits of the page left behind go into the draft."""
        self._flush()
        for child in self.right.winfo_children():
            child.destroy()
        self.ui = _blank_ui()
        if index == 0:
            self._render_general()
        else:
            self.ui["vendor"] = _TITLES[index - 1][0]
            self._render_vendor(self.ui["vendor"])
        self.names.selection_clear(0, "end")
        self.names.selection_set(index)

    def _render_general(self):
        ttk.Label(self.right, text="通用", font=("TkDefaultFont", 12, "bold")).pack(anchor="w", pady=(0, 12))

        ttk.Label(self.right, text="默认厂商").pack(anchor="w")
        combo = ttk.Combobox(
            self.right, state="readonly", values=[title for _vendor, title in _TITLES]
        )
        combo.set(_title_for_vendor(self.draft.config.get("default_vendor")))
        combo.pack(fill="x", pady=(4, 12))
        self.ui["combo"] = combo

        ttk.Label(self.right, text="系统提示词").pack(anchor="w")
        prompt = tk.Text(self.right, height=8, wrap="word")
        prompt.insert("1.0", self.draft.config.get("system_prompt", store.SYSTEM_PROMPT_DEFAULT))
        prompt.pack(fill="both", expand=True, pady=(4, 0))
        self.ui["prompt"] = prompt

    def _render_vendor(self, vendor):
        ttk.Label(
            self.right, text=store.VENDORS[vendor]["name"], font=("TkDefaultFont", 12, "bold")
        ).pack(anchor="w", pady=(0, 12))

        stored = self.draft.original_keys.get(vendor, "")
        current = self.draft.keys.get(vendor, "")
        # A stored key is never shown: only a pending edit appears in the box.
        shown = current if current != stored else ""

        ttk.Label(self.right, text="API Key").pack(anchor="w")
        key_row = ttk.Frame(self.right)
        key_row.pack(fill="x", pady=(4, 0))
        key = tk.StringVar(value=shown)
        entry = ttk.Entry(key_row, textvariable=key, show="*")
        entry.pack(side="left", fill="x", expand=True)
        ttk.Button(key_row, text="粘贴", command=lambda: self._paste_into_key(key)).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(key_row, text="清除", command=lambda: self._clear_key(vendor, key)).pack(
            side="left", padx=(6, 0)
        )
        entry.bind("<Control-v>", lambda event: (self._paste_into_key(key), "break")[1])
        self.ui["key"] = key

        state = ttk.Label(self.right, text=self._key_state_text(vendor))
        state.pack(anchor="w", pady=(2, 12))
        self.ui["key_state"] = state

        ttk.Label(self.right, text="模型").pack(anchor="w")
        model = tk.StringVar(value=self.draft.config.get("model_" + vendor, ""))
        combo = ttk.Combobox(self.right, textvariable=model, values=store.VENDORS[vendor].get("models", []))
        combo.pack(fill="x", pady=(4, 12))
        self.ui["model"] = model
        self.ui["model_combo"] = combo

        if vendor == "cloudflare":
            ttk.Label(self.right, text="Cloudflare 账号 ID").pack(anchor="w")
            account = tk.StringVar(value=self.draft.config.get("cloudflare_account_id", ""))
            ttk.Entry(self.right, textvariable=account).pack(fill="x", pady=(4, 2))
            ttk.Label(
                self.right,
                text="Cloudflare 控制台右下角的 Account ID",
                foreground="#666666",
            ).pack(anchor="w", pady=(0, 12))
            self.ui["cloudflare_account"] = account

        if vendor == "custom":
            ttk.Label(self.right, text="Base URL").pack(anchor="w")
            base = tk.StringVar(value=self.draft.config.get("base_url_custom", ""))
            ttk.Entry(self.right, textvariable=base).pack(fill="x", pady=(4, 12))
            self.ui["base"] = base

        ttk.Button(self.right, text="测试连接", command=self._test_connection).pack(anchor="w")

    # -- connection test ------------------------------------------------
    def _probe_settings(self, vendor):
        key = (self.ui["key"].get() if self.ui.get("key") else "").strip()
        model = (self.ui["model"].get() if self.ui.get("model") else "").strip()
        model = model or self.draft.config.get("model_" + vendor, "")
        if not key:
            messagebox.showwarning("问 AI", "先填 API Key 再测试。", parent=self.root)
            return None
        if not model:
            messagebox.showwarning("问 AI", "先填模型名再测试。", parent=self.root)
            return None

        settings = {"key_" + vendor: key, "model_" + vendor: model}
        if vendor == "cloudflare":
            account = (
                self.ui["cloudflare_account"].get() if self.ui.get("cloudflare_account") else ""
            )
            settings["cloudflare_account_id"] = account.strip()
        if vendor == "custom":
            base = (self.ui["base"].get() if self.ui.get("base") else "").strip()
            if not base:
                messagebox.showwarning("问 AI", "先填 Base URL 再测试。", parent=self.root)
                return None
            settings["base_url_custom"] = base
        return settings

    def _test_connection(self):
        vendor = self.ui.get("vendor")
        if not vendor:
            return
        settings = self._probe_settings(vendor)
        if settings is None:
            return
        threading.Thread(target=self._run_probe, args=(vendor, settings), daemon=True).start()

    def _run_probe(self, vendor, settings):
        import main

        try:
            result = main.probe(settings, vendor)
            self.root.after(0, self._probe_done, True, result.get("model", ""))
        except main.AnswerError as exc:
            self.root.after(0, self._probe_done, False, exc.message)
        except Exception as exc:
            self.root.after(0, self._probe_done, False, str(exc))

    def _probe_done(self, ok, message):
        if ok:
            text = "连接成功：%s" % message if message else "连接成功。"
            messagebox.showinfo("问 AI", text, parent=self.root)
        else:
            messagebox.showerror("问 AI", "连接失败：\n%s" % message, parent=self.root)

    # -- draft ----------------------------------------------------------
    def _key_state_text(self, vendor):
        stored = self.draft.original_keys.get(vendor, "")
        current = self.draft.keys.get(vendor, "")
        if current == stored:
            return "已保存；留空表示不修改" if stored else "未设置"
        if not current:
            return "保存后删除这个 Key"
        return "未保存的新 Key"

    def _refresh_key_state(self, vendor):
        label = self.ui.get("key_state")
        if label is not None:
            label.configure(text=self._key_state_text(vendor))

    def _clear_key(self, vendor, variable):
        self.draft.keys[vendor] = ""
        variable.set("")
        self._refresh_key_state(vendor)

    def _flush(self):
        """Copy the visible widgets into the draft."""
        if self.ui.get("combo") is not None:
            self.draft.config["default_vendor"] = _vendor_for_title(self.ui["combo"].get())
        if self.ui.get("prompt") is not None:
            self.draft.config["system_prompt"] = self.ui["prompt"].get("1.0", "end").strip()
        vendor = self.ui.get("vendor")
        if vendor:
            if self.ui.get("model") is not None:
                self.draft.config["model_" + vendor] = self.ui["model"].get().strip()
            if self.ui.get("base") is not None:
                self.draft.config["base_url_custom"] = self.ui["base"].get().strip()
            if self.ui.get("cloudflare_account") is not None:
                self.draft.config["cloudflare_account_id"] = (
                    self.ui["cloudflare_account"].get().strip()
                )
            if self.ui.get("key") is not None:
                value = self.ui["key"].get().strip()
                if value:
                    self.draft.keys[vendor] = value
                self._refresh_key_state(vendor)

    # -- actions --------------------------------------------------------
    def _paste_into_key(self, variable):
        try:
            text = self.root.clipboard_get()
        except Exception as exc:
            text = self.clipboard_text
            if self.clipboard_reader is not None:
                try:
                    text = self.clipboard_reader() or text
                except Exception:
                    pass
            candidate = (text or "").strip()
            looks_like_key = (
                bool(candidate)
                and len(candidate) <= 512
                and candidate.isascii()
                and not any(ch.isspace() for ch in candidate)
            )
            if looks_like_key:
                _log_clipboard(True, None, "via=host")
                variable.set(candidate)
                self._refresh_key_state(self.ui.get("vendor"))
                return
            _log_clipboard(False, exc, "host-clipboard=%s" % ("other" if candidate else "empty"))
            messagebox.showwarning(
                "问 AI",
                "读不到剪贴板。\n\n"
                "Win顺 目前禁止插件进程直接读剪贴板（作业限制），备用通道（Win顺 剪贴板历史）也没取到可用的 Key。\n"
                "请确认 Win顺 的剪贴板历史是开着的；复制 API Key 后重新打开本窗口，再点「粘贴」。",
                parent=self.root,
            )
            return
        _log_clipboard(True)
        variable.set(str(text).strip())
        self._refresh_key_state(self.ui.get("vendor"))

    def save(self, notify=True):
        self._flush()
        try:
            self.draft.save()
        except Exception as exc:
            messagebox.showerror("问 AI", "保存失败：%s" % exc, parent=self.root)
            return False
        if notify:
            messagebox.showinfo("问 AI", "设置已保存。", parent=self.root)
        if self.on_saved is not None:
            try:
                self.on_saved()
            except Exception:
                pass
        return True

    def _save_and_close(self):
        if self.save():
            self.root.destroy()

    def close(self):
        self._flush()
        if not self.draft.dirty():
            self.root.destroy()
            return

        answer = messagebox.askyesnocancel("问 AI", "有未保存的修改，要保存吗？", parent=self.root)
        if answer is None:
            return
        if answer and not self.save(notify=False):
            return
        self.root.destroy()


def _blank_ui():
    return {
        "vendor": None,
        "combo": None,
        "prompt": None,
        "key": None,
        "key_state": None,
        "model": None,
        "model_combo": None,
        "base": None,
        "cloudflare_account": None,
    }
