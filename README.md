# wechat-linux-cli

面向账号持有者本人 Linux 微信客户端的独立命令行。确定性的客户端操作放在这里；账号选择、业务探索与自然语言工作流由上层 skill 负责。本项目从 [ncut-wechat-skills](https://github.com/gih10012/ncut-wechat-skills) 抽出本地读取及原生发送后端，保留 MIT 版权声明。

当前可用：读取本机已同步的会话与消息，JSON 输出，不标记已读。Linux 微信 4.1.13 上的数据库读取已验收。图片、语音、视频和表情仅返回类型；读取结果不证明远端同步完整或客户端登录有效。

当前为开发预览。原生发送与一次安装的特权辅助服务正在迁移，发送后的客户端本地消息显示正在修复。

| 能力 | 验证范围 |
| --- | --- |
| 本地会话与消息读取 | 已从独立 wheel 安装，仓库外隔离运行并读取真实账号 |
| 本机 Unix 控制服务 | 临时无特权进程的健康检查、权限拒绝及退出清理通过 |
| 一次 sudo 安装、特权自启动服务 | 安装器与 unit 检查通过；真实系统部署待验收 |
| 原生文字发送 | 来源工程的文件传输助手手机收件及防重已实测；本包服务发送待验收 |
| 发送后的 Linux 本地显示 | 尚未接入完整客户端入库流程 |

## 本地安装与使用

需要 Linux、Python 3.11+。压缩消息需要系统 `libzstd`。已有私有密钥时，读取不需要 sudo，也不需要微信进程保持在线。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/wechat-linux status
.venv/bin/wechat-linux conversations --query '联系人或群名' --limit 5
.venv/bin/wechat-linux messages --chat '上一步返回的准确 chat_id' --limit 20
```

`messages` 也支持唯一完整名称、`--before`（不含该 Unix 时间）、`--since`（包含该 Unix 时间）和 `--max-chars`。每次最多返回 50 条，默认按时间从新到旧排列。命令结果写到 stdout，成功退出 0，失败退出 1；`--help` 和 `--version` 使用普通文本。

默认复用 `~/.local/state/wechat-personal/native-keys/<account>.json`，不迁移或复制已有账号状态。使用 `--account` 选择本机别名；需要自定义状态位置时，设置 `WECHAT_LINUX_STATE_DIR`，密钥位于该目录的 `native-keys/` 下。密钥文件必须由当前用户持有，权限为 `0600`。

仓库不含账号数据、密钥或聊天记录。没有密钥时读取会明确返回 `NATIVE_KEYS_REQUIRED`。辅助服务安装器和 `capture-keys` 入口已实现并通过离线测试，尚未进行实际部署和采集验收；普通用户构建、安装计划、一次 sudo 部署及数据位置见[辅助服务说明](docs/service.md)。

## 开发验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖数据库页认证、WAL 已提交帧快照、密钥候选验证和读取筛选；离线测试不代表真实收件、客户端入库或服务验收。
