# 问 AI（me.ai-ask）

Win顺 常驻插件。输入 `ai` + 空格 + 问题，回车提问，答案在结果列表里流式显示，不用先开浏览器。
答案边生成边显示，答案出来后再回车复制全文。

## 安装

从 [Releases](https://github.com/888bik/me.ai-ask/releases) 下载 `me.ai-ask-<版本>.wsplugin` 安装到 WinShun。

需要 WinShun 0.12.0 或更新版本，以及 Python 3（WinShun 自己找 `python.exe`）。

## 用法

| 输入 | 结果 |
|---|---|
| `ai 什么是闭包` | 用设置里的默认厂商（默认 DeepSeek）回答 |
| `ai cf 什么是闭包` | Cloudflare Workers AI |
| `ai ds 什么是闭包` | DeepSeek |
| `ai qw 什么是闭包` | 通义千问（百炼） |
| `ai kimi 什么是闭包` | Moonshot Kimi |
| `ai glm 什么是闭包` | 智谱 GLM |
| `ai gpt 什么是闭包` | OpenAI |
| `ai` | 提示行，不提问 |

提问分两步：输入问题后列表先出现「回车向 AI 提问」占位（只是本地占位，不联网、不花额度）；**回车**才真正发请求。问题可以先打完整再回车，打字慢也不会把半截问题发出去。

结果列表两行：

1. 第一行是答案全文，Enter 复制（`copy` 是完整答案；答案很长时列表下方会出现可滚动的文字区）。
2. 第二行是信息行：模型名 · 用时 · token 数。接口返回 `usage` 时才显示 token。

每次提问都是独立的：不保存上下文，不记聊天记录。

## 设置窗口

插件用自带的设置窗口，不再使用 Win顺 内置设置页（manifest 里的 `settings[]` 已全部删掉）。

打开方式：Win顺 设置 → 插件 → 问 AI，卡片上的「设置」按钮。

窗口左边是「通用」和 7 个厂商（DeepSeek、Cloudflare Workers AI、通义千问、Moonshot Kimi、智谱 GLM、OpenAI、自定义）；点哪个，右边只显示那一家的字段：

| 页面 | 字段 |
|---|---|
| 通用 | 默认厂商（下拉）、系统提示词 |
| 任一厂商 | API Key（`*` 显示，已保存的不回填）、模型（预设可选，也能手填） |
| Cloudflare | 再加 Cloudflare 账号 ID（控制台右下角 Account ID） |
| 自定义 | 再加 Base URL |

已保存的 Key **不会回填到输入框**，框里只会显示你这次新填/粘贴的内容。留空保存表示不修改；要删掉已保存的 Key，点「清除」再保存。

Key 输入框有「粘贴」按钮。**WinShun 0.23 实测给插件进程加了 `JOB_OBJECT_UILIMIT_READCLIPBOARD`**（`plugins.log` 里 `job_ui_flags=0x4`），即使 `clipboard:read` 已确认，插件自己也读不到剪贴板。所以点「粘贴」时插件改用 WinShun 的 `clipboard_history` 工具**现取**最近一次复制的内容——窗口开着时再复制也没问题。

WinShun 的剪贴板历史关着时取不到，窗口会提示。每个厂商页还有「测试连接」：用当前填的 Key / 模型 / Base URL（Cloudflare 还要账号 ID）发一条 `max_tokens=1` 的请求，直接告诉你成功或失败原因，不用等下次提问。

点「保存」后：

- 非密钥项写进 `WS_DATA_DIR\config.json`：默认厂商、系统提示词、各厂商模型、自定义 Base URL。
- API Key 写进 **Windows 凭据管理器**，条目名 `WinShun/me.ai-ask/key_<厂商>`；不写明文文件、不打日志。

窗口里的修改先留在内存（切页不丢），点「保存」才一次性写入。保存成功会弹提示并关闭窗口；保存失败会提示并保持窗口打开。有未保存的修改时关窗，会先问要不要保存。

查询时用的设置（`config.json` + 各厂商 Key）在内存里缓存一份，保存后自动刷新，不会每次查询都去读 7 遍凭据管理器。

**旧的设置项已移除，也不做迁移。** 升级后请在新窗口里重填一次 Key，常用的就是 DeepSeek 和 Cloudflare 两家。

## 去哪申请 Key

| 厂商 | 地址 |
|---|---|
| Cloudflare Workers AI | https://dash.cloudflare.com/profile/api-tokens （新建 Token，权限选 Workers AI） |
| DeepSeek | https://platform.deepseek.com/api_keys |
| 通义千问（百炼） | https://bailian.console.aliyun.com/ → API-KEY |
| Moonshot Kimi | https://platform.moonshot.cn/console/api-keys |
| 智谱 GLM | https://open.bigmodel.cn/usercenter/apikeys |
| OpenAI | https://platform.openai.com/api-keys |

**Cloudflare 还要填账号 ID**：登录 Cloudflare 控制台，右侧栏最下方就是 `Account ID`，填到设置里的 Cloudflare 页。账号 ID 因账号而异，插件不再内置任何人的 ID；第一次用 Cloudflare 会提示你填写。

### Cloudflare 免费额度

Workers AI 免费额度是**每天 1 万 Neurons**，**早上 8 点重置**。
**回答越长，消耗越快**：一次长回答可能吃掉几百上千 Neurons。额度用完时接口返回错误，插件显示状态码。

## deepseek-reasoner（思考型模型）

选 `deepseek-reasoner` 时，接口先推 `reasoning_content`，再推正式回答。

- 思考阶段第二行显示「思考中…」，思考内容**不会**拼进答案。
- 超时是**空闲超时**：连续 8.5 秒没收到任何数据才报「回答超时，请重试」。流一直在出字就不算超时。
- 但 Win顺 给每次查询的上限是 **10 秒**。思考超过 10 秒的问题等不到答案，列表会显示超时。
- 复杂问题求稳，用 `deepseek-chat`。

## 运行依赖

- Win顺 0.12.0 或更新版本。
- Python 3（Win顺 自己找 `python.exe`，支持 python.org 安装和 `pyvenv.cfg` 里的虚拟环境）。
- 只用标准库：`urllib`、`json`、`socket`、`ctypes`、`tkinter`，不需要 `pip install` 任何东西。
- tkinter 只在打开设置窗口时加载，平时常驻不占这部分内存；Windows 版 Python 自带。
- 启动时不做任何网络请求；第一次查询才联网。

## 权限

申请 `network`、`ui` 和 `clipboard:read`。

- `network`：联网只发往**你自己配置的那家厂商地址**：预设的 6 个域名，或你填的自定义 Base URL。
- `ui`：只用于弹出插件自己的设置窗口。插件不注册热键，也不抓取别的窗口。
- `clipboard:read`：设置窗口里按 Ctrl+V 粘贴 API Key 需要它。WinShun 用作业的 UI 限制封住没授权插件的剪贴板，没有这项时 `GetClipboardData` 返回空，表现就是「能打字、粘不进去」。插件只在窗口的粘贴动作里读剪贴板，查询时不碰。
- 不申请 `processes`、`files:*`；不起子进程、不读写你的文件。
- API Key 只放进发往该厂商的 `Authorization` 头，不打印、不写日志、不落明文。
- Win顺 不能阻止插件联网，权限确认里会写明这一点。

## 数据

- `WS_DATA_DIR\config.json`：默认厂商、系统提示词、各厂商模型、自定义 Base URL。不含 Key。
- Windows 凭据管理器：每个厂商一条，目标名 `WinShun/me.ai-ask/key_<厂商>`。
- 不保存聊天记录和上下文；诊断信息只走 stderr（开发者模式的 `plugins.log`），内容不含 Key。

## 失败处理

| 情况 | 显示 |
|---|---|
| 没填当前厂商的 Key | `先在插件设置里填 XX 的 API Key` |
| HTTP 报错 | `HTTP 401：Unauthorized`（状态码 + 简短原因） |
| 超过 8.5 秒没收到任何数据 | `回答超时，请重试` |
| 连不上（断网、DNS、证书） | `网络不可用：…` |
| 接口没返回任何文字 | `模型没有返回内容，请重试` |

所有错误行都是 `valid: false`，Enter 无效，不会返回假答案。

## 协议说明

常驻模式（`runtime.mode = "resident"`）：一个进程常开，stdin/stdout 上跑 JSON-RPC 2.0，一行一条。
流式答案用 `query/partial` 推送，约 150 毫秒最多一次；最后一次 `result` 是完整答案。
`timeoutMs` 是 10000，`debounceMs` 是 300 —— 输入时只刷新「回车向 AI 提问」占位，不联网；回车后插件写待提问状态并 `refresh()`，宿主立刻重新查询、这时才发请求。

manifest 的 `settingsWindow: true` 让卡片多一个「设置」按钮：宿主发 `showSettings`，插件**立刻回 `{}`**，再在独立线程里开窗口；窗口已开着就提到前台，不开第二个。窗口不占用 JSON-RPC 查询循环。

随插件分发的 `winshun.py` 为此做了两处扩展（官方 SDK 目前没有，换成官方文件前要先补上）：

1. `Plugin._handle` 里多一个 `showSettings` 分支（先回包再调处理器），并新增 `on_show_settings` 钩子，`serve(..., on_show_settings=...)` 可传入。
2. `Plugin.tool()` 支持**从其他线程调用**：请求发出后由 serve 循环把响应路由回等待的线程，窗口才能在点击「粘贴」时直接向 WinShun 要剪贴板。

manifest 顶层的 `ai` 字段（暴露给 AI 用）**没有使用**，它和关键字 `ai` 是两回事。

## 开发与测试

```powershell
cd me.ai-ask
python -m unittest -v
```

126 个测试，不发真实网络请求：

- 单元测试用 `unittest.mock` 模拟 `urlopen`，覆盖厂商 URL 拼接（含 GLM 的 `/v4`、Cloudflare 账号 ID）、SSE 解析（含 `reasoning_content`）、前缀解析、缺 Key、HTTP 错误映射（读响应体里的 message）、空闲超时、流式拼接；Cloudflare 账号 ID 为空时不发请求、返回明确错误。
- 回车提问流程：输入时只返回占位且零 HTTP、占位带 `rerun` 与 `data`；回车动作返回 keep、写待提问并调 `refresh()`；重新查询只发一次请求、第二次回到占位；连续输入不联网；回车按最新输入重新解析（含 `ai` 前缀、`cf` 切换厂商、空文本回落）；新问题顶掉旧待提问时不发请求。
- 新进程里 `import main` 之后，`tkinter` 不在 `sys.modules`（窗口模块和 tkinter 都是懒加载）。
- `test_store.py`：`config.json` 读写与损坏回退、凭据目标名、设置合并。
- `test_settings_window.py`：只起一个窗口线程、第二次请求只提窗（不再读宿主剪贴板）、窗口线程不阻塞调用方；待保存字典的脏标记与保存；**A 页改了 Key 切到 B 页再切回来值还在**；Cloudflare 账号 ID 输入框与测试连接参数；有未保存修改时关窗会询问；粘贴按钮（含点击时向宿主现取）；「测试连接」的成功、失败、缺 Key 三条。
- `test_sdk.py`：`showSettings` 立即回空结果、没有处理器也回包、窗口线程忙时查询仍能回答；`tool()` 从其他线程调用时能拿到路由回来的结果和错误。
- 端到端测试起真实进程 + 本地 SSE 服务器，走一遍 `initialize → query（占位）→ action（回车）→ refresh → query → query/partial → result → shutdown`。

## 更新记录

- 1.2.4：改成回车提问。输入时只显示「回车向 AI 提问」占位、不联网；回车后写待提问状态并 `refresh()` 触发重新查询，答案出来后再回车复制。
- 1.2.3：设置窗口不再强制置顶；点别的窗口会正常被压到后面，需要时再点卡片「设置」把它叫回前台。
- 1.2.2：停顿时间改为 1.2 秒，文案改成「停顿 1.2 秒自动提问，回车复制全文」；Cloudflare 账号 ID 改为设置项（不再内置）；打开设置窗口不再预读宿主剪贴板，只有点「粘贴」时才读。

## 许可证

MIT，见 [LICENSE](LICENSE)。作者 888bik。
`winshun.py` 是 Win顺 官方 SDK，随插件一起分发。
