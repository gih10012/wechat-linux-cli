# 本机辅助服务

辅助服务把需要 `CAP_SYS_PTRACE` 的客户端操作集中到本机 Unix socket。安装时使用一次 sudo，日常通过普通用户的 `wechat-linux` 命令调用。它是 systemd **系统服务**，以桌面用户的 UID/GID 运行；不是 root 常驻服务，也不是 `systemctl --user` 服务。

当前安装器通过离线测试，systemd unit 已用本机解释器替代尚未安装的路径完成语法校验；独立安装包已从仓库外实际读取账号，无特权临时服务的健康检查、权限拒绝和退出清理通过。本文的特权部署命令尚未在真实桌面完成安装验收，发送后的客户端本地消息显示仍在修复。

## 环境与资源

- Linux、运行中的 systemd、系统 `/usr/bin/python3` 3.11+，以及该解释器的 `venv`、`ensurepip`。
- Python 运行依赖只有 `pycryptodome>=3.20,<4`，在普通用户阶段预先下载 wheel。
- 本地压缩消息读取使用系统 `libzstd`；原生调用使用 `/usr/bin/gdb` 和 `/usr/bin/gcc`，并要求受支持的微信构建。
- 空闲时常驻一个 Python 服务进程，不轮询微信消息。原生任务串行执行，任务期间会有后端、GDB 和编译子进程；尚未测量稳定的内存、CPU、磁盘占用，不据此承诺资源上限。

服务只保留 `CAP_SYS_PTRACE`，代码和虚拟环境归 root 持有，普通用户不能修改 `/opt` 中的执行代码。控制目录权限 `0700`，socket 权限 `0600`，客户端与服务双向核对连接者 UID。此设计仍依赖桌面账号本身可信。

## 普通用户准备并检查安装计划

在仓库根目录执行。源码构建、依赖下载都不使用 sudo；`dist/` 应只保留一个当前项目 wheel，`wheelhouse/` 应只放一个符合版本约束、适配当前主机的 pycryptodome wheel。

```bash
python3 -m venv .venv
.venv/bin/python -m pip wheel --no-deps --wheel-dir dist .
.venv/bin/python -m pip download --only-binary=:all: --no-deps \
  --dest wheelhouse 'pycryptodome>=3.20,<4'
/usr/bin/python3 -I src/wechat_linux_cli/install.py \
  --source "$PWD" --wheelhouse "$PWD/wheelhouse" --user "$(id -un)"
```

默认只输出 JSON 安装计划，不创建系统目录、不启动服务。计划包含目标 UID、安装路径、wheel 的 SHA-256、完整 unit 和执行步骤。也可以把 `--source` 指向具体项目 wheel 文件。安装器拒绝包内路径穿越、符号链接、`.pth` 钩子、其他项目及 URL 依赖；pip 安装阶段还会核对 wheel 与本机解释器的兼容性。

确认本地安装器源码、wheel 和计划后，才执行一次系统安装：

```bash
sudo /usr/bin/python3 -I "$PWD/src/wechat_linux_cli/install.py" \
  --source "$PWD" --wheelhouse "$PWD/wheelhouse" \
  --user "$(id -un)" --apply
```

安装器把已经检查的 wheel 字节复制到 root 所有的目录，创建复制解释器的虚拟环境，以 `--no-index --no-deps` 离线安装，执行 `pip check`，再以桌面用户运行 CLI/服务的 `--help` 并校验 unit。最后才安装命令、重新加载 systemd 并执行 `enable --now`，同时启用开机启动。

这是首次安装入口：已存在的代码目录、命令或目标 unit 都不会被覆盖。激活前出错会清理本次创建的安装文件；开始激活后若出错则保留文件供检查，因为服务可能已经启动。不要据安装命令报错直接删除运行中的代码或调试器状态。

## 日常检查与启停

```bash
wechat-linux service-status
wechat-linux inspect-pending
systemctl status "wechat-linux-cli@$(id -u).service"
journalctl -u "wechat-linux-cli@$(id -u).service" -n 50 --no-pager
```

首次读取缺少密钥时，服务提供 `wechat-linux capture-keys --account me --seconds 15`。该命令通过受限服务只读扫描当前用户的微信进程内存，验证候选密钥后写入私有文件，响应不输出密钥；扫描时间可设为 1–45 秒。接口已通过离线测试，尚未完成部署后的真实采集验收。已有密钥时可继续直接读取本地数据库。

日志访问权限由主机的 journald 配置决定。普通业务命令不需要 sudo；人工管理系统服务通常仍需 sudo：

```bash
sudo systemctl start "wechat-linux-cli@$(id -u).service"
sudo systemctl stop "wechat-linux-cli@$(id -u).service"
sudo systemctl disable "wechat-linux-cli@$(id -u).service"
```

`disable` 只取消开机启动。服务停机需要等待正在执行的原生调用安全结束；unit 设置 `KillMode=process`、`SendSIGKILL=no`、`TimeoutStopSec=infinity`，不会用超时强杀来结束 GDB。遇到 pending 或停止长期未完成时，保留进程和状态，检查既有请求；不要自动重发、清空状态或强杀客户端/GDB。当前也不配置自动重启。

## 代码、数据与保留内容

| 路径 | 所有者与用途 |
| --- | --- |
| `/opt/wechat-linux-cli/` | root；复制的虚拟环境、受限的 wheel 快照及 `installation.json` 安装清单 |
| `/usr/local/bin/wechat-linux` | root；以隔离 Python 模式执行安装包的命令入口 |
| `/etc/systemd/system/wechat-linux-cli@<UID>.service` | root；指定桌面用户的系统服务 |
| `/run/wechat-linux-cli-<UID>/` | 桌面用户，`0700`；控制 socket 和进程锁，保留至重启或明确清理 |
| `~/.local/state/wechat-linux-cli/service/` | 桌面用户；未完成任务记录及后端结果 |
| `~/.local/state/wechat-linux-cli/native-send/` | 桌面用户；原生任务状态、调试日志和任务构建产物 |
| `~/.local/state/wechat-personal/native-keys/` | 桌面用户；现有本地读取密钥，文件须为 `0600` |

已有 `~/.local/state/ncut-wechat-skills/native-send-trial/` 时，原生任务继续使用该位置，以保留既有 request ID 和重试判断。状态、结果及调试文件可能包含账号信息或消息内容，不进入仓库。服务重装或升级前应保留这些私有状态；当前安装器没有自动升级、卸载或迁移入口。

unit 显式设置桌面用户的 `HOME` 和受限 `PATH`，不继承普通终端的自定义状态目录环境变量。需要调整目录时应单独审阅服务配置，再核对相应目录所有权和权限。
