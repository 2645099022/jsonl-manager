"""
Claude Code JSONL 会话管理 Web 应用
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request

import session_parser

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

PROJECTS_DIR = session_parser.DEFAULT_PROJECTS_DIR


def _projects_dir() -> Path:
    return Path(app.config.get("PROJECTS_DIR", PROJECTS_DIR))


@app.after_request
def _log_session_detail_timing(response):
    """调试: 切会话接口的耗时日志. 仅 /api/.../sessions/<sid> 触发, 打印到 stderr."""
    import time
    import sys
    from flask import g
    t0 = getattr(g, "_sd_t_total0", None)
    t_parse = getattr(g, "_sd_t_parse_done", None)
    if t0 is None or t_parse is None:
        return response
    t_done = time.perf_counter()
    sys.stderr.write(
        f"[jsonl-manager][detail] total={(t_done-t0)*1000:.1f}ms "
        f"parse={(t_parse-t0)*1000:.1f}ms build={(t_done-t_parse)*1000:.1f}ms "
        f"nodes={getattr(g, '_sd_node_count', '?')} "
        f"branches={getattr(g, '_sd_branch_count', '?')} "
        f"-> {response.status_code}\n"
    )
    sys.stderr.flush()
    return response


@app.route("/")
def index():
    return render_template(
        "index.html",
        projects_dir=str(_projects_dir()),
    )


@app.route("/api/config")
def api_config():
    """返回当前生效的 projects_dir 与最近打开过的根目录, 供前端下拉展示."""
    return jsonify(
        {
            "projects_dir": str(_projects_dir()),
            "recent_dirs": session_parser.list_recent_dirs(),
        }
    )


@app.route("/api/config/projects-dir", methods=["PUT"])
def api_set_projects_dir():
    """运行时切换 projects 根目录, 并记入最近列表."""
    data = request.get_json(silent=True) or {}
    raw = (data.get("projects_dir") or "").strip()
    if not raw:
        return jsonify({"error": "projects_dir 不能为空"}), 400

    new_dir = Path(raw).expanduser()
    if not new_dir.exists() or not new_dir.is_dir():
        return jsonify({"error": f"目录不存在: {new_dir}"}), 400

    app.config["PROJECTS_DIR"] = str(new_dir)
    recent = session_parser.record_recent_dir(new_dir)
    return jsonify({"projects_dir": str(new_dir), "recent_dirs": recent})


@app.route("/api/projects")
def api_projects():
    return jsonify({"projects": session_parser.list_projects(_projects_dir())})


@app.route("/api/projects/<project_id>/sessions")
def api_sessions(project_id: str):
    sessions = session_parser.list_sessions(project_id, _projects_dir())
    return jsonify({"sessions": sessions})


@app.route("/api/search")
def api_search():
    query = request.args.get("q", "")
    try:
        limit = int(request.args.get("limit", session_parser.SEARCH_LIMIT))
    except (TypeError, ValueError):
        limit = session_parser.SEARCH_LIMIT
    return jsonify(session_parser.search_sessions(query, _projects_dir(), limit))


@app.route("/api/recycle", methods=["GET"])
def api_recycle_status():
    return jsonify(session_parser.recycle_status(_projects_dir()))


@app.route("/api/recycle/settings", methods=["PUT"])
def api_recycle_settings():
    data = request.get_json(silent=True) or {}
    try:
        max_items = int(data.get("max_items", session_parser.DEFAULT_RECYCLE_MAX))
    except (TypeError, ValueError):
        return jsonify({"error": "max_items must be a number"}), 400

    try:
        status = session_parser.set_recycle_max_items(max_items, _projects_dir())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(status)


@app.route("/api/recycle/<trash_id>/restore", methods=["POST"])
def api_restore_session(trash_id: str):
    try:
        result = session_parser.restore_recycled_session(trash_id, _projects_dir())
    except FileNotFoundError:
        abort(404)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/rollback", methods=["GET"])
@app.route("/api/archive", methods=["GET"])
def api_rollback_status():
    return jsonify(session_parser.rollback_status(_projects_dir()))


@app.route("/api/rollback/<rollback_id>/restore", methods=["POST"])
@app.route("/api/archive/<rollback_id>/restore", methods=["POST"])
def api_restore_rollback_session(rollback_id: str):
    try:
        result = session_parser.restore_rollback_session(rollback_id, _projects_dir())
    except FileNotFoundError:
        abort(404)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/projects/<project_id>/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(project_id: str, session_id: str):
    data = request.get_json(silent=True) or {}
    try:
        result = session_parser.recycle_session(
            project_id,
            session_id,
            _projects_dir(),
            title_hint=data.get("title"),
        )
    except FileNotFoundError:
        abort(404)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/projects/<project_id>/sessions/<session_id>/rollback", methods=["POST"])
@app.route("/api/projects/<project_id>/sessions/<session_id>/archive", methods=["POST"])
def api_rollback_session(project_id: str, session_id: str):
    data = request.get_json(silent=True) or {}
    try:
        result = session_parser.rollback_session(
            project_id,
            session_id,
            _projects_dir(),
            title_hint=data.get("title"),
        )
    except FileNotFoundError:
        abort(404)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@app.route("/api/projects/<project_id>/sessions/<session_id>")
def api_session_detail(project_id: str, session_id: str):
    import time
    from flask import g
    g._sd_t_total0 = time.perf_counter()
    session = session_parser.load_session(project_id, session_id, _projects_dir())
    if session is None:
        abort(404)
    g._sd_t_parse_done = time.perf_counter()
    g._sd_node_count = len(session.nodes)
    g._sd_branch_count = len(session.branches)

    branch_id = request.args.get("branch")
    branches = session.branches
    selected = None
    if branch_id:
        selected = next((b for b in branches if b.branch_id == branch_id), None)
    if selected is None:
        selected = next((b for b in branches if b.is_main), branches[0] if branches else None)

    if selected is None:
        return jsonify(
            {
                "session_id": session.session_id,
                "project_id": session.project_id,
                "title": session.title,
                "cwd": session.cwd,
                "git_branch": session.git_branch,
                "versions": session.versions,
                "branches": [],
                "selected_branch": None,
                "nodes": [],
                "fork_points": [],
            }
        )

    nodes_payload = [session.nodes[u].to_dict() for u in selected.node_uuids if u in session.nodes]

    fork_points = sorted(
        {b.fork_from for b in branches if b.fork_from} - {None}
    )

    # forks_at: 每条旧分支挂在它独有部分的第一条 user/assistant 节点上
    # 该 anchor 不在主线 nodes 里, 前端会把它当成一个独立时间锚点插入时间线
    selected_set = set(selected.node_uuids)
    forks_at: dict[str, list[dict]] = {}
    extra_anchors: list[dict] = []
    if selected.is_main:
        for b in branches:
            if b.is_main or not b.fork_from:
                continue
            unique = [u for u in b.node_uuids if u not in selected_set]
            anchor_uuid = None
            for u in unique:
                if u not in session.nodes:
                    continue
                n = session.nodes[u]
                if n.type in ("user", "assistant") and not n.is_meta and not n.is_tool_result:
                    anchor_uuid = u
                    break
            if anchor_uuid is None:
                anchor_uuid = b.fork_from
            forks_at.setdefault(anchor_uuid, []).append(
                {
                    "branch_id": b.branch_id,
                    "head_uuid": b.head_uuid,
                    "title": b.title,
                    "started_at": b.started_at,
                    "ended_at": b.ended_at,
                    "fork_from": b.fork_from,
                    "is_error": b.is_error,
                    "length": len(unique),
                    "nodes": [session.nodes[u].to_dict() for u in unique if u in session.nodes],
                }
            )
            # 如果 anchor 不在主线 nodes 里, 把它的简要信息作为"额外锚点"提供给前端
            if anchor_uuid not in selected_set and anchor_uuid in session.nodes:
                an = session.nodes[anchor_uuid]
                extra_anchors.append(
                    {
                        "uuid": anchor_uuid,
                        "type": an.type,
                        "role": an.role,
                        "timestamp": an.timestamp,
                        "text": an.text,
                        "is_failed_retry": an.is_failed_retry,
                        "is_meta": an.is_meta,
                        "is_tool_result": an.is_tool_result,
                        "is_command": an.is_command,
                        "is_sidechain": an.is_sidechain,
                    }
                )

    return jsonify(
        {
            "session_id": session.session_id,
            "project_id": session.project_id,
            "title": session.title,
            "cwd": session.cwd,
            "git_branch": session.git_branch,
            "versions": session.versions,
            "branches": [b.to_dict() for b in branches],
            "selected_branch": selected.to_dict(),
            "nodes": nodes_payload,
            "fork_points": list(fork_points),
            "forks_at": forks_at,
            "extra_anchors": extra_anchors,
        }
    )


@app.route("/api/projects/<project_id>/sessions/<session_id>/raw")
def api_session_raw(project_id: str, session_id: str):
    """直接返回原始 jsonl 内容, 供调试或下载"""
    f = _projects_dir() / project_id / f"{session_id}.jsonl"
    if not f.exists():
        abort(404)
    return f.read_text(encoding="utf-8", errors="replace"), 200, {"Content-Type": "application/jsonl; charset=utf-8"}


@app.route("/api/projects/<project_id>/sessions/<session_id>/tree")
def api_session_tree(project_id: str, session_id: str):
    """返回带分叉信息的完整节点图, 供绘制全局分支树"""
    session = session_parser.load_session(project_id, session_id, _projects_dir())
    if session is None:
        abort(404)
    nodes = []
    for uid, node in session.nodes.items():
        nodes.append(
            {
                "uuid": uid,
                "parent_uuid": node.parent_uuid,
                "type": node.type,
                "role": node.role,
                "timestamp": node.timestamp,
                "is_meta": node.is_meta,
                "is_command": node.is_command,
                "summary": (node.text or "").strip()[:80],
                "child_count": len(session.children.get(uid, [])),
            }
        )
    return jsonify(
        {
            "nodes": nodes,
            "branches": [b.to_dict() for b in session.branches],
        }
    )


def main():
    ap = argparse.ArgumentParser(description="Claude Code JSONL 会话管理工具")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument(
        "--projects-dir",
        default=str(PROJECTS_DIR),
        help="Claude Code projects 目录, 默认 ~/.claude/projects",
    )
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    pdir = Path(args.projects_dir).expanduser()
    if not pdir.exists():
        print(f"[警告] projects 目录不存在: {pdir}")
    app.config["PROJECTS_DIR"] = str(pdir)
    if pdir.exists():
        session_parser.record_recent_dir(pdir)

    print(f"[jsonl-manager] projects dir = {pdir}")
    print(f"[jsonl-manager] http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
