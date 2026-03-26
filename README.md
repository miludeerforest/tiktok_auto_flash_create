# TikTok Auto Flash Create

一个用于 **TikTok Seller Center Flash Sale 活动复制与创建** 的桌面自动化工具。  
项目提供 Tkinter 图形界面，适合直接源码运行，也适合打包成 Windows EXE 交给运营同事使用。

---

## 功能概览

- **桌面 GUI 工作流**：填写参考活动、扫描 CDP、检测连接、一键运行
- **不再依赖 xlsx**：改为手动填写 4 条参考活动作为轮转模板
- **活动识别更严格**：只从可用 Upcoming 活动中选择模板，减少误选
- **空商品活动阻断**：复制后若未确认存在实际商品，不会继续保存创建页
- **验证码人工处理提醒**：出现验证码或提示弹窗时，用更接近 Windows 10/11 风格的应用内通知提醒处理
- **支持打包 EXE**：可以通过 PyInstaller 生成 `FlashSaleApp.exe`

---

## 仓库结构

| 文件 | 作用 |
|---|---|
| `app_gui.py` | 图形界面入口 |
| `flashsale_runner.py` | Flash Sale 核心自动化逻辑 |
| `captcha_solver.py` | 验证码辅助检测/处理逻辑 |
| `inspect_captcha.py` | 本地调试验证码页面结构的辅助脚本 |
| `test_flashsale_runner.py` | 与核心业务修复相关的测试 |
| `gui_config.example.json` | 示例配置文件 |
| `requirements.txt` | Python 依赖列表 |
| `FlashSaleApp.spec` | PyInstaller 打包文件 |

---

## 运行环境

推荐环境：

- **Windows 10 / 11**
- **Python 3.10+**
- 已安装并可使用的 **Playwright Chromium**
- 可连接的 CDP 浏览器环境

> 这个项目主要围绕 Windows 桌面使用场景设计，GUI 和 EXE 打包流程也默认以 Windows 为主。

### 建议的公开仓库结构

```text
tiktok_auto_flash_create/
├─ app_gui.py
├─ flashsale_runner.py
├─ captcha_solver.py
├─ inspect_captcha.py
├─ test_flashsale_runner.py
├─ gui_config.example.json
├─ requirements.txt
├─ FlashSaleApp.spec
├─ manual_captcha_resume_deploy.md
└─ README.md
```

---

## 安装步骤

### 1) 克隆仓库

```bash
git clone https://github.com/miludeerforest/tiktok_auto_flash_create.git
cd tiktok_auto_flash_create
```

### 2) 创建虚拟环境（推荐）

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Windows CMD:

```bat
.venv\Scripts\activate.bat
```

### 3) 安装 Python 依赖

```bash
python -m pip install -U pip
python -m pip install -r requirements.txt
```

### 4) 安装 Playwright 浏览器

```bash
python -m playwright install chromium
```

---

## 可选依赖

下面这些能力不是基础运行的硬门槛，但如果你要启用对应功能，可以额外安装：

### 可选：`captcha-recognizer`

用于某些验证码识别增强场景。

```bash
python -m pip install captcha-recognizer
```

如果安装了该库，`FlashSaleApp.spec` 会自动尝试把模型目录打包进去。

---

## 配置文件

### 1) 复制示例配置

```bash
copy gui_config.example.json gui_config.json
```

或手动复制一份并命名为：

```text
gui_config.json
```

### 2) 配置项说明

```json
{
  "batch_rounds": 4,
  "seed_names": [
    "引流-2026-3.3-00:30",
    "微利-2026-3.3-01:00",
    "盈利-2026-3.3-01:30",
    "平本-2026-3.3-02:00"
  ],
  "ai_provider": "openai",
  "ai_api_key": "",
  "ai_model": "",
  "ai_base_url": ""
}
```

字段说明：

- `batch_rounds`：本次运行轮数
- `seed_names`：4 条参考活动名称
- `ai_provider`：可选 `openai` / `gemini`
- `ai_api_key`：如使用 AI 识别能力则填写，否则可留空
- `ai_model`：可留空，留空时使用默认模型
- `ai_base_url`：兼容 OpenAI 协议的可选地址，默认可留空

> 仓库不会提交真实 `gui_config.json`，请使用示例文件自行创建。

---

## 图形界面使用流程

### 1) 启动 GUI

```bash
python app_gui.py
```

### 2) 推荐操作顺序

1. 填写 4 条参考活动名称
2. 点击 **扫描实例**
3. 从下拉框选择店铺实例
4. 点击 **检测当前实例**
5. 点击 **保存配置**
6. 点击 **一键运行**

### 3) 遇到验证码时

如果程序检测到验证码或弹窗：

- GUI 会显示更明显的右上角提醒通知
- 请切到浏览器手动完成验证/关闭弹窗
- 回到程序点击 **手动继续**

详细说明见：

- [`manual_captcha_resume_deploy.md`](./manual_captcha_resume_deploy.md)

---

## 打包 EXE

```bash
python -m PyInstaller "FlashSaleApp.spec" --noconfirm
```

打包完成后，产物位于：

```text
dist/FlashSaleApp.exe
```

---

## 运行时生成文件

这些文件属于本地运行产物，默认**不应提交到公共仓库**：

- `gui_config.json`：本地运行配置
- `gui_run.log`：GUI 运行日志
- `_runtime_screenshots/`：运行过程截图目录
- `.schedule_anchor.txt`：运行锚点
- `.manual_resume.flag`：人工继续信号文件
- `build/`、`dist/`、`release/`：打包与发布产物

---

## 测试

运行项目内已有测试：

```bash
python -m pytest "test_flashsale_runner.py"
```

---

## 常见问题

### 1) 扫描不到 CDP 实例

- 确认浏览器环境已启动
- 确认目标环境开启了 CDP / WebDriver
- 确认当前选择的是正确店铺

### 2) 程序打开了创建页，但没有真正复制商品

当前版本已经增加了创建页商品确认逻辑：  
如果没有确认到实际商品，程序会中止保存，以避免误建空商品活动。

### 3) 运行中卡在验证码

- 按通知提示切回浏览器处理
- 处理后点击 GUI 中的 **手动继续**

### 4) 想调试验证码页面结构

可以使用：

```bash
python inspect_captcha.py
```

也可以通过参数指定 CDP endpoint：

```bash
python inspect_captcha.py http://127.0.0.1:9222
```

或者通过环境变量：

```bash
set CDP_PORT=9222
python inspect_captcha.py
```

---

## 开发说明

- 当前公共仓库默认不包含：日志、截图、构建产物、运行锚点、本地配置
- 如果你修改了打包逻辑，请同步检查 `FlashSaleApp.spec`
- 如果你修改了 GUI 配置结构，请同步更新 `gui_config.example.json`

---

## 安全提醒

- 不要把真实 `gui_config.json`、API key、日志、截图提交到仓库
- 不要把本地 `dist/`、`build/`、`release/` 目录直接推送到公共仓库
- 如果你曾在本地配置里填入过真实 key，请只在你自己的运行环境保存
