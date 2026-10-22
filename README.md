# Codex Quota Watch

macOS 本地 Codex 额度监控：只读查询、10% / 5% 提醒、额度恢复二次确认、历史趋势估算。读取采用 Codex app-server，不发起模型请求。

## 运行

Python 3.9+、已登录的 Codex CLI、macOS。

```sh
CODEX_CLI=/absolute/path/to/codex python3 monitor.py --once
CODEX_CLI=/absolute/path/to/codex python3 monitor.py
python3 -m unittest discover -s tests -v
```

默认从 PATH 查找 codex，也可通过 CODEX_CLI 指定。状态、日志和历史保存在 runtime/（可用 QUOTA_WATCH_ROOT 指定其他目录）。额度恢复确认后退出；新一轮监控使用新的运行目录。--once 仍可能在达到阈值时弹出本机提醒。

## 限制

0.1.0 预发布，提醒通过 macOS osascript / afplay；未支持原生 Windows。当前按 primary 字段读取长窗口，若接口窗口小于一天会明确拒绝，未自动选择其他窗口。API/客户端版本变化可能需要适配。估算假设近期消耗速度延续，不是保证或统计置信区间。不会自动暂停其他聊天或兑换额度。未安装后台定时服务。

首次发布验证包含阈值、去重和趋势逻辑；实时账号接口兼容性仍需用户环境验证。

接口文档：https://learn.chatgpt.com/docs/app-server
