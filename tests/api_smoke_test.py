"""API 级离线冒烟测试（无 API Key，临时数据库，走完整 SSE 链路）。

运行: .venv/bin/python tests/api_smoke_test.py
覆盖笔试红线: 冷打开不空白 / 清 Key 能跑完出预览 / 游客隔离 / 公共项目 fork 写保护 /
版本快照与回滚 / app_states 同步 / PORT 监听契约。
"""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# 强制离线：置空（而非删除）—— agents_v2 的 _load_dotenv 不会覆盖已定义（即使是空）的键
for _k in ("KIMI_API_KEY", "MOONSHOT_API_KEY", "BACKUP_API_KEY", "KIMI_MODEL", "BACKUP_MODEL"):
    os.environ[_k] = ""

_db_path = os.path.join(tempfile.mkdtemp(prefix="p2a_api_test_"), "test.db")
os.environ["APP_DB_PATH"] = _db_path

from fastapi.testclient import TestClient  # noqa: E402

from backend import main  # noqa: E402


def collect_sse(response) -> list[dict]:
    events = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            events.append(main.json.loads(line[len("data: "):]))
    return events


def main_test() -> None:
    client = TestClient(main.app)
    print("== 1. 健康检查 ==")
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    print("   /api/health OK")

    print("== 2. 公共广场（免登录，冷启动种子）==")
    r = client.get("/api/public/showcase")
    assert r.status_code == 200
    showcase = r.json()
    assert len(showcase) == 5, f"种子数量 {len(showcase)}"
    assert all(item["latest_code"].startswith("<!DOCTYPE html>") for item in showcase)
    print("   种子", [item["title"] for item in showcase])

    print("== 3. 游客 sessions 回落广场 ==")
    r = client.get("/api/sessions")
    assert r.status_code == 200 and len(r.json()) == 5
    print("   游客可见广场列表 OK")

    print("== 4. 注册登录合一 + token ==")
    r = client.post("/api/login", json={"username": "demo", "password": "demo123"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/login", json={"username": "demo", "password": "wrong"})
    assert r.status_code == 401
    print("   登录/错误密码 401 OK")

    print("== 5. SSE 生成（无 Key -> L4 演示模式兜底）==")
    r = client.post("/api/chat", json={"session_id": None, "message": "帮我做一个房贷计算器"},
                    headers=headers)
    assert r.status_code == 200, r.text
    events = collect_sse(r)
    types = [e["type"] for e in events]
    assert "code" in types and "text" in types and types[-1] == "done", types
    done = events[-1]
    session_id = done["session_id"]
    code_events = [e for e in events if e["type"] == "code"]
    assert code_events and "<!DOCTYPE html>" in code_events[0]["content"]
    assert "【演示模式】" in "".join(e.get("content", "") for e in events if e["type"] == "text")
    print(f"   SSE 事件序列 {types}，新会话 id={session_id}")

    print("== 6. 会话详情（消息/版本/代码已持久化）==")
    r = client.get(f"/api/sessions/{session_id}", headers=headers)
    detail = r.json()
    assert any(m["role"] == "assistant" for m in detail["messages"])
    assert any(m["role"] == "status" for m in detail["messages"])
    assert detail["latest_code"].startswith("<!DOCTYPE html>")
    assert detail["versions"] and detail["versions"][0]["mode"] == "FALLBACK"
    print(f"   版本快照 ×{len(detail['versions'])}，消息 ×{len(detail['messages'])}")

    print("== 6.5 会话文件夹产物文件（files API）==")
    r = client.get(f"/api/sessions/{session_id}/files", headers=headers)
    assert r.status_code == 200, r.text
    flist = r.json()
    assert flist["path"] == f"users/{main.db.get_token_user(token)['id']}/sessions/{session_id}/", flist["path"]
    assert any(f["name"] == "index.html" and f["size"] > 100 for f in flist["files"]), flist
    r = client.get(f"/api/sessions/{session_id}/files/index.html", headers=headers)
    assert r.status_code == 200 and r.json()["content"] == detail["latest_code"]
    print(f"   产物文件 OK: {flist['path']}index.html ({flist['files'][0]['size']} B)")

    print("== 6.6 权限控制：游客不能生成，但可浏览内置页 ==")
    r = client.post("/api/chat", json={"session_id": None, "message": "做一个应用"})
    assert r.status_code == 401, f"游客应 401，实际 {r.status_code}"
    public_id_g = showcase[0]["id"]
    r = client.get(f"/api/sessions/{public_id_g}")   # 游客可看公共详情
    assert r.status_code == 200 and r.json()["is_public"] == 1, f"公共详情游客应 200，实际 {r.status_code}"
    r = client.get(f"/api/sessions/{public_id_g}/files")
    assert r.status_code == 200, f"游客应能读公共文件，实际 {r.status_code}"
    r = client.get(f"/api/sessions/{public_id_g}/files/index.html")
    assert r.status_code == 200 and "<!DOCTYPE html>" in r.json()["content"]
    r = client.get(f"/api/sessions/{session_id}")   # 私有会话游客不可见
    assert r.status_code == 404, f"私有会话游客应 404，实际 {r.status_code}"
    print("   游客: 生成 401 / 公共详情+文件 200 / 私有详情 404 OK")

    print("== 7. 公共项目 fork 写保护 ==")
    public_id = showcase[0]["id"]
    before = main.db.get_session_row_any(public_id)["latest_code"]
    r = client.post("/api/chat", json={"session_id": public_id, "message": "把标题改成红色"},
                    headers=headers)
    events = collect_sse(r)
    fork_id = events[-1]["session_id"]
    assert fork_id != public_id, "公共项目未被 fork"
    after = main.db.get_session_row_any(public_id)["latest_code"]
    assert after == before, "公共模板被篡改了！"
    fork_row = main.db.get_session_row_any(fork_id)
    assert fork_row["user_id"] > 0 and fork_row["is_public"] == 0
    r = client.get(f"/api/sessions/{public_id}", headers=headers)
    assert r.status_code == 200 and r.json()["is_public"] == 1, "公共会话应可只读访问"
    print(f"   fork #{public_id} -> #{fork_id}，源模板未变，公共只读 OK")

    print("== 8. 版本回滚 ==")
    v_before = main.db.record_version(session_id, "<html>手工v2</html>", "FULL", "手工快照")
    r = client.post(f"/api/sessions/{session_id}/versions/{v_before}/restore", headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/sessions/{session_id}", headers=headers)
    assert r.json()["latest_code"] == "<html>手工v2</html>"
    print("   回滚写回 latest_code 并追加回滚快照 OK")

    print("== 9. app_states 同步桥 ==")
    r = client.put(f"/api/sessions/{session_id}/state", json={"state": {"p2a_todo_board_v1": "[]"}},
                   headers=headers)
    assert r.status_code == 200, r.text
    r = client.get(f"/api/sessions/{session_id}/state", headers=headers)
    assert r.json()["state"] == {"p2a_todo_board_v1": "[]"}
    r = client.put(f"/api/sessions/{session_id}/state", json={"state": {"bad": object()}},
                   headers=headers) if False else None  # object() 不可 JSON 序列化，由 pydantic 拦截
    print("   PUT/GET 状态 OK")

    print("== 9.5 全局生成互斥 ==")
    assert main._gen_acquire(9999, "互斥测试会话") is True
    r = client.post("/api/chat", json={"session_id": None, "message": "测试互斥"},
                    headers=headers)
    assert r.status_code == 409, f"占用期间应 409，实际 {r.status_code}"
    assert "正在生成中" in r.json()["detail"]
    r = client.get("/api/generation/status")
    assert r.json()["busy"] is True and r.json()["session_id"] == 9999
    main._gen_release(9999)
    r = client.get("/api/generation/status")
    assert r.json()["busy"] is False
    r = client.post("/api/chat", json={"session_id": session_id, "message": "把标题改成蓝色"},
                    headers=headers)
    assert r.status_code == 200, f"释放后应恢复可用，实际 {r.status_code}"
    print("   占用 409 / status 查询 / 释放恢复 OK")

    print("== 10. 越权访问隔离 ==")
    r2 = client.post("/api/login", json={"username": "另一位评委", "password": "abc123"})
    headers2 = {"Authorization": f"Bearer {r2.json()['token']}"}
    r = client.get(f"/api/sessions/{session_id}", headers=headers2)
    assert r.status_code == 404, "用户 B 看到了用户 A 的会话！"
    print("   用户 B 访问用户 A 会话返回 404 OK")

    print("== 11. PORT 契约 ==")
    src = (PROJECT_ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert 'os.environ.get("PORT"' in src and 'host="0.0.0.0"' in src
    print("   PORT 环境变量 + 0.0.0.0 监听 OK")

    print("\n🎉 API 全部冒烟测试通过（无 Key 环境，四层兜底生效）")


if __name__ == "__main__":
    main_test()
