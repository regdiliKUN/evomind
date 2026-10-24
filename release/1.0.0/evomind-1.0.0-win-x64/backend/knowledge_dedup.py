"""Shared, deterministic text identity and completeness rules."""
import re
import unicodedata
import hashlib
import json
import time
import uuid

import numpy as np


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
    data = {k: point.get(k, '') for k in ('claim', 'detail', 'source_url', 'source_title', 'book_id')}
    payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
    key = hashlib.sha256((pid + payload).encode()).hexdigest()
    c.execute('INSERT OR IGNORE INTO knowledge_provenance VALUES (?,?,?)', (key, pid, payload))


def ingest(points, vectors, *, session_id='', book_id='', dedup=True):
    from . import memory, config
    Q = memory._normalize(np.stack(vectors))
    threshold = float(config.get('dedup_threshold', .93))
    accepted, accepted_rows, duplicates = [], [], 0
    with memory._lock:
        c = memory.conn()
        existing = [dict(r) for r in c.execute('SELECT rowid AS _rid,* FROM knowledge')] if dedup else []
        by_claim = {}
        for p in existing:
            by_claim.setdefault(normal(p['claim']), []).append(p)
        rids, mat = memory._all_vectors(Q.shape[1])
        by_rid = {p['_rid']: p for p in existing}
        scores = mat @ Q.T if rids and dedup else np.empty((0, len(points)))
        intra = Q @ Q.T if dedup else None
        # Sort richer candidates first, preserving input order on ties.
        order = sorted(range(len(points)), key=lambda i: preference(points[i])) if dedup else range(len(points))
        changed = False
        with c:
            for i in order:
                p = dict(points[i], claim=normal(points[i]['claim']), detail=normal(points[i].get('detail')),
                         topic=memory._norm_topic(points[i].get('topic', '')), book_id=book_id,
                         session_id=session_id, created_at=time.time())
                target = None
                if dedup:
                    target = next((r for r in by_claim.get(normal(p['claim']), []) if equivalent(r, p)), None)
                    if target is None:
                        hits = [(float(scores[j, i]), by_rid[rid]) for j, rid in enumerate(rids)
                                if scores[j, i] >= threshold and not conflicts(by_rid[rid], p)]
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
                    if preference(p) < preference(target):
                        old_key = normal(target['claim'])
                        c.execute('UPDATE knowledge SET claim=?,detail=?,embedding=? WHERE id=?',
                                  (p['claim'], p['detail'], memory._to_blob(vectors[i].tolist()), target['id']))
                        by_claim[old_key] = [row for row in by_claim[old_key] if row is not target]
                        target.update(claim=p['claim'], detail=p['detail'])
                        by_claim.setdefault(normal(target['claim']), []).append(target)
                        # Subsequent candidates must compare against the content actually kept.
                        if target.get('_rid') in rids:
                            scores[rids.index(target['_rid'])] = Q[i] @ Q.T
                        for n, (j, row) in enumerate(accepted_rows):
                            if row is target:
                                accepted_rows[n] = (i, row)
                        changed = True
                    continue
                pid = uuid.uuid4().hex[:16]
                p['id'] = pid
                c.execute('INSERT INTO knowledge(id,topic,claim,detail,source_url,source_title,source_kind,tags,confidence,embedding,session_id,book_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                          (pid, p['topic'], p['claim'], p['detail'], p.get('source_url', ''), p.get('source_title', ''),
                           p.get('source_kind', 'web'), json.dumps(list(p.get('tags') or []), ensure_ascii=False),
                           max(.1, min(1., float(p.get('confidence', .6)))), memory._to_blob(vectors[i].tolist()),
                           session_id, book_id, p['created_at']))
                p['_rid'] = c.execute('SELECT last_insert_rowid()').fetchone()[0]
                accepted.append(p)
                accepted_rows.append((i, p))
                by_claim.setdefault(normal(p['claim']), []).append(p)
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
