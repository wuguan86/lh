# 板块等距下跌监测

这是现有同花顺板块筛选器的网页版。服务保留命令行 CSV 输出，并增加登录、历史结果、手动执行和每天北京时间 18:00 自动检查。

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

命令行兼容入口保持不变：

```powershell
python board_pattern_screener.py
```

## 自动任务

- 每天北京时间 18:00 检查新浪交易日历。
- 最新交易日已有成功结果时不会重复入库。
- 容器错过执行时间后，会在下次启动时补跑最近遗漏的已收盘交易日。
- 同花顺核心行情失败会将任务标记为失败；ETF 或市值龙头数据失败时保留筛选结果并显示“数据暂缺”。
- “首次跌破目标”按收盘价低于目标价确认，最大跌幅再使用确认日至今的日内最低价计算。
