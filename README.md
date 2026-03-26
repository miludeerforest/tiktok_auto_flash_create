# TikTok Auto Flash Create

用于 TikTok Seller Center Flash Sale 活动复制与创建的桌面自动化工具。

## 当前特性

- Tkinter 图形界面，适合打包为 Windows EXE 使用
- 不再依赖 `xlsx` 排期文件，改为手动填写 4 条参考活动
- 自动扫描可用 CDP 店铺实例并连接已有浏览器
- 复制前收紧 Upcoming 活动识别，避免误选错误模板
- 保存前校验创建页是否真的复制到了商品，阻止生成空商品活动
- 运行中检测验证码/弹窗，并通过更接近 Windows 10/11 风格的应用内通知提醒人工处理

## 目录说明

- `app_gui.py`：图形界面入口
- `flashsale_runner.py`：核心自动化逻辑
- `captcha_solver.py`：验证码辅助处理逻辑
- `test_flashsale_runner.py`：关键逻辑测试
- `FlashSaleApp.spec`：PyInstaller 打包文件
- `gui_config.example.json`：示例配置

## 使用方式

### 方式一：源码运行

1. 安装依赖（至少需要 `playwright`，其余依赖按你的环境补齐）
2. 将 `gui_config.example.json` 复制为 `gui_config.json`
3. 按需填写 4 条参考活动
4. 运行：

```bash
python app_gui.py
```

### 方式二：打包 EXE

```bash
python -m PyInstaller "FlashSaleApp.spec" --noconfirm
```

生成文件位于 `dist/FlashSaleApp.exe`。

## 图形界面操作流程

1. 填写 4 条参考活动名称
2. 点击“扫描实例”
3. 选择店铺并检测 CDP
4. 点击“一键运行”
5. 如果出现验证码或人工弹窗处理需求，按界面通知切到浏览器处理，再点击“手动继续”

## 配置说明

`gui_config.json` 字段：

- `batch_rounds`：本次运行轮数
- `seed_names`：4 条参考活动名称
- `ai_provider`：可选 `openai` / `gemini`
- `ai_api_key`：留空或自行填写
- `ai_model`：留空则使用默认
- `ai_base_url`：兼容 OpenAI 协议的可选地址

> 仓库默认不提交真实 `gui_config.json`，请使用 `gui_config.example.json` 自行复制。

## 测试

```bash
python -m pytest "test_flashsale_runner.py"
```

## 注意

- 本仓库不包含你的本地运行日志、截图、构建产物或私有配置
- 如需推送公开仓库，请不要提交真实 API key、日志、截图和本地配置文件
