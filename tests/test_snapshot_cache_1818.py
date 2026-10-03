"""Owned atomic JSON cache avoids reads without losing another writer's section."""
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace as NS
import pytest
from .test_v1_1_1 import state as s

@pytest.fixture
def owner(tmp_path,monkeypatch):
    s.clear();binding=NS(calls=0)
    monkeypatch.setattr(s,'_hermes_home',lambda:tmp_path)
    monkeypatch.setattr(s,'snapshot',lambda b,activity:dict(schema=s.SCHEMA,pid=os.getpid(),updated_at=time.time(),calls=b.calls))
    yield binding
    s.clear()

def foreign(path,atomic=True):
    text=json.dumps({'schema':s.SCHEMA,'processes':{'999999':{'pid':999999,'updated_at':time.time(),'marker':'foreign'}}})
    if atomic:
        temp=path.with_suffix('.foreign');temp.write_text(text);os.replace(temp,path)
    else:path.write_text(text)

def test_own_unchanged_identity_avoids_a_disk_read(owner,monkeypatch):
    assert s.publish(owner,force=True)
    real=Path.read_text;reads=[];path=s.state_path()
    def read(p,*a,**kw):
        if p==path:reads.append(p)
        return real(p,*a,**kw)
    monkeypatch.setattr(Path,'read_text',read);owner.calls+=1
    assert s.publish(owner,force=True) and not reads
    assert json.loads(real(path))['processes'][str(os.getpid())]['calls']==1

@pytest.mark.parametrize('atomic',[False,True])
def test_foreign_writer_is_seen_and_merged(owner,atomic):
    assert s.publish(owner,force=True);path=s.state_path();foreign(path,atomic)
    owner.calls+=1;assert s.publish(owner,force=True)
    assert json.loads(path.read_text())['processes']['999999']['marker']=='foreign'

def test_foreign_replace_immediately_after_our_replace_cannot_poison_stamp(owner,monkeypatch):
    path=s.state_path();real=os.replace;once=True
    def racing(source,destination):
        nonlocal once
        real(source,destination)
        if Path(destination)==path and once:
            once=False
            tmp=path.with_suffix('.racer');tmp.write_text(json.dumps({'processes':{'999999':{'updated_at':time.time(),'marker':'racing'}}}));real(tmp,path)
    monkeypatch.setattr(s.os,'replace',racing)
    assert s.publish(owner,force=True);owner.calls+=1;assert s.publish(owner,force=True)
    assert json.loads(path.read_text())['processes']['999999']['marker']=='racing'

def test_deleted_destination_does_not_resurrect_cached_foreign_sections(owner):
    assert s.publish(owner,force=True);path=s.state_path();foreign(path)
    owner.calls+=1;assert s.publish(owner,force=True);path.unlink()
    owner.calls+=1;assert s.publish(owner,force=True)
    assert set(json.loads(path.read_text())['processes'])=={str(os.getpid())}

def test_home_path_change_never_reuses_other_home_document(owner,tmp_path,monkeypatch):
    assert s.publish(owner,force=True)
    other=tmp_path/'other';monkeypatch.setattr(s,'_hermes_home',lambda:other)
    owner.calls+=1;assert s.publish(owner,force=True)
    assert json.loads(s.state_path().read_text())['processes'][str(os.getpid())]['calls']==1

def test_missing_file_identity_disables_cache(owner,monkeypatch):
    assert s.publish(owner,force=True);path=s.state_path();foreign(path)
    monkeypatch.setattr(s,'_file_stamp',lambda p:None)
    owner.calls+=1;assert s.publish(owner,force=True)
    assert json.loads(path.read_text())['processes']['999999']['marker']=='foreign'

def test_clear_forgets_the_owned_cache(owner):
    assert s.publish(owner,force=True);assert s._cached_document
    s.clear();assert s._cached_stamp is None and s._cached_path==s._cached_document==''
