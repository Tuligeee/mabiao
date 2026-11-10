# 码表核对

门店推币机 / 弹珠机的每日码表录入与对账。

- 线上：http://47.114.33.192/meter/
- 本地开发：http://127.0.0.1:8899/
- 仓库：https://github.com/Tuligeee/mabiao

## 开发流程

```
改本地代码  →  git commit  →  部署 + 推 GitHub
```

两个命令：

```powershell
# 推到服务器（只传代码，数据库不动）
.\deploy\deploy.ps1

# 推到 GitHub（经服务器中转，见下）
& "C:\Users\Tuilge\.workbuddy\binaries\python\envs\default\Scripts\python.exe" deploy\push_github.py
```

**为什么推 GitHub 要绕服务器**：这台电脑连不上 github.com（DNS 和 TLS 都过不去）。服务器上有部署密钥、能直连，所以流程是「本地打包 → 传服务器 → 服务器推」。

服务器上的两把钥匙，各管一个仓库：

| 密钥 | 仓库 | SSH Host 别名 |
|---|---|---|
| `~/.ssh/cwsj_deploy` | `Tuligeee/cwsj` | `github.com` |
| `~/.ssh/mabiao_deploy` | `Tuligeee/mabiao` | `github-mabiao` |

GitHub 的部署密钥全局唯一，一把 key 只能挂一个仓库，所以必须分开。

**部署规矩（重要）**

- 部署只传代码，**不动服务器上的数据库**
- 每次部署前自动备份服务器库
- 本地 `app/data/meter.db` 只是开发用，服务器那份才是真实数据
- 服务器没有库时，才会用本地快照初始化一次

## 目录

| 目录 | 内容 |
|---|---|
| `app/` | 服务端 + 前端（单页，无框架） |
| `app/server.py` | 全部后端：SQLite、宝点取数、对账、接口 |
| `app/app.js` | 前端逻辑与渲染 |
| `app/ui.js` | 自绘控件（选择器、日历、确认框、toast） |
| `app/theme.css` | 品牌层：颜色变量（先引 tokens，再覆盖） |
| `app/tokens.css` | 设计 token（自动生成，别手改） |
| `deploy/` | 部署脚本 |
| `docs/` | 组件规格等文档 |
| `miniprogram/` | 微信小程序版本（暂缓） |

## 部署

```powershell
# 需要带 paramiko 的 python
& "C:\Users\Tuilge\.workbuddy\binaries\python\envs\default\Scripts\python.exe" deploy\deploy.py
```

服务器上：

| 项 | 值 |
|---|---|
| 目录 | `/opt/meter` |
| 进程 | systemd `meter`，监听 `127.0.0.1:8899` |
| 反向代理 | nginx `location /meter/`，带 `X-Forwarded-Prefix` |
| 数据库 | `/opt/meter/data/meter.db` |
| 备份 | `/opt/meter/data/meter.db.bak-<时间戳>` |

## 账号

沿用门店任务管理平台（cwsj）的账号。同域时浏览器直接复用它的登录态，不用二次登录。

## 机台分类

- **推币机**：宝点大类「推币机」，排除名字带「弹珠」的
- **弹珠机**：名字带「弹珠」的（蓝票 / 红票 / 出珠 / 出点 / 弹弹侠 / 塔兰）

一块码表 = 一个读数单元。一台机可能有多个码表（如可乐风暴 4 块），用「合并」把共用一个码表的卡头并成一台。

## 二维码

- 一台机一个码，编码是**稳定编号**（`S18-002`），不是数据库行号
- 扫码进 `/?c=<编号>`，直接进这台机的填写页
- 打印页：设置 → 打印机台二维码
