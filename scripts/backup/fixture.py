"""Seed/check real business models in an isolated recovery test project only."""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from aiive.config import settings
from aiive.db.models import Thread, Event, Task, Attachment, ConversationState
from aiive.auth.security import signing_secret
from aiive.storage import object_store
from aiive.storage.data_paths import data_root

payload = b'aiive-real-recovery-fixture'
thread_id = '0a000000-0000-0000-0000-000000000001'
event_id = '0a000000-0000-0000-0000-000000000002'
task_id = '0a000000-0000-0000-0000-000000000003'
attachment_id = '0a000000-0000-0000-0000-000000000004'
expected_file = data_root() / 'backup-test-expected.json'
engine = create_engine(settings.database_url)
with Session(engine) as session:
    if sys.argv[1] == 'seed':
        request=urllib.request.Request('http://localhost:8000/api/auth/register',
            data=json.dumps({'username':'backup-test','password':'test-only-recovery-password'}).encode(),
            headers={'Content-Type':'application/json'})
        token=json.load(urllib.request.urlopen(request))['access_token']
        ref, digest = object_store.put_content_addressed('chat-attachments', payload)
        session.add(Thread(id=thread_id, title='Recovery historical conversation'))
        session.flush()
        session.get(ConversationState, 'default').current_thread_id=thread_id
        session.add(Event(id=event_id, trace_id='backup-test', thread_id=thread_id,
                          event_type='user_message', payload={'content':'historical message'}))
        session.flush()
        session.add(Task(id=task_id, task_type='reminder', status='completed',
                         title='Recovery historical task', thread_id=thread_id))
        session.add(Attachment(id=attachment_id, thread_id=thread_id, event_id=event_id,
                               filename='recovery.txt', mime='text/plain', size=len(payload),
                               sha256=digest, object_key=ref.key))
        session.commit()
        expected_file.write_text(json.dumps({'secret_sha256': hashlib.sha256(signing_secret().encode()).hexdigest(), 'token':token}))
        for path in ('mcp_sandbox/backup-test/marker', 'task_worktrees/backup-test/marker'):
            f=data_root()/path; f.parent.mkdir(parents=True,exist_ok=True); f.write_bytes(payload)
    else:
        assert session.get(Thread,thread_id).title=='Recovery historical conversation'
        assert session.get(Event,event_id).payload['content']=='historical message'
        assert session.get(Task,task_id).title=='Recovery historical task'
        attachment=session.get(Attachment,attachment_id)
        assert object_store.get_verified(object_store.ObjectRef('chat-attachments',attachment.object_key),attachment.sha256)==payload
        assert attachment.size==len(payload)
        expected=json.loads(expected_file.read_text())
        assert hashlib.sha256(signing_secret().encode()).hexdigest()==expected['secret_sha256']
        def get(path):
            request=urllib.request.Request('http://localhost:8000'+path,
                headers={'Authorization':'Bearer '+expected['token']})
            return urllib.request.urlopen(request).read()
        assert json.loads(get('/api/auth/me'))['username']=='backup-test'
        assert b'historical message' in get('/api/threads/'+thread_id+'/messages')
        assert get('/api/attachments/'+attachment_id+'/content')==payload
        for path in ('mcp_sandbox/backup-test/marker','task_worktrees/backup-test/marker'):
            assert (data_root()/path).read_bytes()==payload
        print('PASS real Thread/Event/Attachment/Task, ObjectRef, bytes, SHA256, Auth Secret, sandbox/worktree')
