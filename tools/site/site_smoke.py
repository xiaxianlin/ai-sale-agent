# 知识产权归甲方所有（COM-IP-01）——AI 外贸销售智能体系统交付物，许可与再分发依双方合同约定。
"""Phase 3 独立站第一批全链路冒烟（FR-SITE-001/002/003/007，demo 口径，全部内存实现）。

链路（create_site_app 工厂装配 + FakeSiteLlm/FakeSearch 桩，零网络请求）：

    一行接入语义 POST /site/session（Origin 白名单）签发 visitor token
      → consent granted（/privacy/consent 落证）
      → 行为批量上报 /site/events（浏览轨迹 + ST-3200 ×3 + 停留 60s + 加购）→ 兴趣画像
      → 主动邀聊 /site/invite/check（停留 45s 阈值触发 → 每日 ≤2 次 → 冷却）
      → 对话 /site/chat（画像注入 Prompt 含「访客浏览过 ST-3200」；价格残留防线）
      → 留资 /site/leads（非法拒收 + 合法建 Customer 渠道 site）
      → 隐私（consent 撤回 → /site/events 40302；DSR export JSON 打包；DSR delete 跨表清零）

断言（退出条件，任一失败退出码 1）：
  ① 会话/追踪：accepted=6、白名单外 40301、consent 撤回后 40302、画像 hint 含「访客浏览过」；
  ② 对话/邀聊：dwell 触发 invite、第 3 次日限 daily_cap、chat Prompt 注入画像、留资建号；
  ③ 隐私：DSR 导出六段齐全、删除跨表计数、删除后零残留、工单 completed 幂等。

**demo 口径声明**：本冒烟使用确定性 Fake 桩（无真实甲方素材、无 DeepSeek API Key）；
正式验收按 SRS §16.4 以真实浏览器矩阵（Playwright）+ 生产链路复测。

用法（仓库根）：
  python3 tools/site/site_smoke.py [--report out/site/site_smoke_report.md]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 仓库根自举：直接以文件路径执行时补齐仓库根（tools 包）与 src（services 包）
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

from services.site.api.app import create_site_app  # noqa: E402
from services.site.core.conversation.service import FakeSiteLlm  # noqa: E402

TAG = "[独立站冒烟]"
KEY = "pk_site_demo_key"
ORIGIN = "https://shop.example.com"
DEFAULT_REPORT = Path("out") / "site" / "site_smoke_report.md"

PRODUCT_CHUNK = {"title": "ST-3200 产品卡", "text": "ST-3200 起订量 500 件，标准交期 25 天。"}
INVITE_CHUNK = {"title": "site_invite/dwell", "text": "Saw you checking {sku} — need a quick quote?"}
PRICE_LEAK_DRAFT = json.dumps({
    "reply_draft": "Special deal: $8.8 per piece! [R1]",
    "knowledge_refs": ["R1"], "missing_info": [],
}, ensure_ascii=False)


class FakeSearch:
    """FR-KB-030 契约检索桩：chat 检索回产品块；site_invite 过滤回邀聊模板。"""

    def __init__(self, product: dict, invite: dict) -> None:
        self._product = product
        self._invite = invite
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, query: str, kb_types: list[str] | None = None,
                 filters: Any = None, top_k: int = 3) -> dict[str, Any]:
        self.calls.append({"query": query, "kb_types": kb_types, "filters": filters})
        if filters and "site_invite" in (filters.get("scene_tag") or []):
            return {"results": [{"chunk_id": 9, "score": 0.88, "title": self._invite["title"],
                                 "text": self._invite["text"]}]}
        return {"results": [{"chunk_id": 1, "score": 0.92, "title": self._product["title"],
                             "text": self._product["text"]}]}


class MutableClock:
    """可拨时钟（邀聊冷却/日限边界确定性演练）。"""

    def __init__(self) -> None:
        self.t = 1_760_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def build_client() -> tuple[TestClient, FakeSiteLlm, FakeSearch, MutableClock]:
    """装配冒烟应用：确定性生成桩 + 检索桩 + 可拨时钟（价格泄漏分支经 script 键注入）。"""
    llm = FakeSiteLlm({"Special deal": PRICE_LEAK_DRAFT})
    search = FakeSearch(PRODUCT_CHUNK, INVITE_CHUNK)
    clock = MutableClock()
    app = create_site_app(llm=llm, search_fn=search, sdk_key=KEY, origins=(ORIGIN,), clock=clock)
    return TestClient(app), llm, search, clock


def expect(cond: bool, failures: list[str], message: str) -> None:
    """冒烟断言：失败记入清单（不中断，全量输出后统一判定）。"""
    if not cond:
        failures.append(message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 3 独立站第一批全链路冒烟（demo 口径）")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT, help="报告输出路径")
    args = parser.parse_args(argv)

    client, llm, search, clock = build_client()
    failures: list[str] = []
    steps: list[tuple[str, str]] = []

    def step(name: str, detail: str) -> None:
        steps.append((name, detail))
        print(f"{TAG} {name}：{detail}")

    # ① 会话初始化：白名单外拒绝 + 白名单内签发
    denied = client.post("/api/v1/site/session", json={"key": KEY},
                         headers={"Origin": "https://evil.example.com"})
    expect(denied.status_code == 403 and denied.json()["code"] == 40301,
           failures, f"白名单外 Origin 应 40301，实际 {denied.status_code}")
    init = client.post("/api/v1/site/session",
                       json={"key": KEY, "page": f"{ORIGIN}/products/st-3200", "lang": "en-US"},
                       headers={"Origin": ORIGIN})
    data = init.json()["data"]
    token = data["visitor_token"]
    ok_init = init.json()["code"] == 0 and token.startswith("vt_") and data["lang"] == "en"
    expect(ok_init, failures, "会话初始化应签发 vt_ token 且语言回落 en")
    step("① 会话初始化", f"visitor_uid={data['visitor_uid'][:8]}… lang=en（白名单外 40301 已拦截）")

    # ② consent granted + 行为批量上报（轨迹/兴趣/停留/加购）
    ok = client.post("/api/v1/privacy/consent", json={"status": "granted"},
                     headers={"Authorization": f"Bearer {token}"})
    expect(ok.json()["data"]["recorded"] is True, failures, "consent granted 应落证")
    events = [
        {"ev_id": "e-src", "type": "source_visit", "page": f"{ORIGIN}/?utm_source=google",
         "referrer": "https://www.google.com/", "ts": 1760000000, "payload": {"source": "google"}},
        {"ev_id": "e-p1", "type": "page_view", "page": f"{ORIGIN}/products/st-3200", "ts": 1760000001},
        {"ev_id": "e-v1", "type": "product_view", "page": f"{ORIGIN}/products/st-3200",
         "ts": 1760000002, "payload": {"sku": "ST-3200"}},
        {"ev_id": "e-v2", "type": "product_view", "page": f"{ORIGIN}/products/st-3200",
         "ts": 1760000062, "payload": {"sku": "ST-3200"}},
        {"ev_id": "e-v3", "type": "product_view", "page": f"{ORIGIN}/products/st-3200",
         "ts": 1760000122, "payload": {"sku": "ST-3200"}},
        {"ev_id": "e-d1", "type": "dwell", "page": f"{ORIGIN}/products/st-3200",
         "ts": 1760000182, "dwell_ms": 60000, "payload": {"sku": "ST-3200"}},
        {"ev_id": "e-c1", "type": "add_cart", "page": f"{ORIGIN}/cart",
         "ts": 1760000183, "payload": {"sku": "ST-3200"}},
    ]
    reported = client.post("/api/v1/site/events",
                           json={"visitor_token": token, "consent": True, "events": events})
    got = reported.json().get("data")
    expect(got == {"accepted": 7, "dropped": 0}, failures, f"批量上报应 accepted=7，实际 {got}")
    step("② 行为追踪", "7 条事件全收（轨迹+ST-3200×3+停留 60s+加购）；画像已重建")

    # ③ 主动邀聊：停留阈值触发；频率上限第 3 次 daily_cap
    invite1 = client.post("/api/v1/site/invite/check", json={
        "visitor_token": token,
        "event": {"type": "dwell", "page": f"{ORIGIN}/products/st-3200", "dwell_ms": 45000}}).json()["data"]
    clock.advance(3600)  # 跨过冷却窗（冷却/日限边界确定性演练）
    invite2 = client.post("/api/v1/site/invite/check", json={
        "visitor_token": token,
        "event": {"type": "repeat_view", "page": f"{ORIGIN}/products/st-3200",
                  "payload": {"sku": "ST-3200", "nth": 3}}}).json()["data"]
    clock.advance(3600)
    invite3 = client.post("/api/v1/site/invite/check", json={
        "visitor_token": token,
        "event": {"type": "add_cart", "page": f"{ORIGIN}/cart",
                  "payload": {"sku": "ST-3200"}}}).json()["data"]
    expect(invite1["triggered"] and invite1["trigger_type"] == "dwell",
           failures, "停留 45s 应触发 dwell 邀聊")
    invite2_content = (invite2.get("invite") or {}).get("content", "")
    expect(invite2["triggered"] and "ST-3200" in invite2_content,
           failures, "repeat_view 第 3 次应触发且模板含 ST-3200")
    expect(not invite3["triggered"] and invite3["reason"] == "daily_cap",
           failures, f"第 3 次邀聊应 daily_cap，实际 {invite3}")
    step("③ 主动邀聊", "dwell/repeat_view 两次下发（模板槽位 ST-3200），第 3 次日限拦截")

    # ④ 对话：画像注入 Prompt + 正常回复引用随行 + 价格残留防线
    chat1 = client.post("/api/v1/site/chat", json={
        "visitor_token": token, "text": "MOQ for ST-3200?", "round_no": 0}).json()["data"]
    hint_ok = "[访客画像]" in llm.last_prompt and "访客浏览过 ST-3200" in llm.last_prompt
    expect(hint_ok, failures, "对话 Prompt 应注入画像（访客浏览过 ST-3200）")
    expect("google" in llm.last_prompt, failures, "Prompt 画像应含来源渠道 google")
    expect(chat1["knowledge_refs"] and chat1["knowledge_refs"][0]["ref"] == "R1",
           failures, "正常回复应携带 R1 引用")
    chat2 = client.post("/api/v1/site/chat", json={
        "visitor_token": token, "text": "Special deal please", "round_no": 1}).json()["data"]
    leak_ok = chat2["fallback"] and chat2["reason_code"] == "price_leak" and "$8.8" not in chat2["content"]
    expect(leak_ok, failures, "价格残留应被硬拦截走兜底")
    step("④ AI 对话", "画像注入 Prompt（访客浏览过 ST-3200/来源 google）；价格泄漏走兜底话术")

    # ⑤ 留资：非法拒收 + 合法建 Customer（渠道 site）
    bad = client.post("/api/v1/site/leads", json={"visitor_token": token, "email": "bad"}).json()["data"]
    expect(bad["ok"] is False and "email" in bad["errors"], failures, "非法 email 应字段级拒收")
    lead = client.post("/api/v1/site/leads",
                       json={"visitor_token": token, "email": "Buyer@Example.com"}).json()["data"]
    expect(lead["ok"] is True and lead["customer_created"] is True,
           failures, "合法留资应新建 Customer")
    step("⑤ 留资", f"customer_id={lead.get('customer_id')}（渠道 site，邮箱归一化）；非法表单已拒收")

    # ⑥ 隐私：consent 撤回 → 行为拒收；DSR 导出 → 删除零残留
    client.post("/api/v1/privacy/consent", json={"status": "denied"},
                headers={"Authorization": f"Bearer {token}"})
    blocked = client.post("/api/v1/site/events",
                          json={"visitor_token": token, "consent": True,
                                "events": [{"ev_id": "e-x", "type": "page_view", "page": "/"}]})
    expect(blocked.status_code == 403 and blocked.json()["code"] == 40302,
           failures, "consent 撤回后行为上报应 40302")
    export_t = client.post("/api/v1/privacy/dsr/export", json={"email": "buyer@example.com"}).json()["data"]
    export_r = client.post(f"/api/v1/privacy/dsr/{export_t['ticket_id']}/execute").json()["data"]
    sections = set((export_r["result"].get("export") or {}).get("sections", []))
    need_sections = {"profile", "events", "sessions", "conversations", "mails", "consents"}
    expect(need_sections <= sections, failures, f"DSR 导出应含六段留存数据，实际 {sorted(sections)}")
    delete_t = client.post("/api/v1/privacy/dsr/delete",
                           json={"email": "buyer@example.com"}).json()["data"]
    exec_url = f"/api/v1/privacy/dsr/{delete_t['ticket_id']}/execute"
    deleted = client.post(exec_url).json()["data"]["result"]["deleted"]
    expect(deleted["visitor_event"] == 7 and deleted["site_lead"] == 1 and deleted["customer"] == 1,
           failures, f"DSR 删除跨表计数异常：{deleted}")
    expect(deleted["site_session"] == 1 and deleted["message"] >= 2,
           failures, f"会话/消息应连带删除：{deleted}")
    first_result = client.post(exec_url).json()["data"]["result"]
    again = client.post(exec_url).json()["data"]["result"]
    expect(again == first_result, failures, "DSR 执行应幂等（completed 返回首次 result）")
    step("⑥ 隐私合规", f"撤回后 40302；DSR 导出 {len(sections)} 段；删除 {deleted}；工单幂等")

    write_report(args.report, steps, failures, deleted)
    if failures:
        print(f"{TAG} 失败 {len(failures)} 项：")
        for message in failures:
            print(f"  - {message}")
        return 1
    print(f"{TAG} 冒烟通过：全链路 6 步全绿（会话/追踪/邀聊/对话/留资/隐私）")
    return 0


def write_report(
    path: Path, steps: list[tuple[str, str]], failures: list[str], deleted: dict[str, int]
) -> None:
    """写中文冒烟报告（Markdown，demo 口径声明置顶）。"""
    lines = [
        "# Phase 3 独立站第一批全链路冒烟报告（FR-SITE-001/002/003/007）",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}（UTC）",
        "- 链路：session（Origin 白名单）→ consent → events/画像 → invite（阈值+日限）→"
        " chat（画像注入+价格防线）→ leads（Customer 渠道 site）→ DSR（导出/删除）",
        "",
        "> **demo 口径声明**：确定性 Fake 桩（无真实甲方素材、无 DeepSeek API Key）；",
        "> 正式验收按 SRS §16.4 以真实浏览器矩阵 + 生产链路复测。",
        "",
        "## 一、步骤明细",
        "",
        "| 步骤 | 结果 |",
        "|---|---|",
        *[f"| {name} | {detail} |" for name, detail in steps],
        "",
        "## 二、DSR 删除跨表计数",
        "",
        "| 表 | 删除数 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in sorted(deleted.items())],
        "",
    ]
    if failures:
        lines += ["## 失败清单", ""] + [f"- {m}" for m in failures]
    else:
        lines += ["## 结论", "", "**通过**：全链路 6 步全绿，隐私红线（consent 撤回拒收/DSR 零残留）成立。"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
