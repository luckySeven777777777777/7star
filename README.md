---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: c33be2acbae4f92c16f9e5a0c17cd2a0_76343347bd1e11f189c8525400393706
    ReservedCode1: DkiiCE1UKu/QaomxOV5KneNAlpzbYgVDizh/QgDsbNtElNoqvu4oA7+1L0ZnjQkC6kjbsXj6QgaZmAeX2PrN+gMQjcQ4r9PP6mSenSptqXCivlbTHNC+XUfEWSglIupje9WeWfKxja4KD61AoU0UZNsFDbsQi0X+t789ctUHYASjuMzaMRpoWVO03ak=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: c33be2acbae4f92c16f9e5a0c17cd2a0_76343347bd1e11f189c8525400393706
    ReservedCode2: DkiiCE1UKu/QaomxOV5KneNAlpzbYgVDizh/QgDsbNtElNoqvu4oA7+1L0ZnjQkC6kjbsXj6QgaZmAeX2PrN+gMQjcQ4r9PP6mSenSptqXCivlbTHNC+XUfEWSglIupje9WeWfKxja4KD61AoU0UZNsFDbsQi0X+t789ctUHYASjuMzaMRpoWVO03ak=
---

# 包子店外卖实时定位追踪系统（Railway 部署版）

- 前端全部界面为**缅文（မြန်မာဘာသာ）**
- 存储使用 **PostgreSQL**（Railway 数据库），**记录永久保存**，重启/重新部署不丢失
- 部署后获得**公网链接**，客户不需要连接店里 WiFi，在任何地方都能实时查看外卖员位置
- 查询方式：输入**订单号 + 外卖员手机号**，地图实时显示位置与配送轨迹

## 一、功能

| 角色 | 页面 | 功能 |
|------|------|------|
| 商家 | `/rider` | 一键生成订单号 |
| 外卖员 | `/rider` | 绑定订单号 + 手机号，GPS 实时上报位置；无 GPS 可「模拟配送」 |
| 客户 / 商家 | `/track` | 输入订单号 + 外卖员手机号，地图实时查看位置、轨迹、状态，每 5 秒自动刷新 |

## 二、部署到 Railway（完整步骤）

### 第 1 步：把项目上传到 GitHub

1. 注册 GitHub 账号（github.com），登录后点右上角 **+ → New repository**，创建公开仓库（如 `baozi-tracking`，不要勾选 README）
2. 把本文件夹 `baozitracking_railway` 里的所有文件上传到该仓库：
   - 网页方式：仓库页面点 **Add file → Upload files**，把 `app.py`、`requirements.txt`、`Procfile`、`templates` 文件夹（含 3 个 html）拖进去，点 Commit

### 第 2 步：在 Railway 创建项目并连接 GitHub

1. 注册 Railway（railway.app），可用 GitHub 账号直接登录
2. 点 **New Project → Deploy from GitHub repo**
3. 选择刚才的仓库 `baozi-tracking`，Railway 会自动开始部署

### 第 3 步：添加 PostgreSQL 数据库（永久保存记录）

1. 在项目页面点 **New → Database → Add PostgreSQL**
2. Railway 会自动生成一个 PostgreSQL 数据库，并自动把连接地址注入环境变量 **`DATABASE_URL`**
3. 回到 Service 页面，点 **Deployments → Redeploy**（重新部署一次，让程序读取数据库）

> 代码里已经写好：检测到 `DATABASE_URL` 就自动使用 PostgreSQL；没有就用 SQLite（仅本地测试）。

### 第 4 步：打开公网链接

1. 部署完成后，在 Service 的 **Settings → Networking** 里会有一个自动生成的公网域名（如 `https://xxx.up.railway.app`）
2. 手机上直接打开这个链接即可使用，客户在外面也能访问，**不需要连接店里 WiFi**

## 三、使用流程

1. 商家/外卖员打开公网链接 → 进「ပို့ဆောင်သူ（外卖员端）」→ 点「စမ်းသပ်အမှာစာ ဖန်တီးရန်（生成订单）」得到订单号
2. 外卖员输入姓名、手机号、订单号 → 点「အမှာစာ ချိတ်ဆက်၍ ပို့ဆောင်စမည်（绑定并开始配送）」
3. 外卖员点「GPS ဖွင့်၍ တင်မည်（开启 GPS 上报）」开始上报；没 GPS 可点「ပို့ဆောင်မှု သရုပ်ပြ（模拟配送）」看效果
4. 客户/商家打开公网链接 → 进「ဖောက်သည် / ဆိုင် ရှာဖွေရေး（查询端）」→ 输入订单号 + 外卖员手机号 → 点「တည်နေရာ ရှာမည်（查询位置）」，即可在地图上实时看到位置和轨迹

## 四、重要说明

### 数据永久保存
- 记录存在 Railway 的 PostgreSQL 里，**部署重启、更新代码都不会丢**。
- 注意：如果删除数据库或重置项目，数据才会清空。数据库连接字符串 `DATABASE_URL` 可以在 Railway 数据库的 Variables 里查看/备份。

### 费用
- Railway 目前没有免费套餐，最低 **Hobby 计划 $5/月起**（含 1 个服务 + 一定用量；PostgreSQL 数据库在额度内）。
- 如果想免费：可用 **Render**（免费 Web Service + 免费 PostgreSQL 试用）或 **Supabase**（免费 PostgreSQL），把数据库连接地址填到环境变量 `DATABASE_URL` 即可，代码不用改。

### 定位权限
- 外卖员手机浏览器需允许定位权限（用 Chrome / Safari 等系统浏览器打开，微信内置浏览器可能受限）。
- 地图使用 OpenStreetMap 免费瓦片，无需申请 Key。

## 五、本地测试（可选，不上传也能先跑）

```bash
pip install -r requirements.txt
python app.py
```

然后浏览器访问 `http://127.0.0.1:5000`。本地没有 `DATABASE_URL`，会自动用 SQLite，仅供测试。

## 六、项目结构

```
baozitracking_railway/
├── app.py              # 后端主程序（Flask + PostgreSQL/SQLite 自动切换）
├── requirements.txt    # 依赖：flask / gunicorn / psycopg2-binary
├── Procfile            # Railway 启动配置（gunicorn）
├── README.md           # 本说明
└── templates/
    ├── index.html      # 首页（缅文）
    ├── rider.html      # 外卖员端（缅文）
    └── track.html      # 客户/商家查询端（缅文）
```

## 七、常见问题

- **部署后页面打不开**：确认已添加 PostgreSQL 并 Redeploy 过一次；看 Deployments 日志有没有报错。
- **查询提示“订单号不存在”**：订单要先在「外卖员端」页面创建/绑定，且手机号必须与绑定时一致。
- **地图加载慢/白屏**：OpenStreetMap 瓦片偶发较慢，可换成高德瓦片（需在高德开放平台申请 Key）。
- **想做成微信小程序 / 安卓 APP**：后端接口原样可用，告诉我，我帮你改造成对应前端。
*（内容由AI生成，仅供参考）*
