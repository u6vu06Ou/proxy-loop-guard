import time
from proxy_loop_guard import LoopGuard
qp = LoopGuard()

def reset_guard():
    qp._loopguard_sessions.clear()
    qp._loopguard_last['events'] = []
    qp._loopguard_last['counters'] = {}
    qp._loopguard_last['updated'] = None

def tool_call(name, args, call_id):
    import json
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': call_id, 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}

def tool_result(call_id, content):
    return {'role': 'tool', 'tool_call_id': call_id, 'content': content}

def base_req():
    return {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}

def assert_guarded(req, label):
    out = qp._loop_guard(req)
    assert '[proxy loop-guard]' in out['messages'][-1]['content'], label
    return out

def assert_not_guarded(req, label):
    out = qp._loop_guard(req)
    assert '[proxy loop-guard]' not in str(out['messages'][-1]), label
    return out

def assert_killed(req, label):
    try:
        qp._loop_guard(req)
        raise SystemExit(label)
    except Exception as e:
        assert getattr(e, 'status_code', None) == 400, f'expected HTTP 400, got {e!r}'
reset_guard()
req = base_req()
for i in range(qp.LOOP_GUARD_CONFIG['generic_warn']):
    req['messages'].append(tool_call('Read', {'file_path': '/project/a.py'}, f'read-{i}'))
assert_guarded(req, 'same-signature guard did not fire at 4x')
reset_guard()
req = base_req()
for i in range(qp.LOOP_GUARD_CONFIG['generic_kill']):
    req['messages'].append(tool_call('Read', {'file_path': '/project/a.py'}, f'readk-{i}'))
assert_killed(req, 'same-signature kill threshold did not raise')
reset_guard()
req = base_req()
for i in range(qp.LOOP_GUARD_CONFIG['zero_progress_warn']):
    cid = f'glob-{i}'
    req['messages'] += [tool_call('Glob', {'pattern': f'missing-{i}-*.py', 'path': '/project'}, cid), tool_result(cid, '[]')]
assert_guarded(req, 'zero-progress guard did not fire on repeated empty results')
reset_guard()
req = base_req()
for i in range(qp.LOOP_GUARD_CONFIG['bash_nonzero_warn']):
    cid = f'bash-nz-{i}'
    req['messages'] += [tool_call('Bash', {'command': 'npm test -- --bad-flag'}, cid), tool_result(cid, 'Exit code: 2\nOutput:\nunknown option --bad-flag')]
assert_guarded(req, 'Bash nonzero guard did not fire at 5x')
reset_guard()
req = base_req()
for i in range(qp.LOOP_GUARD_CONFIG['bash_nonzero_kill']):
    cid = f'bash-nzk-{i}'
    req['messages'] += [tool_call('Bash', {'command': 'npm test -- --bad-flag'}, cid), tool_result(cid, 'Exit code: 2\nOutput:\nunknown option --bad-flag')]
assert_killed(req, 'Bash nonzero kill threshold did not raise')
reset_guard()
req = base_req()
for i in range(qp.LOOP_GUARD_CONFIG['bash_zero_warn'] - 1):
    cid = f'bash-z-{i}'
    req['messages'] += [tool_call('Bash', {'command': 'git status --short'}, cid), tool_result(cid, 'Exit code: 0\nOutput:\n')]
assert_not_guarded(req, 'Bash zero-exit guard fired before 25x')
cid = 'bash-z-24'
req['messages'] += [tool_call('Bash', {'command': 'git status --short'}, cid), tool_result(cid, 'Exit code: 0\nOutput:\n')]
assert_guarded(req, 'Bash zero-exit guard did not fire at 25x')
reset_guard()
req = base_req()
for i in range(20):
    cid = f'normal-{i}'
    req['messages'] += [tool_call('Read', {'file_path': f'/project/file_{i}.py'}, cid), tool_result(cid, f'Exit code: 0\nOutput:\nunique result {i}')]
assert_not_guarded(req, 'diverse normal traffic produced a false positive')

def wf_call(url):
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'WebFetch', 'arguments': '{"url": "%s", "prompt": "varies each time"}' % url}}]}

def turn(url):
    return [wf_call(url), {'role': 'tool', 'content': 'error page'}]
reset_guard()
req = {'model': 'demo-model', 'messages': [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'task'}] + turn('https://example.com/a') + turn('https://example.com/a') + turn('https://example.com/a')}
assert_guarded(req, 'legacy WebFetch guard did not fire at 3x')
reset_guard()
req2 = {'messages': [{'role': 'user', 'content': 'task'}] + turn('https://example.com/a') + turn('https://example.com/a')}
assert_not_guarded(req2, 'WebFetch fired before legacy 3x threshold')
reset_guard()
bash = {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'Bash', 'arguments': '{"command": "git status"}'}}]}
req3 = {'messages': [{'role': 'user', 'content': 't'}, bash, bash, bash, bash]}
assert_not_guarded(req3, 'Bash repeats without exit/output evidence fired')
reset_guard()
reqk = {'messages': [{'role': 'user', 'content': 'task'}]}
for _ in range(qp.LOOP_GUARD_KILL):
    reqk['messages'] += turn('https://example.com/a')
assert_killed(reqk, 'legacy WebFetch kill threshold did not raise')

def grep_call(path, pattern='varies'):
    args = '{"pattern": "%s", "path": "%s"}' % (pattern, path)
    return {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'Grep', 'arguments': args}}]}

def grep_turn(path):
    return [grep_call(path), {'role': 'tool', 'content': 'No matches found'}]
reset_guard()
rg = {'model': 'demo-model', 'messages': [{'role': 'system', 'content': 's'}, {'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_GREP_MAX):
    rg['messages'] += [grep_call('/project/big.json', f'pat{i}'), {'role': 'tool', 'content': f'hit {i}: line {i}'}]
assert_not_guarded(rg, 'v2 Grep guard fired on distinct patterns same path (distinct-pattern regression)')
reset_guard()
rg2 = {'messages': [{'role': 'user', 'content': 'task'}]}
for p in ['a.py', 'b.py', 'c.py', 'd.py']:
    rg2['messages'] += grep_turn(p)
assert_not_guarded(rg2, 'Grep guard fired on distinct paths')
reset_guard()
rgk = {'messages': [{'role': 'user', 'content': 'task'}]}
for _ in range(qp.LOOP_GUARD_GREP_KILL):
    rgk['messages'] += grep_turn('/project/loop.json')
assert_killed(rgk, 'legacy Grep kill threshold did not raise')
reset_guard()
rg3 = {'messages': [{'role': 'user', 'content': 't'}]}
for i in range(qp.LOOP_GUARD_GREP_MAX):
    nopath = {'role': 'assistant', 'content': '', 'tool_calls': [{'function': {'name': 'Grep', 'arguments': '{"pattern": "x%d"}' % i}}]}
    rg3['messages'] += [nopath, {'role': 'tool', 'content': f'hit {i}'}]
assert_not_guarded(rg3, 'v2 Grep guard fired on pathless distinct patterns')
reset_guard()
req = base_req()
for i in range(15):
    cid = f'work-{i}'
    req['messages'] += [tool_call('Edit', {'file_path': f'/project/f{i}.py', 'old': 'a', 'new': 'b'}, cid), tool_result(cid, f'edited file {i}')]
qp._loop_guard(req)
key = qp._session_key(req, req['messages'])
qp._loopguard_sessions[key]['started'] = time.time() - 10 * 60 * 60
req['messages'] += [tool_call('Edit', {'file_path': '/project/next.py', 'old': 'x', 'new': 'y'}, 'work-next'), tool_result('work-next', 'edited next')]
assert_not_guarded(req, 'long-but-progressing session was killed on elapsed time (wall-clock not removed)')
reset_guard()
r1 = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(25):
    path = '/project/shared.json' if i % 5 == 0 else f'/project/file_{i}.json'
    cid = f'r1-{i}'
    r1['messages'] += [tool_call('Grep', {'pattern': f'pat{i}', 'path': path}, cid), tool_result(cid, f'hit {i}: unique line {i}')]
assert_not_guarded(r1, 'R1: distinct-pattern Grep on shared path fired (distinct-pattern regression)')
reset_guard()
r2 = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_CONFIG['grep_warn']):
    cid = f'r2w-{i}'
    r2['messages'] += [tool_call('Grep', {'pattern': 'fixed-pat', 'path': '/project/loop.json'}, cid), tool_result(cid, f'distinct result {i}')]
assert_guarded(r2, 'R2: same-pattern same-path Grep did not warn at 5x')
reset_guard()
r2k = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_CONFIG['grep_kill']):
    cid = f'r2k-{i}'
    r2k['messages'] += [tool_call('Grep', {'pattern': 'fixed-pat', 'path': '/project/loop.json'}, cid), tool_result(cid, f'distinct result {i}')]
assert_killed(r2k, 'R2: same-pattern same-path Grep did not kill at 15x')
reset_guard()
r3 = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_CONFIG['zero_progress_kill']):
    cid = f'r3-{i}'
    r3['messages'] += [tool_call('Grep', {'pattern': f'miss{i}', 'path': '/project/empty.json'}, cid), tool_result(cid, 'No matches found')]
assert_killed(r3, 'R3: all-miss Grep loop did not kill via zero-progress')
reset_guard()
r4 = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_CONFIG['zero_progress_kill']):
    cid = f'r4-{i}'
    r4['messages'] += [tool_call('Edit', {'file_path': '/project/same.py', 'old': f'a{i}', 'new': f'b{i}'}, cid), tool_result(cid, 'The file /project/same.py has been updated.')]
assert_not_guarded(r4, 'R4: same-file Edit with identical result fired (progress tool not exempt)')
reset_guard()
r5 = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
gap = qp.LOOP_GUARD_CONFIG['streak_gap']
for wave in range(qp.LOOP_GUARD_CONFIG['generic_warn']):
    cid = f'r5-s-{wave}'
    r5['messages'] += [tool_call('Read', {'file_path': '/project/repeated.py'}, cid), tool_result(cid, f'content wave {wave}')]
    for j in range(gap + 2):
        cidg = f'r5-fill-{wave}-{j}'
        r5['messages'] += [tool_call('Read', {'file_path': f'/project/other_{wave}_{j}.py'}, cidg), tool_result(cidg, f'filler {wave}/{j}')]
assert_not_guarded(r5, 'R5: streak-broken repeats still fired')
reset_guard()
r5b = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_CONFIG['generic_warn']):
    cid = f'r5b-{i}'
    r5b['messages'] += [tool_call('Read', {'file_path': '/project/repeated.py'}, cid), tool_result(cid, f'content {i}')]
assert_guarded(r5b, 'R5: back-to-back same signature did not warn at 5x')
reset_guard()
r6 = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_CONFIG['generic_warn']):
    cid = f'r6-{i}'
    r6['messages'] += [tool_call('Read', {'file_path': '/project/dedupe.py'}, cid), tool_result(cid, f'result {i}')]
out = qp._loop_guard(r6)
assert '[proxy loop-guard]' in out['messages'][-1]['content'], 'R6: initial warn did not fire'
before = len(out['messages'])
out2 = qp._loop_guard(out)
assert len(out2['messages']) == before, 'R6: warn was re-injected without count growth (messages grew)'
reset_guard()
ra = {'model': 'demo-model', '_session_hint': 'session-A', 'messages': [{'role': 'user', 'content': 'shared task'}]}
rb = {'model': 'demo-model', '_session_hint': 'session-B', 'messages': [{'role': 'user', 'content': 'shared task'}]}
for i in range(3):
    cid = f'r7a-{i}'
    ra['messages'] += [tool_call('Read', {'file_path': f'/project/a_{i}.py'}, cid), tool_result(cid, f'a{i}')]
for i in range(3):
    cid = f'r7b-{i}'
    rb['messages'] += [tool_call('Read', {'file_path': f'/project/b_{i}.py'}, cid), tool_result(cid, f'b{i}')]
ka = qp._session_key(ra, ra['messages'])
kb = qp._session_key(rb, rb['messages'])
assert ka != kb, 'R7: different hints produced same session key'
qp._loop_guard(ra)
qp._loop_guard(rb)
assert ka in qp._loopguard_sessions and kb in qp._loopguard_sessions, 'R7: session keys not registered'
assert qp._loopguard_sessions[ka]['last_count'] == 3, 'R7: session A state polluted by session B'
reset_guard()
r8 = {'model': 'demo-model', '_session_hint': 'session-X', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(2):
    cid = f'r8-{i}'
    r8['messages'] += [tool_call('Read', {'file_path': f'/project/x_{i}.py'}, cid), tool_result(cid, f'x{i}')]
assert '_session_hint' in r8, 'R8: hint missing before guard (test setup wrong)'
qp._loop_guard(r8)
assert '_session_hint' not in r8, 'R8: _session_hint leaked past _loop_guard'
reset_guard()
r9 = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'verify example service free tier'}]}
for i in range(qp.LOOP_GUARD_CONFIG['zero_progress_kill']):
    cid = f'r9-{i}'
    url = f'https://html.duckduckgo.com/html/?q=example+service+{i}'
    r9['messages'] += [tool_call('WebFetch', {'url': url, 'prompt': 'varies'}, cid), tool_result(cid, 'The provided content contains no information about example service. It is exclusively a DuckDuckGo homepage structure.')]
assert_killed(r9, 'R9: DDG shell-page WebFetch loop (varying URL) did not kill via zero-progress')
reset_guard()
r9b = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_CONFIG['zero_progress_warn'] - 1):
    cid = f'r9b-{i}'
    r9b['messages'] += [tool_call('WebFetch', {'url': f'https://html.duckduckgo.com/html/?q=x{i}'}, cid), tool_result(cid, 'DuckDuckGo homepage structure, no information.')]
assert_not_guarded(r9b, 'R9b: shell-page guard fired before zero_progress_warn')
reset_guard()
r9c = {'model': 'demo-model', 'messages': [{'role': 'user', 'content': 'task'}]}
for i in range(qp.LOOP_GUARD_CONFIG['zero_progress_kill']):
    cid = f'r9c-{i}'
    r9c['messages'] += [tool_call('WebFetch', {'url': f'https://docs.example{i}.com/page'}, cid), tool_result(cid, f'real distinct doc content number {i}')]
assert_not_guarded(r9c, 'R9c: distinct-host real-content WebFetch fired (host-scope over-aggregated)')
print('all extracted loop-guard regression scenarios passed')
