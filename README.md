# 微信流水风险分析器 · 服务端版（线上 / 密码访问 / 流水收集）

把分析放到**服务器**运行：浏览器只上传文件、展示报告，分析器 Python 源码不再下发，
别人无法复制；用「访问密码」做进入门槛；每次成功分析后，会把用户上传的流水明细
写入本地 SQLite 收集起来，供站点主人导出去做汇总研究。

> 与「网页版（纯浏览器 Pyodide）」的区别：服务端版**不再依赖 jsdelivr / Pyodide**，
> 因此国内访问更稳；代价是文件需上传到服务器，且本版本会**主动收集**上传的流水明细。

---

## 一、本地运行

```bash
cd server
pip install -r requirements.txt
python server.py
# 打开 http://127.0.0.1:8000
```

- 首次运行若 `site_config.json` 未设密码，会自动启用默认密码 **`wechat2026`**，
  请务必尽快修改（见第二节）。
- 收集的数据落在 `server/data/flow.db`（SQLite）。

---

## 二、访问密码（门槛）

```bash
cd server
# 设置 / 修改访问密码
python manage.py setpw <你的密码>

# 查看已收集量
python manage.py stats

# 导出已收集流水为 CSV（utf-8-sig，Excel 直接打开中文不乱码）
python manage.py export collected_flow.csv
```

- 密码以哈希形式存在 `server/site_config.json`，不存明文。
- 会话用 Flask 签名 Cookie 维持，密码正确后服务端签发，之后才能调用分析接口；
  密码错误 → `401`；未登录调用 `/api/analyze` → `401`。
- 前端也有「退出」按钮清除会话。

### 想做收费 / 多用户？
当前是**单一共享密码**（谁拿到密码谁都能用），适合「小圈子 / 内部 / 先门槛后观察」。
若要按量收费或分用户，可在此基础上扩展：
1. 把 `site_config.json` 的单一密码换成「密码 + 配额」表（参考旧版 `keys.json` 思路）；
2. 或接入数据库 + 支付回调，在 `/api/analyze` 里校验额度。仓库已留好会话与接口结构，改动集中。

---

## 三、流水收集（数据沉淀）

每次成功分析后，服务端把该次上传的**全部解析明细**（过滤前）写入 SQLite：

- `uploads` 表：每次上传一条（时间、文件名、行数、来源 IP）；
- `records` 表：每条交易一行（时间、金额、对手方、类型、备注、方式、收/支方向）。

导出与统计：
- 前端登录后点「导出已收集流水(CSV)」→ 下载 `collected_flow.csv`；
- 后端 `GET /api/export`、`GET /api/stats`（均需登录）。

> 收集的是「交易明细」而非原始文件。若你只想分析、不想留存，可注释掉
> `server.py` 中 `analyze()` 里的 `store_records(...)` 调用。

---

## 四、国内部署（重点）

### 方案 A：云服务器（最通用，推荐）
买一台国内云服务器（腾讯云 CVM / 阿里云 ECS / 华为云等），装 Python 3.10+：

```bash
# 1) 上传整个 server/ 目录
# 2) 安装依赖
pip install -r requirements.txt
pip install gunicorn
# 3) 设置密码
python manage.py setpw <你的密码>
# 4) 生产启动（2~4 个 worker）
gunicorn -w 2 -b 0.0.0.0:8000 server:app
```

- 域名 + HTTPS：用 Nginx 反代 `127.0.0.1:8000`，申请免费 SSL（Let's Encrypt / 云厂商证书）。
- 文件上传上限 30MB，可在 `server.py` 改 `MAX_CONTENT`。
- **无需任何外部 CDN**，国内访问稳定。
- 收集库 `data/flow.db` 随进程目录走，部署时把 `server/` 整体迁移即可保留数据。

### 方案 B：云函数 / 云托管（按量付费，省运维）
- 腾讯云 **CloudBase 云托管** / **云函数**、阿里云 **函数计算 FC**（Custom Runtime）。
- 把 `server/` 作为 Web 服务部署；监听 `0.0.0.0:$PORT`。
- 注意函数超时（默认 3 分钟，PDF 解析通常秒级够用）；多实例时 `flow.db` 建议换云数据库，
  或挂载持久化存储，避免各实例数据分散。

---

## 四-B、免费托管快速上线（不花钱先跑起来）

### 方案 1：Render.com（海外免费，最简单，推荐先试）
- 把 `server/` 整个目录推到 GitHub 仓库；
- 注册 render.com → New → Blueprint → 连该仓库 → 自动识别 `render.yaml` 一键部署；
- 免费版有「冷启动」（几分钟不用会休眠，下次访问要等十几秒唤醒）；
- ⚠️ 免费版磁盘是临时的：服务休眠/重启后 `data/flow.db` 会被清空，**收集的流水会丢**。
  若只想用工具、不介意留存，完全没问题；若要长期沉淀数据，用下方国内方案或挂载持久盘。

### 方案 2：本地 Mac + 内网穿透（零成本，国内可达，但 Mac 需开机）
- 本地运行 `python server.py`，手机连同一 WiFi 用 `http://<Mac局域网IP>:8000` 访问；
- 想用手机流量 / 远程访问：装 `ngrok` 或 `cpolar`（均有免费隧道），把 8000 端口暴露为公网地址；
- 缺点：Mac 关机即不可用（与之前「脱离电脑」诉求冲突，仅适合临时用）。

### 方案 3：国内云（免费额度，需账号 + 实名，速度最快）
- 腾讯云 **CloudBase 云托管**：有免费资源额度，支持 Python 容器，国内节点，适合本工具；
- 阿里云 **函数计算 FC**（Custom Runtime）：每月有免费调用 / 时长额度；
- 两者都需对应云账号并完成实名认证，部署时把 `server/` 作为 Web 服务、监听 `0.0.0.0:$PORT` 即可
  （启动命令与方案 A 相同）。

### 上线后必做
1. `python manage.py setpw <你的密码>` 改掉默认密码 `wechat2026`；
2. 国内方案记得在 Nginx / 网关上加 HTTPS；
3. 收集敏感财务数据，请在页面 /《隐私政策》中告知用户（前端已有橙色提示横幅）。

---

## 五、隐私与合规（重要，务必告知用户）

本版本会**收集并留存**用户上传的流水明细（交易时间、金额、对手方等），属于敏感财务信息。
部署前请务必：

1. 在页面显著位置告知用户数据会被收集（前端已内置橙色提示横幅，可按需修改措辞）；
2. 准备一份《隐私政策 / 用户协议》，说明收集范围、用途、留存期限、是否对外提供；
3. 国内收集个人金融信息需留意《个人信息保护法》相关要求，建议仅用于用户授权的研究目的，
   不对外出售 / 共享，并定期清理。
4. 若仅做内部分析、不想触碰合规红线，可在 `server.py` 注释掉 `store_records(...)`。

---

## 六、目录结构
```
server/
├── server.py                    # Flask 后端：/、/api/login、/api/analyze、/api/export、/api/stats
├── wechat_flow_risk_analyzer.py # 分析器（与本地版同源，已含最新改动）
├── site_config.json             # 密码哈希 + 会话密钥（首次自动生成）
├── manage.py                    # 改密码 / 导出 / 统计
├── requirements.txt
├── README.md
├── data/flow.db                 # 收集到的流水（SQLite，运行时生成）
└── static/
    └── index.html               # 前端：密码登录 + 上传 + 报告展示 + 导出
```
