"""데이터셋(ANA-10.01, BR-ANA-21): 바인딩·기간·품질 필터를 이름 붙여 저장하고 바꿀 때마다 버전을 올린다.

분석은 저장 당시 버전(analyses.dataset_version)을 쓰고, upgrade-dataset으로만 최신 버전으로 올린다.
"""

from __future__ import annotations

from ..clock import iso
from ..common.errors import BusinessError, ErrorCode, FieldError
from ..common.jsonutil import to_jsonable
from ..loader.bindings import parse_bindings, resolve_period
from . import repository as repo
from .service import listing, page_args

S = repo.S


def get_dataset_row(conn, org: int, dataset_id: int) -> dict:
    row = conn.execute(f"SELECT * FROM {S}.datasets WHERE id = %s AND organization_id = %s", (dataset_id, org)).fetchone()
    if row is None:
        raise BusinessError(ErrorCode.DATASET_NOT_FOUND)
    return row


def _view(row: dict) -> dict:
    return to_jsonable({"datasetId": str(row["id"]), "name": row["name"], "bindings": row["bindings"], "period": row["period"],
                        "qualityFilter": row["quality_filter"], "includeVirtual": row["include_virtual"], "version": row["version"],
                        "createdBy": {"userId": str(row["created_by"])} if row.get("created_by") is not None else None,
                        "createdAt": iso(row["created_at"]), "updatedAt": iso(row["updated_at"])})


def _validate(body: dict, now) -> tuple[str, list, dict, str, bool]:
    name = (body.get("name") or "").strip()
    if not (1 <= len(name) <= 100):
        raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("name", "Size", "이름은 1~100자")])
    parse_bindings(body.get("bindings"))
    resolve_period(body.get("period"), now)
    quality = body.get("qualityFilter", "NORMAL_ONLY")
    if quality not in ("NORMAL_ONLY", "INCLUDE_ALL"):
        raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("qualityFilter", "Enum", "NORMAL_ONLY·INCLUDE_ALL")])
    return name, body["bindings"], body["period"], quality, bool(body.get("includeVirtual", False))


class DatasetService:
    def __init__(self, deps):
        self.deps = deps

    def create(self, org: int, user: int | None, body: dict) -> dict:
        now = self.deps.clock.now()
        name, bindings, period, quality, virtual = _validate(body, now)
        with self.deps.pool.connection() as conn:
            row = conn.execute(
                f"""INSERT INTO {S}.datasets (organization_id, name, bindings, period, quality_filter, include_virtual, version, created_by,
                       updated_by, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s) RETURNING *""",
                (org, name, repo.j(bindings), repo.j(period), quality, virtual, user, user, now, now)).fetchone()
            self._snapshot(conn, row, user, now)
        return _view(row)

    def _snapshot(self, conn, row: dict, user, now) -> None:
        conn.execute(f"""INSERT INTO {S}.dataset_versions (organization_id, dataset_id, version, bindings, period, quality_filter,
                            include_virtual, created_by, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (row["organization_id"], row["id"], row["version"], repo.j(row["bindings"]), repo.j(row["period"]),
                      row["quality_filter"], row["include_virtual"], user, now))

    def get(self, org: int, dataset_id: int) -> dict:
        with self.deps.pool.connection() as conn:
            return _view(get_dataset_row(conn, org, dataset_id))

    def update(self, org: int, user: int | None, dataset_id: int, body: dict) -> dict:
        now = self.deps.clock.now()
        name, bindings, period, quality, virtual = _validate(body, now)
        base = body.get("baseVersion")
        if base is None:
            raise BusinessError(ErrorCode.INVALID_REQUEST, errors=[FieldError("baseVersion", "NotNull", "baseVersion이 필요합니다")])
        with self.deps.pool.connection() as conn:
            get_dataset_row(conn, org, dataset_id)
            row = conn.execute(
                f"""UPDATE {S}.datasets SET name = %s, bindings = %s, period = %s, quality_filter = %s, include_virtual = %s,
                       version = version + 1, updated_by = %s, updated_at = %s
                    WHERE id = %s AND organization_id = %s AND version = %s RETURNING *""",
                (name, repo.j(bindings), repo.j(period), quality, virtual, user, now, dataset_id, org, int(base))).fetchone()
            if row is None:
                raise BusinessError(ErrorCode.VERSION_CONFLICT)
            self._snapshot(conn, row, user, now)
        return _view(row)

    def delete(self, org: int, dataset_id: int) -> None:
        with self.deps.pool.connection() as conn:
            get_dataset_row(conn, org, dataset_id)
            used = conn.execute(f"SELECT 1 FROM {S}.analyses WHERE dataset_id = %s LIMIT 1", (dataset_id,)).fetchone()
            if used:
                raise BusinessError(ErrorCode.DATASET_IN_USE)
            conn.execute(f"DELETE FROM {S}.datasets WHERE id = %s AND organization_id = %s", (dataset_id, org))

    def list(self, org: int, page: int | None, size: int | None) -> dict:
        p, s = page_args(page, size)
        with self.deps.pool.connection() as conn:
            total = conn.execute(f"SELECT count(*) AS n FROM {S}.datasets WHERE organization_id = %s", (org,)).fetchone()["n"]
            rows = conn.execute(
                f"""SELECT d.*, jsonb_array_length(d.bindings) AS binding_count,
                       (SELECT count(*) FROM {S}.analyses a WHERE a.dataset_id = d.id AND a.status = 'ACTIVE') AS analysis_count
                    FROM {S}.datasets d WHERE d.organization_id = %s ORDER BY d.updated_at DESC, d.id DESC LIMIT %s OFFSET %s""",
                (org, s, (p - 1) * s)).fetchall()
        items = [to_jsonable({"datasetId": str(r["id"]), "name": r["name"], "bindingCount": r["binding_count"], "version": r["version"],
                              "analysisCount": r["analysis_count"], "updatedAt": iso(r["updated_at"])}) for r in rows]
        return listing(items, total, p, s)

    def versions(self, org: int, dataset_id: int, page: int | None, size: int | None) -> dict:
        p, s = page_args(page, size)
        with self.deps.pool.connection() as conn:
            get_dataset_row(conn, org, dataset_id)
            total = conn.execute(f"SELECT count(*) AS n FROM {S}.dataset_versions WHERE dataset_id = %s", (dataset_id,)).fetchone()["n"]
            rows = conn.execute(f"""SELECT version, jsonb_array_length(bindings) AS binding_count, created_by, created_at
                                    FROM {S}.dataset_versions WHERE dataset_id = %s ORDER BY version DESC LIMIT %s OFFSET %s""",
                                (dataset_id, s, (p - 1) * s)).fetchall()
        items = [to_jsonable({"version": r["version"], "bindingCount": r["binding_count"],
                              "updatedBy": {"userId": str(r["created_by"])} if r["created_by"] is not None else None,
                              "updatedAt": iso(r["created_at"])}) for r in rows]
        return listing(items, total, p, s)

    def upgrade_analysis(self, org: int, dataset_id: int, analysis_id: int) -> dict:
        now = self.deps.clock.now()
        with self.deps.pool.connection() as conn:
            ds = get_dataset_row(conn, org, dataset_id)
            row = conn.execute(f"""UPDATE {S}.analyses SET dataset_version = %s, bindings = %s, period = %s, quality_filter = %s,
                                      include_virtual = %s, updated_at = %s, version = version + 1
                                   WHERE id = %s AND organization_id = %s AND dataset_id = %s AND status = 'ACTIVE' RETURNING id""",
                               (ds["version"], repo.j(ds["bindings"]), repo.j(ds["period"]), ds["quality_filter"], ds["include_virtual"],
                                now, analysis_id, org, dataset_id)).fetchone()
            if row is None:
                raise BusinessError(ErrorCode.ANALYSIS_NOT_FOUND)
        return {"analysisId": str(analysis_id), "datasetId": str(dataset_id), "datasetVersion": ds["version"]}
