"""
Railway MySQL 커넥션 풀 공용 모듈.

여러 cog에서 재사용할 수 있도록 bot.py가 아닌 별도 모듈로 분리했습니다.
사용 전에 반드시 `await init_pool()`을 호출해야 하며 (보통 cog의 cog_load에서 호출),
이미 풀이 생성되어 있다면 기존 풀을 그대로 반환합니다.

필요한 환경변수 (Railway MySQL 플러그인이 자동으로 넣어줌):
    MYSQLHOST, MYSQLPORT, MYSQLUSER, MYSQLPASSWORD, MYSQLDATABASE
"""

import os
from typing import Optional

import aiomysql

_pool: Optional[aiomysql.Pool] = None


async def init_pool() -> aiomysql.Pool:
    """MySQL 커넥션 풀을 생성합니다. 이미 있으면 기존 풀을 그대로 반환합니다."""
    global _pool
    if _pool is not None:
        return _pool

    _pool = await aiomysql.create_pool(
        host=os.getenv("MYSQLHOST"),
        port=int(os.getenv("MYSQLPORT", "3306")),
        user=os.getenv("MYSQLUSER"),
        password=os.getenv("MYSQLPASSWORD"),
        db=os.getenv("MYSQLDATABASE"),
        autocommit=True,
        minsize=1,
        maxsize=5,
    )
    return _pool


async def get_pool() -> aiomysql.Pool:
    if _pool is None:
        return await init_pool()
    return _pool


async def close_pool() -> None:
    """봇 종료 시 호출해서 커넥션 풀을 정리합니다 (선택사항)."""
    global _pool
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
        _pool = None


async def fetchone(query: str, args: tuple = ()) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, args)
            return await cur.fetchone()


async def fetchall(query: str, args: tuple = ()) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(query, args)
            return await cur.fetchall()


async def execute(query: str, args: tuple = ()) -> int:
    """INSERT / UPDATE / DELETE 실행. 영향받은 row 수를 반환합니다."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, args)
            return cur.rowcount
