# 局域网部署：用 HTTPS 反向代理保护登录

Gradio 自己只提供明文 HTTP。只要 `LUX_HOST` 不是 `127.0.0.1`，同事访问时的页面内容和登录密码都会在局域网里明文传输。
推荐做法：让工作台**只监听本机**，再由反向代理对外提供 HTTPS。

## 1. 工作台只监听本机

`.env`：

```
LUX_HOST=127.0.0.1
LUX_PORT=7860
```

## 2. 用 Caddy 对外提供 HTTPS（Windows 单文件，最省事）

下载 Caddy 单个 `caddy.exe`，在同目录建 `Caddyfile`（把 `lux.company.local` 换成内网域名，或直接用本机 IP）：

```
lux.company.local {
    tls internal
    reverse_proxy 127.0.0.1:7860
}
```

运行 `caddy run`。`tls internal` 会生成内部根证书；需要把 Caddy 的根证书
（`%AppData%\Caddy\pki\authorities\local\root.crt`）分发并导入同事电脑的“受信任的根证书颁发机构”，浏览器才不会报警。
如果公司已有内部 CA，也可以用 `tls 证书.crt 私钥.key` 指定公司签发的证书。

## 3. 防火墙

只放行 443 端口，不要放行 7860：

```
New-NetFirewallRule -DisplayName "LUX HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 -Action Allow
```

## 4. 验证

- 在另一台电脑访问 `https://lux.company.local`，地址栏应显示锁形图标。
- 访问 `http://服务器IP:7860` 应打不开。
- 启动时控制台不应再出现“监听局域网地址”的警告。

## 说明

- 登录失败锁定：同一账号连续失败 5 次（`LUX_LOGIN_MAX_FAILURES`）锁定 15 分钟（`LUX_LOGIN_LOCK_SECONDS`）。
  锁定按账号名统计，攻击者故意输错也会让该账号暂时无法登录；锁定期满自动解除。
- 经过反向代理后，工作台看到的来源地址是代理本机，因此目前不能按 IP 限速，只能按账号名。
