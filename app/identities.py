"""人员标识与历史别名登记（质控复核回避）。

规则（与 README 一致）：
  * 人员以全局唯一主标识（person_id）登记，登记时可携带初始历史别名，也可事后增补；
  * 主标识与全部别名构成全局唯一命名空间：任何名称只归属一人——
    别名不得归属多人，也不得转移归属（不提供删除/转移）；
  * 增补别名须携带所见修订号（expected_revision）：登记后修订号为 1，每次成功增补 +1；
    所见修订号与当前不一致（过期）→ 409 identity_revision_stale；
  * 同主标识同内容（含初始别名集合，顺序不敏感）登记重试 → 幂等 200；异内容 → 409；
  * 复核回避检查把姓名解析到人员归属后比较；未登记姓名按原字符串比较。
"""
import sqlite3
from typing import Any

from .db import utcnow_iso
from .schemas import AliasIn, PersonIn
from .service import DomainError, _fingerprint


# --------------------------------------------------------------------------- 查询视图

def _person_view(conn: sqlite3.Connection, person: sqlite3.Row) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT alias FROM person_alias WHERE person_pk = ? ORDER BY id ASC",
        (person["id"],),
    ).fetchall()
    return {
        "person_id": person["person_id"],
        "revision": person["revision"],
        "aliases": [r["alias"] for r in rows],
        "created_at": person["created_at"],
    }


def get_person(conn: sqlite3.Connection, person_id: str) -> dict[str, Any]:
    """人员查询：主标识、当前修订号与全部已登记别名。"""
    row = conn.execute("SELECT * FROM person WHERE person_id = ?", (person_id,)).fetchone()
    if row is None:
        raise DomainError(404, "person_not_found", f"人员标识不存在: {person_id}")
    return _person_view(conn, row)


def list_persons(conn: sqlite3.Connection) -> dict[str, Any]:
    """全部已登记人员（含别名与当前修订号）。"""
    rows = conn.execute("SELECT * FROM person ORDER BY id ASC").fetchall()
    items = [_person_view(conn, r) for r in rows]
    return {"total": len(items), "items": items}


# --------------------------------------------------------------------------- 名称解析（复核回避用）

def _name_owner(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    """名称（主标识或别名）当前归属的人员行；未登记为 None。"""
    row = conn.execute("SELECT * FROM person WHERE person_id = ?", (name,)).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT p.* FROM person p JOIN person_alias pa ON pa.person_pk = p.id WHERE pa.alias = ?",
        (name,),
    ).fetchone()


def resolve_person_id(
    conn: sqlite3.Connection, name: str, cache: dict[str, str | None] | None = None
) -> str | None:
    """把姓名解析为人员标识（主标识或别名均可命中）；未登记为 None。

    cache 供一次批量检查内的重复查询去重（如复核回避逐份项逐角色比对）。
    """
    if cache is not None and name in cache:
        return cache[name]
    owner = _name_owner(conn, name)
    person_id = owner["person_id"] if owner is not None else None
    if cache is not None:
        cache[name] = person_id
    return person_id


def same_person(
    conn: sqlite3.Connection,
    name_a: str,
    name_b: str,
    cache: dict[str, str | None] | None = None,
) -> bool:
    """两个姓名是否同一人：均登记则按人员归属比较；任一方未登记则按原字符串比较。"""
    if name_a == name_b:
        return True
    pa = resolve_person_id(conn, name_a, cache)
    pb = resolve_person_id(conn, name_b, cache)
    return pa is not None and pb is not None and pa == pb


# --------------------------------------------------------------------------- 登记与增补别名

def register_person(conn: sqlite3.Connection, data: PersonIn) -> tuple[dict[str, Any], bool]:
    """登记人员标识。返回 (人员视图, 是否幂等重试)。

    同主标识同内容（含初始别名集合）重试 → 幂等返回；异内容 → 409 person_conflict。
    主标识或任一初始别名已被占用（他人主标识或别名）→ 409 alias_conflict。
    """
    canonical = {"person_id": data.person_id, "aliases": sorted(data.aliases)}
    request_hash = _fingerprint(canonical)
    existing = conn.execute(
        "SELECT * FROM person WHERE person_id = ?", (data.person_id,)
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise DomainError(
                409,
                "person_conflict",
                f"人员标识 {data.person_id} 已登记但登记内容不一致（同标识异内容冲突）",
                {"person_id": data.person_id,
                 "existing_request_hash": existing["request_hash"],
                 "incoming_request_hash": request_hash},
            )
        return _person_view(conn, existing), True

    # 主标识与别名构成全局唯一命名空间：任何名称不得已归属他人（不得归属多人/转移归属）
    for name in [data.person_id, *data.aliases]:
        owner = _name_owner(conn, name)
        if owner is not None:
            raise DomainError(
                409,
                "alias_conflict",
                f"名称 {name} 已归属人员 {owner['person_id']}，别名不得归属多人或转移归属",
                {"name": name, "owner_person_id": owner["person_id"]},
            )

    created_at = utcnow_iso()
    try:
        cur = conn.execute(
            "INSERT INTO person (person_id, revision, request_hash, created_at) VALUES (?,?,?,?)",
            (data.person_id, 1, request_hash, created_at),
        )
    except sqlite3.IntegrityError:
        # 并发兜底：主标识唯一键竞争 → 按已提交结果判定
        existing = conn.execute(
            "SELECT * FROM person WHERE person_id = ?", (data.person_id,)
        ).fetchone()
        if existing is not None and existing["request_hash"] == request_hash:
            return _person_view(conn, existing), True
        raise DomainError(409, "person_conflict", f"人员标识 {data.person_id} 并发写入且内容冲突")
    person_pk = cur.lastrowid
    for alias in data.aliases:
        conn.execute(
            "INSERT INTO person_alias (person_pk, alias, created_at) VALUES (?,?,?)",
            (person_pk, alias, created_at),
        )
    person = conn.execute("SELECT * FROM person WHERE id = ?", (person_pk,)).fetchone()
    return _person_view(conn, person), False


def add_alias(conn: sqlite3.Connection, person_id: str, data: AliasIn) -> dict[str, Any]:
    """增补历史别名：须携带所见修订号，过期（与当前修订号不一致）则拒绝。

    成功：别名落库且修订号 +1；别名已归属任何人（含本人、含任何主标识）→ 409 alias_conflict。
    """
    person = conn.execute("SELECT * FROM person WHERE person_id = ?", (person_id,)).fetchone()
    if person is None:
        raise DomainError(404, "person_not_found", f"人员标识不存在: {person_id}")
    if data.expected_revision != person["revision"]:
        raise DomainError(
            409,
            "identity_revision_stale",
            f"所见修订号已过期：人员 {person_id} 当前修订号为 {person['revision']}，"
            f"提交携带 {data.expected_revision}，请重新查询人员信息后再增补别名",
            {"person_id": person_id,
             "expected_revision": data.expected_revision,
             "current_revision": person["revision"]},
        )
    owner = _name_owner(conn, data.alias)
    if owner is not None:
        raise DomainError(
            409,
            "alias_conflict",
            f"别名 {data.alias} 已归属人员 {owner['person_id']}，别名不得归属多人或转移归属",
            {"alias": data.alias, "owner_person_id": owner["person_id"], "person_id": person_id},
        )
    try:
        conn.execute(
            "INSERT INTO person_alias (person_pk, alias, created_at) VALUES (?,?,?)",
            (person["id"], data.alias, utcnow_iso()),
        )
    except sqlite3.IntegrityError:
        # 并发兜底：别名唯一键竞争 → 按已提交结果判定
        raise DomainError(
            409,
            "alias_conflict",
            f"别名 {data.alias} 并发写入冲突，别名不得归属多人或转移归属",
            {"alias": data.alias},
        )
    conn.execute("UPDATE person SET revision = revision + 1 WHERE id = ?", (person["id"],))
    person = conn.execute("SELECT * FROM person WHERE id = ?", (person["id"],)).fetchone()
    return _person_view(conn, person)
