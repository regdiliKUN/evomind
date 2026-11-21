"""Shared, deterministic text identity and completeness rules."""
import re
import unicodedata
import hashlib
import json
import time
import uuid

import numpy as np

from . import evidence


def normal(text):
    text = unicodedata.normalize('NFC', text or '').replace('\r\n', '\n')
    if '```' in text or re.search(r'(?m)^ {2,}\S', text):
        return text.strip('\r\n')
    return ' '.join(text.split())


def equivalent(a, b):
    if normal(a.get('claim')) != normal(b.get('claim')):
        return False
    da, db = normal(a.get('detail')), normal(b.get('detail'))
    if da and db and conflicts(a, b):
        return False
    return da == db or not da or not db or da in db or db in da


def conflicts(a, b):
    """Do not automatically collapse opposing conditions, numbers or code."""
    for field in ('claim', 'detail'):
        left, right = normal(a.get(field)), normal(b.get(field))
        if not left or not right or left == right:
            continue
        if '```' in left or '```' in right:
            return True
        numbers = lambda text: set(re.findall(r'(?<![A-Za-z0-9_])[+-]?\d+(?:\.\d+)?%?', text))
        na, nb = numbers(left), numbers(right)
        if na and nb and not (na <= nb or nb <= na):
            return True
        negative = lambda text: bool(re.search(r'\b(?:not|never|no|without|cannot)\b|不|禁止|无需|无须|不能|未', text, re.I))
        if negative(left) != negative(right):
            return True
    return False


def preference(p):
    # More supporting content wins; an equal amount of content keeps the oldest.
    return (-len(normal(p.get('detail'))), -len(normal(p.get('claim'))),
            p.get('created_at') or 0., p.get('id', ''))


def merge_items(items):
    result = []
    for item in items:
        match = next((p for p in result if equivalent(p, item)), None)
        if match is None:
            result.append(dict(item))
        else:
            ids = list(dict.fromkeys(match['source_ids'] + item['source_ids']))
            if preference(item) < preference(match):
                match.update(item)
            match['source_ids'] = ids
    return result


def provenance(c, pid, point):
    """Immutable snapshot of a merged-away source, including its evidence.

    The old payload recorded only five text fields, so after dedup there was no
    way to tell which citation supported the surviving claim. Keep the same
    table shape and widen the JSON payload — no migration needed.
    """
    fields = ('claim', 'detail', 'source_url', 'source_title', 'source_kind', 'book_id',
              'evidence', 'evidence_hash', 'evidence_locator', 'verification', 'status',
              'model_version', 'embed_model_version', 'created_by', 'confidence')
    data = {k: point.get(k, '') for k in fields}
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    # 去重键只用「身份字段」。status / revision / verification 会在合并过程中
    # 被就地更新，若把它们算进 key，同一份来源会被重复记成多条 merged_sources。
    # 不含 source_kind：候选 dict 里常常没这个字段（''），库里读回却是默认值
    # 'web'，同一个来源会被算成两条。它也不参与「这两份内容是不是同一份」。
    identity = {k: data[k] for k in
                ('claim', 'detail', 'source_url', 'source_title', 'book_id', 'evidence')}
    key = hashlib.sha256(
        (pid + json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)).encode()
    ).hexdigest()
    c.execute('INSERT OR IGNORE INTO knowledge_provenance VALUES (?,?,?)', (key, pid, payload))


def ingest(points, vectors, *, session_id='', book_id='', dedup=True):
    from . import memory, config
    Q = memory._normalize(np.stack(vectors))
    threshold = float(config.get('dedup_threshold', .93))
    accepted, accepted_rows, duplicates = [], [], 0
    with memory._lock:
        c = memory.conn()
        rids, mat = memory._all_vectors(Q.shape[1])
        # _all_vectors 含 archived / disputed / superseded 行；同文查重已带
        # ACTIVE_FILTER，语义查重也必须只与「可检索」旧行比对，否则一条
        # 已归档的近重复会成为 target：新知识点不入库，反而把旧行改写。
        life = memory.active_mask(rids) if dedup else None
        scores = mat @ Q.T if rids and dedup else np.empty((0, len(points)))
        intra = Q @ Q.T if dedup else None
        # Sort richer candidates first, preserving input order on ties.
        order = sorted(range(len(points)), key=lambda i: preference(points[i])) if dedup else range(len(points))
        changed = False
        # 查重只需要「命中的那几行」，不是整张表。原来的实现每次入库都把
        # 1.7 万条记录（含 4KB 的 embedding blob，合计 70MB）读成 Python 字典，
        # 再逐条 normal() 建索引——一批 30 条候选要花 300ms 以上，随库线性变慢。
        # 现在：同文查重交给 SQL 的 IN 查询，语义查重靠已缓存的向量矩阵，
        # 行内容按 rowid 懒加载（命中通常只有 0-2 行）。
        cols = ('id,claim,detail,source_url,source_title,source_kind,tags,confidence,created_at,'
                'status,revision,evidence,evidence_hash,evidence_locator,verification')
        row_cache: dict[int, dict] = {}

        def row(rid: int) -> dict:
            got = row_cache.get(rid)
            if got is None:
                r = c.execute(
                    f'SELECT rowid AS _rid,{cols} FROM knowledge WHERE rowid=?', (rid,)
                ).fetchone()
                got = dict(r) if r else {}
                row_cache[rid] = got
            return got

        by_claim: dict[str, list[int]] = {}
        if dedup:
            wanted = list(dict.fromkeys(normal(p['claim']) for p in points))
            for start in range(0, len(wanted), 400):
                part = wanted[start:start + 400]
                marks = ','.join('?' * len(part))
                for r in c.execute(
                    f'SELECT rowid AS _rid,claim FROM knowledge '
                    f'WHERE claim IN ({marks}) AND {evidence.ACTIVE_FILTER}', part
                ):
                    by_claim.setdefault(normal(r['claim']), []).append(r['_rid'])
        with c:
            for i in order:
                p = dict(points[i], claim=normal(points[i]['claim']), detail=normal(points[i].get('detail')),
                         topic=memory._norm_topic(points[i].get('topic', '')), book_id=book_id,
                         session_id=session_id, created_at=time.time())
                key = normal(p['claim'])
                target = None
                if dedup:
                    target = next((row(rid) for rid in by_claim.get(key, []) if equivalent(row(rid), p)), None)
                    if target is None:
                        # 用 numpy 挑出过阈值的行，别在 Python 里逐行比较：
                        # 1.7 万条 × 每批 30 个候选 = 52 万次标量取值，全在解释器里跑。
                        near = np.flatnonzero(scores[:, i] >= threshold) if scores.shape[0] else ()
                        hits = [(float(scores[j, i]), row(rids[j])) for j in near
                                if (life is None or bool(life[j]))
                                and not conflicts(row(rids[j]), p)]
                        hits += [(float(intra[j, i]), r) for j, r in accepted_rows
                                 if intra[j, i] >= threshold and not conflicts(r, p)]
                        if hits:
                            target = max(hits, key=lambda hit: hit[0])[1]
                if target is not None:
                    duplicates += 1
                    provenance(c, target['id'], target)
                    provenance(c, target['id'], p)
                    old_tags = target.get('tags') or []
                    if isinstance(old_tags, str):
                        try:
                            old_tags = json.loads(old_tags)
                        except (ValueError, TypeError):
                            old_tags = []
                    if not isinstance(old_tags, list):
                        old_tags = []
                    tags = list(dict.fromkeys(old_tags + list(p.get('tags') or [])))
                    c.execute('UPDATE knowledge SET tags=? WHERE id=?',
                              (json.dumps(tags, ensure_ascii=False), target['id']))
                    target['tags'] = tags

                    # A duplicate that carries a real citation can repair a row
                    # that had none. This is the cheapest way to raise evidence
                    # coverage on an existing database without re-crawling it.
                    new_ev = str(p.get('evidence') or '')
                    if new_ev and not str(target.get('evidence') or ''):
                        from . import governance
                        governance._snapshot(c, target['id'], target, 'update',
                                             actor='dedup', reason='evidence_backfill')
                        c.execute(
                            'UPDATE knowledge SET evidence=?,evidence_hash=?,evidence_locator=?,'
                            'verification=?,status=CASE WHEN status IN (\'archived\',\'disputed\','
                            '\'superseded\') THEN status ELSE \'unverified\' END,'
                            'revision=revision+1,updated_at=? WHERE id=?',
                            (new_ev[:2000], p.get('evidence_hash', ''),
                             p.get('evidence_locator', ''), p.get('verification', ''),
                             time.time(), target['id']),
                        )
                        target.update(evidence=new_ev, verification=p.get('verification', ''))
                    if preference(p) < preference(target):
                        from . import governance
                        governance._snapshot(c, target['id'], target, 'update',
                                             actor='dedup', reason='richer_duplicate')
                        old_key = normal(target['claim'])
                        c.execute('UPDATE knowledge SET claim=?,detail=?,embedding=?,'
                                  'revision=revision+1,updated_at=? WHERE id=?',
                                  (p['claim'], p['detail'], memory._to_blob(vectors[i]),
                                   time.time(), target['id']))
                        if old_key in by_claim:
                            by_claim[old_key] = [x for x in by_claim[old_key] if x != target['_rid']]
                        target.update(claim=p['claim'], detail=p['detail'])
                        by_claim.setdefault(normal(target['claim']), []).append(target['_rid'])
                        # Subsequent candidates must compare against the content actually kept.
                        if target.get('_rid') in rids:
                            scores[rids.index(target['_rid'])] = Q[i] @ Q.T
                        for n, (j, kept) in enumerate(accepted_rows):
                            if kept is target:
                                accepted_rows[n] = (i, kept)
                        changed = True
                    continue
                pid = uuid.uuid4().hex[:16]
                p['id'] = pid
                cur = c.execute(
                    'INSERT INTO knowledge('
                    'id,topic,claim,detail,source_url,source_title,source_kind,tags,confidence,'
                    'embedding,session_id,book_id,created_at,evidence,evidence_locator,'
                    'evidence_hash,status,revision,created_by,model_version,embed_model_version,'
                    'verification,supersedes,updated_at,reviewed_at,archived_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (pid, p['topic'], p['claim'], p['detail'], p.get('source_url', ''), p.get('source_title', ''),
                     p.get('source_kind', 'web'), json.dumps(list(p.get('tags') or []), ensure_ascii=False),
                     max(.1, min(1., float(p.get('confidence', .6)))), memory._to_blob(vectors[i]),
                     session_id, book_id, p['created_at'],
                     str(p.get('evidence', ''))[:2000], p.get('evidence_locator', ''),
                     p.get('evidence_hash', ''), p.get('status', 'unverified'),
                     int(p.get('revision', 1)), p.get('created_by', ''),
                     p.get('model_version', ''), p.get('embed_model_version', ''),
                     p.get('verification', ''), p.get('supersedes', ''),
                     p['created_at'], None, None))
                from . import governance
                governance._snapshot(c, pid, p, 'create',
                                     actor=p.get('created_by', '') or 'ingest', reason='')
                # 用游标的 lastrowid，而不是每插一行再 SELECT last_insert_rowid()：
                # 整本书 240 条就是 240 次多余往返。
                p['_rid'] = cur.lastrowid
                row_cache[p['_rid']] = p
                accepted.append(p)
                accepted_rows.append((i, p))
                by_claim.setdefault(key, []).append(p['_rid'])
        if accepted or duplicates:
            if changed:
                memory._invalidate()
            else:
                for i, p in accepted_rows:
                    memory._append_vector(p['_rid'], vectors[i].tolist())
            memory._bump_search_cache()
            memory._bump_topic_index()
            memory._bump_version()
    return {'added':len(accepted), 'dup':duplicates, 'skipped':0,
            'accepted':[p['claim'] for p in accepted], 'accepted_points':accepted,
            'ids':[p['id'] for p in accepted]}
