"""Durable edit receipts and extraction barriers for the single-process sidecar."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from uuid import uuid4


class MemoryEdits:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self.lock = RLock()
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS revisions (owner TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (owner TEXT, id TEXT, fingerprint TEXT, result TEXT,
                    PRIMARY KEY(owner, id));
                CREATE TABLE IF NOT EXISTS sources (owner TEXT, id TEXT, seen TEXT, PRIMARY KEY(owner,id));
                CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, owner TEXT, revision INTEGER,
                    messages TEXT, result TEXT);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=60)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def owner(user, agent):
        if not user or not agent:
            raise ValueError('identity_required')
        return json.dumps([user, agent])

    @staticmethod
    def revision(db, owner):
        row = db.execute('SELECT revision FROM revisions WHERE owner=?', (owner,)).fetchone()
        return row[0] if row else 0

    def receipt(self, user, agent, operation):
        with self.connect() as db:
            row = db.execute('SELECT result FROM operations WHERE owner=? AND id=?',
                             (self.owner(user, agent), operation)).fetchone()
        return json.loads(row[0]) if row and row[0] else {
            'status': 'error', 'code': 'outcome_unknown' if row else 'operation_not_found'}

    def apply(self, memory, user, agent, operation, edit):
        if not isinstance(operation, str) or not operation or len(operation) > 512:
            raise ValueError('invalid_operation')
        action = edit.get('action')
        required = {'action', 'content'} if action == 'append' else {'action', 'memory_id', 'old_text', 'new_text'}
        if action not in {'append', 'replace'} or set(edit) != required or any(
                not isinstance(value, str) or len(value) > 16000 for value in edit.values()):
            raise ValueError('invalid_edit')
        if action == 'append' and not edit['content'].strip() or action == 'replace' and (
                not edit['memory_id'] or not edit['old_text']):
            raise ValueError('invalid_edit')
        owner = self.owner(user, agent)
        fingerprint = hashlib.sha256(json.dumps(edit, sort_keys=True).encode()).hexdigest()
        with self.lock, self.connect() as db:
            old = db.execute('SELECT fingerprint,result FROM operations WHERE owner=? AND id=?',
                             (owner, operation)).fetchone()
            if old:
                if old[0] != fingerprint:
                    return {'status': 'error', 'code': 'operation_conflict'}
                return json.loads(old[1]) if old[1] else {'status': 'error', 'code': 'outcome_unknown'}
            if action == 'replace':
                record = memory.get(edit['memory_id'])
                if not record or record.get('user_id') != user or record.get('agent_id') != agent:
                    return {'status': 'error', 'code': 'memory_not_found'}
                if record.get('memory') != edit['old_text']:
                    return {'status': 'error', 'code': 'stale_memory'}
                if edit['new_text'] == edit['old_text']:
                    return {'status': 'unchanged'}
            # Persist the barrier BEFORE external I/O: an uncertain write must still invalidate old extraction.
            db.execute('INSERT INTO operations VALUES (?,?,?,NULL)', (owner, operation, fingerprint))
            db.execute('INSERT INTO revisions VALUES (?,1) ON CONFLICT(owner) DO UPDATE SET revision=revision+1', (owner,))
            db.commit()
            try:
                if action == 'append':
                    memory.add(edit['content'], user_id=user, agent_id=agent, infer=False,
                               metadata={'source': 'explicit_memory_edit', 'operation_id': operation})
                elif edit['new_text']:
                    memory.update(edit['memory_id'], data=edit['new_text'])
                else:
                    memory.delete(edit['memory_id'])
            except Exception:
                return {'status': 'error', 'code': 'outcome_unknown'}
            result = {'status': 'updated', 'operation_id': operation, 'revision': self.revision(db, owner)}
            db.execute('UPDATE operations SET result=? WHERE owner=? AND id=?', (json.dumps(result), owner, operation))
            return result

    def prepare(self, user, agent, source, messages, *, register_only=False):
        if not source or len(source) > 512 or len(messages) > 128 or sum(map(len, messages)) > 64000:
            raise ValueError('invalid_snapshot')
        owner = self.owner(user, agent)
        with self.lock, self.connect() as db:
            revision = self.revision(db, owner)
            row = db.execute('SELECT seen FROM sources WHERE owner=? AND id=?', (owner, source)).fetchone()
            # Hashes retain no transcript and prevent old full-history snapshots resurfacing after edits.
            seen = json.loads(row[0]) if row else {}
            fresh = []
            for text in messages:
                digest = hashlib.sha256(text.encode()).hexdigest()
                if digest not in seen:
                    seen[digest] = revision if row or revision == 0 else -1
                if seen[digest] == revision:
                    fresh.append({'role': 'user', 'content': text})
            db.execute('INSERT OR REPLACE INTO sources VALUES (?,?,?)', (owner, source, json.dumps(seen)))
            if register_only:
                return None
            ref = str(uuid4())
            db.execute('INSERT INTO snapshots VALUES (?,?,?,?,NULL)', (ref, owner, revision, json.dumps(fresh)))
            return ref

    def extract(self, memory, user, agent, reference):
        owner = self.owner(user, agent)
        with self.lock, self.connect() as db:
            row = db.execute('SELECT revision,messages,result FROM snapshots WHERE id=? AND owner=?',
                             (reference, owner)).fetchone()
            if not row:
                raise ValueError('invalid_snapshot_reference')
            if row[2]:
                return json.loads(row[2])
            if row[0] != self.revision(db, owner):
                result = {'status': 'superseded'}
            elif not json.loads(row[1]):
                result = {'status': 'skipped'}
            else:
                # Durable unknown marker prevents graph retry replaying an uncertain external write.
                result = {'status': 'error', 'code': 'outcome_unknown'}
                db.execute('UPDATE snapshots SET result=? WHERE id=?', (json.dumps(result), reference))
                db.commit()
                try:
                    memory.add(json.loads(row[1]), user_id=user, agent_id=agent,
                               metadata={'source': 'runtime_turn_ingestion', 'source_turn': reference})
                    result = {'status': 'completed'}
                except Exception:
                    pass
            db.execute('UPDATE snapshots SET result=?,messages=? WHERE id=?', (json.dumps(result), '[]', reference))
            return result
