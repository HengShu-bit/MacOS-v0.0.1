# macOS 微信多开器

这是一个纯 Python 工具。它不会修改 `/Applications` 里的官方微信，而是在当前用户的：

```text
~/Applications/微信多开/
```

创建一个具有独立 Bundle ID 的微信副本，再使用 macOS 自带的临时签名启动。默认副本不会注册 `weixin://` 链接，避免抢占官方微信。

## 最简单的用法

双击：

```text
一键双开微信.command
```

首次运行会先显示账号风险提示；输入 `yes` 后创建 `微信 2.app`。之后会直接复用并启动“官方微信 + 微信 2”。

也可以在终端执行：

```bash
/usr/bin/python3 wechat_multi.py doctor
/usr/bin/python3 wechat_multi.py start --extra 1
```

脚本启动副本后会同时确认两件事：本次副本的主可执行文件确实有进程在运行，以及 `~/Library/Containers/<副本 Bundle ID>` 独立容器存在。旧的空容器目录不会单独被当成启动成功。

需要三开时：

```bash
/usr/bin/python3 wechat_multi.py start --extra 2
```

支持最多 5 个额外实例。

## 微信升级后

官方微信更新后，先从菜单中完全退出所有微信副本，再用当前版本重建：

```bash
/usr/bin/python3 wechat_multi.py create --extra 1 --replace --launch
```

`--replace` 会先检查旧副本的主进程和 Helper 是否已经退出；仍有进程时会拒绝更新，避免新旧版本同时访问聊天数据库。旧 App 会在同一目录中改名为带时间戳的 `.app.backup-...`（不再是可直接双击的 App），而不是直接删除。Bundle ID 保持不变，因此副本账号数据不会被主动删除。
如果 `start` 检测到副本来自旧 Build，它会先停止并提示重建，不会静默启动过期副本。

## 其他命令

```bash
# 只查看计划，不写文件
/usr/bin/python3 wechat_multi.py create --extra 1 --dry-run

# 查看已有副本
/usr/bin/python3 wechat_multi.py list

# 只启动已有副本
/usr/bin/python3 wechat_multi.py launch --extra 1

# 自动化或非交互环境中，确认风险后显式放行
/usr/bin/python3 wechat_multi.py start --extra 1 --accept-risk
```

## 本机验证

已在 macOS 26.4、微信 4.1.11（Build 269136）、系统 Python 3.9.6 上验证：官方微信与副本可同时运行，副本生成独立容器，重签名副本通过 `codesign --verify --deep --strict`。项目自带的 22 项单元测试也全部通过。

## 重要说明

- 首次创建约需复制 658 MB 的微信程序。本工具优先使用 APFS 写时复制，通常很快且初始占用较小。
- 副本使用 ad-hoc 临时签名，不再是腾讯原始签名；macOS 可能要求你在“隐私与安全性”中重新授予通知、麦克风、摄像头或文件访问权限。
- 重签名会移除腾讯 Team ID、App Group 和原沙盒权限；副本的系统安全边界不等同于官方 App。不要用关键账号或在副本中处理高敏感信息。
- 创建前会核对源 App 的腾讯 Team ID 和代码完整性；复用或单独启动副本前也会重新校验副本签名。
- 工具不注入插件、不改聊天数据库、不读取账号密码，也不关闭系统完整性保护（SIP）。
- **账号风险：** 微信个人账号使用规范把“微信多开插件、外挂、软件或系统”列为常见违规类型，可能导致警告、功能限制或封号。参见[微信个人账号使用规范](https://weixin.qq.com/agreement/personal_account)。
- 更低风险的方式是使用另一个 macOS 用户会话、另一台设备或虚拟机运行未修改的官方微信。
- 微信或 macOS 更新后可能需要重建副本，无法保证未来版本继续可用。
- 请先用非关键账号测试，并自行备份重要聊天记录。
