"""me.ai-ask — Win顺 常驻插件。

输入「ai + 空格 + 问题」回车，答案流式出现在结果列表里。
全部厂商走 OpenAI 兼容接口（POST {base_url}/chat/completions，stream: true）。
只用标准库：urllib 发请求，json 解析 SSE。
"""

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import store
import winshun

DEFAULT_VENDOR = "deepseek"
SYSTEM_PROMPT_DEFAULT = store.SYSTEM_PROMPT_DEFAULT
THROTTLE_SEC = 0.15
# Win顺 给每次查询 10 秒（manifest 的 timeoutMs）。留一点余量先回答超时。
HTTP_TIMEOUT = 8.5

VENDORS = store.VENDORS

_settings_cache = {"data_dir": None, "settings": None}
_settings_lock = threading.Lock()


def cached_settings(data_dir):
    """config.json + every vendor key, read once and reused across queries."""
    folder = str(data_dir or "")
    with _settings_lock:
        if _settings_cache["settings"] is not None and _settings_cache["data_dir"] == folder:
            return _settings_cache["settings"]
    settings = store.load_settings(folder)
    with _settings_lock:
        _settings_cache["data_dir"] = folder
        _settings_cache["settings"] = settings
    return settings


def refresh_settings_cache():
    with _settings_lock:
        _settings_cache["data_dir"] = None
        _settings_cache["settings"] = None

PREFIXES = {
    "cf": "cloudflare",
    "ds": "deepseek",
    "qw": "qwen",
    "kimi": "kimi",
    "glm": "glm",
    "gpt": "openai",
}


class AnswerError(Exception):
    """kind: missing_key / config / http / timeout / network / empty."""

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind
        self.message = message


class Throttle:
    """Lets a streaming update through at most once per interval."""

    def __init__(self, interval):
        self.interval = interval
        self._last = None

    def ready(self, now):
        if self._last is None or now - self._last >= self.interval:
            self._last = now
            return True
        return False


def endpoint(base_url):
    return base_url.rstrip("/") + "/chat/completions"


def parse_input(text, default_vendor):
    """'cf 什么是闭包' -> ('cloudflare', '什么是闭包')。"""
    text = (text or "").strip()
    if not text:
        return default_vendor, ""

    parts = text.split(None, 1)
    vendor = PREFIXES.get(parts[0].lower())
    if vendor is None:
        return default_vendor, text
    return vendor, (parts[1].strip() if len(parts) > 1 else "")


def parse_sse_line(line):
    """One SSE line -> ('delta', text) | ('usage', dict) | ('done', None) | None."""
    if not line:
        return None

    line = line.strip()
    if not line or line.startswith(":") or not line.startswith("data:"):
        return None

    payload = line[5:].strip()
    if payload == "[DONE]":
        return ("done", None)

    try:
        data = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    choices = data.get("choices") or []
    if choices:
        delta = choices[0].get("delta") or {}
        content = delta.get("content")
        if content:
            return ("delta", content)
        reasoning = delta.get("reasoning_content")
        if reasoning:
            return ("reasoning", reasoning)

    usage = data.get("usage")
    if isinstance(usage, dict):
        return ("usage", usage)
    return None


def vendor_config(settings, vendor):
    preset = VENDORS[vendor]
    base = (settings.get("base_url_" + vendor) or preset["base"] or "").strip()
    model = (settings.get("model_" + vendor) or preset["model"] or "").strip()
    key = (settings.get("key_" + vendor) or "").strip()

    if not base:
        raise AnswerError("config", "先在插件设置里填自定义厂商的 Base URL")
    if not key:
        raise AnswerError("missing_key", "先在插件设置里填 %s 的 API Key" % preset["name"])
    if not model:
        raise AnswerError("config", "先在插件设置里填 %s 的模型名" % preset["name"])
    return base, model, key


def _urlopen(request, timeout):
    return urllib.request.urlopen(request, timeout=timeout)


def _short(value, limit=80):
    text = str(value).replace("\n", " ").strip()
    return text[:limit]


def _body_message(exc):
    """The API's own message: OpenAI `error.message`, Cloudflare `errors[0].message`."""
    try:
        raw = exc.read()
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""

    error = data.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict) and first.get("message"):
            return str(first["message"])
    return ""


def http_error_message(exc):
    detail = _short(_body_message(exc), 80)
    if detail:
        return "HTTP %s：%s" % (exc.code, detail)
    return "HTTP %s：%s" % (exc.code, _short(exc.reason, 60))


def answer(settings, question, vendor, on_delta=None):
    """Asks one vendor and returns {'text', 'model', 'elapsed', 'tokens'}."""
    base, model, key = vendor_config(settings, vendor)
    system_prompt = (settings.get("system_prompt") or SYSTEM_PROMPT_DEFAULT).strip()

    body = {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
    }
    request = urllib.request.Request(
        endpoint(base),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )

    pieces = []
    usage = None
    throttle = Throttle(THROTTLE_SEC)
    started = time.monotonic()
    last_data = started

    try:
        with _urlopen(request, timeout=HTTP_TIMEOUT) as response:
            for raw in response:
                now = time.monotonic()
                if now - last_data > HTTP_TIMEOUT:
                    raise AnswerError("timeout", "回答超时，请重试")
                last_data = now

                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
                parsed = parse_sse_line(line)
                if parsed is None:
                    continue

                kind, value = parsed
                if kind == "reasoning":
                    if on_delta is not None and throttle.ready(now):
                        on_delta("", True)
                elif kind == "delta":
                    pieces.append(value)
                    if on_delta is not None and throttle.ready(now):
                        on_delta("".join(pieces), False)
                elif kind == "usage":
                    usage = value
                elif kind == "done":
                    break
    except urllib.error.HTTPError as exc:
        raise AnswerError("http", http_error_message(exc))
    except (socket.timeout, TimeoutError):
        raise AnswerError("timeout", "回答超时，请重试")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise AnswerError("timeout", "回答超时，请重试")
        raise AnswerError("network", "网络不可用：%s" % _short(exc.reason))
    except OSError as exc:
        raise AnswerError("network", "网络不可用：%s" % _short(exc))

    elapsed = time.monotonic() - started
    text = "".join(pieces).strip()
    if not text:
        raise AnswerError("empty", "模型没有返回内容，请重试")
    if on_delta is not None:
        on_delta(text, False)

    tokens = None
    if isinstance(usage, dict):
        tokens = usage.get("total_tokens") or usage.get("completion_tokens")
    return {"text": text, "model": model, "elapsed": elapsed, "tokens": tokens}


PROBE_TIMEOUT = 8.0


def probe(settings, vendor):
    """One tiny request to check key, model and base URL. Returns {'model': name}."""
    base, model, key = vendor_config(settings, vendor)
    body = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    request = urllib.request.Request(
        endpoint(base),
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )

    try:
        with _urlopen(request, timeout=PROBE_TIMEOUT) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise AnswerError("http", http_error_message(exc))
    except (socket.timeout, TimeoutError):
        raise AnswerError("timeout", "连接超时，请检查 Base URL 和网络")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise AnswerError("timeout", "连接超时，请检查 Base URL 和网络")
        raise AnswerError("network", "网络不可用：%s" % _short(exc.reason))
    except OSError as exc:
        raise AnswerError("network", "网络不可用：%s" % _short(exc))

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return {"model": model}
    served = data.get("model") if isinstance(data, dict) else None
    return {"model": served or model}


def hint_row():
    return winshun.item(
        "ai-hint",
        "问 AI：ai + 空格 + 问题",
        "回车提问；cf / ds / qw / kimi / glm / gpt 切换厂商",
        valid=False,
    )


def info_text(model, elapsed, tokens):
    text = "%s · %.1f 秒" % (model, elapsed)
    if tokens:
        text += " · %s token" % tokens
    return text


def on_query(query, plugin):
    settings = cached_settings(plugin.data_dir or winshun.data_dir())
    default_vendor = settings.get("default_vendor") or DEFAULT_VENDOR
    if default_vendor not in VENDORS:
        default_vendor = DEFAULT_VENDOR

    vendor, question = parse_input(query.text, default_vendor)
    if not question:
        return [hint_row()]

    def emit(partial_text, thinking):
        if thinking:
            rows = [
                winshun.item("answer", "…", valid=False),
                winshun.item("info", "思考中…", valid=False),
            ]
        else:
            rows = [
                winshun.item("answer", partial_text, copy=partial_text, valid=False),
                winshun.item("info", "正在生成…", valid=False),
            ]
        plugin.partial(query.query_id, rows)

    try:
        result = answer(settings, question, vendor, on_delta=emit)
    except AnswerError as exc:
        return [winshun.item("error", exc.message, valid=False)]

    return [
        winshun.item("answer", result["text"], copy=result["text"]),
        winshun.item("info", info_text(result["model"], result["elapsed"], result["tokens"]), valid=False),
    ]


def on_action(request, plugin):
    item = request.item or {}
    text = item.get("copy") or item.get("title") or ""
    return [winshun.copy(text), winshun.hide()]


def _host_clipboard(plugin):
    """Read the newest clipboard text through WinShun.

    WinShun puts JOB_OBJECT_UILIMIT_READCLIPBOARD on plugin processes even when
    clipboard:read was granted, so Tk sees an empty clipboard. The host's own
    clipboard_history tool is not restricted.
    """
    try:
        result = plugin.tool("clipboard_history", limit=1, kind="text", max_chars=20000)
    except Exception as exc:
        winshun.debug("me.ai-ask: clipboard_history failed: %r" % (exc,))
        return ""

    if isinstance(result, str):
        return result
    if not isinstance(result, dict):
        return ""
    if result.get("error"):
        winshun.debug("me.ai-ask: clipboard_history error: %r" % (result["error"],))
        return ""

    for key in ("text", "plainText", "plain", "content"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    items = result.get("items")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        value = items[0].get("text")
        if isinstance(value, str):
            return value
    return ""


def on_show_settings(plugin):
    """The SDK already answered showSettings; only start or raise the window.

    settings_window is imported here, not at module level: tkinter is only
    loaded when the user actually opens the window.
    """
    import settings_window

    settings_window.show_window(
        plugin.data_dir or winshun.data_dir(),
        refresh_settings_cache,
        _host_clipboard(plugin),
        lambda: _host_clipboard(plugin),
    )


def main():
    winshun.serve(on_query, on_action, name="me.ai-ask", on_show_settings=on_show_settings)


if __name__ == "__main__":
    main()
