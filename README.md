# 板块形态监测

这是同花顺板块与沪深大市值个股筛选器的网页版。服务保留命令行板块 CSV 输出，并增加登录、历史结果、手动执行和每天北京时间 18:00 自动检查。

网页提供四个独立入口：板块等距下跌、板块 MACD 底背离、个股等距下跌和个股 MACD 底背离。个股范围为沪深 A 股中总市值严格大于 300 亿元的股票，技术分析统一使用前复权行情。

## Docker Compose 部署

1. 复制环境变量示例并修改管理员密码与会话密钥：

   ```bash
   cp .env.example .env
   ```

2. 启动服务：

   ```bash
   docker compose up -d --build
   ```

3. 在服务器本机通过 `http://127.0.0.1:8000` 检查服务，再由 Nginx 或 Caddy 对外提供访问。健康检查地址为 `/health`。

   如果暂时不配置反向代理、需要直接使用公网 IP 访问，请在 `.env` 中设置：

   ```bash
   APP_BIND_HOST=0.0.0.0
   ```

   然后重新创建容器：

   ```bash
   docker compose up -d --build
   ```

   同时在腾讯云控制台的安全组入站规则中放行 TCP `8000`（来源建议仅填写你的办公 IP）。访问地址为 `http://服务器公网IP:8000`。

生产环境应通过 Nginx、Caddy 或云平台反向代理提供 HTTPS，并将 `.env` 中的 `COOKIE_SECURE` 改为 `true`。Compose 仅把端口绑定到服务器回环地址，并固定使用一个 Uvicorn worker，避免外部绕过代理或创建重复的 18:00 调度任务。
建议同时在反向代理上对 `/login` 配置请求频率限制；应用内限流作为补充保护，连续 5 次失败后同一客户端 5 分钟内不会继续验证密码。
不要把 Uvicorn 的受信代理设置为通配地址。只有在确认实际代理地址或网段，并确保代理覆盖客户端传入的 `X-Forwarded-For` 后，才配置 `FORWARDED_ALLOW_IPS`；否则保持默认值更安全。

SQLite 数据库和最新 CSV 保存在名为 `screening-data` 的 Docker 命名卷中。Docker 会按部署目录添加前缀，可用以下命令查找实际卷名：

```bash
docker volume ls | grep screening-data
```

## 本地运行

```powershell
python -m pip install -r requirements-dev.txt
$env:APP_USERNAME = "admin"
$env:APP_PASSWORD = "请替换为强密码"
$env:SESSION_SECRET = "请替换为至少32位的随机字符串"
uvicorn board_screening.web:create_default_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

命令行默认仍执行等距下跌：

```powershell
python board_pattern_screener.py
```

执行日线、周线、月线 MACD 底背离筛选：

```powershell
python board_pattern_screener.py --strategy macd-divergence
```

底背离首次运行会回填约 12 年日线并写入 SQLite 行情缓存，耗时明显长于后续增量运行。周线和月线仅使用交易日历确认已经结束的完整周期。

最小上涨幅度通过环境变量 `MIN_WAVE_RISE_PERCENT` 配置，数值按百分数填写，默认值为 `10`。Docker Compose 可直接修改 `.env`；本地命令行可在运行前执行 `$env:MIN_WAVE_RISE_PERCENT = "10"`。最近局部低点对应的上涨幅度不足时，筛选器会继续向左寻找更大级别的局部低点；所有候选波段均不达标时才过滤该板块。

## 自动任务

- 每天北京时间 18:00 检查新浪交易日历。
- 板块与个股的两类策略按顺序串行执行，并分别保留历史和最新 CSV。
- 最新交易日已有成功结果时不会重复入库。
- 容器错过执行时间后，会在下次启动时补跑最近遗漏的已收盘交易日。
- 同花顺核心行情失败会将任务标记为失败；ETF 数据失败时保留筛选结果并显示“数据暂缺”。
- “首次跌破目标”按收盘价低于目标价确认，最大跌幅再使用确认日至今的日内最低价计算。
