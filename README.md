<div align="center">

# 🌙 UESTC AutoCheckIn

**电子科技大学「晚安成电」班级管理工作台与个人签到工具**

一个服务管理多个学校账号：按账号查看可管理班级、按学号或姓名预览并提交情况上报、配置明确的学生上报规则；同一账号也可使用学生接口完成自己的签到。个人自动签到是独立的可选功能，新账号默认关闭。

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-fbbf24?style=flat-square)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-Welcome-22c55e?style=flat-square)](https://github.com/AndingDrLin/UESTC-AutoCheckIn/pulls)

[快速开始](#-快速开始) · [功能特性](#-功能特性) · [管理后台](#-管理后台) · [配置项](#-配置项) · [FAQ](#-faq) · [使用边界与风险](#-使用边界与风险)

</div>

---

> [!IMPORTANT]
> 本项目通过模拟浏览器请求完成操作，**不是学校官方客户端**。仅使用本人账号及学校明确授权的班级管理权限，先阅读文末的[使用边界与风险](#-使用边界与风险)。学校可能随时调整接口、加密方式或风控策略，由此产生的后果由使用者自行承担。

## 📖 项目简介

「晚安成电」的班干部账号既需要管理班级，也可能需要完成本人的学生签到。本项目将两种操作分开，复用同一账号的加密凭据和会话：

- **班级工作台**：按操作账号、日期和班级查询记录，选择具体学生，预览后提交情况上报。
- **上报规则**：以学生学号为目标，指定日期范围、星期、执行时间、情况和原因，不把同名学生或整个班级隐式作为目标。
- **我的签到**：使用学生接口执行本人签到，可单独开启随机计划；成功仍以回读 `CHECKED_IN` 为准。
- **账号与会话**：学校账号各自保存 CAS 凭据、指纹和加密会话；被管理学生不需要提供密码。

控制台继续使用一个 `ADMIN_PASSWORD`，不是多操作员权限系统。学校班干部权限由学校接口决定，本地无法授予。多学校账号可以共存，任何操作都绑定一个明确账号，不会在失败时自动切换账号。

## ✨ 功能特性

| | 特性 | 说明 |
| :---: | --- | --- |
| 🗓️ | **个人随机调度** | 仅为明确启用个人自动签到的账号在窗口内生成持久计划，默认 21:30–23:00，多账号错开 30 秒。 |
| 🔁 | **个人签到重试** | 个人签到按退避策略重试；情况上报写请求结果不明时不盲目重发。 |
| 🔐 | **会话复用** | 登录成功后加密保存 `SESSION`，后续优先复用，减少登录次数与二次验证风险。 |
| 🧬 | **浏览器指纹** | 保存 `MULTIFACTOR_BROWSER_FINGERPRINT` 后，学校会把账号视为可信设备，通常不再要求手机验证。 |
| ✅ | **结果可核对** | 提交后回读 `today`，以 `recordStatusCode == CHECKED_IN` 作为唯一成功标准，并展示打卡时间与异常标记。 |
| 👥 | **多学校账号** | 每个账号独立凭据、会话、可管理班级和可选个人签到；被管理学生无需密码。 |
| 🖥️ | **Web 管理后台** | 班级查询、明确选人、上报预览/提交、定时规则、本人签到、账号与日志。 |
| ⌨️ | **CLI** | `account` / `class` / `student` / `report` / `self` / `rule` / `plan` / `doctor` / `serve`。 |
| 🔔 | **通知** | 支持 ServerChan 或任意 JSON Webhook，成功 / 失败 / 需要人工处理时推送。 |
| 🐳 | **开箱即用** | 多阶段构建的精简镜像，非 root 运行，内置健康检查、时区与数据卷。 |

## 🚀 快速开始

### 1. 环境要求

- 一台能长期在线的机器（NAS、迷你主机、云服务器均可）；
- Docker 与 **Docker Compose v2**；
- 容器能访问 `idas.uestc.edu.cn` 与 `jzsz.uestc.edu.cn`；
- 你的账号已在「晚安成电」微应用中绑定宿舍，平时能在手机上正常打卡。

### 2. 克隆并配置

```bash
git clone https://github.com/AndingDrLin/UESTC-AutoCheckIn.git
cd UESTC-AutoCheckIn
cp .env.example .env
```

Windows PowerShell 使用：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`，**至少修改这两项**：

```dotenv
ADMIN_PASSWORD=换成一个长随机密码
APP_SECRET=换成一个固定的长随机字符串
```

生成随机值：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

> [!WARNING]
> `APP_SECRET` 用于加密已保存的账号密码。**首次启动后不要再随意更换**，否则旧密码将无法解密，只能重新录入。留空时会自动生成到数据卷的 `secret.key`，同样需要备份。

### 3. 启动

```bash
docker compose up -d --build
docker compose logs -f checkin
```

打开 <http://127.0.0.1:8000>，用 `ADMIN_PASSWORD` 登录。

#### GitHub Actions 多架构镜像

`.github/workflows/docker.yml` 使用 Docker Buildx 和 QEMU 构建同一个支持 `linux/amd64`、`linux/arm64` 的多平台镜像：

- 面向 `main` 的 Pull Request：仅构建验证，不登录镜像仓库、不发布。
- 推送到 `main`：发布到 `ghcr.io/<owner>/<repository>`（名称自动转小写），标签包含 `latest`、`main` 和 `sha-<短提交号>`。
- 推送 `v*` 标签：发布原始标签和提交标签；语义版本如 `v1.2.3` 还生成 `1.2.3`、`1.2`，不会覆盖 `latest`。
- 可在 Actions 页面手动触发；会发布所选分支/标签对应的镜像，只有选择 `main` 才更新 `latest`。

发布使用仓库自带的 `GITHUB_TOKEN` 和任务级 `packages: write`，不需要配置 Docker Hub 密钥。组织策略须允许发布 GHCR 包；首次发布后，如需匿名拉取，请在 GitHub Package 设置中确认包为公开。已有包须允许当前仓库的 Actions 写入。

工作流首次成功发布后可拉取，Docker 会自动选择宿主机架构：

```bash
docker pull ghcr.io/andingdrlin/uestc-autocheckin:latest
```

Fork 请替换为自己的小写 owner/repository。现有 Compose 仍默认本地构建；使用发布镜像时，将服务的 `image` 改为上述 GHCR 地址，然后执行 `docker compose pull` 和 `docker compose up -d --no-build`，保留原有环境变量、端口和数据卷。

### 4. 在后台完成配置

1. 在「账号与会话」添加学校账号，填写账号名称、学号、统一身份认证密码和浏览器指纹。可添加多个账号，各自拥有独立会话；新账号默认不启用个人自动签到。
2. 在班级工作台选择操作账号、日期和学校授权的班级，加载学生记录。只查看自己获授权的班级。
3. 选择明确的学生，填写情况类型、原因和说明，先查看预览；核对操作账号、日期和每位学生后再提交。
4. 如需本人签到，在「我的签到」配置真实 GCJ-02 宿舍坐标和窗口，先执行只读链路测试，再明确开启个人自动签到。
5. 如需定时情况上报，在「上报规则」为具体学号设置日期范围、星期和执行时间；规则与个人签到计划相互独立。

之后保持容器运行即可。默认只监听 `127.0.0.1:8000`；需要局域网访问时，把 `.env` 里的 `APP_BIND` 改为 `0.0.0.0`，并自行加 HTTPS 反向代理与访问控制。

> [!TIP]
> 首次部署先加载可管理班级或执行只读链路测试。预览不会提交情况上报；个人签到 CLI 也须显式传入 `--submit` 才提交。

## 🔑 浏览器指纹获取方法

学校用 `MULTIFACTOR_BROWSER_FINGERPRINT` 把「账号 + 浏览器」记为可信设备。填写后，自动登录通常不再触发手机二次验证；不填则可能每次登录都要验证，导致自动打卡失败。

### 获取步骤

1. 用**平时登录学校系统的那台电脑和浏览器**打开 <https://idas.uestc.edu.cn/authserver/login>，正常登录一次（这一次可以用手机验证）。
2. 按 `F12`（或 `Ctrl+Shift+I`）打开开发者工具，切换到 **Application / 应用** 面板。如果顶部没有这一项，点 `»` 展开更多面板。
3. 左侧依次展开 **Storage → Cookies → `https://idas.uestc.edu.cn`**。
4. 在顶部 Filter 输入 `MULTIFACTOR`，找到名为 `MULTIFACTOR_BROWSER_FINGERPRINT` 的 Cookie。
5. 复制它的 **Value**（32 位十六进制，不要带引号或空格），粘贴到添加 / 编辑账号页的「浏览器指纹」。
6. 保存后程序会自动转成大写。

> [!TIP]
> 列表里找不到这个 Cookie 时，先退出学校登录，再重新登录并完成一次手机验证；仍然没有就换 Chrome / Edge 重试。不要用无痕模式，关闭窗口后 Cookie 会丢失。

> [!NOTE]
> 指纹必须和固定的 User-Agent 配套使用。本项目默认使用一个真实的桌面 Chrome UA；如果你修改了 `USER_AGENT`，请确保它与产生指纹的浏览器一致，否则仍可能被要求二次验证。指纹本身不是密码，但可以标识设备，请不要发到公开 Issue 或截图里。

## 📍 宿舍坐标

- 坐标使用 **GCJ-02**（高德 / 腾讯地图坐标系），不是 WGS-84，也不是手机 GPS 的原始坐标。
- 推荐用高德官方的[坐标拾取工具](https://www.amap.com/ssr/coordinate-picker)：
  1. 打开链接，搜索「电子科技大学清水河校区 + 宿舍楼名」，或直接拖动地图到宿舍位置；
  2. 在宿舍所在位置单击，工具会显示该点的经度和纬度（通常写作「经度, 纬度」）；
  3. 把**纬度**填到表单的「纬度 latitude」，把**经度**填到「经度 longitude」，不要填反；
  4. 坐标保留 6 位小数即可，例如 `30.749700, 103.934500`。
- 学校打卡点通常有几百米半径，坐标越接近宿舍越好；如果学校接口可用，编辑页的「从学校打卡点中选择」会自动填充坐标、楼栋和地址，优先用它。
- 也可以手动填写经纬度。仅做班级管理的账号无需坐标；开启个人自动签到或实际提交本人签到前必须配置有效非零坐标。如果提示「不在打卡范围」，先核对坐标顺序和打卡点半径。

## ⏰ 每日调度

- 默认窗口：`21:30–23:00`，由 `CHECKIN_WINDOW_START` / `CHECKIN_WINDOW_END` 控制。
- 平台实际允许提交的时间约为 `21:00–23:30`，本项目的默认窗口落在这个范围内。
- 仅对明确启用个人自动签到的账号生成随机时间，多账号默认至少间隔 `MIN_USER_GAP_SEC` 秒。
- 个人计划在当天首次调度时写入 `plans` 表，**重启不会重新随机**。需要重新随机时，在「我的签到」操作或执行 `python -m app.cli plan regenerate`。
- 提交成功后会回读 `today`，只有 `recordStatusCode == CHECKED_IN` 才算最终成功；如果平台标记 `abnormalFlag`，会以警告形式显示，提醒人工核对。
- 已打卡的用户会被直接跳过，不会重复提交。

### 定时情况上报

上报规则绑定一个明确的操作账号和学生学号，并包含情况类型、原因、说明、生效起止日期、ISO 星期（1=周一，7=周日）和执行时间。规则不会借用另一账号的权限；个人自动签到关闭不影响该账号的上报规则。重叠规则需先消除冲突，避免多个管理账号重复上报同一学生。

规则任务与个人签到 `plans` 分开存储，每条规则每天至多领取一个任务。不补交历史日期；执行前重新读取当天学生记录及有效选项。失败、学校确认接收和写结果不明分别记录，写结果不明不自动重发，也不能当成已签到。Web 可编辑规则并重新校验权限及重叠；编辑不会重置当天已领取任务。删除规则会删除其任务列表，实际提交的审计日志仍保留。不要通过删除重建绕过未确认状态，先人工核对学校记录。

## 💻 管理后台

| 页面 | 作用 |
| --- | --- |
| `/` | 班级工作台：选择账号、日期和班级，查询学生，明确选择目标并预览、提交情况上报 |
| `/self` | 本人学生签到、个人随机计划和只读链路测试，与班干部上报分开 |
| `/accounts` | 多学校账号、加密凭据和会话管理；个人自动签到默认关闭 |
| `/rules` | 明确学号的上报规则、启停、日期/星期/时间及独立任务记录 |
| `/logs` | 运行日志，可按用户筛选、清空 |
| `/settings` | 版本、时区、窗口、调度器状态、数据库大小、环境变量说明 |
| `/healthz` | 公开健康检查（不含敏感信息） |

「测试链路」是**只读**的：会登录 / 复用会话、预热、进入微应用、读取今日状态和打卡点，**不会提交打卡**。第一次部署时建议先点它。

「立即打卡」只在平台时段（21:00–23:30）可用，用于补打卡或人工验证。平台每天只允许一次，请谨慎点击。

## ⌨️ 常用命令

```bash
# 查看日志
docker compose logs -f checkin

# 停止 / 启动 / 重建
docker compose stop
docker compose start
docker compose up -d --build

# 进入容器执行 CLI
docker compose exec checkin python -m app.cli doctor
docker compose exec checkin python -m app.cli doctor --network
docker compose exec checkin python -m app.cli account list
# 省略 --password 则安全交互输入；默认不启用本人自动签到
docker compose exec checkin python -m app.cli account add --name "班干部账号 A" --student-id 2020xxxxxx
docker compose exec checkin python -m app.cli class list --account 2020xxxxxx
docker compose exec checkin python -m app.cli plan show
docker compose exec checkin python -m app.cli plan regenerate

# 本人签到：默认只读，显式 --submit 才真正提交
docker compose exec checkin python -m app.cli self run --account 2020xxxxxx
docker compose exec checkin python -m app.cli self run --account 2020xxxxxx --submit

# 班干部情况上报：默认预览，确认后增加 --submit
docker compose exec checkin python -m app.cli report --account 2020xxxxxx --student-id 2023190505033 --location 校内 --reason 学习 --remark "正在努力复习母猪的产后护理。"

# 查找上报选项与学生，--account 始终表示操作账号，不是目标学生
docker compose exec checkin python -m app.cli report --account 2020xxxxxx --options --location 校内
docker compose exec checkin python -m app.cli student list --account 2020xxxxxx --keyword 2023190505033

# 明确日期范围和 ISO 星期；定时规则会自动修改学校记录，创建前请核对
docker compose exec checkin python -m app.cli rule add --account 2020xxxxxx --student-id 2023190505033 --location 校内 --reason 学习 --remark "正在复习" --start-date 2026-10-21 --end-date 2026-10-31 --weekdays 1,2,3,4,5 --run-time 22:00
docker compose exec checkin python -m app.cli rule jobs --account 2020xxxxxx
```

`doctor` 会检查数据目录、数据库、加密、窗口和默认密码；加上 `--network` 还会访问学校登录页，确认网络和页面结构正常。

## 🔧 配置项

所有配置都通过环境变量注入，完整示例见 [`.env.example`](.env.example)。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ADMIN_PASSWORD` | `changeme` | 管理后台密码。**必须修改**，否则仪表盘会持续告警。 |
| `APP_SECRET` | 空 | 加密账号密码、签名管理会话的密钥。留空时首次启动生成到数据卷的 `secret.key`。保持稳定；每个数据存储只运行一个应用实例。 |
| `TZ` | `Asia/Shanghai` | 容器时区，影响随机窗口和页面显示。 |
| `DATA_DIR` | `/data`（容器内） | SQLite、`secret.key` 的持久化目录。 |
| `DB_PATH` | `$DATA_DIR/checkin.db` | 可选，覆盖数据库路径。 |
| `CHECKIN_WINDOW_START` | `21:30` | 随机窗口开始，`HH:MM`。 |
| `CHECKIN_WINDOW_END` | `23:00` | 随机窗口结束，必须晚于开始。 |
| `RETRY_INTERVAL_SEC` | `180` | 个人签到失败重试基准间隔，实际按 1x / 1.5x / 2x 退避并加抖动。 |
| `MAX_ATTEMPTS` | `20` | 个人签到单日最大尝试次数，不适用于情况上报写请求。 |
| `MIN_USER_GAP_SEC` | `30` | 多账号个人签到计划之间的最小间隔。 |
| `LOGIN_COOLDOWN_SEC` | `600` | 同一用户两次登录的最小间隔。 |
| `MAX_LOGINS_PER_DAY` | `3` | 同一用户每天最多登录次数，降低风控风险。 |
| `HTTP_TIMEOUT` | `20` | 单次学校接口超时（秒）。 |
| `USER_AGENT` | 固定 Chrome UA | 不建议修改；必须与指纹对应的浏览器一致。 |
| `SCHEDULER_ENABLED` | `true` | 关闭后保留 Web / CLI 手动操作，同时停止个人自动签到和定时情况上报。 |
| `REVERSE_GEOCODE` | `false` | 提交前是否调用学校逆地理编码接口。关闭时使用用户地址或默认「四川省成都市」。 |
| `COOKIE_SECURE` | `false` | 仅在 HTTPS 反向代理后设为 `true`。 |
| `NOTIFY_WEBHOOK` | 空 | 全局通知地址；用户级 `notify_url` 可覆盖。 |
| `APP_BIND` | `127.0.0.1` | Compose 映射到宿主机的监听地址。 |
| `APP_PORT` | `8000` | 宿主机端口。 |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`。 |

## 💾 数据、备份与迁移

默认使用 Docker 命名卷 `uestc-checkin-data`，挂载到容器的 `/data`：

```text
/data/checkin.db    SQLite：学校账号、个人计划、上报预览、规则与任务、日志、加密会话
/data/secret.key    未设置 APP_SECRET 时自动生成的密钥
```

> [!NOTE]
> 直接在项目目录运行 `python -m app.cli ...` 时，数据写在项目的 `data/` 目录，和 Docker 命名卷**不共享**。首次用 Docker 部署时，建议启动后在 Web 后台重新添加用户；不要把本机 `data/` 当成容器数据。

备份（容器运行时）：

```bash
docker compose cp checkin:/data ./uestc-data-backup
```

或使用一次性容器打包：

```bash
docker run --rm -v uestc-checkin-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/uestc-data.tgz -C /data .
```

恢复时把文件放回卷内即可。**务必同时备份 `APP_SECRET`（或 `secret.key`）和 `checkin.db`**：换了密钥，已保存的用户密码将无法解密，只能重新录入。

升级会在原数据库增加上报预览、规则和任务存储，不删除原学校账号、密码、会话或个人计划。原账号的个人自动签到启停状态保持不变；只有新增账号默认关闭。内部 `users` 表仍用于学校账号，`enabled` 表示个人自动签到开关，不表示学校班干部权限或账号可否登录。迁移前备份数据库和对应密钥；一个数据存储只运行一个应用 worker。

情况上报预览绑定操作账号并有有效期，提交为一次性操作；提交前重新解析当天记录。中断或网络超时后可能无法确定学校是否已接收，界面会保留未确认状态，不自动切换账号或盲目重发。请先到学校核对，再决定是否重新准备新预览。

迁移到另一台机器：复制整个项目 + `.env` + 数据卷备份，在新机器执行 `docker compose up -d --build`，再恢复 `/data`。

## 🩺 故障排查

先看 `docker compose logs -f checkin`，再到 `/logs` 页面按用户筛选。常见情况：

| 现象 | 原因与处理 |
| --- | --- |
| `要求二次验证` / `MFARequired` | 指纹未填、填错，或与 User-Agent 不匹配。按上文重新获取指纹；仍不行时先在常用浏览器上完成一次手机验证，再复制新的指纹。 |
| `code=4001` / `登录无效或已过期` | 门户未预热或会话过期。程序会自动重登并预热；持续出现时点「测试链路」，检查是否能进入微应用。 |
| `用户名或密码错误` | 填的是教务 / 其他密码，或密码已修改。编辑用户，填写新的统一身份认证密码。 |
| `不在打卡范围` / 距离过远 | 坐标不是宿舍位置，或打卡点半径很小。用「从学校打卡点中选择」重新选点。 |
| `微应用未放行（checkLogin 未跳转到 H5）` | 账号未绑定「晚安成电」、微应用权限异常，或门户会话无效。先在手机 / 浏览器手动打开一次微应用确认。 |
| 页面显示「调度器未运行」 | `SCHEDULER_ENABLED=false`，或容器启动失败。看日志与 `/healthz`。 |
| 时间不对 | 容器 `TZ` 必须是 `Asia/Shanghai`；`docker compose exec checkin date` 核对。 |
| 健康检查失败 | 容器内 `PORT` 与映射端口不一致，或应用启动异常。看 `docker compose logs`。 |
| 登录页结构变化 | 学校改版。`doctor --network` 会报「未找到 execution 或 pwdEncryptSalt」，需要更新解析逻辑。 |

调试时可以把 `LOG_LEVEL=DEBUG`，但日志会更多；**不要把包含姓名、学号、手机号的响应原文贴到公开 issue**。

## 🔒 安全说明

- 用户密码使用 Fernet 对称加密后存入 SQLite；密钥来自 `APP_SECRET` 或数据卷中的 `secret.key`。
- 管理后台只用一个密码，登录态是 HMAC 签名的 HttpOnly Cookie；所有修改操作都有 CSRF token。
- 默认只绑定 `127.0.0.1`。如果要暴露到局域网 / 公网，请使用 HTTPS 反向代理，设置 `COOKIE_SECURE=true`，并加 IP 白名单或额外认证。
- 容器以非 root 用户 `appuser`（UID 10001）运行。
- 数据卷里包含可解密用户密码的密钥与密文，请像对待密码一样保护备份。
- 建议定期 `docker compose pull && docker compose up -d --build` 更新基础镜像与依赖。

## 📁 项目结构

```text
app/
  main.py            FastAPI 路由、管理后台、CSRF、模板上下文
  asgi.py            uvicorn 入口（app.asgi:app）
  cli.py             account / class / student / report / self / rule / plan / doctor / serve
  config.py          环境变量与 Settings
  db.py              SQLite schema 与数据访问
  planning.py        随机窗口、重试退避
  scheduler.py       后台调度线程、计划生成、重试
  security.py        Fernet、管理会话、密码校验
  notifier.py        ServerChan / JSON Webhook
  uestc/
    cas.py           统一身份认证登录
    crypto.py        统一身份认证密码 AES-128-CBC 加密
    http.py          requests 封装（手动 302、Cookie 控制）
    checkin.py       门户预热、微应用、today / checkin API
    service.py       单用户完整打卡编排
    admin.py         班干部协议接口与精确匹配
    admin_service.py 多账号会话、预览、提交、规则与上报任务编排
    errors.py        异常分类
  templates/         Jinja2 页面
  static/            CSS
tests/               pytest（不访问学校网络）
Dockerfile
docker-compose.yml
.env.example
```

## 🛠️ 开发与测试

```bash
python -m venv .venv

# Windows
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest

# Linux / macOS
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

测试覆盖数据库、随机计划、AES 加密、管理会话、H5 载荷 / 错误分类、统一身份认证响应分类，以及 FastAPI 管理后台的主要页面和 CSRF。测试不会访问学校网络。

如果要连真实环境做只读验证：

```bash
docker compose exec checkin python -m app.cli doctor --network
docker compose exec checkin python -m app.cli self run --account 2020xxxxxx
```

不带 `--submit` 的 `self run` 不提交本人签到，只验证登录、预热、微应用、今日状态和打卡点。`report` 默认只生成预览；与只读验证一样，可能消耗一次 CAS 登录并更新缓存会话。

## 🗺️ Roadmap

- [x] 多用户、随机窗口、失败重试
- [x] 加密会话复用与浏览器指纹
- [x] Web 管理后台与完整 CLI
- [x] ServerChan / 通用 Webhook 通知
- [ ] 一键部署模板（NAS / 群晖 / 1Panel）
- [ ] 多架构镜像（`amd64` / `arm64`）发布
- [ ] 更细粒度的审计日志与告警策略

> 有想加的功能？欢迎开 [Issue](https://github.com/AndingDrLin/UESTC-AutoCheckIn/issues) 或直接提 PR。

## ❓ FAQ

<details>
<summary><b>会被学校检测到吗？会不会封号？</b></summary>

程序模拟的是正常浏览器的完整链路，并内置了登录冷却、每日登录上限、随机时间与多用户错峰。但它终究不是官方客户端，学校可能调整风控策略。**请只用于自己的账号，不要批量代打、出售服务或绕过学校的合理限制**，风险由使用者自行承担。
</details>

<details>
<summary><b>电脑必须一直开着吗？</b></summary>

是。容器需要持续运行，机器休眠、断网或 Docker 停止都会导致当天无法自动执行。建议放在 NAS、迷你主机或云服务器上，并把系统设置为不休眠，同时配置通知。
</details>

<details>
<summary><b>支持多用户 / 多账号吗？</b></summary>

支持多个学校账号，各自独立凭据、指纹、会话、学校授权班级和上报规则。每个账号还可选择配置本人的学生签到，默认关闭；个人签到关闭不妨碍班级管理。控制台仍共用一个管理密码，不提供多操作员隔离。
</details>

<details>
<summary><b>忘记管理后台密码了怎么办？</b></summary>

修改 `.env` 中的 `ADMIN_PASSWORD`，然后执行 `docker compose up -d` 重建容器即可。
</details>

<details>
<summary><b>APP_SECRET 丢了，数据还能恢复吗？</b></summary>

如果当初留空，密钥在数据卷的 `secret.key` 里，恢复该文件即可。如果显式设置过 `APP_SECRET` 且已经丢失，已保存的密码无法解密，只能在后台重新录入。**备份时请务必将密钥与数据库一起备份。**
</details>

<details>
<summary><b>一定要用 Docker 吗？</b></summary>

不一定。项目本身是标准 Python 应用，可以直接 `python -m app.cli serve` 运行；但 Docker 方式自带时区、数据卷、健康检查和非 root 用户，长期运行更省心。注意本地运行的 `data/` 与容器命名卷不共享。
</details>

<details>
<summary><b>可以固定某个时间打卡吗？</b></summary>

个人签到继续在配置窗口内随机执行，不提供固定秒级签到。情况上报规则则使用明确的 `HH:MM` 执行时间，受后台调度轮询和网络耗时影响，不保证秒级准点。两类任务分开管理。
</details>

<details>
<summary><b>提交成功但页面显示未确认？</b></summary>

以回读 `today` 的结果为准。平台每天只允许一次打卡，回读未确认时不要反复点「立即打卡」，先到平台核对，再查看 `/logs` 中的错误详情。
</details>

## 班干部协议 API

`app/uestc/admin.py` 根据 HAR 封装学校的低层班干部接口。直接调用本模块须提供已登录、已预热且获学校授权的 `UestcHttp`；本模块不执行登录或重试。Web、CLI 和定时上报统一使用 `app/uestc/admin_service.py:AdminService` 管理账号会话、预览、一次性提交和操作记录。

| Python 函数 | 学校接口（相对于 `/daily-checkin/api`） | 返回值 |
| --- | --- | --- |
| `fetch_checkin_count(client, business_date)` | `GET /class-cadre/checkin-count` | 整数计数 |
| `fetch_my_classes(client)` | `GET /class-cadre/my-classes` | 原始班级列表，包含 `classKey` |
| `fetch_checkin_list(client, business_date, class_key, *, keyword="", page_index=1, page_size=10)` | `GET /class-cadre/checkin-list` | 完整响应，包含同级 `data` 和 `page` |
| `fetch_situation_types(client)` | `POST /common/attachMore` | `situationTypeOptions` 列表 |
| `fetch_case_keywords(client, location_type_code)` | `GET /case-keyword/active` | 原始关键词列表，包含 `remarkRequired` |
| `report_situations(client, reports)` | `POST /class-cadre/situation/report` | 学校返回的布尔确认值 |

`business_date` 使用 `YYYY-MM-DD`；分页从 1 开始，不自动遍历后续页。`page` 保留学校的 `pageIndex`、`pageSize`、`totalCount`、`orderBy`、`totalPage`，其中 `totalCount` 在 HAR 中为字符串。记录 ID、班级键及其他原始字段不转换。

`location_type_code` 为 `CAMPUS` 或 `OUTSIDE`。`reports` 是 JSON 数组，每项包含 `classKey`、`recordId`、`locationTypeCode`、`caseKeywordId`、`caseRemark`；班级键和关键词 ID 应从对应查询接口获取。**上报会修改学校记录**，布尔确认不代表学生已完成签到。接口失败沿用现有的 `SessionInvalid`、`ApiError`、`HttpStatusError`、`NetworkError` 异常，不把失败当成空列表或成功。

### 按学号或姓名上报

以下高层 API 自动查询可管理班级和全部分页，精确匹配学生，并将中文情况类型、原因解析为学校所需的 ID：

- `resolve_student(client, business_date, *, student_id=None, student_name=None, class_key=None)`：返回 `{"classKey": ..., "record": ...}`。
- `build_situation_report(client, business_date, location, reason, remark, *, student_id=None, student_name=None, class_key=None)`：只查询并构造单条上报字典，**不提交**；可交给 `report_situations` 批量提交。
- `report_student_situation(...)`：参数同上，解析成功后提交一次，返回学校的布尔确认值，**会修改学校记录**。

```python
from app.uestc.admin import build_situation_report, report_student_situation

# client 为调用方已登录并预热的 UestcHttp 会话。
payload = build_situation_report(
    client,
    "2026-10-21",
    "校内",
    "学习",
    "正在努力复习母猪的产后护理。",
    student_id="2023190505033",
)

# 仅在确需修改学校记录时调用；这不是预览。
acknowledged = report_student_situation(
    client,
    "2026-10-21",
    "校内",
    "学习",
    "正在努力复习母猪的产后护理。",
    student_id="2023190505033",
)
```

学号和姓名至少提供一个；同时提供时必须都匹配。姓名不做模糊匹配，同名时须增加学号或 `class_key` 限定。未找到学生、情况类型或原因，匹配不唯一，或原因要求说明但说明为空白时，抛出 `ApiError` 且不提交。原因只在选定的情况类型内查找。每天的 `recordId` 按指定日期重新查询，不缓存、不跨日复用；低层选项从学校读取，不硬编码。控制台和 CLI 使用上层 `AdminService.prepare(...)` / `submit(...)`，而不是直接调用低层提交函数。

## 🤝 贡献

欢迎任何形式的贡献：报告 Bug、改进文档、补充测试、提交新功能。

1. Fork 本仓库并创建分支：`git checkout -b feat/your-feature`；
2. 提交前运行测试：`.venv/Scripts/python -m pytest`；
3. 提交信息建议使用 `feat: ...` / `fix: ...` / `docs: ...`；
4. 发起 Pull Request，并描述改动目的与验证方式。

如果这个项目帮你省下了每晚的闹钟，欢迎点一个 ⭐ **Star**，这是对作者最大的支持。

## 🚨 使用边界与风险

> [!WARNING]
> 请在使用前完整阅读本节。

- 本项目通过模拟浏览器请求完成打卡，**不是学校官方客户端**。学校可能随时调整接口、加密方式、风控策略或使用条款。
- 本人签到只使用自己的账号；班级情况上报仅限学校明确授权的管理范围，内容须符合真实情况。**不要批量代打、出售服务或绕过学校的合理限制**。
- 自动打卡的前提是：人在宿舍附近、账号状态正常、微应用已绑定。坐标造假或人不在宿舍可能被平台判定异常（`abnormalFlag`），后果由使用者承担。
- 容器必须持续运行；机器休眠、断网、Docker 停止都会导致当天无法自动执行。
- 平台每天只允许一次打卡。提交成功但回读未确认时，不要反复手动点击「立即打卡」，先到平台核对。
- 首次部署请先用「测试链路」验证，不要直接等到晚上。
- 本项目采用 MIT License，不对因使用本软件产生的任何直接或间接损失负责。

## 🙏 致谢

- [richard297779/UESTC-MoonCheck](https://github.com/richard297779/UESTC-MoonCheck)：原 ESP32 固件项目，本项目的协议细节、接口行为与抓包结论大量参考了它的 `docs/api-contract.md` 与 `tools/pc_reference/`。
- [FastAPI](https://fastapi.tiangolo.com/)、[uvicorn](https://www.uvicorn.org/)、[requests](https://requests.readthedocs.io/)、[cryptography](https://cryptography.io/)：让这个服务足够小，也足够稳。

## 📄 License

本项目基于 [MIT License](LICENSE) 开源。

<div align="center">

**Made with 🌙 for UESTC**

如果这个项目对你有帮助，请点一个 ⭐ Star 支持一下。

</div>
