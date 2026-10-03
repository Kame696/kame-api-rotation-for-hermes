"""Exact delivered boundaries and ownership restoration across stream recovery."""
from types import SimpleNamespace as NS
import pytest
from .test_v1_1_1 import Agent, KEYS, _binding, cut, answer, conversation, dispatch_binding as d, settings
from .test_v1_1_1 import stitch

@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    settings.forget()
    for k in list(settings._ENV_FOR.values())+list(settings._NUMBER_ENV_FOR.values()):monkeypatch.delenv(k,raising=False)
    monkeypatch.setenv('KAME_SHARE_POOL_HEALTH','0');monkeypatch.setattr(d,'_publish',lambda *a,**k:None)
    yield
    settings.forget()

@pytest.mark.parametrize('boundary',[' ', '  ', '\n', '\n\n'])
def test_stripped_partial_stub_does_not_repeat_visible_boundary(boundary):
    prefix='The retained marker is amber-5821.'+boundary
    final=prefix+'The final total is 2206.'
    a=Agent();n=0
    def host(agent,request):
        nonlocal n
        n+=1
        agent._fire_stream_delta(prefix if n==1 else final)
        return cut(prefix.rstrip()) if n==1 else answer(final)
    binding=_binding();result=binding.run(host,a,conversation(),(),{})
    assert n==2 and result.choices[0].message.content==final
    assert a.screen==final
    assert binding.stitched==1 and binding.mid_stream_cuts==0

def test_non_equivalent_response_suffix_is_not_discarded():
    delivery=NS(text='prefix')
    assert d._contribution(delivery,'','prefix suffix',False)=='prefix suffix'

def test_instance_funnel_restored_after_success():
    a=Agent();original=lambda text:a.shown.append(text)
    a._fire_stream_delta=original
    binding=_binding();binding.run(lambda *args:answer('OK'),a,conversation(),(),{})
    assert a.__dict__['_fire_stream_delta'] is original

def test_instance_funnel_restored_after_control_flow():
    a=Agent();original=lambda text:a.shown.append(text);a._fire_stream_delta=original
    def stop(*args):raise InterruptedError('stop')
    with pytest.raises(InterruptedError):_binding().run(stop,a,conversation(),(),{})
    assert a.__dict__['_fire_stream_delta'] is original

def test_inherited_funnel_not_left_as_an_instance_field():
    a=Agent();_binding().run(lambda *args:answer('OK'),a,conversation(),(),{})
    assert '_fire_stream_delta' not in a.__dict__

def test_foreign_funnel_replacement_during_attempt_survives():
    a=Agent();foreign=lambda text:None
    def host(agent,request):agent._fire_stream_delta=foreign;return answer('OK')
    _binding().run(host,a,conversation(),(),{})
    assert a.__dict__['_fire_stream_delta'] is foreign

def test_nested_delivery_restores_the_outer_capture():
    a=Agent();outer,original=d._install_delivery(a,d._Progress())
    inner,restore=d._install_delivery(a,d._Progress())
    a._fire_stream_delta('inner');d._remove_delivery(a,restore)
    assert a._fire_stream_delta is outer
    a._fire_stream_delta(' outer');d._remove_delivery(a,original)
    assert a.screen=='inner outer' and outer.text=='inner outer'
    assert '_fire_stream_delta' not in a.__dict__

@pytest.mark.parametrize('gap',[' ', '  ', '\n', '\n\n', '\n    '])
def test_repeated_whitespace_boundary_is_trimmed_even_across_deltas(gap):
    seen='A sufficiently long retained marker.'+gap
    s=stitch.Stitcher(seen,probe_chars=1)
    text=s.feed(seen.rstrip())
    text+=''.join(s.feed(c) for c in gap+'next')+s.flush()
    assert seen+text==seen+'next'

def test_new_nonmatching_boundary_formatting_is_retained():
    seen='A sufficiently long retained marker. '
    assert stitch.stitch_text(seen,seen.rstrip()+'\n    next')=='\n    next'

def test_restart_skipping_retains_the_new_word_separator():
    seen=' '.join('retained'+str(i) for i in range(80))
    s=stitch.Stitcher(seen,probe_chars=40)
    fresh=seen+' next'
    got=''.join(s.feed(fresh[i:i+13]) for i in range(0,len(fresh),13))+s.flush()
    assert got==' next'

def test_default_keeps_a_productive_answer_through_more_than_ten_cuts():
    a=Agent(KEYS[:1]);n=0
    def host(agent,request):
        nonlocal n
        n+=1;text=''.join(f'Unique segment {i:02d}. ' for i in range(n))
        agent._fire_stream_delta(text)
        return cut(text.rstrip()) if n<=12 else answer(text+'DONE')
    b=_binding();result=b.run(host,a,conversation(),(),{})
    assert n==13 and result.id!=d.PARTIAL_STUB_ID
    assert result.choices[0].message.content.endswith('DONE')
    assert b.resumes==12 and b.mid_stream_cuts==0

def test_explicit_operator_ceiling_still_stops_continuation(monkeypatch):
    monkeypatch.setenv('KAME_STREAM_RESUME_LIMIT','1');a=Agent(KEYS[:1]);n=0
    def host(agent,request):
        nonlocal n
        n+=1;text=''.join(f'Unique segment {i:02d}. ' for i in range(n));agent._fire_stream_delta(text);return cut(text.rstrip())
    b=_binding();result=b.run(host,a,conversation(),(),{})
    assert n==2 and result.id==d.PARTIAL_STUB_ID and b.mid_stream_cuts==1

def test_automatic_resume_budget_is_null_not_infinity():
    assert d.DispatchBinding._resume_budget(Agent(),conversation()) is None

def test_automatic_mode_still_stops_when_no_key_adds_any_words(monkeypatch):
    monkeypatch.setenv('KAME_STREAM_RESUME_LIMIT','-1');a=Agent(KEYS[:1]);n=0
    text='No new contribution after this retained sentence.'
    def host(agent,request):
        nonlocal n
        n+=1;assert n<=4;agent._fire_stream_delta(text);return cut(text)
    result=_binding().run(host,a,conversation(),(),{})
    assert n<=4 and result.id==d.PARTIAL_STUB_ID


HOST_RETRY_NOTICE = "\n\n⚠ Connection dropped mid tool-call; reconnecting…\n\n"
HOST_STALLED_NOTICE = "\n\n⚠ Stream stalled mid tool-call (fixture_action); the action was not executed. Ask me to retry if you want to continue."

def owned_notice(agent, text, method):
    namespace = {'__name__': 'agent.chat_completion_helpers'}
    exec("def _quiet(fn, text):\n    fn(text)\ndef "+method+"(agent, text):\n    _quiet(agent._fire_stream_delta, text)\n", namespace)
    namespace[method](agent, text)

@pytest.mark.parametrize('method,text',[('_handle_stream_error',HOST_RETRY_NOTICE),('_partial_stream_stub',HOST_STALLED_NOTICE)])
def test_only_owned_recovery_diagnostics_are_deferred(method,text):
    a=Agent();delivery,restore=d._install_delivery(a,d._Progress())
    owned_notice(a,text,method)
    assert a.screen=='' and delivery.notices==[text] and delivery.text==''
    d._remove_delivery(a,restore)
    delivery.show_terminal_notices()
    assert a.screen==text

@pytest.mark.parametrize('text',[HOST_RETRY_NOTICE,HOST_STALLED_NOTICE])
def test_identical_provider_text_is_never_filtered(text):
    a=Agent();delivery,restore=d._install_delivery(a,d._Progress())
    a._fire_stream_delta(text);d._remove_delivery(a,restore)
    assert a.screen==text and delivery.notices==[]

def test_host_internal_tool_retry_deduplicates_preamble():
    a=Agent();delivery,restore=d._install_delivery(a,d._Progress())
    a._fire_stream_delta('Reading retained reference. ')
    owned_notice(a,HOST_RETRY_NOTICE,'_handle_stream_error')
    a._fire_stream_delta('Reading retained reference. ');delivery.finish()
    d._remove_delivery(a,restore)
    assert a.screen==delivery.text=='Reading retained reference. '
    assert delivery.replayed and delivery.clean_content('answer'+HOST_STALLED_NOTICE)=='answer'+HOST_STALLED_NOTICE

def test_unknown_host_diagnostic_shape_stays_visible():
    a=Agent();delivery,restore=d._install_delivery(a,d._Progress())
    text='\n\n⚠ A future host diagnostic'
    owned_notice(a,text,'_handle_stream_error');d._remove_delivery(a,restore)
    assert a.screen==text and delivery.notices==[]


@pytest.mark.parametrize('seen',['Reading ', 'A long retained reference. '])
def test_exact_tool_replay_prefix_trimmed_across_character_chunks(seen):
    s=d._ReplayStitcher(seen)
    assert ''.join(s.feed(c) for c in seen+'done')+s.flush()=='done'

def test_changed_tool_replay_text_retains_new_content():
    s=d._ReplayStitcher('Reading ')
    assert s.feed('Different preamble. ')+s.flush()=='Different preamble. '

def test_tool_replay_content_reconciliation_does_not_repeat_short_prefix():
    delivery=NS(text='',replayed=True)
    assert d._contribution(delivery,'Reading ','Reading ',True)==''


@pytest.mark.parametrize('prefix',['Yes.', 'Reading '])
def test_automatic_continuation_ends_exact_short_no_progress(prefix):
    a=Agent(keys=[KEYS[0]]);calls=[]
    def host(agent,request):
        calls.append(request)
        if len(calls)>5:raise InterruptedError('test-only observation cutoff')
        agent._fire_stream_delta(prefix)
        return cut(prefix.strip())
    result=_binding().run(host,a,conversation(),(),{})
    assert len(calls)==3 and result.id==d.PARTIAL_STUB_ID
    assert a.screen==prefix and result.choices[0].message.content==prefix
