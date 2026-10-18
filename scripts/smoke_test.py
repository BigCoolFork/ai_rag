#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
精简私有化 RAG 知识库 —— 端到端冒烟测试（黑盒 HTTP）

对一个已运行的实例（本机 uvicorn 或 docker compose 部署）验证验收 Checklist：
  0. 健康检查五项全绿
  1. 认证：注册 / 重复注册 409 / 错误密码 401 / 登录 / /me / 伪造令牌 401
  2. 入库：DOCX 与 PDF 上传、切片数 > 0、重复上传去重、非法类型 400、列表可见
  3. 非流式问答：命中带答案与 sources（DOCX 章节 / PDF 真实页码）；无命中不编造
  4. SSE 流式：逐 token 帧 + 结束 done 帧（带 sources / hit）
  5. 删除联动：登记记录消失、再问无命中（向量同步清除）
  6. 权限隔离：B 账号看不到 A 的文档、删 A 的文档 404、问 A 的内容无命中

运行方式：
  # 开发机（.venv 已装 requirements.txt）
  python scripts/smoke_test.py
  # 指定实例 / 超时（纯 CPU 冷启动问答较慢，默认 600s）
  python scripts/smoke_test.py --base-url http://192.168.1.10:8000 --timeout 600
  # 只验证鉴权与文档管理，跳过 LLM 问答（无需模型，秒级完成）
  python scripts/smoke_test.py --quick
  # 离线交付的容器内运行（镜像自带全部依赖）
  docker cp scripts/smoke_test.py rag-personal-app:/tmp/smoke_test.py
  docker compose exec app python /tmp/smoke_test.py --base-url http://127.0.0.1:8000

退出码：全部通过 0；任一失败 1。
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
import sys
import time
from dataclasses import dataclass, field

import httpx

# ---------------------------------------------------------------------------
# 测试夹具数据（独特事实，避免 LLM 凭常识作答；与任何真实资料无关）
# ---------------------------------------------------------------------------
DOCX_POLICY_NO = "ZX-2026-SMOKE-09"
# 年假天数：LLM 可能答阿拉伯数字 12 或中文「十二」，两者都算正确
DOCX_FACT_ANSWERS = ("12", "十二")
PDF_PROJECT_NO = "PROJECT-ZX-7718"
PDF_FACT_ANSWER = "5B"   # 评审会议室（仅写在 PDF 第 2 页）
UNRELATED_QUESTION = "19 世纪法国印象派绘画的代表人物有哪些？请列举。"
PASSWORD = "Smoke#123456"


def _build_docx() -> bytes:
    """生成含唯一事实编号的中文 DOCX（两段，保证切片数 >= 1）。"""
    from docx import Document

    doc = Document()
    doc.add_heading("星河公司 2026 年带薪年假管理办法（冒烟测试版）", level=1)
    doc.add_paragraph(
        f"政策编号 {DOCX_POLICY_NO}。第一条 为规范员工带薪年假管理，"
        "根据国家有关规定并结合公司实际情况，特制定本办法。本办法适用于与公司签订"
        "正式劳动合同、连续工作满十二个月以上的全体在职员工，劳务派遣人员另行规定。"
    )
    doc.add_paragraph(
        "第二条 年假天数按累计工龄计算：入职满一年不满五年的正式员工，"
        "每年享有十二天带薪年假；入职满五年不满十年的，每年十五天；"
        "入职满十年及以上的，每年二十天。国家法定休假日、休息日不计入年假假期。"
    )
    doc.add_paragraph(
        "第三条 年假应在当年自然年度内休完，最迟不超过次年三月三十一日，"
        "逾期未休的年假自动作废，公司不另行折算工资。因工作需要确不能安排休假的，"
        "须经部门负责人书面同意并报人力资源部备案。"
        "第四条 员工申请年假须提前五个工作日通过 OA 系统提交，由直属上级审批。"
    )
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_pdf() -> bytes:
    """生成两页英文 PDF：第 1 页项目概况，第 2 页才出现会议室事实（验页码来源）。"""
    import fitz  # PyMuPDF

    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text(
        (72, 72),
        f"Internal Memo - Project {PDF_PROJECT_NO}\n\n"
        "This document is a synthetic fixture for smoke testing.\n"
        "The quarterly review covers milestones, risks and budget.\n"
        "All names and numbers below are fictional.",
        fontsize=12,
    )
    p2 = doc.new_page()
    p2.insert_text(
        (72, 72),
        f"Page 2 - Logistics\n\n"
        f"The review meeting for project {PDF_PROJECT_NO} is held\n"
        "in conference room 5B, building 3, at 10:00 AM.\n"
        "Attendees: QA, backend and delivery teams.",
        fontsize=12,
    )
    buf = doc.tobytes()
    doc.close()
    return buf


# ---------------------------------------------------------------------------
# 结果收集
# ---------------------------------------------------------------------------
@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        if ok:
            self.passed.append(name)
            print(f"  PASS  {name}")
        else:
            self.failed.append((name, detail))
            print(f"  FAIL  {name}\n        {detail}")

    def section(self, title: str) -> None:
        print(f"\n[{title}]")


class SmokeFatal(Exception):
    """前置条件失败，后续场景无法继续。"""


# ---------------------------------------------------------------------------
# API 小封装（失败直接带响应体抛错）
# ---------------------------------------------------------------------------
class Api:
    def __init__(self, base_url: str, timeout: float) -> None:
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)
        self.token: str | None = None

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def register(self, username: str, password: str = PASSWORD) -> httpx.Response:
        return self.http.post("/api/auth/register",
                              json={"username": username, "password": password})

    def login(self, username: str, password: str = PASSWORD) -> httpx.Response:
        return self.http.post("/api/auth/login",
                              json={"username": username, "password": password})

    def login_as(self, username: str, password: str = PASSWORD) -> None:
        r = self.login(username, password)
        if r.status_code != 200:
            raise SmokeFatal(f"登录 {username} 失败：{r.status_code} {r.text[:200]}")
        self.token = r.json()["access_token"]

    def me(self) -> httpx.Response:
        return self.http.get("/api/auth/me", headers=self.headers())

    def upload(self, filename: str, content: bytes, ctype: str,
               token: str | None = None) -> httpx.Response:
        h = {"Authorization": f"Bearer {token or self.token}"}
        files = {"file": (filename, io.BytesIO(content), ctype)}
        return self.http.post("/api/documents/upload", headers=h, files=files)

    def docs(self) -> httpx.Response:
        return self.http.get("/api/documents/list", headers=self.headers())

    def delete(self, doc_id: int) -> httpx.Response:
        return self.http.delete(f"/api/documents/{doc_id}", headers=self.headers())

    def wait_until_success(self, doc_id: int, timeout: float = 600.0) -> dict | None:
        """轮询文档列表，直到指定 doc 状态非 pending（success/failed）。

        入库已改为后台异步，上传响应仅返回 pending；用此方法等待后台完成。
        :return: 最终文档记录；超时时返回 None
        """
        deadline = time.time() + timeout
        last: dict | None = None
        while time.time() < deadline:
            r = self.docs()
            items = r.json().get("items", []) if r.status_code == 200 else []
            last = next((it for it in items if it["id"] == doc_id), None)
            if last is not None and last.get("status") != "pending":
                return last
            time.sleep(3)
        return last

    def ask(self, question: str) -> httpx.Response:
        return self.http.post("/api/chat/ask", headers=self.headers(),
                              json={"question": question})

    def stream(self, question: str) -> tuple[list[dict], httpx.Response | None]:
        """读取 SSE 全量帧；HTTP 层失败时返回空帧列表与响应。"""
        frames: list[dict] = []
        with self.http.stream(
            "GET", "/api/chat/stream",
            params={"question": question, "token": self.token or ""},
        ) as r:
            if r.status_code != 200:
                return [], r
            for line in r.iter_lines():
                if line.startswith("data:"):
                    try:
                        frames.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
        return frames, None

    def close(self) -> None:
        self.http.close()


# ---------------------------------------------------------------------------
# 各验收场景
# ---------------------------------------------------------------------------
def section_health(api: Api, rep: Report) -> None:
    rep.section("0/6 健康检查")
    r = api.http.get("/api/health")
    ok = r.status_code == 200
    detail = f"{r.status_code} {r.text[:200]}"
    if ok:
        checks = r.json().get("checks", {})
        not_ready = [k for k, v in checks.items() if not v.get("ready")]
        ok = not not_ready
        detail = f"未就绪项：{not_ready}" if not_ready else \
            f"模型：{', '.join(r.json().get('installed_models', []))}"
    rep.check("GET /api/health 返回 200 且五项全绿", ok, detail)
    if not ok:
        raise SmokeFatal("服务未就绪，冒烟无法继续")


def section_auth(api: Api, user_a: str, rep: Report) -> None:
    rep.section("1/6 认证与令牌")
    r = api.register(user_a)
    rep.check("注册新用户返回 201", r.status_code == 201,
              f"{r.status_code} {r.text[:200]}")

    r = api.register(user_a)
    rep.check("重复注册返回 409", r.status_code == 409,
              f"{r.status_code} {r.text[:200]}")

    r = api.login(user_a, "wrong-password-000")
    rep.check("错误密码登录返回 401", r.status_code == 401,
              f"{r.status_code} {r.text[:200]}")

    r = api.login(user_a)
    ok = r.status_code == 200 and bool(r.json().get("access_token"))
    rep.check("正确密码登录返回 JWT", ok, f"{r.status_code} {r.text[:200]}")
    if not ok:
        raise SmokeFatal("无法获取令牌")
    api.token = r.json()["access_token"]

    r = api.me()
    rep.check("Bearer 令牌访问 /me 返回当前用户",
              r.status_code == 200 and r.json().get("username") == user_a,
              f"{r.status_code} {r.text[:200]}")

    saved = api.token
    api.token = None
    r = api.docs()
    rep.check("无令牌访问业务接口返回 401", r.status_code == 401,
              f"{r.status_code} {r.text[:200]}")
    api.token = "not-a-real-jwt.signature.payload"
    r = api.me()
    rep.check("伪造令牌返回 401", r.status_code == 401,
              f"{r.status_code} {r.text[:200]}")
    api.token = saved


def section_upload(api: Api, rep: Report) -> dict[str, int]:
    """返回 {'docx': id, 'pdf': id}。"""
    rep.section("2/6 文档入库与去重")
    ids: dict[str, int] = {}

    # 夹具字节只生成一次：python-docx 每次保存都会写入新时间戳，
    # 若重新生成字节流，SHA256 不同，无法验证「同文件去重」
    docx_bytes = _build_docx()
    pdf_bytes = _build_pdf()
    docx_ctype = ("application/vnd.openxmlformats-officedocument."
                  "wordprocessingml.document")

    r = api.upload("年假管理办法.docx", docx_bytes, docx_ctype)
    body = r.json() if r.status_code == 200 else {}
    # 异步入队：上传响应立即返回 pending，chunk_count 为 None
    ok = (r.status_code == 200 and not body.get("duplicated")
          and body.get("document", {}).get("status") == "pending")
    rep.check("DOCX 上传：200、入队成功（status=pending）",
              ok, f"{r.status_code} {r.text[:300]}")
    if ok:
        doc_id = body["document"]["id"]
        final = api.wait_until_success(doc_id)
        ok2 = final is not None and final.get("status") == "success" \
            and (final.get("total_chunks") or 0) > 0
        rep.check("DOCX 后台入库成功（轮询至 status=success，切片数 > 0）",
                  ok2, json.dumps(final, ensure_ascii=False)[:300])
        if ok2:
            ids["docx"] = doc_id

    r = api.upload("项目评审纪要.pdf", pdf_bytes, "application/pdf")
    body = r.json() if r.status_code == 200 else {}
    ok = (r.status_code == 200 and not body.get("duplicated")
          and body.get("document", {}).get("status") == "pending")
    rep.check("PDF 上传：200、入队成功（status=pending）",
              ok, f"{r.status_code} {r.text[:300]}")
    if ok:
        doc_id = body["document"]["id"]
        final = api.wait_until_success(doc_id)
        ok2 = final is not None and final.get("status") == "success" \
            and (final.get("total_chunks") or 0) > 0
        rep.check("PDF 后台入库成功（轮询至 status=success，切片数 > 0）",
                  ok2, json.dumps(final, ensure_ascii=False)[:300])
        if ok2:
            ids["pdf"] = doc_id

    # 必须复用同一字节流，才能命中同用户 SHA256 去重
    r = api.upload("年假管理办法.docx", docx_bytes, docx_ctype)
    body = r.json() if r.status_code == 200 else {}
    rep.check("同一文件重复上传命中去重（duplicated=true）",
              r.status_code == 200 and body.get("duplicated") is True,
              f"{r.status_code} {r.text[:300]}")

    r = api.upload("note.txt", b"plain text not allowed", "text/plain")
    rep.check("非法类型 .txt 返回 400", r.status_code == 400,
              f"{r.status_code} {r.text[:200]}")

    r = api.docs()
    items = r.json().get("items", []) if r.status_code == 200 else []
    names = {it["original_name"] for it in items}
    rep.check("文档列表包含两份文档且均 success",
              r.status_code == 200 and r.json().get("total") >= 2
              and {"年假管理办法.docx", "项目评审纪要.pdf"} <= names
              and all(it["status"] == "success" for it in items),
              f"{r.status_code} {r.text[:300]}")
    return ids


def section_ask(api: Api, rep: Report) -> None:
    rep.section("3/6 非流式问答（命中 / 页码来源 / 不编造）")

    r = api.ask(f"根据政策 {DOCX_POLICY_NO}，入职满一年不满五年的员工年假有多少天？")
    if r.status_code != 200:
        rep.check("DOCX 问答命中并回答年假天数", False,
                  f"{r.status_code} {r.text[:300]}")
    else:
        data = r.json()
        sources = data.get("sources", [])
        answer = data.get("answer", "")
        ok = (data.get("hit") is True
              and any(fact in answer for fact in DOCX_FACT_ANSWERS)
              and len(sources) >= 1
              and any(s.get("doc_name") == "年假管理办法.docx"
                      and s.get("file_type") == "docx" for s in sources))
        rep.check("DOCX 问答：hit=true、答案含 12/十二 天、sources 指向 DOCX",
                  ok, json.dumps(data, ensure_ascii=False)[:400])

    r = api.ask(f"Where is the review meeting for {PDF_PROJECT_NO}? "
                "Answer with the room number.")
    if r.status_code != 200:
        rep.check("PDF 问答命中且来源标注第 2 页", False,
                  f"{r.status_code} {r.text[:300]}")
    else:
        data = r.json()
        sources = data.get("sources", [])
        ok = (data.get("hit") is True and PDF_FACT_ANSWER in data.get("answer", "")
              and any(s.get("file_type") == "pdf" and s.get("page") == 2
                      for s in sources))
        rep.check("PDF 问答：hit=true、答案含 5B、至少一条来源 page=2",
                  ok, json.dumps(data, ensure_ascii=False)[:400])

    r = api.ask(UNRELATED_QUESTION)
    if r.status_code != 200:
        rep.check("无关问题不编造（hit=false、sources 为空）", False,
                  f"{r.status_code} {r.text[:300]}")
    else:
        data = r.json()
        rep.check("无关问题不编造（hit=false、sources 为空）",
                  data.get("hit") is False and data.get("sources") == [],
                  json.dumps(data, ensure_ascii=False)[:300])


def section_sse(api: Api, rep: Report) -> None:
    rep.section("4/6 SSE 流式问答")
    frames, err = api.stream(f"政策 {DOCX_POLICY_NO} 规定年假最少多少天？")
    if err is not None:
        rep.check("SSE 返回 token 帧与 done 帧", False,
                  f"HTTP {err.status_code}")
        return
    types = [f.get("type") for f in frames]
    tokens = "".join(f.get("data", "") for f in frames if f.get("type") == "token")
    done = next((f for f in frames if f.get("type") == "done"), None)
    errors = [f for f in frames if f.get("type") == "error"]
    ok = (types.count("token") >= 1 and tokens.strip() != ""
          and done is not None and done.get("hit") is True
          and len(done.get("sources", [])) >= 1 and not errors)
    detail = f"帧类型序列={types[:8]}... token 字数={len(tokens)}"
    rep.check("SSE：至少 1 个 token 帧 + done(hit=true, sources 非空)、无 error",
              ok, detail)


def section_delete(api: Api, ids: dict[str, int], rep: Report,
                   run_llm: bool = True) -> None:
    rep.section("5/6 删除联动（登记 + 向量 + 文件）")
    docx_id = ids.get("docx")
    if docx_id is None:
        rep.check("删除 DOCX 后列表不再包含", False, "缺少 DOCX 文档 id，无法测试")
        return

    r = api.delete(docx_id)
    rep.check("删除文档返回 200 且 deleted=true",
              r.status_code == 200 and r.json().get("deleted") is True,
              f"{r.status_code} {r.text[:200]}")

    r = api.docs()
    items = r.json().get("items", []) if r.status_code == 200 else []
    rep.check("删除后文档列表不再包含该文件",
              all(it["id"] != docx_id for it in items),
              f"{r.status_code} {r.text[:300]}")

    r = api.delete(docx_id)
    rep.check("重复删除返回 404", r.status_code == 404,
              f"{r.status_code} {r.text[:200]}")

    if not run_llm:
        print("  （--quick：跳过删除后再问的检索验证）")
        return
    r = api.ask(f"政策 {DOCX_POLICY_NO} 的年假天数是多少？")
    if r.status_code != 200:
        rep.check("删除后再问无命中（向量已清除）", False,
                  f"{r.status_code} {r.text[:300]}")
    else:
        data = r.json()
        rep.check("删除后再问无命中（hit=false、sources 为空）",
                  data.get("hit") is False and data.get("sources") == [],
                  json.dumps(data, ensure_ascii=False)[:300])


def section_isolation(api: Api, user_a: str, user_b: str,
                      ids: dict[str, int], rep: Report,
                      run_llm: bool = True) -> None:
    rep.section("6/6 权限隔离（A 文档对 B 不可见 / 不可删 / 不可检索）")
    pdf_id = ids.get("pdf")
    if pdf_id is None:
        rep.check("B 账号看不到 A 的文档", False, "缺少 PDF 文档 id，无法测试")
        return

    r = api.register(user_b)
    rep.check("B 账号注册成功", r.status_code in (201, 409),
              f"{r.status_code} {r.text[:200]}")
    api.login_as(user_b)

    r = api.docs()
    items = r.json().get("items", []) if r.status_code == 200 else []
    rep.check("B 的文档列表为空，看不到 A 的 PDF",
              r.status_code == 200 and all(it["id"] != pdf_id for it in items),
              f"{r.status_code} total={r.json().get('total') if r.status_code == 200 else '-'}")

    r = api.delete(pdf_id)
    rep.check("B 删除 A 的文档返回 404（不泄露存在性）",
              r.status_code == 404, f"{r.status_code} {r.text[:200]}")

    if run_llm:
        r = api.ask(f"Where does {PDF_PROJECT_NO} hold its review meeting?")
        if r.status_code != 200:
            rep.check("B 检索不到 A 的文档内容", False,
                      f"{r.status_code} {r.text[:300]}")
        else:
            data = r.json()
            rep.check("B 问答无法命中 A 的私有文档",
                      data.get("hit") is False and data.get("sources") == [],
                      json.dumps(data, ensure_ascii=False)[:300])
    else:
        print("  （--quick：跳过 B 的跨账号检索验证）")

    # 恢复 A 身份做收尾清理（删除 A 剩余的 PDF）
    api.login_as(user_a)
    r = api.delete(pdf_id)
    if r.status_code != 200:
        print(f"  提示：清理 A 的 PDF(id={pdf_id}) 失败：{r.status_code}")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="RAG 知识库端到端冒烟测试")
    parser.add_argument("--base-url",
                        default="http://127.0.0.1:8000",
                        help="被测实例地址（默认 http://127.0.0.1:8000）")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="单次 HTTP 超时秒数（纯 CPU 冷启动默认 600）")
    parser.add_argument("--quick", action="store_true",
                        help="跳过 LLM 问答与 SSE，只验健康/认证/文档管理")
    args = parser.parse_args()

    suffix = secrets.token_hex(3)
    user_a = f"smoke_a_{suffix}"
    user_b = f"smoke_b_{suffix}"
    print(f"目标实例：{args.base_url.rstrip('/')}")
    print(f"测试账号：{user_a} / {user_b}（随机生成，不与既有用户冲突）")

    api = Api(args.base_url, args.timeout)
    rep = Report()
    started = time.time()
    try:
        section_health(api, rep)
        section_auth(api, user_a, rep)
        ids = section_upload(api, rep)
        if not args.quick:
            section_ask(api, rep)
            section_sse(api, rep)
        else:
            print("\n[3/6 非流式问答]  --quick 已跳过")
            print("[4/6 SSE 流式问答] --quick 已跳过")
        section_delete(api, ids, rep, run_llm=not args.quick)
        section_isolation(api, user_a, user_b, ids, rep, run_llm=not args.quick)
    except SmokeFatal as exc:
        print(f"\n致命错误，终止：{exc}")
        return 1
    finally:
        api.close()

    elapsed = time.time() - started
    total = len(rep.passed) + len(rep.failed)
    print("\n" + "=" * 60)
    print(f"冒烟结果：{len(rep.passed)}/{total} 通过，耗时 {elapsed:.0f}s")
    if rep.failed:
        print("失败项：")
        for name, detail in rep.failed:
            print(f"  - {name}: {detail}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
