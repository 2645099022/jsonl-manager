# Claude Code JSONL 会话管理工具

一个本地 Web 工具，用于浏览和管理 Claude Code 在 `~/.claude/projects/` 下保存的对话 jsonl 文件。

## 功能

- 三栏布局：项目列表 / 会话列表 / 分支侧栏 + 消息详情
- 完整还原对话内容：用户消息、assistant 文本、工具调用、工具结果、命令、meta 标记
- **rewind 分支可视化**：jsonl 中 rewind 后被丢弃的旧对话不会消失，本工具把同一会话内的每条 leaf 路径识别为一条分支
  - 主线分支（绿色）：jsonl 末尾节点所在的分支，也就是当前最新的对话
  - 回滚分支（橙色，标 `↺ 回滚`）：rewind 之前的旧线，可点击切换查看
  - 在消息流中分叉点会用 `⎇ 分叉点` 高亮，并插入一条分隔条
  - 侧栏主线视图会显示时间线骨架，分叉点处折叠所有旧分支
- **失败请求识别**：API 错误后用户重发的同内容消息会自动标 `⚠ 失败请求`，与 rewind 区分开
- **回收站（Recycle）**：会话移入回收站而非直接删除；可设置最多保留条数；一键还原
- **回档（Archive）**：会话级"撤销"操作，把当前 jsonl 移动到 `.jsonl-manager-rollback/` 目录，随时还原
- **全局搜索**：内置 SQLite FTS5 全文索引，跨整个 projects 目录按对话内容搜，命中后高亮并可定位到原消息。零外部依赖，首次搜索建一次索引后查询毫秒级
- 工具调用 / 工具结果用可折叠面板展示，输入输出一目了然
- 按标题或会话 ID 即时过滤

## 项目结构

```
jsonl-manager/
├── app.py                  # Flask 入口与 API
├── session_parser.py       # jsonl 解析、分支重建、回收站/回档、FTS5 全文索引搜索
├── requirements.txt
├── run.bat / run.sh        # 启动脚本
├── templates/
│   └── index.html
└── static/
    ├── css/app.css
    └── js/app.js
```

## 安装与运行

```bash
cd jsonl-manager
pip install -r requirements.txt

# 默认监听 127.0.0.1:5000, 自动读取 %USERPROFILE%/.claude/projects
python app.py

# 指定其它 projects 目录
python app.py --projects-dir /custom/path

# 指定监听地址 / 端口 / 调试模式
python app.py --host 0.0.0.0 --port 8000 --debug

# Windows 双击或命令行
run.bat
```

打开浏览器访问 `http://127.0.0.1:5000` 即可。

### 全局搜索索引

全局搜索使用 Python 内置 `sqlite3` 的 FTS5 全文索引，**无需任何外部程序**。

- 索引落盘在 `<projects_dir>/.jsonl-manager-index/index.db`，每个 projects 根目录一套
- 首次搜索时建索引（当前规模约数秒），之后查询毫秒级；重启不重建，只增量同步变化过的会话
- 索引会自动包含对话正文与工具结果（命令输出/文件内容）
- 查询 ≥3 字符走 FTS5，2 字符走 `LIKE` 兜底（trigram 分词器最短索引单位是 3 字符）

## API

| 路径 | 方法 | 说明 |
| ---- | ---- | ---- |
| `GET /api/config` | | 当前生效的 `projects_dir` |
| `GET /api/projects` | | 项目列表 |
| `GET /api/projects/<pid>/sessions` | | 会话列表（含分支数、是否含 rewind） |
| `GET /api/projects/<pid>/sessions/<sid>?branch=<id>` | | 指定分支的消息流 |
| `GET /api/projects/<pid>/sessions/<sid>/tree` | | 全图节点 + 分支信息 |
| `GET /api/projects/<pid>/sessions/<sid>/raw` | | 原始 jsonl |
| `GET /api/search?q=<query>&limit=<n>` | | FTS5 全局内容搜索 |
| `GET /api/recycle` | | 回收站状态（条目、上限） |
| `PUT /api/recycle/settings` | | 修改回收站最大保留数 `{"max_items": N}` |
| `POST /api/recycle/<trash_id>/restore` | | 还原回收站中的会话 |
| `GET /api/rollback` / `GET /api/archive` | | 回档列表 |
| `POST /api/rollback/<rollback_id>/restore` / `POST /api/archive/<rollback_id>/restore` | | 还原回档会话 |
| `DELETE /api/projects/<pid>/sessions/<sid>` | | 把会话移入回收站（body 可选 `{"title": "..."}`） |
| `POST /api/projects/<pid>/sessions/<sid>/rollback` / `POST .../archive` | | 把会话回档（body 可选 `{"title": "..."}`） |

## 关于 rewind 的实现说明

Claude Code 的 jsonl 中每条记录通过 `parentUuid` 串成一棵树。正常情况下这棵树是单链；执行 `/rewind` 后，用户从某个旧节点继续，新节点的 `parentUuid` 指向那个旧节点，于是该节点出现两个子节点 —— 这就是分叉的由来。本工具：

1. 把所有没有子节点的 uuid 视为 leaf
2. 每个 leaf 反向回溯到 root，形成一条分支
3. jsonl 末尾节点所在的分支被标记为主线，其余为"回滚"分支
4. 在消息流中，分叉点节点用边框高亮，并在其后插入提示条
5. 侧栏主线视图会按时间正序把所有分叉点排成时间线，分叉点处折叠旧分支

这样无论你切换到哪条分支，都能直观看到与主线的关系以及独立内容从哪里开始。

## 关于失败请求识别

Claude Code 在 API 出错时会自动用同一条用户消息重试，最终 jsonl 里会出现多条**同文本的 user 节点**，它们的子树里既有 `isApiErrorMessage=true` 的 assistant、也有正常 assistant。本工具通过 BFS 子树判断：

- 子树里**只有错误 assistant** → 整条链标 `is_failed_retry`，前端显示 `⚠ 失败请求`
- 子树里**错误+正常都有** → 视为成功重试，不标
- 子树里**没有 assistant**（只有 attachment/tool_result）→ 找同 parent 的同文本兄弟，若有正常回复则也判为失败请求

## 关于回档与回收站

- **回收站**：会话级软删除。会话从原位置移到 `.jsonl-manager-recycle/` 下，`max_items` 控制保留上限。还原时回到原项目，文件名冲突自动加 `-restored-<时间戳>`。
- **回档**：会话级备份。会话从原位置移到 `.jsonl-manager-rollback/` 下，与回收站相互独立。回档常用于"先备份当前对话再重新操作"的场景。
- 两个目录都不会出现在 `/api/projects` 与会话扫描里。

## 隐私

工具默认只读 `~/.claude/projects` 下的 jsonl（包含完整对话内容）。如部署到非本机监听，请自行考虑访问控制（反向代理鉴权、防火墙等）。