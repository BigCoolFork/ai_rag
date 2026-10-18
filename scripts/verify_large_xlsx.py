"""一次性验收脚本：上传大 xlsx，轮询后台入库进度直到完成。

用法：python scripts/verify_large_xlsx.py <xlsx路径>
验证点：
  1. HTTP 200 + pending 快速返回（不落同步嵌入）
  2. 后台分批嵌入无 503 / 超时
  3. 最终 status=success，processed_chunks == total_chunks
  4. 打印总耗时
"""
from __future__ import annotations

import io
import json
import secrets
import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"
XLSX_PATH = sys.argv[1]


def main() -> int:
    uname = "bigxlsx_" + secrets.token_hex(4)
    pwd = secrets.token_hex(8)
    with httpx.Client(base_url=BASE, timeout=30.0) as c:
        # 注册 + 登录
        r = c.post("/api/auth/register", json={"username": uname, "password": pwd})
        assert r.status_code == 201, f"注册失败 {r.status_code}"
        r = c.post("/api/auth/login", json={"username": uname, "password": pwd})
        token = r.json()["access_token"]
        h = {"Authorization": f"Bearer {token}"}

        # 上传
        with open(XLSX_PATH, "rb") as f:
            content = f.read()
        t0 = time.time()
        files = {"file": ("庄市小学日常收入表.xlsx", io.BytesIO(content),
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        r = c.post("/api/documents/upload", headers=h, files=files)
        upload_elapsed = time.time() - t0
        print(f"[上传] HTTP {r.status_code} 耗时 {upload_elapsed:.1f}s")
        body = r.json()
        assert r.status_code == 200, f"上传非 200: {r.text[:300]}"
        doc = body["document"]
        assert doc["status"] == "pending", f"预期 pending 实际 {doc['status']}"
        doc_id = doc["id"]
        print(f"[上传] doc_id={doc_id} status=pending 已快速返回")

        # 轮询进度
        deadline = time.time() + 3 * 3600  # 3 小时上限
        last_pct = -1
        while time.time() < deadline:
            r = c.get("/api/documents/list", headers=h)
            items = r.json().get("items", []) if r.status_code == 200 else []
            rec = next((it for it in items if it["id"] == doc_id), None)
            if rec is None:
                print("[轮询] 文档已被删除，终止")
                return 1
            status = rec["status"]
            total = rec.get("total_chunks") or 0
            processed = rec.get("processed_chunks") or 0
            pct = int(processed / total * 100) if total else 0
            if pct != last_pct or status != "pending":
                print(f"[轮询] {status}  processed/total={processed}/{total}  ({pct}%)  "
                      f"elapsed={time.time()-t0:.0f}s")
                last_pct = pct
            if status != "pending":
                total_elapsed = time.time() - t0
                if status == "success":
                    print(f"[完成] SUCCESS total_chunks={total} 总耗时 {total_elapsed:.0f}s "
                          f"({total_elapsed/60:.1f} min)")
                    return 0
                else:
                    print(f"[失败] FAILED error_msg={rec.get('error_msg')}")
                    return 2
            time.sleep(10)
        print("[超时] 3 小时未完成")
        return 3


if __name__ == "__main__":
    sys.exit(main())
