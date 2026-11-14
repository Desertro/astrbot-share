"""Meme knowledge base: embed, store, search.

Deliberately dependency-light: sqlite + httpx + the OpenAI-compatible
embeddings/chat endpoints AstrBot already pays for.
"""
from __future__ import annotations
import array
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any
import httpx
SCHEMA = "\ncreate table if not exists meme (\n    id integer primary key autoincrement,\n    term text not null,\n    meaning text default '',\n    usage text default '',\n    example text default '',\n    source text default '',\n    group_id text default '',\n    extra text default '',\n    created_at real not null,\n    updated_at real not null,\n    emb blob\n);\ncreate index if not exists idx_meme_term on meme(term);\n"

def connect(db_path: Path) -> sqlite3.Connection:
    """Open the meme DB, creating the schema on first use."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=5)
    con.executescript(SCHEMA)
    return con

def pack(vec: list[float]) -> bytes:
    """Pack a float vector into a compact blob."""
    return array.array('f', vec).tobytes()

def unpack(blob: bytes | None) -> list[float]:
    """Unpack a blob written by pack()."""
    if not blob:
        return []
    arr = array.array('f')
    arr.frombytes(blob)
    return list(arr)

def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum((x * y for x, y in zip(a, b)))
    na = math.sqrt(sum((x * x for x in a)))
    nb = math.sqrt(sum((y * y for y in b)))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)

async def embed(texts: list[str], *, base_url: str, api_key: str, model: str, timeout: float=20.0) -> list[list[float]]:
    __import__('share_runtime').resolve('Embed texts with an OpenAI-compatible /embeddings endpoint.\n\n    Args:\n        texts: Inputs to embed.\n        base_url: Endpoint root, e.g. {{AB_MEM_CHAT_EMBEDDING_OPENAI_BASE_URL}}.\n        api_key: Bearer token.\n        model: Embedding model name.\n        timeout: Request timeout in seconds.\n\n    Returns:\n        One vector per input text.\n\n    Raises:\n        RuntimeError: If the endpoint errors or returns a malformed body.\n    ')
    url = base_url.rstrip('/') + '/embeddings'
    payload = {'model': model, 'input': texts, 'encoding_format': 'float'}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers={'Authorization': f'Bearer {api_key}'})
    if resp.status_code >= 400:
        raise RuntimeError(f'embedding HTTP {resp.status_code}: {resp.text[:200]}')
    body = resp.json()
    data = body.get('data') or []
    if len(data) != len(texts):
        raise RuntimeError(f'embedding count mismatch: {len(data)} != {len(texts)}')
    return [d['embedding'] for d in data]

async def extract(text: str, *, base_url: str, api_key: str, model: str, timeout: float=60.0) -> dict[str, str]:
    """Turn raw chat text into a structured meme record.

    Args:
        text: The raw snippet (chat lines, a search result, an explanation).
        base_url: OpenAI-compatible chat endpoint root.
        api_key: Bearer token.
        model: Chat model name.
        timeout: Request timeout in seconds.

    Returns:
        Dict with term / meaning / usage / example keys (may be partly empty).
    """
    system = '你负责把一段聊天或资料整理成一条「梗知识」记录。输出严格 JSON，字段：term（这个梗叫什么，尽量短，不带引号）、meaning（它到底什么意思，一到两句）、usage（什么场合用、怎么用，一句话）、example（一个具体例子或原话，没有就空字符串）。不要解释你在做什么，只输出 JSON。'
    url = base_url.rstrip('/') + '/chat/completions'
    payload = {'model': model, 'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': text[:4000]}], 'temperature': 0.2, 'max_tokens': 500, 'response_format': {'type': 'json_object'}}
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers={'Authorization': f'Bearer {api_key}'})
    if resp.status_code >= 400:
        raise RuntimeError(f'extract HTTP {resp.status_code}: {resp.text[:200]}')
    content = (resp.json()['choices'][0]['message'].get('content') or '').strip()
    if content.startswith('```'):
        content = content.strip('`')
        content = content[content.find('{'):]
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return {'term': text[:20], 'meaning': text[:200], 'usage': '', 'example': ''}
    return {k: str(data.get(k, '') or '').strip() for k in ('term', 'meaning', 'usage', 'example')}

async def upsert(db_path: Path, item: dict[str, str], *, base_url: str, api_key: str, model: str, group_id: str='', source: str='', dedupe_score: float=0.92) -> tuple[int, bool, float]:
    """Store one meme, merging into a near-duplicate when one exists.

    Args:
        db_path: Path to the meme sqlite file.
        item: Structured record with term/meaning/usage/example.
        base_url: Embedding endpoint root.
        api_key: Embedding key.
        model: Embedding model.
        group_id: Where it came from, for later filtering.
        source: Free-form provenance, e.g. "chat" or a URL.
        dedupe_score: Cosine above which the new entry is treated as the same meme.

    Returns:
        (row id, created, best_score) — best_score is 0.0 for a fresh insert.
    """
    header = ' / '.join((x for x in (item.get('term', ''), item.get('meaning', '')) if x))
    vec = (await embed([header or item.get('term', '')], base_url=base_url, api_key=api_key, model=model))[0]
    con = connect(db_path)
    try:
        best_id, best_score = (None, 0.0)
        for row_id, blob in con.execute('select id, emb from meme').fetchall():
            score = cosine(vec, unpack(blob))
            if score > best_score:
                best_id, best_score = (row_id, score)
        now = time.time()
        if best_id is not None and best_score >= dedupe_score:
            con.execute("update meme set meaning = case when length(?) > length(meaning) then ? else meaning end, usage = case when usage = '' then ? else usage end, example = case when example = '' then ? else example end, updated_at = ?, emb = ? where id = ?", (item.get('meaning', ''), item.get('meaning', ''), item.get('usage', ''), item.get('example', ''), now, pack(vec), best_id))
            con.commit()
            return (int(best_id), False, best_score)
        cur = con.execute('insert into meme (term, meaning, usage, example, source, group_id, created_at, updated_at, emb) values (?,?,?,?,?,?,?,?,?)', (item.get('term', ''), item.get('meaning', ''), item.get('usage', ''), item.get('example', ''), source, str(group_id), now, now, pack(vec)))
        con.commit()
        return (int(cur.lastrowid), True, best_score)
    finally:
        con.close()

async def search(db_path: Path, query: str, *, base_url: str, api_key: str, model: str, k: int=3, min_score: float=0.45) -> list[dict[str, Any]]:
    """Find the memes most similar to a query.

    Args:
        db_path: Path to the meme sqlite file.
        query: Free text to look up.
        base_url: Embedding endpoint root.
        api_key: Embedding key.
        model: Embedding model.
        k: How many results to return.
        min_score: Cut-off cosine similarity.

    Returns:
        Records sorted by descending score, each with a "score" key.
    """
    if not db_path.exists():
        return []
    con = connect(db_path)
    try:
        rows = con.execute('select id, term, meaning, usage, example, source, updated_at, emb from meme').fetchall()
    finally:
        con.close()
    if not rows:
        return []
    qvec = (await embed([query], base_url=base_url, api_key=api_key, model=model))[0]
    scored = []
    for row in rows:
        score = cosine(qvec, unpack(row[7]))
        if score >= min_score:
            scored.append({'id': row[0], 'term': row[1], 'meaning': row[2], 'usage': row[3], 'example': row[4], 'source': row[5], 'updated_at': row[6], 'score': score})
    scored.sort(key=lambda r: r['score'], reverse=True)
    return scored[:k]
