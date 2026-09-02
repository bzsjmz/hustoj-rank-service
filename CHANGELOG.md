# 更新日志

## [1.6.0] - 2026-09-02

### 新增

- 增加 SQLite 持久化学生名册 `student_roster`。名册从历史快照恢复后只增不减；新抓到的规范学号会自动加入。
- 增加 `scripts/migrate_roster.py`，用于已有数据库的一次性名册回填。
- 每次采集日志和 `latest.json` 输出历史名册人数、当前 ranklist 人数、缺失人数和覆盖率。
- 增加独立的 `oj-rank-render.service`，通过 Unix socket 为 AstrBot 提供按需图片渲染。
- 增加页面内容哈希缓存和并发 singleflight；相同页面的并发请求只触发一次渲染。

### 优化

- 采集服务不再同步渲染图片，避免渲染阻塞 WebVPN 采集。
- 常用榜单默认只预渲染前 5 页；未预渲染页面在收到请求时生成。
- 周榜、月榜改为仅在收到请求时生成图片。
- 页面内容没有变化时复用现有 PNG，不重复截图。
- 为采集器和渲染器增加 CPU、内存、任务数及 I/O 调度约束；启用 sysstat 资源采样。

### AstrBot 插件

- `/榜单`、两院榜单、班级榜单、专业榜单、周榜和月榜均支持按需页渲染。
- 图片快照号与榜单 JSON 不一致时自动重新请求渲染，避免发送陈旧图片。
- 保留文字榜单回退；渲染服务不可用时不会影响数据查询。
- 插件升级至 v1.6.0，新增 `render_timeout_seconds` 配置。

### 部署注意

- AstrBot 容器需要只读挂载采集器的 `share/` 和 `run/` 目录：`/oj-rank-share`、`/oj-rank-render`。
- 已有数据库升级时先停止采集服务，再以低 I/O 优先级执行一次 `python -m scripts.migrate_roster`。
- 不要将 `.env`、WebVPN Profile、SQLite 数据库、真实榜单导出或日志提交到仓库。
