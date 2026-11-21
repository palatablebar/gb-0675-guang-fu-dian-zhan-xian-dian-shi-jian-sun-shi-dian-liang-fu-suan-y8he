import sqlite3, json
from datetime import datetime, timezone
from pathlib import Path
from app.errors import BusinessError
from app.settlement import finalization_payload, finalization_digest, build

def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con=sqlite3.connect(path,timeout=15)
    con.row_factory=sqlite3.Row
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA busy_timeout=15000')
    con.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, digest TEXT UNIQUE NOT NULL, result TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT '待复核')")
    # 事件定稿独立于批次：重试同一事件按结果摘要去重；结果变化则叠加新版本，
    # 旧版只标记“已替换”而不删除，切换失败或未定时仍可回看已确认结果。
    con.execute("CREATE TABLE IF NOT EXISTS finalizations (id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL, run_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT '已定稿', supersedes INTEGER, created_at TEXT NOT NULL, UNIQUE(event_key,version))")
    con.execute('CREATE INDEX IF NOT EXISTS finalizations_digest ON finalizations(digest)')
    return con

def save(path, result):
    run_id=result['manifest']['run_id']
    with connect(path) as con:
        con.execute('INSERT OR IGNORE INTO runs(id,digest,result) VALUES(?,?,?)', (run_id,result['manifest']['sha256'],json.dumps(result,ensure_ascii=False)))
    return get(path,run_id)

def get(path, run_id):
    with connect(path) as con: row=con.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not row: raise BusinessError('NOT_FOUND','批次不存在',404)
    result=json.loads(row['result']);result.update(revision=row['revision'],status=row['status'])
    # 规则升级前保存的批次没有事件结算字段，读取时按当前口径补算，页面不会空白。
    if 'event_settlements' not in result and 'energy_rows' in result:
        build(result)
    keys=[s['event_key'] for s in result.get('event_settlements',[])]
    result['finalizations']=[f for f in finalizations(path) if f['event_key'] in keys]
    return result

def history(path):
    with connect(path) as con:
        return [dict(r) for r in con.execute('SELECT id,revision,status FROM runs ORDER BY rowid DESC LIMIT 100')]

def review(path,run_id,revision):
    with connect(path) as con:
        cur=con.execute('UPDATE runs SET revision=revision+1,status=? WHERE id=? AND revision=?',('已复核',run_id,revision))
        if cur.rowcount!=1:
            if not con.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():
                raise BusinessError('NOT_FOUND','批次不存在',404)
            raise BusinessError('STALE_VERSION','复核版本已更新，请刷新',409)
    return get(path,run_id)

def _row_to_finalization(row):
    record=json.loads(row['payload'])
    record.update(finalization_id=row['id'],event_key=row['event_key'],version=row['version'],
                  digest=row['digest'],run_id=row['run_id'],finalization_status=row['status'],
                  supersedes=row['supersedes'],created_at=row['created_at'])
    return record

def finalize(path, run_id, settlements):
    """为已结算事件生成定稿。

    幂等：事件最新版本的结果摘要与本次一致时直接复用，不新增记录；
    重试同一事件（含切换样例后重试）不会重复生成定稿。
    叠加：结果变化时写入 version+1 并把旧版标记为“已替换”，旧版不删除。
    未定事件不写入、也不删除既有定稿，因此切换失败时已确认结果仍在。
    """
    created,reused,pending=[],[],[]
    now=datetime.now(timezone.utc).isoformat()
    with connect(path) as con:
        # 串行化同一库的定稿写入，避免并发重试各自算出同一 version 而撞唯一键。
        con.execute('BEGIN IMMEDIATE')
        for settlement in settlements:
            if settlement.get('status')!='SETTLED':
                pending.append(settlement['event_key']);continue
            payload=finalization_payload(run_id,settlement);digest=finalization_digest(payload)
            latest=con.execute('SELECT * FROM finalizations WHERE event_key=? ORDER BY version DESC LIMIT 1',(settlement['event_key'],)).fetchone()
            if latest and latest['digest']==digest:
                reused.append(_row_to_finalization(latest));continue
            version=1 if latest is None else latest['version']+1
            supersedes=None if latest is None else latest['id']
            cur=con.execute('INSERT INTO finalizations(event_key,version,digest,payload,run_id,status,supersedes,created_at) VALUES(?,?,?,?,?,?,?,?)',
                            (settlement['event_key'],version,digest,json.dumps(payload,ensure_ascii=False),run_id,'已定稿',supersedes,now))
            if supersedes is not None:
                con.execute('UPDATE finalizations SET status=? WHERE id=?',('已替换',supersedes))
            created.append(_row_to_finalization(con.execute('SELECT * FROM finalizations WHERE id=?',(cur.lastrowid,)).fetchone()))
    return {'run_id':run_id,'created':created,'reused':reused,'pending_events':pending}

def finalizations(path,event_key=None):
    with connect(path) as con:
        if event_key:
            rows=con.execute('SELECT * FROM finalizations WHERE event_key=? ORDER BY version DESC',(event_key,)).fetchall()
        else:
            rows=con.execute('SELECT * FROM finalizations ORDER BY id DESC').fetchall()
    return [_row_to_finalization(r) for r in rows]

def latest_finalizations(path,event_keys=None):
    with connect(path) as con:
        rows=con.execute('SELECT * FROM finalizations ORDER BY version DESC').fetchall()
    latest={}
    for row in rows:
        if row['event_key'] in latest:continue
        if event_keys is not None and row['event_key'] not in event_keys:continue
        latest[row['event_key']]=_row_to_finalization(row)
    return latest
