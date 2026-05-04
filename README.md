# 4399全自动注册

4399游戏平台批量自动注册工具，支持验证码自动识别、实名认证、并发注册、注册后自动登录获取Sauth凭证。可通过GitHub Actions云端运行。

如果失效可以直接在issues里反馈。

## 功能

- 自动识别验证码（自定义CNN模型 / ddddocr）
- 自动实名认证
- 多线程并发注册
- 注册成功后自动登录，获取Sauth凭证
- 支持代理IP
- 支持GitHub Actions云端运行，所有参数可视化配置
- 支持按注册数量或运行时长自动停止

## 目录结构

| 文件 | 说明 |
|---|---|
| auto_register_4399.py | 主程序 |
| login_4399.py | 登录模块，获取Sauth |
| captcha_pipeline.py | 验证码模型训练流水线（下载/标注/训练/推理） |
| captcha_model.pth | 训练好的验证码识别模型 |
| sfz.txt | 实名认证用身份证（格式：`姓名----身份证号`） |
| IP.txt | 代理IP列表（格式：`ip:port`，每行一个） |
| 4399.txt | 输出：注册成功的账号密码 |
| sauth.json | 输出：登录后的Sauth凭证 |
| used_sfz.txt | 已使用的身份证记录 |
| register.log | 运行日志 |
| requirements.txt | Python依赖 |
| .github/workflows/register.yml | GitHub Actions工作流 |

## GitHub Actions 使用（推荐）

### 1. Fork 或上传代码到你的GitHub仓库

### 2.（可选）配置Secret

如果不想把身份证数据放在仓库里，可以配置Secret：

```bash
# 本地生成base64编码
base64 -w 0 sfz.txt
```

到仓库 **Settings → Secrets and variables → Actions**，新建Secret：
- 名称：`SFZ_DATA`
- 值：上面输出的base64字符串

### 3. 运行

到仓库 **Actions** 页 → 选择 **4399 Auto Register** → **Run workflow**，填写参数后运行。

### 4. 查看结果

运行完成后在 Actions 页面下载 **Artifacts**，包含：
- `4399.txt` — 账号密码
- `sauth.json` — Sauth登录凭证
- `register.log` — 运行日志

### 可配置参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| count | 成功注册数量（0=不限） | 0 |
| duration | 最大运行时长(秒) | 3600 |
| workers | 并发线程数 | 3 |
| max_sfz_uses | 每个身份证最大使用次数 | 4 |
| username_prefix | 用户名前缀（留空=纯随机） | |
| username_len | 用户名总长度 | 7 |
| password_len | 密码长度 | 10 |
| use_custom_model | 使用自定义验证码模型 | true |
| auto_login | 注册后自动登录获取Sauth | true |
| use_proxy | 使用代理IP | false |
| max_captcha_retry | 验证码最大重试次数 | 3 |
| min_interval | 每轮最小间隔(秒) | 1 |
| max_interval | 每轮最大间隔(秒) | 3 |

工作流默认每6小时自动运行一次（cron: `0 */6 * * *`），可在 `.github/workflows/register.yml` 中修改。

## 本地运行

### 环境要求

- Python 3.12+
- 如使用自定义模型：需要PyTorch（CPU即可）

### 安装

```bash
pip install -r requirements.txt
# PyTorch（如需自定义验证码模型）
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### 运行

```bash
# 按数量运行
python auto_register_4399.py --count 10

# 按时长运行（3600秒）
python auto_register_4399.py --duration 3600

# 无限运行
python auto_register_4399.py

# 也可以通过环境变量覆盖配置
USE_PROXY=true WORKERS=5 python auto_register_4399.py --count 20
```

### 环境变量配置

所有配置项都支持环境变量覆盖，未设置时使用代码中的默认值：

| 环境变量 | 对应配置 |
|---|---|
| USE_PROXY | 是否使用代理 |
| PROXY_FILE | 代理IP文件路径 |
| USE_CUSTOM_MODEL | 是否使用自定义模型 |
| CUSTOM_MODEL_FILE | 模型文件路径 |
| MAX_CAPTCHA_RETRY | 验证码重试次数 |
| MAX_SFZ_USES | 身份证最大使用次数 |
| CAPTCHA_LENGTH | 验证码长度 |
| USERNAME_PREFIX | 用户名前缀 |
| USERNAME_LEN | 用户名长度 |
| PASSWORD_LEN | 密码长度 |
| AUTO_LOGIN | 注册后自动登录 |
| SFZ_FILE | 身份证文件路径 |
| USED_SFZ_FILE | 已使用身份证文件路径 |
| OUTPUT_FILE | 输出文件路径 |
| SAUTH_FILE | Sauth输出文件路径 |
| LOG_FILE | 日志文件路径 |
| WORKERS | 并发线程数 |
| MIN_INTERVAL | 最小间隔 |
| MAX_INTERVAL | 最大间隔 |

## 数据文件格式

### sfz.txt（身份证）

```
姓名----18位身份证号
王春莲----370123196401240541
刘如喜----370123196412110515
```

### IP.txt（代理IP）

```
ip:port
1.231.81.166:3128
101.251.204.174:8080
```

### 4399.txt（输出：账号密码）

```
username----password
```

### sauth.json（输出：Sauth凭证）

每行一个JSON对象：
```json
{"username": "abc1234", "password": "x9c0ehiys7", "sauth": "{\"sauth_json\": \"...\"}"}
```

## 验证码模型训练

如需训练自己的验证码识别模型：

```bash
# 下载验证码图片
python captcha_pipeline.py collect

# 自动标注
python captcha_pipeline.py label

# 人工检查标注后训练
python captcha_pipeline.py train

# 或一键全流程
python captcha_pipeline.py all
```

## 赞助

- [原作者 mcqtss](https://afdian.net/@mcqtss)
- [A_DW_MC](https://www.ifdian.net/a/A_DW_MC?utm_source=copylink&utm_medium=link)
