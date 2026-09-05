# HUSTOJ Rank Service

当前版本变更见 [CHANGELOG.md](CHANGELOG.md)。

一个独立部署的 HUSTOJ 排行榜采集、历史快照和统计服务，附带可选的 AstrBot 查询插件。

采集器通过 Playwright persistent Chromium 使用管理员人工建立的 WebVPN 会话。它不会保存或填写账号密码，也不会破解验证码。AstrBot 只读消费 `share/` 中原子发布的 JSON、CSV 和 PNG，不接触 Cookie、浏览器 Profile 或 SQLite。

## 架构

```text
systemd + Xvfb/noVNC
  -> persistent Chromium -> WebVPN -> HUSTOJ ranklist.php
  -> 完整性校验 -> SQLite 历史快照
  -> 原子导出 JSON/CSV
  -> 独立低优先级渲染服务 -> 按页缓存 PNG
  -> 只读 share/ + Unix socket -> AstrBot 插件
```

采集服务可以独立运行；AstrBot 只是一个可选查询前端。其他机器人或网页也可以读取相同的公开导出。

## 主要能力

- 分页完整抓取，空首页、异常字段、冲突重复或达到页数上限时拒绝整轮。
- SQLite 原子提交当前榜与不可变历史快照。
- 首次从全部历史快照恢复只增不减的持久化学生名册；以后每轮只增量更新。
- 总榜、日变化、周榜、月榜、班级榜、专业榜和图片原子发布。
- 班级人数与参与率以历史名册为分母。
- 学号前缀、总长度、专业段和班级段均可配置。
- WebVPN 失效时停在人工认证状态，不循环刷新登录入口。

## 学号结构

学号的字符规则和分段都可以配置：

```dotenv
OJ_PREFIX=2026
STUDENT_ID_LENGTH=12
MAJOR_CODE_LENGTH=4
CLASS_CODE_LENGTH=6
STUDENT_ID_PATTERN=[0-9]+
```

这表示 `前缀(4) + 专业代码(4) + 班级扩展(2) + 学生序号(2)`。其他学校可以使用不同长度，例如：

```dotenv
OJ_PREFIX=26
STUDENT_ID_LENGTH=10
MAJOR_CODE_LENGTH=3
CLASS_CODE_LENGTH=5
```

此时 `2612301001` 会被解析为专业 `26123`、班级 `2612301`。字母数字混合学号可把 `STUDENT_ID_PATTERN` 设置为 `[A-Za-z0-9]+`；为保证导出路径安全，标识符只允许字母、数字、下划线和连字符。`OJ_LOOKUP_PREFIXES` 配置 `/查榜` 可以额外检索的年级前缀，不影响其他榜单。

## 快速开始

环境建议为 Ubuntu/Debian、Python 3.10+。人工登录模式还需要 Xvfb、Fluxbox、x11vnc、noVNC 和 websockify。

```bash
git clone https://github.com/bzsjmz/hustoj-rank-service.git
cd hustoj-rank-service
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PLAYWRIGHT_BROWSERS_PATH="$PWD/data/ms-playwright" .venv/bin/playwright install chromium
cp .env.example .env
```

编辑 `.env`，至少填写 `WEBVPN_ORIGIN`、`OJ_PROXY_BASE` 和学号结构。代理地址必须来自你已获授权的正常 WebVPN 访问。

安装 systemd 单元：

```bash
sudo ./scripts/install_systemd.sh
sudo systemctl start oj-rank.service oj-rank-render.service
```

服务仅把 VNC/noVNC 绑定到回环地址。通过 SSH 隧道访问：

```bash
ssh -N -L 6080:127.0.0.1:6080 user@server
```

然后打开 `http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=scale`，手动完成登录。不要把 5900 或 6080 暴露到公网。

## AstrBot 插件

插件位于 `plugins/astrbot_plugin_oj_rank/`。渲染器只预生成常用榜单前 3 页；其他页以及周榜、月榜由插件通过 Unix socket 请求生成。同一页面的并发请求会合并成一次渲染。

将插件安装到 AstrBot 插件目录，并把 `share/` 与渲染 socket 目录只读挂载：

```yaml
volumes:
  - /path/to/hustoj-rank-service/share:/oj-rank-share:ro
  - /path/to/hustoj-rank-service/run:/oj-rank-render:ro
```

插件支持总榜、查榜、班级/专业榜、变化榜、统计 CSV 等命令。两院榜单是当前参考部署保留的可选扩展，依赖 `COLLEGE_SPLIT_SNAPSHOT_ID`；其他学校可以在 `app/college.py` 和插件命令中改名或移除。

## 专业名称

`app/college.py` 中的 `MAJOR_DISPLAY_NAMES` 是显示层映射。代码长度与 `MAJOR_CODE_LENGTH` 不一致的条目会在导出时自动忽略；未配置的专业仍可用数字代码查询。

## 数据与安全边界

以下内容绝不能提交或公开：

- `data/webvpn-profile/` 和 `webvpn-storage-state.json`；
- `.env`、诊断密钥、Session 状态与事件；
- `rank.db`、真实榜单 JSON/CSV/PNG、统计导出和日志。

仓库的 `.gitignore` 已覆盖这些路径，但发布前仍应人工复核。示例数据全部为虚构数据。请在采集和展示学生信息前确认学校规定、授权范围与适用隐私法律。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
cd plugins/astrbot_plugin_oj_rank
python3 -m unittest discover -s tests -v
```

测试不需要真实 WebVPN 账号或浏览器会话。

## License

MIT，详见 [LICENSE](LICENSE)。许可证只覆盖代码，不赋予任何人访问学校系统或处理学生数据的权利。
