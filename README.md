# migi

`migi` is a task-oriented desktop GUI vision automation CLI focused on skill-style integration for LLM agents.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-alpha-orange)

[Switch to English](#english) | [切换到中文](#中文)

---

## English

### Navigation

- [What It Does](#what-it-does)
- [Current Model Support](#current-model-support)
- [Install](#install)
- [Quick Start](#quick-start)
- [CLI Usage](#cli-usage)
- [Configuration](#configuration)
- [Advanced: Custom Action Parser](#advanced-custom-action-parser)
- [JSON Output Contract](#json-output-contract)
- [Platform and Dependencies](#platform-and-dependencies)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Roadmap](#roadmap)

### What It Does

- Uses screenshots + multimodal model inference to understand current desktop UI
- Supports instruction-driven automation via `see` (analyze only) and `act` (analyze + execute)
- Supports local image understanding via `image` / `vision`
- Returns machine-readable JSON for every command
- Includes a skill installer for agent platforms

### Current Model Support

`migi` currently ships with a Doubao-oriented action parser by default (`doubao`), and at this stage **only `doubao-seed` is officially supported**.

Notes:

- You can still pass custom model/base URL values, but the built-in action parsing logic is currently tuned for Doubao-style action outputs.
- If you need a different model, use `--action-parser custom` with your own parser callable.

### Install

```bash
pip install migi-cli
```

or:

```bash
uv pip install migi-cli
```

### Quick Start

1) Configure credentials and model:

```bash
migi setup --api-key "YOUR_API_KEY" --model "doubao-seed" --base-url "https://ark.cn-beijing.volces.com/api/v3"
```

2) Analyze current screen (no execution):

```bash
migi see "What apps are visible on the screen?"
```

3) Analyze and execute:

```bash
migi act "Click the search box and type Li Bai"
```

4) Install skill package:

```bash
migi install --target cursor
```

5) Understand a local image file:

```bash
migi image ./example.png "Describe the key objects and visible text."
```

### CLI Usage

```bash
migi <command> [options]
```

Core commands:

- `setup` / `init`: initialize or update model config
- `status`: show effective runtime config and dependency status
- `config show`: alias of `status`
- `see <instruction>`: analyze screen only
- `act <instruction>`: analyze and execute actions
- `image <image_path> [instruction]` / `vision`: analyze a local image file
- `install`: install skill package(s)

### Configuration

#### Config Sources and Priority

For runtime values, priority is:

1. CLI flags (`--api-key`, `--model`, `--base-url`, etc.)
2. Config file (`~/.config/migi/config.json`)

#### Config File Location

Default path:

- `~/.config/migi/config.json` (or `$XDG_CONFIG_HOME/migi/config.json`)

Run `migi setup` to write the config interactively, or set fields via CLI flags:

```bash
migi setup --api-key "YOUR_API_KEY" --model "doubao-seed" --base-url "https://ark.cn-beijing.volces.com/api/v3"
```

### Advanced: Custom Action Parser

When using non-Doubao model outputs, provide your own parser:

```bash
migi act "..." \
  --action-parser custom \
  --action-parser-callable "your_module:your_parser"
```

Your parser callable should accept:

```python
def your_parser(response: str, img_width: int, img_height: int, scale_factor: int):
    ...
```

Coordinate behavior in executor:

- Recommended: normalized `0..1000` coordinate space (independent of screen resolution)
- Also accepted: `0..1` ratio coordinates
- Also accepted: absolute screenshot pixel coordinates  
  (`migi` remaps screenshot coordinates to the actual pyautogui control space for DPI/scaling differences)

### JSON Output Contract

All commands print exactly one JSON object to stdout.

- `compact` (default, token-efficient):
  - success: `ok`, `cmd`, `code`, `data`
  - failure: `ok`, `cmd`, `code`, `error` (and `data` when needed)
- `full` (debug-friendly):
  - `ok`, `command`, `code`, `message`, `data`, `error`, `meta`

Switch mode:

```bash
migi status --json full
```

### Platform and Dependencies

Target runtime:

- Python: `>=3.11`
- OS: macOS / Linux / Windows (desktop environment required)

Runtime dependencies:

- Required package dependency: `httpx`
- Local image understanding (`image` / `vision`) requires: `pillow`
- Optional but practically required for GUI automation: `mss`, `pyautogui`, `pyperclip`, `pillow`

Install optional GUI dependencies:

```bash
pip install mss pyautogui pyperclip pillow
```

### Troubleshooting

- **`CONFIG_MISSING` for API key/model/base URL**
  - Run `migi setup` again, or set env vars directly.
- **No action executed after `act`**
  - Start with `migi see "..."` to inspect response first.
  - Ensure model is `doubao-seed` and parser is `doubao`.
- **Dependency error for GUI modules**
  - Install missing packages: `mss pyautogui pyperclip pillow`.
- **`which <app>` / `where <app>` returns not found (exit code 1)**
  - This is expected for many GUI apps (they are not in PATH).
  - For app launch tasks, `migi` now uses a 3-stage fallback chain:
    - Direct command launch first (macOS `open`, Windows `Start-Process`)
    - Then shortcut search (macOS `Command+Space`, Windows `Win+S`)
    - Then GUI-visible search fallback if shortcut action fails
    - macOS: `Command+Space` -> type app name -> select the app entry under Applications -> Enter
    - Windows: `Win+S` -> type app name -> Enter
- **Config path permission issue**
  - Use `--config-path` to specify a writable location.
- **Need to use another model**
  - Switch to `--action-parser custom` and implement `module:function`.

### FAQ

- **Is `migi` production-ready?**
  - Current release is alpha and focuses on a stable CLI/JSON contract.
- **Can I use OpenAI-compatible providers directly?**
  - Yes, request transport is OpenAI-compatible, but built-in parsing is currently optimized for Doubao-style outputs.
- **Why only `doubao-seed` is officially supported now?**
  - The default parser backend is Doubao-oriented; parser behavior for other models is not officially guaranteed yet.
- **How to integrate with agents?**
  - Use the stable compact JSON mode and install skills via `migi install`.

### Roadmap

- Multi-model official parser support
- Safer and richer action execution controls
- More robust cross-platform test coverage
- Better parser debug tooling and evaluation suites

---

## 中文

[返回 English](#english) | [点击切换到中文](#中文)

### 导航

- [项目简介](#项目简介)
- [当前模型支持说明（重要）](#当前模型支持说明重要)
- [安装](#安装)
- [快速开始](#快速开始)
- [命令总览](#命令总览)
- [配置方式](#配置方式)
- [高级用法：自定义解析器](#高级用法自定义解析器)
- [JSON 输出协议](#json-输出协议)
- [平台与依赖](#平台与依赖)
- [故障排查](#故障排查)
- [常见问题（FAQ）](#常见问题faq)
- [路线图](#路线图)

### 项目简介

`migi` 是一个面向任务的桌面 GUI 视觉自动化 CLI，重点用于 LLM Agent 的 skill 化集成与调用。

- 通过截图 + 多模态模型理解当前界面
- 支持 `see`（只分析）与 `act`（分析并执行）
- 支持 `image` / `vision`（针对本地图片做图像理解）
- 全部命令输出稳定 JSON，方便程序消费
- 内置技能安装能力（如 Cursor）

### 当前模型支持说明（重要）

目前项目默认只实现了豆包方向的动作解析器（`doubao`），因此**当前仅官方支持 `doubao-seed` 模型**。

- 你仍可传入其他模型参数，但内置解析逻辑目前针对 Doubao 风格动作输出
- 若要接入其他模型，请使用 `custom` 解析器自行适配

### 安装

```bash
pip install migi-cli
```

或：

```bash
uv pip install migi-cli
```

### 快速开始

1) 初始化配置（推荐）：

```bash
migi setup --api-key "你的密钥" --model "doubao-seed" --base-url "https://ark.cn-beijing.volces.com/api/v3"
```

2) 仅分析当前屏幕：

```bash
migi see "屏幕上有哪些应用？"
```

3) 分析并执行动作：

```bash
migi act "点击搜索框并输入 李白"
```

4) 安装 Cursor 技能包：

```bash
migi install --target cursor
```

5) 理解一张本地图片：

```bash
migi image ./example.png "这张图里有哪些关键元素和文字？"
```

### 命令总览

```bash
migi <command> [options]
```

- `setup` / `init`：初始化或更新模型配置
- `status`：查看当前生效配置与依赖状态
- `config show`：`status` 的别名
- `see <instruction>`：只做视觉分析，不执行动作
- `act <instruction>`：视觉分析并执行动作
- `image <image_path> [instruction]` / `vision`：分析本地图片内容
- `install`：安装技能包

### 配置方式

#### 配置优先级（高到低）

1. 命令行参数（CLI）
2. 配置文件（`~/.config/migi/config.json`）

#### 配置文件路径

默认：

- `~/.config/migi/config.json`（或 `$XDG_CONFIG_HOME/migi/config.json`）

通过 `migi setup` 交互式写入配置，或通过命令行参数设置：

```bash
migi setup --api-key "你的密钥" --model "doubao-seed" --base-url "https://ark.cn-beijing.volces.com/api/v3"
```

### 高级用法：自定义解析器

接入非 Doubao 风格输出时，可使用自定义解析器：

```bash
migi act "..." \
  --action-parser custom \
  --action-parser-callable "你的模块:你的函数"
```

函数签名建议：

```python
def your_parser(response: str, img_width: int, img_height: int, scale_factor: int):
    ...
```

执行器坐标兼容策略：

- 推荐使用 `0..1000` 归一化坐标（与屏幕分辨率无关）
- 兼容 `0..1` 比例坐标
- 兼容截图像素绝对坐标  
  （`migi` 会把截图坐标重映射到 pyautogui 实际控制坐标，适配 DPI/缩放差异）

### JSON 输出协议

所有命令都只向标准输出打印一个 JSON 对象。

- `compact`（默认，节省 token）：
  - 成功：`ok`, `cmd`, `code`, `data`
  - 失败：`ok`, `cmd`, `code`, `error`（必要时含 `data`）
- `full`（调试模式）：
  - `ok`, `command`, `code`, `message`, `data`, `error`, `meta`

切换方式：

```bash
migi status --json full
```

### 平台与依赖

运行环境建议：

- Python：`>=3.11`
- 操作系统：macOS / Linux / Windows（需要桌面环境）

依赖说明：

- 必需包依赖：`httpx`
- 本地图片理解（`image` / `vision`）依赖：`pillow`
- GUI 自动化常用依赖：`mss`、`pyautogui`、`pyperclip`、`pillow`

安装 GUI 相关依赖：

```bash
pip install mss pyautogui pyperclip pillow
```

### 故障排查

- **提示 `CONFIG_MISSING`（缺少 key/model/base_url）**
  - 重新执行 `migi setup`，或直接设置环境变量。
- **执行 `act` 没有动作**
  - 先用 `migi see "..."` 检查模型输出。
  - 确保模型使用 `doubao-seed`，解析器使用 `doubao`。
- **出现 GUI 依赖缺失报错**
  - 安装：`mss pyautogui pyperclip pillow`。
- **`which <app>` / `where <app>` 返回未找到（exit code 1）**
  - 这是常见现象，很多 GUI 应用并不在 PATH 中。
  - `migi` 对“打开应用”默认使用三段式回退链路：
    - 先命令直启（macOS `open`，Windows `Start-Process`）
    - 再快捷键搜索（macOS `Command+Space`，Windows `Win+S`）
    - 若快捷键动作失败，再自动走 GUI 可见入口回退流程
    - macOS：`Command+Space` -> 输入应用名 -> 先选中“应用程序”分组中的目标应用 -> 回车
    - Windows：`Win+S` -> 输入应用名 -> 回车
- **配置文件写入失败（权限问题）**
  - 使用 `--config-path` 指向可写目录。
- **想接入其他模型**
  - 使用 `--action-parser custom` 并实现 `module:function` 自定义解析器。

### 常见问题（FAQ）

- **现在可以直接用于生产吗？**
  - 当前版本为 alpha，优先保证 CLI 与 JSON 协议稳定。
- **是否兼容 OpenAI 接口格式？**
  - 传输层兼容，但内置动作解析目前主要针对豆包输出风格。
- **为什么当前只官方支持 `doubao-seed`？**
  - 因为默认解析器是豆包方向实现，其他模型暂未给出官方解析保证。
- **如何与 Agent 集成？**
  - 推荐使用默认 compact JSON 输出，并通过 `migi install` 安装技能。

### 路线图

- 增加多模型官方解析支持
- 增强动作执行安全与控制能力
- 完善跨平台自动化测试覆盖
- 提供更强的解析调试与评估工具
