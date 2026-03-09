---
name: migi
description: 当任务需要识别当前桌面界面并执行 GUI 动作（点击、输入、滚动、快捷键），或需要对本地图片文件做视觉理解时使用此技能；优先先观察再执行，并在界面变化后分步重试。
---

# migi

## 何时使用

- 需要基于当前屏幕执行桌面 GUI 自动化：点击、输入、滚动、快捷键。
- 任务依赖"先看界面，再决定动作"。
- 需要针对一张本地图片文件做视觉理解（识别元素、文字、布局、关系）。

## 何时不要使用

- 纯 shell 或脚本任务（文件处理、服务管理、接口调用）。
- 仅通过网页 API 可完成的任务。
- 安装 skill、安装依赖、发布打包等环境管理任务。

## 速度优化原则

1. **能用 shell 就不用 migi**：打开应用、拷贝文件到剪贴板、打开 URL 等，优先用 shell 命令完成（见下方跨平台 shell 速查表）。
2. **能直接 `act` 就不要先 `see`**：如果你已经知道界面状态（刚执行过操作、刚 see 过、或任务描述足够明确），直接 `act`。
3. **指令要简短精确**：冗长指令会增加模型推理时间。用 10-20 字描述核心意图即可。
4. **同屏多步合并一条 `act`**：如果多个动作在同一界面上且不会引发界面变化，写到一条 `act` 里。
5. **只在不确定时才 `see`**：界面刚发生跳转、弹窗、刷新等变化后才需要重新观察。

## 跨平台 shell 速查表

| 操作 | macOS | Windows (PowerShell) |
|---|---|---|
| 打开应用 | `open -a "AppName"` | `Start-Process "AppName"` |
| 打开 URL | `open "https://..."` | `Start-Process "https://..."` |
| 打开文件夹 | `open ~/Documents` | `explorer $HOME\Documents` |
| 打开系统设置 | `open "x-apple.systempreferences:"` | `Start-Process ms-settings:` |
| 图片拷贝到剪贴板 | `osascript -e 'set the clipboard to (read (POSIX file "/path/to/img.png") as «class PNGf»)'` | `powershell -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Image]::FromFile('C:\path\to\img.png'))"` |
| 等待 N 秒 | `sleep N` | `Start-Sleep -Seconds N` |

## 执行决策树

```
任务到达
  ├─ 可用 shell 完成？→ 直接 shell，不调 migi
  ├─ 已知界面状态 + 目标明确？→ migi act "简短指令"
  ├─ 不确定界面状态？→ migi see "简短问题" → 再 act
  ├─ 需要分析本地图片？→ migi image <path> "问题"
  └─ 界面会变化？→ 拆成多步，每步一个 act，变化后再观察
```

## 指令写法（简短优先）

- 点击：`migi act "点击左侧微信图标"`
- 输入：`migi act "点击搜索框，输入 李白"`
- 发送文字：`migi act "在输入框输入 你好，按回车"`
- 粘贴并发送：`migi act "点击输入框，按 Ctrl+V 粘贴，按回车发送"`（migi 会自动适配 macOS Command / Windows Ctrl）
- 滚动：`migi act "向下滚动一屏"`
- 图片理解：`migi image ./pic.png "提取所有可见文字"`
- 锚点写法：用"文案 + 方位"，如"底部输入框"、"搜索结果中的李白"，避免绝对坐标。

## 高频任务剧本

### 打开应用

```bash
# macOS
open -a "WeChat"

# Windows (PowerShell)
Start-Process "WeChat"

# 仅在 shell 失败时才用 migi
migi act "用系统搜索打开微信"
```

### 微信发文字消息

```bash
# --- 第 1 步：打开微信 ---
# macOS
open -a "WeChat"
# Windows
# Start-Process "WeChat"

sleep 1  # Windows: Start-Sleep -Seconds 1

# --- 第 2 步：搜索联系人 ---
migi act "点击搜索框，输入 李白"
migi act "点击搜索结果中的李白"

# --- 第 3 步：发消息 ---
migi act "在输入框输入 你好啊，按回车发送"
```

### 微信发送图片

发送图片的正确方式：先用 shell 将图片拷贝到系统剪贴板，再到微信输入框粘贴发送。

**macOS：**

```bash
# 1. 将图片拷贝到系统剪贴板
osascript -e 'set the clipboard to (read (POSIX file "/absolute/path/to/image.png") as «class PNGf»)'

# 2. 在微信输入框粘贴并发送（可附带文字）
migi act "点击输入框，按 Command+V 粘贴，输入 请看这张图，按回车发送"
```

**Windows (PowerShell)：**

```powershell
# 1. 将图片拷贝到系统剪贴板
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Image]::FromFile("C:\path\to\image.png"))

# 2. 在微信输入框粘贴并发送
migi act "点击输入框，按 Ctrl+V 粘贴，输入 请看这张图，按回车发送"
```

**只发图片不附带文字：**

```bash
# macOS
osascript -e 'set the clipboard to (read (POSIX file "/path/to/image.png") as «class PNGf»)'
migi act "点击输入框，按 Command+V 粘贴，按回车发送"

# Windows (PowerShell)
# Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Image]::FromFile("C:\path\to\image.png"))
# migi act "点击输入框，按 Ctrl+V 粘贴，按回车发送"
```

### 微信发送图片完整流程

```bash
# --- 打开微信并找到联系人 ---
# macOS
open -a "WeChat"
# Windows: Start-Process "WeChat"
sleep 1

migi act "点击搜索框，输入 小伍"
migi act "点击搜索结果中的小伍"

# --- 拷贝图片到剪贴板 ---
# macOS
osascript -e 'set the clipboard to (read (POSIX file "/path/to/image.png") as «class PNGf»)'
# Windows (PowerShell): Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::SetImage([System.Drawing.Image]::FromFile("C:\path\to\image.png"))

# --- 粘贴并发送 ---
migi act "点击输入框，按 Command+V 粘贴，输入 请查看这张图，按回车发送"
# Windows: migi act "点击输入框，按 Ctrl+V 粘贴，输入 请查看这张图，按回车发送"
```

### 打开系统设置

```bash
# macOS
open "x-apple.systempreferences:"

# Windows (PowerShell)
# Start-Process ms-settings:
```

### 文件管理器操作

```bash
# macOS
open ~/Documents
# Windows: explorer $HOME\Documents

# 后续 GUI 操作
migi act "右键空白处，点击新建文件夹"
```

## 失败恢复

- 找不到元素：`migi see "当前窗口有哪些可见元素"`，然后用更具体锚点重试。
- 界面已变化：停止盲操作，`see` 后再继续。
- 动作未触发：改写为更明确指令重试。
- 误点或焦点错误：先回到稳定界面（关弹窗/返回上层），再继续。

## 安全与稳定性约束

- 自动化期间避免人工抢占鼠标键盘。
- 避免绝对坐标，优先语义锚点定位。
- 组合动作只用于同屏无变化场景；一旦界面会变，必须拆步。
- 发送类动作前确认焦点在目标会话，避免误发。
- 不要用 `which` / `where` 判断 GUI 应用可用性。
- migi 引擎内部已自动适配 macOS/Windows 的快捷键映射（Command↔Ctrl），指令中写任一种均可。
