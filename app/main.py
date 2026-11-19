"""FastAPI 入口：路由、预览令牌缓存、失效管控。"""
from __future__ import annotations

import json
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Tuple

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import Database, StaleVersionError
from .models import (
    ActiveInput,
    ValidationError,
    parse_cases_csv,
    parse_relatives_csv,
    validate_locks,
    validate_quotas,
)
from .splitter import SplitterError, solve_split

import os

DB_PATH = Path(os.environ.get("SPLIT_DB_PATH",
                              Path(__file__).resolve().parent.parent / "data" / "app.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="病例切分平台", version="1.0.0")
db = Database(str(DB_PATH))


# --------------------------------------------------------------------------- #
# 预览缓存：仅存内存。重启即清空——重启后不存在任何“旧预览”可被发布。
# 每个预览绑定其求解时的数据版本；版本一旦变动，令牌立即作废。
# --------------------------------------------------------------------------- #
_PREVIEW_MAX = 64
_previews: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_preview_lock = threading.Lock()


def _add_preview(version: int, input_json: dict, result: dict) -> str:
    token = uuid.uuid4().hex
    with _preview_lock:
        _previews[token] = {
            "version": version,
            "input_json": input_json,
            "result": result,
            "created_at": time.time(),
        }
        _previews.move_to_end(token)
        while len(_previews) > _PREVIEW_MAX:
            _previews.popitem(last=False)
    return token


def _get_preview(token: str) -> Dict[str, Any]:
    with _preview_lock:
        preview = _previews.get(token)
        if preview is None:
            raise StaleVersionError("该预览不存在或已因重启/过期被清除，不能发布，请重新求解。")
        current_version = db.get_version()
        if preview["version"] != current_version:
            raise StaleVersionError(
                f"预览依据的输入版本 v{preview['version']} 已变动为"
                f" v{current_version}，该预览已永久失效，不能发布，请重新求解。"
            )
        return preview


# --------------------------------------------------------------------------- #
# 请求模型
# --------------------------------------------------------------------------- #
class QuotasPayload(BaseModel):
    g1_label0: int = Field(ge=0)
    g1_label1: int = Field(ge=0)
    g2_label0: int = Field(ge=0)
    g2_label1: int = Field(ge=0)


class LocksPayload(BaseModel):
    locks: list  # [{"case_id": str, "group": 1|2}, ...]


class PublishPayload(BaseModel):
    preview_token: str


# --------------------------------------------------------------------------- #
# 异常处理
# --------------------------------------------------------------------------- #
@app.exception_handler(ValidationError)
async def _validation_handler(_request, exc: ValidationError):
    return JSONResponse(status_code=400, content={"detail": "输入校验失败", "errors": exc.errors})


@app.exception_handler(SplitterError)
async def _splitter_handler(_request, exc: SplitterError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(StaleVersionError)
async def _stale_handler(_request, exc: StaleVersionError):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


# --------------------------------------------------------------------------- #
# 状态与上传
# --------------------------------------------------------------------------- #
@app.get("/api/health")
def health():
    return {"status": "ok", "version": db.get_version()}


@app.get("/api/state")
def get_state():
    version, active = db.get_state()
    label_totals = {0: 0, 1: 0}
    for c in active.cases:
        label_totals[c.label] += 1
    return {
        "dataset_version": version,
        "label_totals": label_totals,
        "patient_count": len({c.patient_id for c in active.cases}),
        "input": active.to_json(),
        "manifests": db.list_manifests(),
    }


async def _read_upload(file: UploadFile) -> bytes:
    raw = await file.read()
    if not raw:
        raise ValidationError([f"上传文件 {file.filename!r} 为空。"])
    if len(raw) > 2 * 1024 * 1024:
        raise ValidationError(["上传文件超过 2MB 限制。"])
    return raw


@app.post("/api/upload/cases")
async def upload_cases(file: UploadFile = File(...)):
    raw = await _read_upload(file)
    # 解析/校验全部完成且无异常后才写库；失败不触碰源数据
    cases = parse_cases_csv(raw)
    new_version = db.replace_cases(cases)
    return {"dataset_version": new_version, "case_count": len(cases),
            "notice": "病例已替换；亲属关系与指向已删除病例的锁定已清空，需要时请重新设置。"}


@app.post("/api/upload/relatives")
async def upload_relatives(file: UploadFile = File(...)):
    raw = await _read_upload(file)
    _, active = db.get_state()
    if not active.cases:
        raise ValidationError(["请先上传病例 CSV，再上传亲属关系。"])
    relatives = parse_relatives_csv(raw, [c.patient_id for c in active.cases])
    new_version = db.replace_relatives(relatives)
    return {"dataset_version": new_version, "pair_count": len(relatives)}


@app.put("/api/quotas")
def update_quotas(payload: QuotasPayload):
    _, active = db.get_state()
    totals = {0: 0, 1: 0}
    for c in active.cases:
        totals[c.label] += 1
    quotas = validate_quotas(payload.model_dump(), totals)
    new_version = db.update_quotas(quotas)
    return {"dataset_version": new_version, "quotas": payload.model_dump()}


@app.put("/api/locks")
def update_locks(payload: LocksPayload):
    _, active = db.get_state()
    locks = validate_locks(payload.locks, [c.case_id for c in active.cases])
    new_version = db.update_locks(locks)
    return {"dataset_version": new_version, "lock_count": len(locks)}


# --------------------------------------------------------------------------- #
# 求解（预览）与发布
# --------------------------------------------------------------------------- #
@app.post("/api/solve")
def solve():
    # 先取输入快照与版本；z3 计算在数据库锁之外进行，计算期间允许其他请求改输入
    version, active = db.get_state()
    if not active.cases:
        raise ValidationError(["还没有病例数据，请先上传病例 CSV。"])
    input_snapshot = active.to_json()
    result = solve_split(
        active.cases, active.relatives, active.quotas, active.locks
    )
    # 计算结束后复核版本：若期间输入被改动，本次结果对应旧版本，拒绝生成预览
    current_version = db.get_version()
    if current_version != version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"计算期间输入已从 v{version} 变动到 v{current_version}，"
                "本次结果已作废且不会生成预览，请重新求解。"
            ),
        )
    token = None
    if result["status"] == "sat":
        token = _add_preview(version, input_snapshot, result)
    return {
        "dataset_version": version,
        "preview_token": token,
        "result": result,
        "input_snapshot": input_snapshot,
    }


@app.post("/api/publish")
def publish(payload: PublishPayload):
    preview = _get_preview(payload.preview_token)  # 过期/失效 -> 409
    result = preview["result"]
    if result.get("status") != "sat":
        raise HTTPException(status_code=400, detail="无解的预览不能发布。")
    manifest = db.publish_manifest(
        expected_version=preview["version"],
        input_data=preview["input_json"],
        result=result,
    )
    return manifest


@app.get("/api/manifests")
def manifests():
    return {"dataset_version": db.get_version(), "manifests": db.list_manifests()}


@app.get("/api/manifests/{manifest_id}")
def manifest_detail(manifest_id: str):
    manifest = db.get_manifest(manifest_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="清单不存在。")
    return manifest


@app.get("/api/manifests/{manifest_id}/download")
def manifest_download(manifest_id: str):
    manifest = db.get_manifest(manifest_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail="清单不存在。")
    if manifest["expired"]:
        raise HTTPException(
            status_code=409,
            detail=(
                "该清单依据的输入已变动，清单已永久失效，禁止下载。"
                "可在“已发布清单”中查看其历史记录与失效原因。"
            ),
        )
    result = manifest["result"]
    assignments = result["assignments"]
    locks = manifest["input"].get("locks", {})
    lines = ["病例号,患者号,标签,分组编号,分组名称,是否锁定"]
    for c in manifest["input"]["cases"]:
        cid = c["case_id"]
        grp = assignments[cid]
        locked = "是" if cid in locks else "否"
        lines.append(
            f"{cid},{c['patient_id']},{c['label']},{grp},"
            f"{'训练集' if grp == 1 else '评估集'},{locked}"
        )
    csv_text = "﻿" + "\n".join(lines) + "\n"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="split_manifest_{manifest_id[:8]}_v{manifest["dataset_version"]}.csv"'
            )
        },
    )


@app.get("/api/version-log")
def version_log():
    return {"current_version": db.get_version(), "log": db.version_log()}


# 静态前端（最后挂载，避免覆盖 /api 路由）
_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
