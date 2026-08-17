# Codex Session Guard

[English](README.md) · [返回项目首页](../README.zh-CN.md)

Codex Session Guard 是一个针对本地 Codex 会话数据保持只读的 Codex Desktop 会话大小审计工具（默认仍会写入本地报告日志）。它会扫描 Codex 数据目录中的会话 JSONL 文件，并在可用时结合 `state_5.sqlite` 中的任务元数据，帮助你：

- 按任务汇总会话大小，并在元数据可用时把父任务与子 Agent 的大小合并计算；
- 查看单个会话文件的大小（`--view files`）；
- 按大小阈值标记 SAFE、WATCH、WARNING 或 CRITICAL 风险。

工具不会读取对话正文，也不会修改 Codex 会话数据。只有在能够以独占且不修改数据的方式读取 `state_5.sqlite` 时，才会加载完整任务元数据；如果其他进程占用数据库，工具不会等待，而是立即退回到文件名、大小和修改时间等文件属性。降级模式下，标题和工作区不可用，父任务/子 Agent 关系无法重建，任务汇总仅限从文件名解析出的标识符。会话候选会按规范化后的物理文件身份去重：路径别名、符号链接和硬链接指向同一文件时，文件数和总大小只计一次。默认情况下会把报告追加写入日志，因此不能笼统地说程序完全不写文件。

## 要求

- Python 3.11 或更高版本；
- [`uv`](https://docs.astral.sh/uv/)；
- macOS/Linux 风格的直接可执行调用：脚本使用 `uv` shebang，可使用 `./codex/codex_session_guard.py`。这只描述该调用形式，不构成其他平台支持声明。

从仓库根目录运行时，可使用以下两种调用形式：

```bash
# macOS/Linux 风格的直接 shebang 调用
./codex/codex_session_guard.py --no-log

# 显式调用 uv
uv run codex/codex_session_guard.py --no-log
```

`--no-log` 是隐私优先的快速开始方式；它可防止正常扫描创建或追加报告日志。省略它会启用默认日志，并将纯文本报告追加到脚本旁的 `codex/codex_session_guard.log`。任何位于 Codex 数据目录内或属于硬链接的自定义日志目标都会在写入前被拒绝，因此数据库和会话文件保持不变。

## 常用命令

以下命令均从仓库根目录运行。JSON 输出可能包含本地路径等信息，因此示例使用 `--no-log`：

```bash
# 查看单个 JSONL 会话文件
uv run codex/codex_session_guard.py --view files --no-log

# 输出机器可读 JSON（不追加日志）
uv run codex/codex_session_guard.py --json --no-log

# 自定义 WATCH、WARNING、CRITICAL 阈值（十进制 GB）
uv run codex/codex_session_guard.py --thresholds 5,10,15 --no-log

# 最多显示 10 行
uv run codex/codex_session_guard.py --top 10 --no-log

# 只显示至少 1.5 十进制 GB 的行
uv run codex/codex_session_guard.py --min-size 1.5 --no-log

# 仅扫描活动任务，不含已归档任务
uv run codex/codex_session_guard.py --active-only --no-log

# 指定 Codex 数据目录
uv run codex/codex_session_guard.py --codex-home /path/to/.codex --no-log

# 查看完整帮助（包括 --log-file、--color、--version 等选项）
uv run codex/codex_session_guard.py --help
```

## 参数速查

| 参数 | 作用 |
| --- | --- |
| `--view tasks\|files` | 按任务聚合，或查看单个 JSONL 文件 |
| `--json` | 输出机器可读 JSON |
| `--thresholds A,B,C` | 设置三个递增的十进制 GB 阈值 |
| `--top N` / `-n N` | 最多显示 N 行 |
| `--min-size GB` | 按十进制 GB 过滤小于指定大小的行 |
| `--active-only` | 排除已归档任务（默认包含归档任务） |
| `--codex-home PATH` | 指定 Codex 数据目录 |
| `--log` / `--no-log` | 开启或关闭追加日志 |
| `--help` | 显示 CLI 帮助 |

## 默认行为与退出码

默认视图是按任务聚合（`--view tasks`），并包含已归档任务；最多显示 20 行（`--top 20`）。阈值默认为 10/15/20 十进制 GB。Codex 数据目录优先取 `$CODEX_HOME`，否则取 `~/.codex`。默认日志路径是脚本旁的 `codex/codex_session_guard.log`，以追加方式写入；可用 `--log-file` 指定 Codex 数据目录之外的非硬链接路径，或用 `--no-log` 关闭日志。

程序会先在所选视图的全部行上计算最大条目风险，再应用 `--top` 和 `--min-size` 显示过滤；因此过滤条件不会降低退出码，甚至可能出现输出没有行但退出码为 10、15 或 20 的情况。对于阈值 `A,B,C`（均为十进制 GB 且严格递增），风险区间为：SAFE `< A`；WATCH `A <= size < B`；WARNING `B <= size < C`；CRITICAL `>= C`。

| 退出码 | 含义 |
| ---: | --- |
| 0 | SAFE：`size < A` |
| 10 | WATCH：`A <= size < B` |
| 15 | WARNING：`B <= size < C` |
| 20 | CRITICAL：`size >= C` |
| 2 | Codex home 无效/缺失，或日志写入失败 |

Typer 可能对命令行解析错误使用其自身的非零退出码；不要假定解析错误一定是上述某个精确数值。

## 隐私提示

报告与日志可能包含任务标题、完整 Codex home 路径、工作区名称或路径、会话路径（JSON 输出使用 `--view files` 时，或扫描错误中）、线程 ID 或其前缀、时间戳以及扫描错误。普通 Rich 文本的 `files` 视图不会显示完整路径。建议日常使用 `--no-log`，分享终端输出或 JSON 前先检查内容，不要盲目转发。项目的 `.gitignore` 会忽略 `*.log`，但生成的文件仍应由你自行审核。

感谢使用与反馈。[@chaogetalks](https://x.com/chaogetalks) 用爱开源；本项目按 [MIT License](../LICENSE) 发布。
