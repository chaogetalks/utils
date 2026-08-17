# chaogetalks-utils

[English](README.md)

用爱开源的实用小工具集合。

## 工具索引

| 工具 | 简介 |
| --- | --- |
| [Codex Session Guard](codex/README.zh-CN.md) | 面向本地 Codex Desktop 会话的大小审计工具；不会修改会话数据。 |

## 快速开始

```bash
./codex/codex_session_guard.py --no-log
```

使用说明与参数详见 [Codex Session Guard 中文指南](codex/README.zh-CN.md)。

工具不会修改会话数据，但默认运行会写入本地报告日志；使用 `--no-log` 可避免写入日志。

本项目按 [MIT License](LICENSE) 发布；工具按现状提供，请自行判断是否适合你的环境，并自行承担使用风险。

由 [@chaogetalks](https://x.com/chaogetalks) 用爱制作与维护。
