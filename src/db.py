"""
db.py
PostgreSQL 비동기 커넥션 풀 헬퍼 (LOGGING 봇 / anonymous_chat_cog.py 전용).
(MySQL(aiomysql) -> PostgreSQL(psycopg3) 마이그레이션 버전)

anonymous_chat_cog.py가 기대하는 인터페이스(init_pool / fetchone / execute)를
그대로 유지해서, 호출부 코드는 전혀 손대지 않아도 되도록 만들었다.

접속 정보 탐색 규칙은 src/utils/db.py(ROOM_MANAGER 봇 쪽)와 동일하다.
"""

from __future__ import annotations
import os
from typing import Any, Sequence
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

_pool: AsyncConnectionPool | None = None

_URL_ENV_CANDIDATES = ["DATABASE_URL", "DATABASE_PUBLIC_URL"]
_HOST_ENV_CANDIDATES = ["PGHOST", "POSTGRES_HOST", "DB_HOST"]
_PORT_ENV_CANDIDATES = ["PGPORT", "POSTGRES_PORT", "DB_PORT"]
_USER_ENV_CANDIDATES = ["PGUSER", "POSTGRES_USER", "DB_USER"]
_PASSWORD_ENV_CANDIDATES = ["PGPASSWORD", "POSTGRES_PASSWORD", "DB_PASSWORD"]
_DB_ENV_CANDIDATES = ["PGDATABASE", "POSTGRES_DB", "DB_NAME", "DB_DATABASE"]


def _first_env(candidates: list[str]) -> str | None:
    for name in candidates:
        val = os.getenv(name)
        if val:
            return val
    return None


def _resolve_conninfo() -> str:
    url_raw = _first_env(_URL_ENV_CANDIDATES)
    if url_raw:
        return url_raw

    host = _first_env(_HOST_ENV_CANDIDATES)
    user = _first_env(_USER_ENV_CANDIDATES)
    password = _first_env(_PASSWORD_ENV_CANDIDATES)
    db_name = _first_env(_DB_ENV_CANDIDATES)
    port = _first_env(_PORT_ENV_CANDIDATES) or "5432"

    if not host or not user or not db_name:
        checked = _URL_ENV_CANDIDATES + _HOST_ENV_CANDIDATES + _USER_ENV_CANDIDATES + _DB_ENV_CANDIDATES
        raise RuntimeError(
            "PostgreSQL 접속 정보를 환경변수에서 찾지 못했습니다. "
            "봇이 돌아가는 Railway 서비스의 Variables 탭에 DB 접속 정보가 "
            "참조(Reference)로 연결되어 있는지 확인하세요. "
            f"확인한 변수 이름: {', '.join(checked)}"
        )

    return f"host={host} port={port} user={user} password={password or ''} dbname={db_name}"


async def init_pool() -> None:
    """커넥션 풀을 초기화한다 (cog_load 등에서 최초 1회 호출). 이미 있으면 아무것도 안 함."""
    global _pool
    if _pool is None:
        conninfo = _resolve_conninfo()
        _pool = AsyncConnectionPool(conninfo, min_size=1, max_size=5, open=False)
        await _pool.open()


async def _get_pool() -> AsyncConnectionPool:
    if _pool is None:
        await init_pool()
    return _pool


async def fetchone(sql: str, params: Sequence[Any] = ()) -> dict | None:
    """SELECT 하나 실행해서 첫 행을 dict로 반환. 없으면 None. (aiomysql.DictCursor와 동일한 사용감)"""
    pool = await _get_pool()
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchone()


async def execute(sql: str, params: Sequence[Any] = ()) -> None:
    """INSERT/UPDATE/DELETE 등 결과를 안 돌려받는 쿼리 실행 (자동 커밋)."""
    pool = await _get_pool()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
        await conn.commit()


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None