"""Durable queue shared by the container API and the host GPU worker."""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

DATA = Path(os.environ.get('DATA_DIR', '/data'))


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / 'studio.sqlite3', timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=30000')
    return db


def init():
    for name in ('uploads', 'outputs', 'logs'):
        (DATA / name).mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, params TEXT NOT NULL, status TEXT NOT NULL,
          progress INTEGER DEFAULT 0, stage TEXT DEFAULT '等待调度',
          created REAL NOT NULL, started REAL, finished REAL,
          error TEXT, output TEXT, cancel_requested INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
        ''')


def unpack(row):
    if row is None:
        return None
    result = dict(row)
    result['params'] = json.loads(result['params'])
    result['video_url'] = f"/api/jobs/{result['id']}/video" if result['status'] == 'succeeded' else None
    result.pop('output', None)
    return result


def get(job_id):
    with connect() as db:
        return unpack(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


def create(params):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0] >= 8:
            raise ValueError('队列已满，最多同时保留 8 个待处理任务')
        job_id = uuid.uuid4().hex
        db.execute('INSERT INTO jobs(id, params, status, created) VALUES(?,?,?,?)',
                   (job_id, json.dumps(params, ensure_ascii=False), 'queued', time.time()))
    return get(job_id)


def update(job_id, **values):
    allowed = {'status', 'progress', 'stage', 'started', 'finished', 'error', 'output', 'cancel_requested'}
    if not values or not set(values).issubset(allowed):
        raise ValueError('Invalid update')
    with connect() as db:
        db.execute('UPDATE jobs SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE id=?',
                   (*values.values(), job_id))


def cancel(job_id):
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row:
            return None
        if row['status'] == 'queued':
            db.execute("UPDATE jobs SET status='cancelled',stage='已取消',finished=?,cancel_requested=1 WHERE id=?",
                       (time.time(), job_id))
        elif row['status'] == 'running':
            db.execute("UPDATE jobs SET cancel_requested=1,stage='正在停止推理' WHERE id=?", (job_id,))
    return get(job_id)


def claim():
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if row is None:
            return None
        db.execute("UPDATE jobs SET status='running',started=?,stage='加载模型',progress=1 WHERE id=?",
                   (time.time(), row['id']))
    return get(row['id'])


def heartbeat(info):
    with connect() as db:
        db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('worker',?)", (json.dumps(info),))


def worker_info():
    with connect() as db:
        row = db.execute("SELECT value FROM meta WHERE key='worker'").fetchone()
    info = json.loads(row[0]) if row else {}
    info['online'] = time.time() - info.get('heartbeat', 0) < 25
    return info
