# wechat-linux-cli

面向账号持有者本人 Linux 微信客户端的独立命令行。确定性的客户端操作放在这里；账号选择、业务探索与自然语言工作流由上层 skill 负责。本项目从 [ncut-wechat-skills](https://github.com/gih10012/ncut-wechat-skills) 抽出本地读取及原生发送后端，保留 MIT 版权声明。

当前可用：读取本机已同步的会话与消息，JSON 输出，不标记已读。Linux 微信 4.1.13 上的数据库读取已验收。图片、语音、视频和表情仅返回类型；读取结果不证明远端同步完整或客户端登录有效。

当前为开发预览。2026-09-30 的真实排队发送已验证文件传输助手文字：手机只收到一条、中文/换行/emoji 完整，Linux 聊天窗口显示正常，本地数据库独立读回一条。同请求 ID 只读重放，未再次提交。系统服务已真实部署并启用自启动，普通 CLI 文件传输助手发送及相同 ID 防重通过；个人身份发给 ClawBot 的文字由 iLink 实际收到，机器人回执在 Linux 历史独立读回。

发送使用客户端自身的任务调度、消息创建及入库流程，没有直接补写数据库。服务先执行不发送的排队构造预检，匹配同一客户端 PID/启动时间后才提交文字、图片、文件、卡片或表情；版本、调度器及活动协程引用检查失败会拒绝。早期同步预检曾导致客户端崩溃，该同步 C 入口保持禁用；独立实验模块的新请求默认拒绝，服务只显式启用已验收的排队入口。已有请求仍可只读查询。原生操作短暂附加调试器，仍有版本依赖及客户端异常退出风险。

| 能力 | 验证范围 |
| --- | --- |
| 本地会话与消息读取 | 已从独立 wheel 安装，仓库外隔离运行并读取真实账号 |
| 朋友圈读取 | 全部已加载动态或指定人，默认20条分页、完整字段及`--all`；正常微信窗口加载更早历史后读回已验收，远端同步范围单独报告 |
| 本机 Unix 控制服务 | 临时无特权进程的健康检查、权限拒绝及退出清理通过 |
| 一次 sudo 安装、特权自启动服务 | 真实部署、自启动 enabled、桌面 UID/CAP_SYS_PTRACE 及 root 代码所有权均已核对 |
| 原生文字发送 | 普通 CLI filehelper 手机单次收件、Linux 显示与防重；个人微信→ClawBot→本人文字往返已实测 |
| 原生图片发送 | 2026-10-01 普通 CLI 的 PNG/JPEG 发给 ClawBot，iLink 各单次收到、下载字节一致、Linux UI 显示；PNG 同 ID 重放未再次提交 |
| 原生文件发送 | 2026-10-01 普通 CLI 中文文件名 TXT、ZIP→ClawBot 各单次入站，文件名/下载字节一致、ZIP完整性与Linux UI通过；同ID重放未重新提交 |
| 原生转发与自定义 XML | 2026-10-01 普通 CLI→filehelper 公众号文章5、小程序33及修改标题/描述的 XML，独立字段/服务器 ID/Linux 卡片显示与防重通过；本人手机单次完整显示及点击打开确认通过 |
| 原生表情发送 | 2026-10-01 普通 CLI→filehelper 动画 GIF，type47 XML的MD5/长度、服务器ID、Linux动画与防重通过；本人确认手机正常。ClawBot手机端不支持自定义表情 |
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

## 朋友圈读取

```bash
wechat-linux moments --limit 20
wechat-linux moments --cursor '上一页的 next_cursor'
wechat-linux moments --user '精确微信用户ID或唯一完整联系人名称' --limit 20
wechat-linux moments --all
wechat-linux moments --user '精确微信用户ID' --all --include-xml
```

默认每页20条，`--limit`范围1..100。续页须保持相同账号和用户筛选；`--all`返回全部已加载条目，带游标时返回其后全部条目。64位动态ID以字符串返回，按无符号ID倒序分页，不因SQLite的有符号边界漏条或重复。用户名称必须唯一完整匹配，不猜同名对象。

每条包含未截断正文、作者、时间、内容类型、位置、全部媒体引用及客户端可见点赞/评论；`details`保留其他XML节点、重复项和属性，`--include-xml`额外返回原始XML。图片/视频引用含私有资源参数，输出应存入本机私有目录；这里没有下载或解码媒体文件。解析失败的条目保留ID并返回`content_complete:false`和`parse_error`。

此命令从`sns/sns.db`的已认证只读快照读取，无需sudo、调试器或辅助服务，不写源数据库。`cached_history_exhausted`只表示当前缓存已读完，`server_history_complete`和`server_sync_verified`保持false。CLI不主动请求云端历史；需要更新或全部可见历史时，上层skill通过computer-use打开正常微信朋友圈/指定人相册并加载更早页面，再调用此命令。完整范围以微信实际允许查看的时间段和明确的窗口末尾为准；网络失败或缓存数量暂时不变不能判定云端历史读完。历史缓存中也可能保留当前窗口不再显示的旧条目，应标明来源和快照时间。

图片使用 `wechat-linux send-image --recipient filehelper --file /绝对路径/image.png --request-id 本次唯一ID`，支持 PNG/JPEG、单个文件最多 10 MiB。服务先保存私有快照，构造预检与发送绑定同一目标、客户端及图片哈希。同 ID/目标/图片字节返回旧结果，改文件名不会重发，内容或格式动作冲突会拒绝。图片数据库类型读回仅提供候选记录，内容、UI 和收件仍需独立验收。原生表情包和媒体 OneBot 动作继续开发，不能把普通图片当作这些格式已完成。

文件使用 `wechat-linux send-file --recipient 精确chat_id --file /路径/文件.zip --request-id 本次唯一ID`，支持常规文件 1 字节到 10 MiB，保留最多 255 UTF-8 字节的原文件名。预检、发送与防重绑定目标、文件名和字节哈希；改名会冲突。快照目录保留 700，文件 600，客户端可在本地插入返回后继续上传。默认同名数据库记录只是候选，`ok` 不证明上传或投递完成；未知状态保留原 ID。

原生表情使用 `wechat-linux send-sticker --recipient '精确chat_id' --file /路径/sticker.gif --request-id 本次唯一ID`。常规 GIF/PNG/JPEG 输入为1字节到10 MiB，服务以桌面用户读取并私存快照，通过专用表情请求让客户端解析、准备媒体和上传，生成原生类型47。真实端到端验收覆盖动画GIF向文件传输助手发送，PNG/JPEG表情及其他收件人仍待验收。相同ID绑定目标和字节；图片/表情动作互换或内容改变会冲突，原文件删除后用 send-status 查询。默认类型读回只是候选，提交成功不自动证明收件。ClawBot测试取得服务器ID及匹配XML，但本人确认其手机端仅支持emoji、自定义表情不可用，不能以此宣称ClawBot GIF投递通过。

转发卡片和自定义 XML 使用安装后的普通命令：

```bash
wechat-linux messages --chat '精确源chat_id' --limit 20
wechat-linux forward --chat '精确源chat_id' --local-id 123 --database message/message_0.db --recipient '精确目标chat_id' --request-id 本次唯一ID
wechat-linux message-xml --chat '精确源chat_id' --local-id 123 --database message/message_0.db
wechat-linux send-xml --recipient '精确目标chat_id' --file /路径/card.xml --request-id 另一唯一ID
```

`forward` 精确读取本机源消息的 appmsg XML，经客户端原生卡片解析、构造和消息创建流程发送。跨数据库分片的本地 ID 不唯一时必须指定 `database`，不自动挑选。当前支持公众号文章5、小程序33/36；实际验收覆盖5和33，36尚无真实样例验收。自定义 XML 为1..65536字节 UTF-8 `msg/appmsg` 文件，需非空标题和对应格式必需字段，不接受NUL、DTD或实体声明。客户端规范化 XML，不保证任意标签原样传输；小程序须保留真实 appid、username、页面路径和有效资源引用。合并聊天记录及其他 XML 类型仍待实现。

同 ID 防重绑定目标、XML字节和源消息身份；转发与直接XML、不同源或修改内容相互冲突。原始XML、资源参数和快照仅保存在本机私有状态。`ok` 表示提交完成，`local_history_card_matches` 是新增标题/类型/URL匹配候选，手机收件和可点击另行验收。2026-10-01 普通CLI实际转发文章和小程序、发送修改标题/中文描述/换行/emoji的XML，均取得服务器ID并在Linux完整显示；独立字段读回和同ID防重通过，本人已通过ClawBot确认手机每种一条、显示完整及可打开。

## Protocol web links

`wechat-linux web resolve --url 'ACTUAL_LINK'` parses HTTP URLs or one explicit
HTTP webview parameter without consuming authentication or guessing opaque
mini-program tickets. `--probe` performs a GET; a successful response does not
prove a successful business page. `web open --url 'HTTP_PAGE' --browser chrome`
or `edge` requests a normal browser window.

For an HTTP equivalent observed in the current client, `web bind --url
'EXACT_SOURCE_LINK' --target 'OBSERVED_HTTP_SOURCE' --view json` saves a private
exact-source mapping. `web relay --url 'EXACT_SOURCE_LINK' --seconds 300
--browser chrome` streams its temporary localhost URL, serves complete readonly
JSON/text/HTML text, and closes on Ctrl+C or expiry. Both CLIs share owner-only
`~/.local/state/wechat-web/bindings.json`. Optional private HTTP session headers
must declare their exact origin; authenticated cross-origin redirects are
rejected. The relay does not provide client authentication, interactive JS SDK
compatibility, or a mini-program runtime. No native helper/service is needed
for these commands and no school-specific adapters are added.

## Existing call audio

```sh
wechat-linux audio streams --pid CLIENT_PID --start-time PROC_START_TIME
wechat-linux audio play --pid CLIENT_PID --start-time PROC_START_TIME --source-output STREAM_ID --file /path/notification.wav --request-id UNIQUE_ID
wechat-linux audio status --request-id UNIQUE_ID
wechat-linux audio recover --request-id UNIQUE_ID
```

These commands require a local PulseAudio-compatible server (including
PipeWire), `pactl`, and `paplay`. `play` accepts mono/stereo PCM WAV, up to
5 minutes and 32 MiB. The caller first confirms the call is connected and
authorizes its participants, then selects one capture stream from `streams`.
A capture stream by itself does not prove connection. The commands do not
place, accept, invite members to, or hang up a call.

Playback temporarily routes that exact process/start time/stream identity to
a private null-sink monitor, then restores its original input and removes the
module. No global defaults are changed. A disconnected, muted, replaced, or
manually rerouted stream stops playback. Same-ID replay never plays again;
changed content/target conflicts. Journals are private in
`~/.local/state/wechat-audio/`. Following an abrupt process exit, check the
original ID and run `recover` to clean up before any new audio request;
recovery never repeats audio.

Actual acceptance covers a normal GUI WeCom ↔ Linux WeChat private call:
source CLI playback of generated Chinese speech was independently captured
at each receiving client's selected output stream (envelope correlations
0.88 and 0.93). Standalone routing, restoration, and no-playback replay also
passed. Installed-command acceptance is recorded separately by the skill.
Selected-member group calls and call acceptance remain in development.
The audio result's `remote_delivery_verified` and
`call_connection_verified` stay false: neither is inferred from local playback.

The underlying monitor-source behavior is documented in
[PulseAudio modules](https://wiki.freedesktop.org/www/Software/PulseAudio/Documentation/User/Modules/).

## Normal private-call controls on niri

```sh
wechat-linux call inspect --pid CLIENT_PID --start-time PROC_START_TIME
wechat-linux call open --pid CLIENT_PID --start-time PROC_START_TIME --chat EXACT_CONTACT_ID
wechat-linux call start --pid CLIENT_PID --start-time PROC_START_TIME --chat EXACT_CONTACT_ID --request-id CALL_ID
wechat-linux call answer --pid CLIENT_PID --start-time PROC_START_TIME --invitation-token OBSERVED_TOKEN --request-id ANSWER_ID
wechat-linux call status --request-id CALL_ID
wechat-linux call play --request-id CALL_ID --file /path/notification.wav --audio-request-id AUDIO_ID --wait-seconds 30
wechat-linux call hangup --request-id CALL_ID
```

These commands operate the normal Qt UI through AT-SPI and keyboard input,
using `niri`, `wtype`, and system `/usr/bin/python3` with PyGObject/libatspi.
They require a logged-in desktop and share its focus. They do not use a native
VoIP API. GUI placement, connected-state readback, replay without a second
invitation, and normal hangup have been tested against an independently
accepting WeCom client. A further source CLI test waited for connection before
playing generated Chinese speech and restoring the capture stream.

The display-session helper from `niri-computer-use` preserves power/brightness
and provides a watchdog. Its default path is
`~/.local/share/niri-computer-use/bin/niri-desktop-session.py`; override with
`WECHAT_DESKTOP_SESSION_HELPER`. Missing/failed entry refuses input. An already
owned display session is refused. Read-only inspection does not wake a screen.

`open` and `start` resolve an exact ID in the selected account's contact
database, require those account databases to remain open in the specified
process, and verify the visible unique contact label and external-contact
namespace before any invitation. The UI does not expose its exact native ID;
ambiguous labels in the same namespace are refused. `open` can navigate group
chats, but group calls and selected-member invitations are not implemented yet.
`inspect` reports incoming invitations with an `invitation_token`. `answer`
accepts that exact invitation: its process, accessibility objects and matching
compositor popup must remain unchanged. Animated caption dots do not change the
token. The caller caption does not expose an exact contact ID, and authorization
to accept must be established separately. A token is not a caller identity.
Ordinary installed CLI acceptance, waiting for connection, generated WAV
playback, same-ID replay and normal hangup passed against a WeCom GUI caller.

Private call journals are in `~/.local/state/wechat-calls/`. An ID binds its
account, target or invitation token, process and start time. Repeating it never
redials or accepts another invitation. Hangup
requires the original accessibility object and compositor window identity;
another call cannot be hung up using an old ID. A ringing capture stream does
not prove connection: `call play` requires that same call window's connected
timer and hangup control. The audio result still does not prove remote delivery.
For an uncertain invitation, inspect the original ID and independently check
that it ended before explicitly using `call resolve --request-id CALL_ID
--ended`. Resolution only updates the journal, retains an unknown outcome,
and never sends another invitation or repeats sound.
