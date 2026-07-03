"""
Claude Code JSONL 会话解析器

职责:
- 扫描 ~/.claude/projects 下的项目目录与会话 jsonl 文件
- 把单条 jsonl 文件解析为 UUID -> 节点的图
- 利用 parentUuid 重建父子关系, 识别 rewind 产生的分叉
- 抽取每条消息的可读摘要 (文本/工具调用/工具结果)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_PROJECTS_DIR = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".claude" / "projects"
RECYCLE_DIR_NAME = ".jsonl-manager-recycle"
RECYCLE_INDEX_FILE = "sessions.json"
RECYCLE_CONFIG_FILE = "config.json"
DEFAULT_RECYCLE_MAX = 30
ROLLBACK_DIR_NAME = ".jsonl-manager-rollback"
ROLLBACK_INDEX_FILE = "sessions.json"
EVERYTHING_DIR = Path(os.environ.get("JSONL_MANAGER_EVERYTHING_DIR", r"E:\Everything"))
EVERYTHING_SEARCH_LIMIT = 80
# 应用级配置 (独立于任一 projects_dir, 用于记忆最近打开的 projects 根目录)
APP_CONFIG_DIR = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".jsonl-manager"
APP_CONFIG_FILE = APP_CONFIG_DIR / "config.json"
RECENT_DIRS_MAX = 12
SESSION_FILE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:-.+)?$",
    re.IGNORECASE,
)


@dataclass
class Node:
    """会话中的一条消息节点 (来自 jsonl 单行记录)"""

    uuid: str
    parent_uuid: str | None
    type: str  # user / assistant / system / summary / file-history-snapshot ...
    timestamp: str | None
    role: str | None
    text: str
    tool_calls: list[dict] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)
    is_meta: bool = False
    is_sidechain: bool = False
    is_command: bool = False
    is_tool_result: bool = False
    is_failed_retry: bool = False
    model: str | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        # raw 字段不返回: 前端从未使用 (n.raw 在 static/js/app.js 0 引用),
        # 但每个 node 都跟着占大量带宽. 想看原 jsonl 调 /api/.../raw
        return {
            "uuid": self.uuid,
            "parent_uuid": self.parent_uuid,
            "type": self.type,
            "timestamp": self.timestamp,
            "role": self.role,
            "text": self.text,
            "tool_calls": self.tool_calls,
            "tool_results": self.tool_results,
            "is_meta": self.is_meta,
            "is_sidechain": self.is_sidechain,
            "is_command": self.is_command,
            "is_tool_result": self.is_tool_result,
            "is_failed_retry": self.is_failed_retry,
            "model": self.model,
        }


@dataclass
class Branch:
    """会话内的一条线性分支 (rewind 会生成多条)"""

    branch_id: str
    head_uuid: str
    node_uuids: list[str]
    started_at: str | None
    ended_at: str | None
    is_main: bool
    is_active: bool  # 当前 jsonl 末尾所在的分支
    is_error: bool   # 该分支独有部分只收到 API 错误回复 (用户因错误回滚后重发)
    fork_from: str | None  # 该分支与主线在哪个 uuid 处分叉
    title: str

    def to_dict(self) -> dict:
        return {
            "branch_id": self.branch_id,
            "head_uuid": self.head_uuid,
            "node_uuids": self.node_uuids,
            "length": len(self.node_uuids),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "is_main": self.is_main,
            "is_active": self.is_active,
            "is_error": self.is_error,
            "fork_from": self.fork_from,
            "title": self.title,
        }


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

_COMMAND_RE = re.compile(r"<command-name>([^<]+)</command-name>")
_LOCAL_CMD_RE = re.compile(r"<local-command-stdout>")


def _extract_text(content: Any) -> tuple[str, list[dict], list[dict], bool]:
    """从 message.content 抽取纯文本/工具调用/工具结果"""
    tool_calls: list[dict] = []
    tool_results: list[dict] = []
    is_tool_result = False

    if content is None:
        return "", tool_calls, tool_results, is_tool_result

    if isinstance(content, str):
        return content, tool_calls, tool_results, is_tool_result

    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "text":
                text_parts.append(item.get("text", ""))
            elif itype == "thinking":
                text_parts.append("[thinking]\n" + item.get("thinking", ""))
            elif itype == "tool_use":
                tool_calls.append(
                    {
                        "id": item.get("id"),
                        "name": item.get("name"),
                        "input": item.get("input", {}),
                    }
                )
            elif itype == "tool_result":
                is_tool_result = True
                inner = item.get("content")
                if isinstance(inner, list):
                    parts: list[str] = []
                    for sub in inner:
                        if isinstance(sub, dict):
                            if sub.get("type") == "text":
                                parts.append(sub.get("text", ""))
                            elif sub.get("type") == "image":
                                parts.append("[image]")
                    inner_text = "\n".join(parts)
                else:
                    inner_text = str(inner) if inner is not None else ""
                tool_results.append(
                    {
                        "tool_use_id": item.get("tool_use_id"),
                        "is_error": item.get("is_error", False),
                        "text": inner_text,
                    }
                )
            elif itype == "image":
                text_parts.append("[image]")
        return "\n".join(p for p in text_parts if p), tool_calls, tool_results, is_tool_result

    return str(content), tool_calls, tool_results, is_tool_result


def _parse_record(record: dict) -> Node | None:
    """把一条 jsonl 记录变成 Node, 跳过非消息记录"""
    rtype = record.get("type")
    uuid = record.get("uuid") or record.get("messageId")
    if not uuid:
        return None
    if rtype == "file-history-snapshot":
        return None

    msg = record.get("message") or {}
    if isinstance(msg, dict):
        role = msg.get("role")
        model = msg.get("model")
        content = msg.get("content")
    else:
        role = None
        model = None
        content = msg

    text, tool_calls, tool_results, is_tool_result = _extract_text(content)

    is_command = bool(_COMMAND_RE.search(text)) if isinstance(text, str) else False

    return Node(
        uuid=uuid,
        parent_uuid=record.get("parentUuid"),
        type=rtype or "unknown",
        timestamp=record.get("timestamp"),
        role=role,
        text=text,
        tool_calls=tool_calls,
        tool_results=tool_results,
        is_meta=bool(record.get("isMeta")),
        is_sidechain=bool(record.get("isSidechain")),
        is_command=is_command,
        is_tool_result=is_tool_result,
        model=model,
        raw=record,
    )


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Session 解析
# --------------------------------------------------------------------------- #


@dataclass
class Session:
    """单个 jsonl 文件解析结果"""

    project_id: str
    session_id: str
    file_path: str
    file_size: int
    mtime: float
    nodes: dict[str, Node]
    children: dict[str, list[str]]
    roots: list[str]
    branches: list[Branch]
    title: str
    cwd: str | None
    git_branch: str | None
    versions: list[str]

    def head_count(self) -> int:
        """没有子节点的 leaf, 也就是各条分支的终点"""
        return sum(1 for u in self.nodes if u not in self.children)

    def message_count(self) -> int:
        return sum(1 for n in self.nodes.values() if n.type in ("user", "assistant"))

    def to_summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "title": self.title,
            "file_size": self.file_size,
            "mtime": self.mtime,
            "node_count": len(self.nodes),
            "message_count": self.message_count(),
            "branch_count": len(self.branches),
            "has_rewind": any(not b.is_main and b.fork_from for b in self.branches),
            "cwd": self.cwd,
            "git_branch": self.git_branch,
        }


def parse_session_file(path: Path, project_id: str) -> Session:
    nodes: dict[str, Node] = {}
    order: list[str] = []
    cwd: str | None = None
    git_branch: str | None = None
    versions: list[str] = []
    last_record_uuid: str | None = None

    with path.open("r", encoding="utf-8", errors="replace") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            if cwd is None and record.get("cwd"):
                cwd = record["cwd"]
            if git_branch is None and record.get("gitBranch"):
                git_branch = record["gitBranch"]
            v = record.get("version")
            if v and v not in versions:
                versions.append(v)

            node = _parse_record(record)
            if node is None:
                continue
            if node.uuid in nodes:
                continue
            nodes[node.uuid] = node
            order.append(node.uuid)
            last_record_uuid = node.uuid

    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for uid in order:
        node = nodes[uid]
        if node.parent_uuid and node.parent_uuid in nodes:
            children.setdefault(node.parent_uuid, []).append(uid)
        else:
            roots.append(uid)

    # 唯一的"修剪"操作: 同一次 assistant 响应被 jsonl 拆成多行 (共享 message.id),
    # 把它们按 jsonl 出现顺序串成线性链 - 这是纯 jsonl 写入格式问题, 不是分叉
    children, order = _merge_assistant_msgid_splits(nodes, children, order)

    # 修正客户端会话恢复时接错锚点的 "Continue from where you left off." 续接标记,
    # 把它重接回中断时真正的落点 - 否则中断前的真实工作会被劈成一条伪分支
    children, order = _reattach_misplaced_continue(nodes, children, order)
    roots = [u for u in order if not (nodes[u].parent_uuid and nodes[u].parent_uuid in nodes)]

    # 标记 API 错误重试 (静默合并: 这些 leaf 不会被当作独立分支)
    _mark_failed_retries(nodes, children)

    branches = _build_branches(nodes, children, roots, order, last_record_uuid)
    title = _build_title(nodes, branches)

    stat = path.stat()
    return Session(
        project_id=project_id,
        session_id=path.stem,
        file_path=str(path),
        file_size=stat.st_size,
        mtime=stat.st_mtime,
        nodes=nodes,
        children=children,
        roots=roots,
        branches=branches,
        title=title,
        cwd=cwd,
        git_branch=git_branch,
        versions=versions,
    )


def _mark_failed_retries(nodes: dict[str, Node], children: dict[str, list[str]]) -> None:
    """
    全局识别"失败请求"用户消息: 它的下游 assistant 全部 isApiErrorMessage=true,
    没有任何正常响应. 这种消息后面的同内容重发, 不是 rewind 而是错误重试.

    实现: 对每条 user 节点, BFS 它的子树寻找最近的 assistant. 若仅遇到 API 错误,
    则把它标记为 is_failed_retry. 同时把通向 API 错误的中间链路 (attachment 等)
    一并标记, 这样上层折叠时整条链都视作延续.
    """
    for uid, node in nodes.items():
        if node.type != "user" or node.is_meta or node.is_tool_result:
            continue
        # BFS 找下游最近的 assistant
        stack = [(uid, [uid])]
        seen = {uid}
        saw_error = False
        saw_normal = False
        error_paths: list[list[str]] = []
        all_paths_to_assistant: list[list[str]] = []
        # 8 层在普通对话中足够 (user → assistant 多为 1–3 跳).
        # 这里留个上限只是防御极端嵌套的 jsonl, 避免栈过深; 命中后该 user 不参与本次判定.
        _BFS_MAX_DEPTH = 32
        while stack and not saw_normal:
            u, path = stack.pop()
            if len(path) > _BFS_MAX_DEPTH:
                continue
            for c in children.get(u, []):
                if c in seen:
                    continue
                seen.add(c)
                cn = nodes.get(c)
                if cn is None:
                    continue
                if cn.type == "assistant":
                    all_paths_to_assistant.append(path + [c])
                    if cn.raw.get("isApiErrorMessage"):
                        saw_error = True
                        error_paths.append(path + [c])
                        continue
                    saw_normal = True
                    break
                stack.append((c, path + [c]))

        # 仅 API 错误回复 - 失败重试
        if saw_error and not saw_normal:
            node.is_failed_retry = True
            for p in error_paths:
                for u in p:
                    nodes[u].is_failed_retry = True
            continue

        # 完全没有 assistant 回复 (用户消息后只有 attachment 等无意义节点)
        # 而且这条消息有同内容的兄弟得到了正常回复 - 也是失败请求
        if not all_paths_to_assistant and node.parent_uuid:
            siblings = children.get(node.parent_uuid, [])
            txt = (node.text or "").strip()
            if txt:
                for sib in siblings:
                    if sib == uid:
                        continue
                    sn = nodes.get(sib)
                    if sn is None or sn.type != "user":
                        continue
                    if (sn.text or "").strip() == txt:
                        node.is_failed_retry = True
                        # 把它下面的所有节点都标 failed
                        stk = [uid]
                        seen2 = {uid}
                        while stk:
                            x = stk.pop()
                            nodes[x].is_failed_retry = True
                            for c in children.get(x, []):
                                if c not in seen2:
                                    seen2.add(c)
                                    stk.append(c)
                        break


#: 同一轮真实的多 tool_use 拆分, 相邻块之间的间隔取决于工具执行耗时, 但不会
#: 跨到"几十分钟后"这种量级. 经验值: 本工具见过的真实拆分间隔都在 ~110s 以内,
#: 而低熵 id (代理/中转后端吐出的 "chatcmpl-13" 这类短 id) 在长会话里撞出的
#: 假拆分, 间隔最小也有 ~64 分钟 (最大见过 18 小时). 用这个阈值隔开两者,
#: 避免把不相关的两轮对话误拼成一条链、伪造出并不存在的 rewind 分支。
_MSGID_SPLIT_MAX_GAP_SECONDS = 1200  # 20 分钟


def _merge_assistant_msgid_splits(
    nodes: dict[str, Node],
    children: dict[str, list[str]],
    order: list[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """
    把同一次 assistant 响应被 jsonl 拆成多行的情况折叠为线性链, 但只在安全时才折叠.

    背景: 一次响应包含多个 tool_use 块时 (常见于并行工具调用), Claude Code 会写
    多条 assistant 行, 共享同一个 message.id; 每个 tool_use 块名下都会正确挂着
    "它自己的" tool_result —— 这在原始数据里天然形成一个分叉 (某个块同时是
    "同一轮的下一块"和"自己 tool_result"的父节点), 但这不是 rewind, 只是并行
    调用的正常记法, 需要折叠成一条线才不会被误判成分支。

    折叠规则: 按 message.id 分组, 把 assistant 块与它们各自的 tool_result 按
    jsonl 出现顺序穿成一条候选链, 依次把后一个节点接到前一个节点下面——但只要
    发现"即将当父节点用的那个节点"已经独立长出了候选集合之外的真实子节点
    (最典型: 客户端在中途被打断后自动注入 "Continue from where you left off."
    却接错了锚点, 挂在了某个 tool_result 下面), 就立刻停止折叠, 不再继续往后
    穿。这样才不会把那段真实的、无关的后续内容误焊接到折叠链上, 伪造出一次
    并不存在的 rewind。同时保留时间窗口保护: 低熵 id (代理/中转后端吐出的
    "chatcmpl-13" 这类短 id) 可能在毫不相关的两轮对话间复用, 相邻候选节点间隔
    过大 (经验阈值 20 分钟, 真实并行拆分间隔通常在 ~110s 以内) 一律视为 id
    碰撞, 同样停止折叠。

    "真实子节点"不包括 attachment (PreToolUse/PostToolUse 等 hook 的回显节点):
    它是客户端自动挂的元数据, 不是对话分支, 且它自己的 parent_uuid 从不会被
    本函数改写 (只有 asst_set/related_tr 里的节点会被重新挂父), 放行它不会
    误吞它下面真正的内容——如果它底下确实还有真实的 assistant/user 回复, 那段
    内容原地不动, 仍会被 _build_branches 按证据正常识别成分支; 这里只是不让
    一个纯 hook 回显节点单独挡住本该拉直的并行 tool_use 折叠。
    """
    asst_groups: dict[str, list[str]] = {}
    for uid in order:
        n = nodes[uid]
        msg = n.raw.get("message") if isinstance(n.raw.get("message"), dict) else None
        mid = (msg or {}).get("id") if msg else None
        if n.type == "assistant" and mid:
            asst_groups.setdefault(mid, []).append(uid)

    order_index = {u: i for i, u in enumerate(order)}
    new_parent: dict[str, str] = {}

    # 折叠前的原始子节点表, 用来判断某个候选节点是否已经独立长出"候选集合
    # 之外"的真实子节点 —— 这张表必须基于原始 parentUuid 建立, 不能在循环
    # 过程中被本函数自己的改写污染。
    original_children: dict[str, list[str]] = {}
    for uid in order:
        n = nodes[uid]
        if n.parent_uuid and n.parent_uuid in nodes:
            original_children.setdefault(n.parent_uuid, []).append(uid)

    for mid, auuids in asst_groups.items():
        if len(auuids) < 2:
            continue
        asst_set = set(auuids)
        related_tr = [
            uid for uid in order
            if nodes[uid].is_tool_result and nodes[uid].parent_uuid in asst_set
        ]
        candidates = asst_set | set(related_tr)
        chain = sorted(candidates, key=lambda u: order_index.get(u, 1 << 30))

        prev = None
        for u in chain:
            if prev is not None:
                extra_kids = [
                    k for k in original_children.get(prev, [])
                    if k != u and k not in candidates and nodes[k].type != "attachment"
                ]
                if extra_kids:
                    break  # prev 已经独立长出候选集合之外的真实内容, 到此为止
                prev_ts = _parse_ts(nodes[prev].timestamp)
                cur_ts = _parse_ts(nodes[u].timestamp)
                gap = (cur_ts - prev_ts).total_seconds() if prev_ts and cur_ts else 0.0
                if gap > _MSGID_SPLIT_MAX_GAP_SECONDS:
                    break  # id 碰撞, 到此为止
                new_parent[u] = prev
            prev = u


    if not new_parent:
        return children, order

    for uid, np in new_parent.items():
        nodes[uid].parent_uuid = np

    new_children: dict[str, list[str]] = {}
    for uid in order:
        n = nodes[uid]
        if n.parent_uuid and n.parent_uuid in nodes:
            new_children.setdefault(n.parent_uuid, []).append(uid)
    return new_children, order


#: 会话恢复标记 "Continue from where you left off." 与它被错挂的 tool_result
#: 之间, 时间戳跳变超过这个阈值才认定是"接错锚点". 真实的并行工具调用后紧接
#: 续接不会隔这么久; 而客户端中断→恢复往往隔几十分钟。取 5 分钟做保守下限。
_CONTINUE_MARKER = "Continue from where you left off."
_CONTINUE_REATTACH_MIN_GAP_SECONDS = 300  # 5 分钟


def _reattach_misplaced_continue(
    nodes: dict[str, Node],
    children: dict[str, list[str]],
    order: list[str],
) -> tuple[dict[str, list[str]], list[str]]:
    """
    把接错锚点的会话恢复标记 (isMeta 的 "Continue from where you left off.")
    重接到"中断时刻真正的最后一个节点", 从而消除一次并不存在的分叉。

    背景: 客户端在会话中断后恢复时, 会自动注入一条 isMeta 的
    "Continue from where you left off." (其唯一子是 model=<synthetic> 的
    "No response requested." 占位回复)。正常情况下它应该挂在"模型上次真正停下
    的叶子"上, 让对话自然接续。但客户端偶尔会把它错挂到更早的某个 tool_result
    上 —— 典型是并行工具调用留下的某个 tool_result 兄弟。这样一来, 中断前那段
    真实工作 (报告、抓取等) 就被劈成一条独立分支, 而主线反而走进了这条只有
    空续接的死胡同。用户观感上这本是"连在一起"的一次对话, 却凭空多出一个分支。

    判定 (三者同时满足才动, 否则原样返回):
      1. 该节点是 isMeta 的续接标记;
      2. 它当前挂在一个 tool_result 上, 且父子时间戳跳变超过阈值 (接错锚点的
         信号 —— 真实续接紧跟其后, 不会隔几十分钟);
      3. 存在一个时间上早于它、且不在它自己子树内的节点 (即中断时刻真正的
         最后落点)。

    修正: 把续接标记重接到"时间戳早于它、离它最近"的非后代节点上。该节点就是
    中断发生时对话真正停在的地方, 接上去后整条时间线单调连续, 分叉消失。
    """
    new_parent: dict[str, str] = {}
    for c in order:
        n = nodes[c]
        if not (n.is_meta and (n.text or "").strip() == _CONTINUE_MARKER):
            continue
        parent = n.parent_uuid
        pn = nodes.get(parent) if parent else None
        if pn is None or not pn.is_tool_result:
            continue
        c_ts = _parse_ts(n.timestamp)
        p_ts = _parse_ts(pn.timestamp)
        if c_ts is None or p_ts is None:
            continue
        if (c_ts - p_ts).total_seconds() < _CONTINUE_REATTACH_MIN_GAP_SECONDS:
            continue  # 父子时间接近, 是正常续接, 不动

        # 续接标记自己的子树 (重接目标必须排除, 否则成环)
        descendants: set[str] = set()
        stack = [c]
        while stack:
            x = stack.pop()
            descendants.add(x)
            stack.extend(children.get(x, []))

        # 时间上早于续接标记、且不在其子树内、离它最近的节点 = 中断时真正的落点
        best: str | None = None
        best_ts = None
        for u in order:
            if u in descendants:
                continue
            t = _parse_ts(nodes[u].timestamp)
            if t and t < c_ts and (best_ts is None or t > best_ts):
                best_ts = t
                best = u
        if best and best != parent:
            new_parent[c] = best

    if not new_parent:
        return children, order

    for uid, np in new_parent.items():
        nodes[uid].parent_uuid = np

    new_children: dict[str, list[str]] = {}
    for uid in order:
        n = nodes[uid]
        if n.parent_uuid and n.parent_uuid in nodes:
            new_children.setdefault(n.parent_uuid, []).append(uid)
    return new_children, order


def _walk_to_leaf(start: str, nodes: dict[str, Node], children: dict[str, list[str]],
                  preferred_leaf: str | None = None) -> list[str]:
    """从某节点出发沿子链走到叶子节点; 多子时优先走能到达 preferred_leaf 的方向, 否则走最新的子节点"""
    path = [start]
    cur = start
    while True:
        kids = children.get(cur)
        if not kids:
            break
        if len(kids) == 1:
            cur = kids[0]
        else:
            chosen = None
            if preferred_leaf:
                for k in kids:
                    if _can_reach(k, preferred_leaf, children):
                        chosen = k
                        break
            if chosen is None:
                kids_sorted = sorted(
                    kids,
                    key=lambda u: nodes[u].timestamp or "",
                    reverse=True,
                )
                chosen = kids_sorted[0]
            cur = chosen
        path.append(cur)
    return path


def _can_reach(start: str, target: str, children: dict[str, list[str]]) -> bool:
    if start == target:
        return True
    stack = [start]
    seen = {start}
    while stack:
        u = stack.pop()
        for c in children.get(u, []):
            if c == target:
                return True
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return False


def _has_branch_response_evidence(path: list[str], main_set: set[str], nodes: dict[str, Node]) -> bool:
    """
    A non-main leaf is only a real rewind branch if its unique segment contains
    evidence that the model actually started/responded on that path. Sibling
    user prompts that only have attachments/reminders are usually manual
    interrupts or quick prompt edits, not rewind history worth surfacing.

    一个孤立的 tool_result 本身不算证据: 并行 tool_use 场景下, 每个 tool_use
    块各自挂着自己的 tool_result, 天然形成"多个子节点", 但走不通的那条往往就是
    一条死胡同——tool_result 收到了, 却再没有 assistant 接着回应, 直接成了叶子。
    这不是用户主动 rewind 出来的分支, 只是并行调用的正常记法。若这个 tool_result
    之后确实还有真实的 assistant 回复, 那条回复本身在 path 里, 会被上面的
    assistant 分支命中, 不会漏判。

    失败重试也不算证据: API 报错 / 额度不足 / 模型不可用时, 用户往往手动把同一句
    话重发几次直到成功。前几次尝试各自挂着一个报错 assistant, 被 _mark_failed_retries
    标成 is_failed_retry —— 这些是"同一次提问的失败尝试", 不是用户主动 rewind 出的
    历史分支, 跳过。合成的 "No response requested." (model=<synthetic>) 也是客户端
    在打断/续接时自动补的占位回复, 同样不算真实响应。这样一条 unique 段里若只剩下
    失败尝试 + 末尾的 turn_duration 等元数据叶子, 就不会被误判成分支。
    """
    for u in path:
        if u in main_set:
            continue
        n = nodes[u]
        if n.is_failed_retry:
            continue
        if n.type == "assistant":
            # API 报错 assistant 本身就是失败尝试的证据面, 但若它是分支唯一落点
            # (没有被 _mark_failed_retries 标记), 仍应折叠掉, 不当真实分支。
            if n.raw.get("isApiErrorMessage"):
                continue
            msg = n.raw.get("message") if isinstance(n.raw.get("message"), dict) else None
            model = (msg or {}).get("model") if msg else None
            txt = (n.text or "").strip()
            if model == "<synthetic>" and txt.startswith("No response requested"):
                continue
            return True
        if n.type == "system" and (
            n.raw.get("subtype") == "api_error"
            or n.raw.get("level") == "error"
        ):
            return True
    return False


def _build_branches(
    nodes: dict[str, Node],
    children: dict[str, list[str]],
    roots: list[str],
    order: list[str],
    last_record_uuid: str | None,
) -> list[Branch]:
    """
    为每个 leaf 生成一条从 root 开始的完整路径作为"分支".
    rewind 会让某父节点产生多个子, 因此 leaf 数量 == 分支数.
    """
    leaves = [u for u in order if u not in children]
    if not leaves:
        return []

    # 找到主 leaf - jsonl 末尾节点
    main_leaf = last_record_uuid if last_record_uuid in leaves else None
    if main_leaf is None:
        main_leaf = max(leaves, key=lambda u: nodes[u].timestamp or "")

    main_path = _path_to_leaf(main_leaf, nodes)
    main_set = set(main_path)
    branches: list[Branch] = []
    for idx, leaf in enumerate(leaves):
        # 从 leaf 反向回溯到 root
        rev: list[str] = []
        cur: str | None = leaf
        seen: set[str] = set()
        while cur and cur in nodes and cur not in seen:
            rev.append(cur)
            seen.add(cur)
            parent = nodes[cur].parent_uuid
            if parent and parent in nodes:
                cur = parent
            else:
                cur = None
        path = list(reversed(rev))

        # 找该分支与主线的分叉点
        fork_from: str | None = None
        if leaf != main_leaf:
            if not _has_branch_response_evidence(path, main_set, nodes):
                continue
            for u in reversed(path):
                if u in main_set and len(children.get(u, [])) > 1:
                    fork_from = u
                    break

        first_ts = next((nodes[u].timestamp for u in path if nodes[u].timestamp), None)
        last_ts = next((nodes[u].timestamp for u in reversed(path) if nodes[u].timestamp), None)
        is_main = leaf == main_leaf

        # 非主线分支的 path 也是从 root 开始的完整路径, 若直接取 path 里第一条
        # 用户消息做标题, 每条分支都会显示成会话开头那句话 (因为它们共享同一个
        # 前缀), 完全看不出真正分叉在哪. 优先用分叉点之后、该分支独有的内容
        # 做标题; 独有部分里找不到真实用户消息时(极少见), 才退回整条 path。
        unique_part = [] if is_main else [u for u in path if u not in main_set]
        if not is_main and unique_part:
            title = _branch_title(unique_part, nodes, require_real=True) or _branch_title(path, nodes)
        else:
            title = _branch_title(path, nodes)

        # 该分支独有部分若只收到 API 错误回复, 标 is_error
        is_error = False
        if not is_main:
            saw_err = False
            saw_ok = False
            for u in unique_part:
                n = nodes[u]
                # API 错误形态 1: assistant 节点带 isApiErrorMessage
                if n.type == "assistant" and n.raw.get("isApiErrorMessage"):
                    saw_err = True
                    continue
                # API 错误形态 2: system 节点 subtype=api_error 或 level=error
                if n.type == "system" and (
                    n.raw.get("subtype") == "api_error"
                    or n.raw.get("level") == "error"
                ):
                    saw_err = True
                    continue
                # 正常 assistant 回复
                if n.type == "assistant":
                    txt = (n.text or "").strip()
                    if not txt.startswith("No response requested"):
                        saw_ok = True
                        break
            if saw_err and not saw_ok:
                is_error = True

        branches.append(
            Branch(
                branch_id=leaf[:8],
                head_uuid=leaf,
                node_uuids=path,
                started_at=first_ts,
                ended_at=last_ts,
                is_main=is_main,
                is_active=is_main,
                is_error=is_error,
                fork_from=fork_from,
                title=title,
            )
        )

    branches.sort(key=lambda b: (0 if b.is_main else 1, -(_parse_ts(b.ended_at).timestamp() if _parse_ts(b.ended_at) else 0)))
    return branches


def _path_to_leaf(leaf: str, nodes: dict[str, Node]) -> list[str]:
    rev: list[str] = []
    cur: str | None = leaf
    seen: set[str] = set()
    while cur and cur in nodes and cur not in seen:
        rev.append(cur)
        seen.add(cur)
        cur = nodes[cur].parent_uuid
    return list(reversed(rev))


def _branch_title(path: list[str], nodes: dict[str, Node], require_real: bool = False) -> str:
    """
    用第一条真实用户消息作为分支标题, 跳过命令/caveat/工具结果.

    require_real=True 时, 找不到真实用户文本 (只有命令记录或完全没有) 就返回
    空字符串而不是退到命令兜底/uuid 片段 —— 供调用方判断"这段 path 里没有
    足够信息撑起标题", 从而自己决定要不要换一段 path 再试。
    """
    fallback = ""
    for u in path:
        n = nodes[u]
        if n.type != "user" or n.is_meta or n.is_tool_result:
            continue
        if not n.text:
            continue
        t = n.text.strip()
        # 命令记录区分度低, 暂存兜底
        if "<command-" in t or "<local-command-" in t:
            if not fallback:
                m = _COMMAND_RE.search(t)
                fallback = (m.group(1).strip() if m else t[:40])
            continue
        t = re.sub(r"<[^>]+>", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            return t[:80]
    if require_real:
        return ""
    return fallback or (path[0][:8] if path else "(空)")


def _build_title(nodes: dict[str, Node], branches: list[Branch]) -> str:
    """会话标题取主分支首条用户消息"""
    main = next((b for b in branches if b.is_main), None)
    if main:
        return main.title
    for n in nodes.values():
        if n.type == "user" and not n.is_meta and n.text:
            t = re.sub(r"<[^>]+>", " ", n.text)
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                return t[:60]
    return "(空会话)"


# --------------------------------------------------------------------------- #
# Project 扫描
# --------------------------------------------------------------------------- #


def _recycle_dir(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path:
    return projects_dir / RECYCLE_DIR_NAME


def _recycle_index_path(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path:
    return _recycle_dir(projects_dir) / RECYCLE_INDEX_FILE


def _recycle_config_path(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path:
    return _recycle_dir(projects_dir) / RECYCLE_CONFIG_FILE


def _rollback_dir(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path:
    return projects_dir / ROLLBACK_DIR_NAME


def _rollback_index_path(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Path:
    return _rollback_dir(projects_dir) / ROLLBACK_INDEX_FILE


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json_file(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_app_config() -> dict:
    data = _read_json_file(APP_CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}


def _save_app_config(cfg: dict) -> None:
    _write_json_file(APP_CONFIG_FILE, cfg)


def list_recent_dirs() -> list[str]:
    """返回最近打开过的 projects 根目录 (最新在前, 已去重)."""
    cfg = _load_app_config()
    recent = cfg.get("recent_dirs")
    if not isinstance(recent, list):
        return []
    return [str(p) for p in recent if isinstance(p, str) and p]


def record_recent_dir(projects_dir: Path) -> list[str]:
    """把 projects_dir 记入最近列表 (移到最前, 去重, 截断到上限). 返回更新后的列表."""
    norm = str(Path(projects_dir).expanduser())
    recent = [d for d in list_recent_dirs() if d != norm]
    recent.insert(0, norm)
    recent = recent[:RECENT_DIRS_MAX]
    cfg = _load_app_config()
    cfg["recent_dirs"] = recent
    _save_app_config(cfg)
    return recent


def _recycle_max_items(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> int:
    cfg = _read_json_file(_recycle_config_path(projects_dir), {})
    try:
        max_items = int(cfg.get("max_items", DEFAULT_RECYCLE_MAX))
    except (AttributeError, TypeError, ValueError):
        max_items = DEFAULT_RECYCLE_MAX
    return max(1, min(max_items, 1000))


def _read_recycle_index(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> list[dict]:
    data = _read_json_file(_recycle_index_path(projects_dir), [])
    return data if isinstance(data, list) else []


def _write_recycle_index(projects_dir: Path, entries: list[dict]) -> None:
    _write_json_file(_recycle_index_path(projects_dir), entries)


def _read_rollback_index(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> list[dict]:
    data = _read_json_file(_rollback_index_path(projects_dir), [])
    return data if isinstance(data, list) else []


def _write_rollback_index(projects_dir: Path, entries: list[dict]) -> None:
    _write_json_file(_rollback_index_path(projects_dir), entries)


def _sanitize_recycle_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:120] or "session"


def _find_recycle_entry(trash_id: str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> tuple[int, dict, list[dict]]:
    entries = _prune_recycle(projects_dir)
    for idx, entry in enumerate(entries):
        if entry.get("trash_id") == trash_id:
            return idx, entry, entries
    raise FileNotFoundError(trash_id)


def _find_rollback_entry(rollback_id: str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> tuple[int, dict, list[dict]]:
    entries = _rollback_entries(projects_dir)
    for idx, entry in enumerate(entries):
        if entry.get("rollback_id") == rollback_id:
            return idx, entry, entries
    raise FileNotFoundError(rollback_id)


def _rollback_entries(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> list[dict]:
    if not _rollback_index_path(projects_dir).exists():
        return []

    entries = []
    changed = False
    for entry in _read_rollback_index(projects_dir):
        rollback_path = entry.get("rollback_path")
        if rollback_path and Path(rollback_path).exists():
            entries.append(entry)
        else:
            changed = True
    if changed:
        _write_rollback_index(projects_dir, entries)
    return entries


def _prune_recycle(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> list[dict]:
    if not _recycle_index_path(projects_dir).exists():
        return []

    max_items = _recycle_max_items(projects_dir)
    entries = []
    changed = False
    for entry in _read_recycle_index(projects_dir):
        trashed_path = entry.get("trashed_path")
        if trashed_path and Path(trashed_path).exists():
            entries.append(entry)
        else:
            changed = True

    entries.sort(key=lambda e: (e.get("deleted_at") or "", e.get("trash_id") or ""))
    remove_count = max(0, len(entries) - max_items)
    if remove_count:
        changed = True
    for entry in entries[:remove_count]:
        trashed_path = entry.get("trashed_path")
        if not trashed_path:
            continue
        try:
            Path(trashed_path).unlink(missing_ok=True)
        except OSError:
            pass

    kept = entries[remove_count:]
    if changed:
        _write_recycle_index(projects_dir, kept)
    return kept


def recycle_status(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> dict:
    entries = _prune_recycle(projects_dir)
    entries.sort(key=lambda e: e.get("deleted_at") or "", reverse=True)
    return {
        "max_items": _recycle_max_items(projects_dir),
        "count": len(entries),
        "sessions": entries,
    }


def set_recycle_max_items(max_items: int, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> dict:
    if max_items < 1:
        raise ValueError("max_items must be at least 1")
    if max_items > 1000:
        raise ValueError("max_items must be 1000 or less")
    _write_json_file(_recycle_config_path(projects_dir), {"max_items": max_items})
    return recycle_status(projects_dir)


def recycle_session(
    project_id: str,
    session_id: str,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
    title_hint: str | None = None,
) -> dict:
    source = projects_dir / project_id / f"{session_id}.jsonl"
    if not source.exists():
        raise FileNotFoundError(str(source))
    if not source.is_file():
        raise ValueError("session path is not a file")

    title = (title_hint or "").strip() or session_id

    stat = source.stat()
    deleted_at = datetime.now().astimezone().isoformat(timespec="seconds")
    safe_project = _sanitize_recycle_part(project_id)
    safe_session = _sanitize_recycle_part(session_id)
    trash_id = f"{int(time.time() * 1000)}__{safe_project}__{safe_session}"
    target = _recycle_dir(projects_dir) / f"{trash_id}.jsonl"
    counter = 1
    while target.exists():
        target = _recycle_dir(projects_dir) / f"{trash_id}-{counter}.jsonl"
        counter += 1

    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)

    entry = {
        "trash_id": target.stem,
        "project_id": project_id,
        "session_id": session_id,
        "title": title,
        "deleted_at": deleted_at,
        "original_path": str(source),
        "trashed_path": str(target),
        "file_size": stat.st_size,
        "mtime": stat.st_mtime,
    }
    entries = _read_recycle_index(projects_dir)
    entries.append(entry)
    _write_recycle_index(projects_dir, entries)
    return {"deleted": True, "session": entry, "recycle": recycle_status(projects_dir)}


def restore_recycled_session(trash_id: str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> dict:
    idx, entry, entries = _find_recycle_entry(trash_id, projects_dir)
    trashed_path = Path(entry.get("trashed_path") or "")
    if not trashed_path.exists():
        entries.pop(idx)
        _write_recycle_index(projects_dir, entries)
        raise FileNotFoundError(trash_id)

    project_id = entry.get("project_id")
    session_id = entry.get("session_id")
    if not project_id or not session_id:
        raise ValueError("recycle entry is missing original session metadata")

    project_dir = projects_dir / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    target = project_dir / f"{session_id}.jsonl"
    restored_session_id = session_id
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        restored_session_id = f"{session_id}-restored-{stamp}"
        target = project_dir / f"{restored_session_id}.jsonl"
        counter = 1
        while target.exists():
            restored_session_id = f"{session_id}-restored-{stamp}-{counter}"
            target = project_dir / f"{restored_session_id}.jsonl"
            counter += 1

    trashed_path.replace(target)
    entries.pop(idx)
    _write_recycle_index(projects_dir, entries)
    return {
        "restored": True,
        "project_id": project_id,
        "session_id": restored_session_id,
        "path": str(target),
        "recycle": recycle_status(projects_dir),
    }


def rollback_status(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> dict:
    entries = _rollback_entries(projects_dir)
    entries.sort(key=lambda e: e.get("rolled_back_at") or "", reverse=True)
    return {
        "count": len(entries),
        "sessions": entries,
    }


def rollback_session(
    project_id: str,
    session_id: str,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
    title_hint: str | None = None,
) -> dict:
    source = projects_dir / project_id / f"{session_id}.jsonl"
    if not source.exists():
        raise FileNotFoundError(str(source))
    if not source.is_file():
        raise ValueError("session path is not a file")

    title = (title_hint or "").strip() or session_id
    stat = source.stat()
    rolled_back_at = datetime.now().astimezone().isoformat(timespec="seconds")
    safe_project = _sanitize_recycle_part(project_id)
    safe_session = _sanitize_recycle_part(session_id)
    rollback_id = f"{int(time.time() * 1000)}__{safe_project}__{safe_session}"
    target = _rollback_dir(projects_dir) / f"{rollback_id}.jsonl"
    counter = 1
    while target.exists():
        target = _rollback_dir(projects_dir) / f"{rollback_id}-{counter}.jsonl"
        counter += 1

    target.parent.mkdir(parents=True, exist_ok=True)
    source.replace(target)

    entry = {
        "rollback_id": target.stem,
        "project_id": project_id,
        "session_id": session_id,
        "title": title,
        "rolled_back_at": rolled_back_at,
        "original_path": str(source),
        "rollback_path": str(target),
        "file_size": stat.st_size,
        "mtime": stat.st_mtime,
    }
    entries = _read_rollback_index(projects_dir)
    entries.append(entry)
    _write_rollback_index(projects_dir, entries)
    return {"rolled_back": True, "session": entry, "rollback": rollback_status(projects_dir)}


def restore_rollback_session(rollback_id: str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> dict:
    idx, entry, entries = _find_rollback_entry(rollback_id, projects_dir)
    rollback_path = Path(entry.get("rollback_path") or "")
    if not rollback_path.exists():
        entries.pop(idx)
        _write_rollback_index(projects_dir, entries)
        raise FileNotFoundError(rollback_id)

    project_id = entry.get("project_id")
    session_id = entry.get("session_id")
    if not project_id or not session_id:
        raise ValueError("rollback entry is missing original session metadata")

    project_dir = projects_dir / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    target = project_dir / f"{session_id}.jsonl"
    restored_session_id = session_id
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        restored_session_id = f"{session_id}-rollback-{stamp}"
        target = project_dir / f"{restored_session_id}.jsonl"
        counter = 1
        while target.exists():
            restored_session_id = f"{session_id}-rollback-{stamp}-{counter}"
            target = project_dir / f"{restored_session_id}.jsonl"
            counter += 1

    rollback_path.replace(target)
    entries.pop(idx)
    _write_rollback_index(projects_dir, entries)
    return {
        "restored": True,
        "project_id": project_id,
        "session_id": restored_session_id,
        "path": str(target),
        "rollback": rollback_status(projects_dir),
    }


def _everything_es_path() -> Path | None:
    configured = EVERYTHING_DIR / "es.exe"
    if configured.exists():
        return configured
    found = shutil.which("es.exe") or shutil.which("es")
    return Path(found) if found else None


def _everything_exe_path() -> Path | None:
    configured = EVERYTHING_DIR / "Everything.exe"
    if configured.exists():
        return configured
    found = shutil.which("Everything.exe") or shutil.which("Everything")
    return Path(found) if found else None


def _everything_ipc_ready(es: Path | None = None) -> bool:
    es = es or _everything_es_path()
    if not es:
        return False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            [str(es), "-get-everything-version"],
            cwd=str(es.parent),
            capture_output=True,
            text=True,
            timeout=1.5,
            creationflags=flags,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _start_everything(es: Path | None = None) -> bool:
    exe = _everything_exe_path()
    if not exe:
        return False

    try:
        os.startfile(str(exe))  # type: ignore[attr-defined]
    except OSError:
        try:
            subprocess.Popen(
                [str(exe)],
                cwd=str(exe.parent),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except OSError:
            return False

    deadline = time.time() + 12.0
    while time.time() < deadline:
        if _everything_ipc_ready(es):
            return True
        time.sleep(0.35)
    return False


def _ensure_everything_ready(es: Path) -> tuple[bool, str | None]:
    if _everything_ipc_ready(es):
        return True, None
    if _start_everything(es):
        return True, None
    return False, "Everything IPC 不可用，请确认 Everything.exe 能正常打开"


def _everything_query_literal(query: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f]+", " ", query).strip()
    cleaned = cleaned[:200].replace('"', " ")
    return f'content:"{cleaned}"'


def _run_everything_command(args: list[str], es: Path, timeout: int = 15) -> subprocess.CompletedProcess[str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        [str(es), *args],
        cwd=str(es.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=flags,
    )


def _is_searchable_session_file(path: Path, projects_dir: Path) -> bool:
    if path.suffix.lower() != ".jsonl":
        return False
    try:
        rel = path.resolve().relative_to(projects_dir.resolve())
    except (OSError, ValueError):
        return False
    if len(rel.parts) != 2:
        return False
    project_id, filename = rel.parts
    if project_id in {RECYCLE_DIR_NAME, ROLLBACK_DIR_NAME, "subagents"}:
        return False
    if filename.startswith("agent-"):
        return False
    return bool(SESSION_FILE_RE.match(path.stem))


def _everything_files_from_output(output: str, projects_dir: Path, limit: int) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for line in (output or "").splitlines():
        raw = line.strip().strip('"')
        if not raw:
            continue
        path = Path(raw)
        if not _is_searchable_session_file(path, projects_dir):
            continue
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            files.append(path)
        if len(files) >= limit:
            break
    return files


def _filesystem_jsonl_files(projects_dir: Path, limit: int = 5000) -> list[Path]:
    if not projects_dir.exists():
        return []
    files: list[Path] = []
    for path in projects_dir.rglob("*.jsonl"):
        if not path.is_file() or not _is_searchable_session_file(path, projects_dir):
            continue
        files.append(path)
        if len(files) >= limit:
            break
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return files


def _run_everything_file_query(
    projects_dir: Path,
    limit: int,
    query: str | None = None,
    use_content: bool = False,
) -> tuple[list[Path], str | None]:
    es = _everything_es_path()
    if not es:
        return [], f"未找到 Everything CLI：{EVERYTHING_DIR / 'es.exe'}"

    ready, ready_error = _ensure_everything_ready(es)
    if not ready:
        return [], ready_error

    limit = max(1, min(int(limit or EVERYTHING_SEARCH_LIMIT), 5000))
    search_terms = ["ext:jsonl"]
    if query:
        search_terms.append(_everything_query_literal(query) if use_content else query)

    args = [
        "-n",
        str(limit),
        "-full-path-and-name",
        "-sort",
        "date-modified-descending",
        "-path",
        str(projects_dir),
        *search_terms,
    ]

    try:
        proc = _run_everything_command(args, es)
    except subprocess.TimeoutExpired:
        return [], "Everything 搜索超时"
    except OSError as exc:
        return [], f"Everything CLI 启动失败：{exc}"

    if proc.returncode == 8:
        ready, ready_error = _ensure_everything_ready(es)
        if not ready:
            return [], ready_error
        try:
            proc = _run_everything_command(args, es)
        except subprocess.TimeoutExpired:
            return [], "Everything 已启动，但搜索仍然超时"
        except OSError as exc:
            return [], f"Everything CLI 启动失败：{exc}"

    if proc.returncode not in (0, 1):
        msg = (proc.stderr or proc.stdout or "").strip()
        return [], msg or f"Everything 搜索失败，退出码 {proc.returncode}"

    return _everything_files_from_output(proc.stdout, projects_dir, limit), None


def _run_everything_query(query: str, projects_dir: Path, limit: int) -> tuple[list[Path], str | None]:
    """
    用 Everything 的 content 模式拿到"含有 query"的文件路径, 然后再并入整个 projects 目录
    下的 jsonl 全量, 顺序按 mtime 倒序截断. 这样即使内容匹配没结果, 也能在
    everything_search_sessions 阶段做按文本的兜底匹配.
    """
    files, error = _run_everything_file_query(projects_dir, limit * 3, query=query, use_content=True)
    if error:
        return _filesystem_jsonl_files(projects_dir), None

    all_files, all_error = _run_everything_file_query(projects_dir, 5000)
    if all_error and not files:
        return _filesystem_jsonl_files(projects_dir), None

    ordered: list[Path] = []
    seen: set[Path] = set()
    for path in [*files, *all_files]:
        key = path.resolve()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered, None


def _search_snippet(text: str, query: str, radius: int = 54) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return ""
    idx = compact.lower().find(query.lower())
    if idx < 0:
        return compact[: radius * 2]
    start = max(0, idx - radius)
    end = min(len(compact), idx + len(query) + radius)
    prefix = "..." if start else ""
    suffix = "..." if end < len(compact) else ""
    return prefix + compact[start:end] + suffix


def _search_matches_in_file(path: Path, query: str, max_matches: int = 3) -> list[dict]:
    project_id = path.parent.name
    session_id = path.stem
    matches: list[dict] = []
    title = ""
    q = query.lower()
    try:
        stat = path.stat()
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                node = _parse_record(record)
                if not node:
                    continue
                if not title and node.role == "user" and node.text.strip():
                    title = re.sub(r"\s+", " ", node.text).strip()[:80]

                haystacks: list[tuple[str, str]] = []
                if node.text:
                    haystacks.append((node.text, node.role or node.type or "message"))
                for result in node.tool_results:
                    text = result.get("text") or ""
                    if text:
                        haystacks.append((text, "tool"))

                for text, role in haystacks:
                    if q not in text.lower():
                        continue
                    matches.append(
                        {
                            "project_id": project_id,
                            "session_id": session_id,
                            "title": title or session_id,
                            "uuid": node.uuid,
                            "timestamp": node.timestamp,
                            "role": role,
                            "snippet": _search_snippet(text, query),
                            "mtime": stat.st_mtime,
                            "file_size": stat.st_size,
                        }
                    )
                    break
                if len(matches) >= max_matches:
                    break
    except OSError:
        return []
    if not title:
        for item in matches:
            item["title"] = session_id
    return matches


def everything_search_sessions(
    query: str,
    projects_dir: Path = DEFAULT_PROJECTS_DIR,
    limit: int = EVERYTHING_SEARCH_LIMIT,
) -> dict:
    query = re.sub(r"\s+", " ", query or "").strip()
    if len(query) < 2:
        return {"query": query, "results": [], "count": 0, "available": True}

    files, error = _run_everything_query(query, projects_dir, limit)
    if error:
        return {"query": query, "results": [], "count": 0, "available": False, "error": error}

    results: list[dict] = []
    for path in files:
        results.extend(_search_matches_in_file(path, query))
        if len(results) >= limit:
            break
    results.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    return {
        "query": query,
        "results": results[:limit],
        "count": len(results[:limit]),
        "available": True,
    }


def list_projects(projects_dir: Path = DEFAULT_PROJECTS_DIR) -> list[dict]:
    if not projects_dir.exists():
        return []
    out: list[dict] = []
    for entry in sorted(projects_dir.iterdir()):
        if entry.name == RECYCLE_DIR_NAME:
            continue
        if entry.name == ROLLBACK_DIR_NAME:
            continue
        if not entry.is_dir():
            continue
        sessions = list(entry.glob("*.jsonl"))
        if not sessions:
            continue
        last = max((s.stat().st_mtime for s in sessions), default=0)
        cwd = _decode_project_id(entry.name)
        out.append(
            {
                "project_id": entry.name,
                "cwd": cwd,
                "session_count": len(sessions),
                "mtime": last,
                "size": sum(s.stat().st_size for s in sessions),
            }
        )
    out.sort(key=lambda p: p["mtime"], reverse=True)
    return out


def _decode_project_id(pid: str) -> str:
    """projects 子目录名是把 cwd 中的特殊字符替换为 - 形成的, 这里做个尽力还原"""
    s = pid
    if re.match(r"^[A-Za-z]--", s):
        s = s[0] + ":/" + s[3:]
    s = s.replace("--", "/").replace("-", "/")
    return s


def list_sessions(project_id: str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> list[dict]:
    pdir = projects_dir / project_id
    if not pdir.is_dir():
        return []
    summaries: list[dict] = []
    for f in pdir.glob("*.jsonl"):
        try:
            session = parse_session_file(f, project_id)
        except Exception as e:
            summaries.append(
                {
                    "session_id": f.stem,
                    "project_id": project_id,
                    "title": f"(解析失败: {e})",
                    "file_size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                    "node_count": 0,
                    "message_count": 0,
                    "branch_count": 0,
                    "has_rewind": False,
                    "cwd": None,
                    "git_branch": None,
                }
            )
            continue
        summaries.append(session.to_summary())
    summaries.sort(key=lambda s: s["mtime"], reverse=True)
    return summaries


# LRU 缓存: 键 = (pid, sid, mtime, size). 文件改动时 mtime/size 必变, 自动失效.
_SESSION_CACHE: OrderedDict = OrderedDict()
_SESSION_CACHE_MAX = 64


def load_session(project_id: str, session_id: str, projects_dir: Path = DEFAULT_PROJECTS_DIR) -> Session | None:
    f = projects_dir / project_id / f"{session_id}.jsonl"
    if not f.exists():
        return None
    try:
        stat = f.stat()
        key = (project_id, session_id, stat.st_mtime, stat.st_size)
    except OSError:
        return parse_session_file(f, project_id)

    cached = _SESSION_CACHE.get(key)
    if cached is not None:
        _SESSION_CACHE.move_to_end(key)
        return cached

    session = parse_session_file(f, project_id)
    _SESSION_CACHE[key] = session
    # 同 (pid, sid) 的旧键清掉 (mtime 变了)
    stale = [k for k in _SESSION_CACHE if k[0] == project_id and k[1] == session_id and k != key]
    for k in stale:
        _SESSION_CACHE.pop(k, None)
    while len(_SESSION_CACHE) > _SESSION_CACHE_MAX:
        _SESSION_CACHE.popitem(last=False)
    return session


def clear_session_cache() -> None:
    """测试或管理用: 清空会话缓存."""
    _SESSION_CACHE.clear()
