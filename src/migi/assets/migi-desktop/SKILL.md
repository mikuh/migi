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
6. **优先前台窗口裁剪**：默认让 `migi` 优先观察前台窗口，减少全屏干扰；只有需要看 Dock、桌面、系统搜索或跨应用切换时，才显式加 `--capture-mode screen`。
7. **跨界面任务打开多步执行**：像“打开应用 -> 搜索 -> 发送消息”这类任务，优先用 `--max-steps 3` 或更高，而不是手动一条条补救。
8. **点名应用的任务会先切前台**：像“给某人发送微信消息”这类明确点名应用的任务，`migi` 现在会先尝试直启/切到目标应用，再开始视觉步骤。
9. **发送前先确认会话**：让指令里明确写收件人，`migi` 会优先搜索并选中该会话；不要依赖“当前正好打开的是对话窗口”。
10. **危险快捷键默认受限**：除非任务明确要求关闭/退出，否则像 `Command+W`、`Ctrl+W`、`Command+Q` 这类窗口管理快捷键会被执行层拦下。
11. **切到目标应用后会自动收窄视野**：当任务先把微信这类目标应用切到前台后，后续步骤会优先回到窗口裁剪，减少整屏干扰和推理延迟。
12. **微信发文字优先走专用流程**：像“给 geb 发送微信消息，说 xxx”这类指令，会优先走微信专用发送流程：切前台 -> 搜索联系人 -> 确认会话 -> 发送文字。
13. **搜索后优先回车命中首个精确结果**：微信专用流程在输入联系人后，会先尝试用回车打开首个搜索结果；如果仍未确认到正确会话，再回退到视觉点选。
14. **窗口边界查询做了低延迟回退**：macOS 前台窗口边界查询现在使用更短超时，并在异常时回退到最近一次成功窗口区域，减少偶发 2 秒级截图抖动。
15. **纯键盘步骤跳过预截图**：微信“搜索联系人”和“发送文本”阶段是确定性快捷键/输入动作，默认不再先截图，可直接减少这两步的截图耗时。

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
  └─ 界面会变化？→ 优先 migi act --max-steps 3 "简短指令"
```

## 指令写法（简短优先）

- 点击：`migi act "点击左侧微信图标"`
- 输入：`migi act "点击搜索框，输入 李白"`
- 发送文字：`migi act "在输入框输入 你好，按回车"`
- 粘贴并发送：`migi act "点击输入框，按 Ctrl+V 粘贴，按回车发送"`（migi 会自动适配 macOS Command / Windows Ctrl）
- 滚动：`migi act "向下滚动一屏"`
- 聚焦当前应用窗口：`migi act --capture-mode window "点击搜索框，输入 李白"`（默认 `auto` 也会优先这样做）
- 需要看整个桌面：`migi act --capture-mode screen "打开微信"`
- 跨界面任务：`migi act --max-steps 3 "给 geb 发送微信，说 gui测试"`
- 指名微信类任务：`migi act --max-steps 3 "给 geb 发送微信消息，说 gui测试"`（会优先尝试把微信切到前台）
- 发送前确认收件人：`migi act --max-steps 3 "给 geb 发送微信消息，说 已收到"`（明确写出 `geb`，避免误发给当前会话）
- 微信纯文字消息：优先用 `给 <收件人> 发送微信消息，说 <内容>` 这种格式，最容易命中专用流程。
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

# --- 第 2-3 步：搜索联系人并发消息 ---
migi act --max-steps 3 --capture-mode window "给李白发送微信，说 你好啊"
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
- 搜索框/输入框总是点偏：优先加 `--capture-mode window`，让模型只看当前前台窗口。
- 任务卡在第一步：优先加 `--max-steps 3`，让它带着前一步历史继续推理。
- 误点或焦点错误：先回到稳定界面（关弹窗/返回上层），再继续。

## 安全与稳定性约束

- 自动化期间避免人工抢占鼠标键盘。
- 避免绝对坐标，优先语义锚点定位。
- 组合动作只用于同屏无变化场景；一旦界面会变，必须拆步。
- 发送类动作前确认焦点在目标会话，避免误发。
- 对“给某人发消息”这类任务，始终把收件人名字写进指令里，不要只写“发送这句话”。
- 不要用 `which` / `where` 判断 GUI 应用可用性。
- migi 引擎内部已自动适配 macOS/Windows 的快捷键映射（Command↔Ctrl），指令中写任一种均可。

## 发布约定

- 每次功能改动发布前，补丁版本号需同步更新（`pyproject.toml` 与 `src/migi/__init__.py` 保持一致），并打对应 Git tag。
