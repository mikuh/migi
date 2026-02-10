---
name: migi
description: 使用 migi CLI 进行桌面 GUI 视觉自动化。通过截屏理解界面并执行点击、输入、滚动、快捷键等动作。适用于打开应用、操作微信/QQ/浏览器、控制桌面等任务。
---

# Migi

截屏 -> 视觉模型分析 -> 桌面动作执行（支持同屏多动作组合，减少模型调用次数）。

## 快速开始

```bash
# 1) 首次初始化模型配置（推荐）
migi setup

# 2) 仅识别当前屏幕，不执行动作
migi see "屏幕上有什么应用"

# 3) 识别并执行动作
migi act "点击搜索框并输入 李白"
```

## 命令总览

| 命令 | 用途 |
|------|------|
| `migi setup` | 初始化/更新模型配置 |
| `migi status` | 查看当前生效配置与依赖状态 |
| `migi see "<指令>"` | 只分析，不执行 GUI 动作 |
| `migi act "<指令>"` | 分析并执行 GUI 动作 |
| `migi install --target <app>` | 安装 skill 到已知平台目录 |
| `migi install --path <custom_dir>` | 安装 skill 到自定义目录 |

## 核心能力

### 1) 视觉理解（不触发动作）

```bash
migi see "屏幕上有什么应用"
migi see "当前打开的是什么网页"
```

### 2) 单动作执行

| 操作类型 | 指令示例 |
|---------|---------|
| 点击 | `"点击微信图标"` `"点击搜索按钮"` |
| 双击 | `"双击文件夹"` |
| 右键 | `"右键点击文件"` |
| 输入 | `"在搜索框输入 Python"` |
| 滚动 | `"向下滚动"` `"向上滚动页面"` |
| 快捷键 | `"按 Command+C"` `"按回车键"` |

### 3) 多动作组合（一次调用，多个动作）

当界面不变化时，模型可一次返回多个动作，减少调用次数。

#### 搜索场景：点击 + 输入

```bash
migi act "点击搜索框并输入 李白"
migi act "点击搜索框并输入 Python教程"
```

#### 对话框场景：点击 + 输入 + 回车

```bash
migi act "点击消息输入框，输入 你好啊 并按回车发送"
migi act "点击输入框，输入 收到，谢谢！ 然后回车"
```

适合组合的场景：
- 搜索场景：点击 + 输入（通常自动触发搜索）
- 对话框场景：点击 + 输入 + 回车

不适合组合的场景（界面会变化）：
- 打开应用 -> 等待窗口出现 -> 操作窗口
- 搜索 -> 等待结果出现 -> 点击结果

## 典型任务示例

### 微信发消息（4 步）

```bash
# 1) 打开微信
migi act "点击 Dock 栏的微信图标"

# 2) 搜索联系人
migi act "点击搜索框并输入 李白"

# 3) 选择联系人
migi act "点击搜索结果中的李白"

# 4) 发送消息
migi act "点击消息输入框，输入 你好啊 并按回车发送"
```

### 打开系统设置

```bash
migi act "点击左上角苹果图标"
migi act "点击系统设置"
```

### Finder 文件操作

```bash
migi act "点击 Finder"
migi act "双击文档文件夹"
migi act "右键空白处，点击新建文件夹"
```

## JSON 输出规范（LLM 友好）

`migi` 默认输出 compact JSON（推荐给 agent / LLM）：

- 成功：`ok`, `cmd`, `code`, `data`
- 失败：`ok`, `cmd`, `code`, `error`（必要时附 `data`）

如需排障可使用 full 模式：

```bash
migi status --json full
```

## 配置路径

- 首选：`~/.config/migi/config.json`
- 若不可写：自动回退到 `~/.migi/config.json`（用户级全局）
- 可通过 `MIGI_CONFIG_PATH` 指定共享配置路径

## 安装 skill

```bash
# 安装到全部已知平台（Cursor / Claude Code / OpenCode / NeoStream / Lingxibox）
migi install --target all

# 安装到 Cursor（默认路径自动探测）
migi install --target cursor

# 未知应用或手动目录
migi install --path /path/to/skills
```

## 注意事项

1. 界面变化后需要重新截图再执行下一步（例如打开应用、切页、搜索后）。
2. 自动化会真实控制鼠标和键盘，执行期间请勿人工抢占操作。
3. 输入内容若以 `\\n` 结尾或指令中明确要求回车，会触发发送/确认动作。

