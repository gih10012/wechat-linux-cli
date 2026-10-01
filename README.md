# wechat-linux-cli

面向账号持有者本人 Linux 微信客户端的独立命令行。确定性的客户端操作放在这里；账号选择、业务探索与自然语言工作流由上层 skill 负责。本项目从 [ncut-wechat-skills](https://github.com/gih10012/ncut-wechat-skills) 抽出本地读取及原生发送后端，保留 MIT 版权声明。

当前可用：读取本机已同步的会话与消息，JSON 输出，不标记已读。Linux 微信 4.1.13 上的数据库读取已验收。图片、语音、视频和表情仅返回类型；读取结果不证明远端同步完整或客户端登录有效。

当前为开发预览。2026-09-30 的真实排队发送已验证文件传输助手文字：手机只收到一条、中文/换行/emoji 完整，Linux 聊天窗口显示正常，本地数据库独立读回一条。同请求 ID 只读重放，未再次提交。系统服务已真实部署并启用自启动，普通 CLI 文件传输助手发送及相同 ID 防重通过；个人身份发给 ClawBot 的文字由 iLink 实际收到，机器人回执在 Linux 历史独立读回。

发送使用客户端自身的任务调度、消息创建及入库流程，没有直接补写数据库。服务先执行不发送的排队构造预检，匹配同一客户端 PID/启动时间后才提交文字、图片或文件；版本、调度器及活动协程引用检查失败会拒绝。早期同步预检曾导致客户端崩溃，该同步 C 入口保持禁用；独立实验模块的新请求默认拒绝，服务只显式启用已验收的排队入口。已有请求仍可只读查询。原生操作短暂附加调试器，仍有版本依赖及客户端异常退出风险。

| 能力 | 验证范围 |
| --- | --- |
| 本地会话与消息读取 | 已从独立 wheel 安装，仓库外隔离运行并读取真实账号 |
| 本机 Unix 控制服务 | 临时无特权进程的健康检查、权限拒绝及退出清理通过 |
| 一次 sudo 安装、特权自启动服务 | 真实部署、自启动 enabled、桌面 UID/CAP_SYS_PTRACE 及 root 代码所有权均已核对 |
| 原生文字发送 | 普通 CLI filehelper 手机单次收件、Linux 显示与防重；个人微信→ClawBot→本人文字往返已实测 |
| 原生图片发送 | 2026-10-01 普通 CLI 的 PNG/JPEG 发给 ClawBot，iLink 各单次收到、下载字节一致、Linux UI 显示；PNG 同 ID 重放未再次提交 |
| 原生文件发送 | 2026-10-01 普通 CLI 中文文件名 TXT、ZIP→ClawBot 各单次入站，文件名/下载字节一致、ZIP完整性与Linux UI通过；同ID重放未重新提交 |
| 发送后的 Linux 本地显示 | 排队发送经真实客户端入库，本地读回一条，Linux 窗口显示获本人确认 |

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

仓库不含账号数据、密钥或聊天记录。没有密钥时读取会明确返回 `NATIVE_KEYS_REQUIRED`。辅助服务安装器和 `capture-keys` 入口已实现并通过离线测试，系统部署已通过，首次新账号密钥采集待独立验收；普通用户构建、安装计划、一次 sudo 部署及数据位置见[辅助服务说明](docs/service.md)。

安装辅助服务后，普通用户通过 `wechat-linux send-text --recipient filehelper --text '消息文字' --request-id '本次唯一ID'` 请求发送，状态用 `wechat-linux send-status --request-id '原ID'` 只读查询。收件人接受任意精确原生会话 ID，包括私聊与群聊；文字为 1–1024 UTF-8 字节。CLI 不硬编码收件人授权白名单，调用 skill/agent 按用户当前任务或事先直接/间接授权执行写入，已有授权不重复询问。实际收发验收目前覆盖 filehelper 与 ClawBot，其他目标及格式的运行结果各自记录。每次操作固定一个 ID，相同正文及对象返回旧结果，冲突拒绝；未知结果不换 ID 重发。`ok` 证明客户端调用成功，`local_history_integrated` 单独记录数据库读回，手机投递与 UI 显示另有独立确认，不能互相推断。

## 开发验证

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试覆盖数据库页认证、WAL 已提交帧快照、密钥候选验证和读取筛选；离线测试不代表真实收件、客户端入库或服务验收。

图片使用 `wechat-linux send-image --recipient filehelper --file /绝对路径/image.png --request-id 本次唯一ID`，支持 PNG/JPEG、单个文件最多 10 MiB。服务先保存私有快照，构造预检与发送绑定同一目标、客户端及图片哈希。同 ID/目标/图片字节返回旧结果，改文件名不会重发，内容或格式动作冲突会拒绝。图片数据库类型读回仅提供候选记录，内容、UI 和收件仍需独立验收。原生表情包、公众号卡片和媒体 OneBot 动作继续开发，不能把普通图片当作这些格式已完成。

文件使用 `wechat-linux send-file --recipient 精确chat_id --file /路径/文件.zip --request-id 本次唯一ID`，支持常规文件 1 字节到 10 MiB，保留最多 255 UTF-8 字节的原文件名。预检、发送与防重绑定目标、文件名和字节哈希；改名会冲突。快照目录保留 700，文件 600，客户端可在本地插入返回后继续上传。默认同名数据库记录只是候选，`ok` 不证明上传或投递完成；未知状态保留原 ID。
