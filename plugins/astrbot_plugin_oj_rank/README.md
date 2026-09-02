# AstrBot OJ 榜单查询插件

这是 HUSTOJ Rank Service 的可选只读前端。插件只读取 `/oj-rank-share` 中原子发布的 JSON 和图片，不接触 WebVPN、Cookie、浏览器 Profile 或 SQLite。

主要命令：`/榜单`、`/查榜`、`/班级榜单`、`/专业榜单`、`/最卷班级`、`/最卷专业`、`/OJ使用`、`/变化`、`/周榜`、`/月榜`、`/榜单状态`、`/统计数据` 和 `/帮助`。

学号总长度、专业段和班级段由采集器写入 `academic-labels.json`，插件会自动读取，因此不要求固定 12 位学号。`/查榜` 的年级范围由采集器的 `OJ_LOOKUP_PREFIXES` 决定。

两院榜单命令是参考部署的可选扩展；不需要时可以从 `main.py` 和 `rank_data.py` 的允许命令集合中移除。

`/OJ使用` 是参考部署的新生登录教程，通过 QQ 合并转发发送文字与静态截图；其他学校部署时应替换其中的 WebVPN 地址、初始密码规则和 `assets/oj_guide` 图片。

图片按页缓存：常用榜单只预生成前 5 页，其余页面以及周榜、月榜在收到请求时生成；相同页面的并发请求只触发一次渲染。建议将共享目录和渲染 socket 目录只读挂载：

```yaml
volumes:
  - /path/to/hustoj-rank-service/share:/oj-rank-share:ro
  - /path/to/hustoj-rank-service/run:/oj-rank-render:ro
```

首次联调时白名单为空，所有会话均可查询。正式使用前可在 AstrBot 插件配置中设置 `allowed_users` 或 `allowed_groups`。
