# 码表核对 · 本地工具

每天只填门店机器上的**码表累计读数**，上币/下币从宝点两个接口自动拉，误差自动算。

## 起服务

```powershell
$py="C:\Users\Tuilge\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$env:PORT="8899"
& $py "C:\Users\Tuilge\Desktop\码表线上化\app\server.py"
```

浏览器打开 <http://127.0.0.1:8899/>。

数据落在 `app\data\meter.db`（SQLite 单文件），备份就复制它。

## 三个页签

| 页签 | 干什么 |
|---|---|
| 录入 | 选门店+日期，填 10 个码表单元的今日入币/出币；填完点「保存并拉宝点数据」 |
| 核对 | 按机台看 卡头上币 / 下币 / 卡头收币 / 码表收币 / 误差 / 状态 |
| 设置 | 门店（从宝点列表选）、机台清单（共用码表开关、停用/启用）、历史回填、改动记录 |

## 取数口径

| 侧 | 宝点菜单 | 接口 |
|---|---|---|
| 上币 | 机台消费统计 → 机台消费币汇总 | `POST /reports/report/shop/coinoutplay/list` |
| 下币 | 机台获分记录 → 按设备汇总 | `POST /terminals/backend/sport/get/sport_integral_summary/list` |

- 关联键是**通讯ID**，不是机台名+P位（P位会随卡头换位变）
- 下币 = `integralQuantity / 10`
- 参与核对的机台 = `项目分组 → 推币机` 大类（映射表 `machine_type_map.json`）
- 子类型「屏幕机」= 四联机共用一个码表，其余每个通讯ID 一个码表

## 状态含义

| 状态 | 意思 |
|---|---|
| 正常 | 两边都有数，可以算误差 |
| 首日 | 没有前一天读数，算不出当日增量 |
| 读数下降 | 今天的读数比昨天小：可能抄错，也可能换表/清账，要人确认 |
| 缺码表 | 这天没填读数 |
| 缺卡头 | 宝点这天没有这台机的数据 |

## 登录态

宝点 JWT 12 小时过期。过期后页面顶部会显示「宝点未连接」，重跑
`C:\Users\Tuilge\Desktop\上套餐工具` 的连接流程刷新 `自动化合集\_shared` 里的共享 token。
