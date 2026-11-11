"""me.ai-ask storage.

Non-secret settings live in ``config.json`` under ``WS_DATA_DIR``.
API keys live in the Windows Credential Manager, one entry per vendor,
named ``WinShun/me.ai-ask/key_<vendor>`` — never in a file, never in a log.

Only the standard library is used: ``ctypes`` reaches advapi32 directly.
"""

import ctypes
import json
import os
from ctypes import wintypes

CLOUDFLARE_BASE_TEMPLATE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"

VENDORS = {
    "cloudflare": {
        "name": "Cloudflare Workers AI",
        "base": "",
        "model": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
        "models": [
            "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
            "@cf/qwen/qwen2.5-coder-32b-instruct",
            "@cf/openai/gpt-oss-120b",
        ],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "qwen": {
        "name": "通义千问",
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "models": ["qwen-plus", "qwen-turbo"],
    },
    "kimi": {
        "name": "Moonshot Kimi",
        "base": "https://api.moonshot.cn/v1",
        "model": "kimi-k2",
        "models": ["kimi-k2"],
    },
    "glm": {
        "name": "智谱 GLM",
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "models": ["glm-4-flash", "glm-4-plus"],
    },
    "openai": {
        "name": "OpenAI",
        "base": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
        "models": ["gpt-4o-mini", "gpt-4o"],
    },
    "custom": {
        "name": "自定义",
        "base": "",
        "model": "",
        "models": [],
    },
}

# Display order of the settings window, "通用" comes before these.
VENDOR_ORDER = ["deepseek", "cloudflare", "qwen", "kimi", "glm", "openai", "custom"]

SYSTEM_PROMPT_DEFAULT = "用中文简短回答，200 字以内"

DEFAULT_CONFIG = {
    "default_vendor": "deepseek",
    "system_prompt": SYSTEM_PROMPT_DEFAULT,
    "base_url_custom": "",
    "cloudflare_account_id": "",
}
for _vendor, _spec in VENDORS.items():
    DEFAULT_CONFIG["model_" + _vendor] = _spec["model"]

CREDENTIAL_PREFIX = "WinShun/me.ai-ask/"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_IS_WINDOWS = os.name == "nt"

_api_cache = None


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def _api():
    """advapi32 with the credential entry points typed, created once."""
    global _api_cache
    if _api_cache is None:
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
        ]
        api.CredReadW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        _api_cache = api
    return _api_cache


def credential_target(vendor):
    return CREDENTIAL_PREFIX + "key_" + vendor


def read_secret(vendor):
    """The stored API key, or "" when there is none."""
    if not _IS_WINDOWS:
        return ""
    pointer = ctypes.POINTER(_CREDENTIAL)()
    if not _api().CredReadW(credential_target(vendor), _CRED_TYPE_GENERIC, 0, ctypes.byref(pointer)):
        return ""
    try:
        cred = pointer.contents
        size = cred.CredentialBlobSize
        if not size:
            return ""
        raw = ctypes.string_at(cred.CredentialBlob, size)
        return raw.decode("utf-16-le", "replace")
    finally:
        _api().CredFree(ctypes.cast(pointer, ctypes.c_void_p))


def write_secret(vendor, secret):
    """Store an API key; an empty value removes it."""
    if not _IS_WINDOWS:
        raise OSError("凭据管理器只在 Windows 上可用")
    secret = str(secret or "")
    if not secret:
        delete_secret(vendor)
        return

    blob = secret.encode("utf-16-le")
    buffer = (ctypes.c_byte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = credential_target(vendor)
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = "me.ai-ask"
    if not _api().CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "写入凭据失败")


def delete_secret(vendor):
    if not _IS_WINDOWS:
        return
    _api().CredDeleteW(credential_target(vendor), _CRED_TYPE_GENERIC, 0)


def config_path(data_dir):
    return os.path.join(str(data_dir), "config.json")


def load_config(data_dir):
    """config.json merged over the defaults; unreadable files fall back to defaults."""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(data_dir), encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return config
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key in config and isinstance(value, str):
                config[key] = value
    return config


def save_config(data_dir, values):
    """Write the full config (defaults + values) atomically."""
    folder = str(data_dir)
    os.makedirs(folder, exist_ok=True)
    config = dict(DEFAULT_CONFIG)
    config.update({key: value for key, value in values.items() if isinstance(value, str)})

    path = config_path(folder)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def load_settings(data_dir):
    """The dict main.answer() expects: config.json plus the credential secrets."""
    settings = load_config(data_dir)
    for vendor in VENDORS:
        settings["key_" + vendor] = read_secret(vendor)
    return settings
