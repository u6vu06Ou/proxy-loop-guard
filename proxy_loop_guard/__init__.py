"""Loop detection extracted from a production LLM proxy. No network I/O."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import threading
import time

__all__ = ["LoopGuard", "LoopDetected", "DEFAULT_CONFIG"]

_DEFAULT_LOGGER = logging.getLogger("proxy_loop_guard")
_DEFAULT_LOGGER.addHandler(logging.NullHandler())


class LoopDetected(RuntimeError):
    """The next model request was blocked, before any upstream call.

    ``status_code`` and ``detail`` allow explicit mapping to an HTTP response.
    This exception does not terminate processes or cancel running tools.
    """

    def __init__(self, *, status_code: int = 400, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


DEFAULT_CONFIG = {
    'window_calls': 160,
    'generic_warn': 5,
    'generic_kill': 12,
    'zero_progress_warn': 5,
    'zero_progress_kill': 12,
    'web_warn': 3,
    'web_kill': 9,
    'grep_warn': 5,
    'grep_kill': 15,
    'bash_nonzero_warn': 5,
    'bash_nonzero_kill': 12,
    'bash_zero_warn': 25,
    'bash_zero_kill': 50,
    'stagnant_warn_turns': 3,
    'stagnant_kill_turns': 5,
    'streak_gap': 8,
}


class LoopGuard:
    """Stateful detector for OpenAI-style chat/tool history.

    Reuse one instance across requests. ``check`` mutates the given request,
    appending a warning when needed. Thresholds and session state are isolated
    per instance. Calls to the public API are serialized with a lock.
    """

    def __init__(self, config: dict | None = None, *, logger: logging.Logger | None = None):
        self.LOOP_GUARD_CONFIG = dict(DEFAULT_CONFIG)
        if config is not None:
            unknown = set(config) - set(DEFAULT_CONFIG)
            if unknown:
                raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
            self.LOOP_GUARD_CONFIG.update(config)
        for key, value in self.LOOP_GUARD_CONFIG.items():
            if type(value) is not int or value < 1:
                raise ValueError(f"{key} must be a positive integer")
        for key, value in self.LOOP_GUARD_CONFIG.items():
            if "warn" in key and value > self.LOOP_GUARD_CONFIG[key.replace("warn", "kill")]:
                raise ValueError(f"{key} must not exceed its kill threshold")
        call_limits = [value for key, value in self.LOOP_GUARD_CONFIG.items() if key.endswith("_kill")]
        if self.LOOP_GUARD_CONFIG["window_calls"] < max(call_limits):
            raise ValueError("window_calls must cover every call-based kill threshold")
        self.logger = logger if logger is not None else _DEFAULT_LOGGER
        self._lock = threading.RLock()
        self._loopguard_sessions = {}
        self._loopguard_last = {"events": [], "counters": {}, "updated": None}
        self.LOOP_GUARD_MAX = self.LOOP_GUARD_CONFIG["web_warn"]
        self.LOOP_GUARD_KILL = self.LOOP_GUARD_CONFIG["web_kill"]
        self.LOOP_GUARD_GREP_MAX = self.LOOP_GUARD_CONFIG["grep_warn"]
        self.LOOP_GUARD_GREP_KILL = self.LOOP_GUARD_CONFIG["grep_kill"]
        self.LOOP_GUARD_TOOLS = ['WebFetch', 'WebSearch', 'web_fetch', 'web_search']
        self.PROGRESS_TOOLS = ['Edit', 'MultiEdit', 'NotebookEdit', 'TaskCreate', 'TaskList', 'TaskUpdate', 'TodoWrite', 'Write', 'edit', 'multi_edit', 'update_plan', 'write']

    def check(self, request: dict, *, session_id: str) -> dict:
        """Check complete chat history before sending the next model request.

        Supply a stable, unique session ID on every call. Warnings are added in
        place. Raises LoopDetected at a hard threshold. Internal session hints
        are removed on both paths; do not forward a blocked request upstream.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not isinstance(request, dict) or not isinstance(request.get("messages"), list):
            raise ValueError("request must contain a messages list")
        with self._lock:
            request["_session_hint"] = session_id
            return self._loop_guard(request)

    def snapshot(self) -> dict:
        """Return detached diagnostics for the latest evaluation.

        Targets may contain commands, URLs or file paths. Treat diagnostics as
        application data; do not expose them on a public debug endpoint.
        """
        with self._lock:
            return copy.deepcopy(self._loopguard_last)

    def reset(self) -> None:
        """Forget all in-memory sessions and latest diagnostics."""
        with self._lock:
            self._loopguard_sessions.clear()
            self._loopguard_last = {"events": [], "counters": {}, "updated": None}

    def _stable_json(self, value) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

    def _short_hash(self, value: str) -> str:
        return hashlib.sha256(value.encode('utf-8', errors='replace')).hexdigest()[:16]

    def _load_tool_args(self, args_text: str):
        try:
            return json.loads(args_text or '{}')
        except Exception:
            return args_text or ''

    def _tool_arg_value(self, args_obj, name: str, default: str='') -> str:
        return str(args_obj.get(name, default)) if isinstance(args_obj, dict) else default

    def _tool_signature(self, name: str, args_text: str) -> tuple[str, str, str]:
        args_obj = self._load_tool_args(args_text)
        normalized = self._stable_json(args_obj)
        return (name, self._short_hash(normalized), normalized)

    def _tool_result_text(self, content) -> str:
        if isinstance(content, str):
            return content
        try:
            return self._stable_json(content)
        except Exception:
            return str(content)

    def _result_fingerprint(self, content) -> tuple[str, str]:
        text = self._tool_result_text(content)
        normalized = re.sub('\\s+', ' ', text).strip()
        empty_patterns = ('', '[]', '{}', 'null', 'none', 'no matches', 'no matches found', 'no results', 'not found', '(empty)')
        low = normalized.lower()
        if low in empty_patterns or re.fullmatch('(no matches found\\.?|no matches\\.?)', low or ''):
            return ('EMPTY', 'empty')
        if 'duckduckgo' in low and re.search('(no information|does not contain|security challenge|human verification|homepage structure|provided content)', low):
            return ('SHELL_PAGE', 'shell-page')
        return (self._short_hash(normalized), normalized[:120])

    def _bash_exit_code(self, output: str) -> int | None:
        patterns = ['Exit code:\\s*(-?\\d+)', 'exit code\\s+(-?\\d+)', 'exited with code\\s+(-?\\d+)']
        for pat in patterns:
            mt = re.search(pat, output, re.IGNORECASE)
            if mt:
                try:
                    return int(mt.group(1))
                except ValueError:
                    return None
        return None

    def _zero_progress_scope(self, name: str, args_obj) -> str:
        if not isinstance(args_obj, dict):
            return ''
        if name == 'Grep':
            return str(args_obj.get('path', ''))
        if name in self.LOOP_GUARD_TOOLS:
            raw = str(args_obj.get('url') or args_obj.get('query') or '')
            m = re.match('https?://([^/?#]+)', raw)
            return m.group(1).lower() if m else raw
        for key in ('path', 'file_path', 'cwd', 'command'):
            if key in args_obj:
                return str(args_obj.get(key) or '')
        return ''

    def _first_user_task(self, messages: list) -> str:
        for m in messages:
            if m.get('role') != 'user':
                continue
            c = m.get('content')
            if isinstance(c, str) and c.strip():
                return c.strip()[:1200]
            if isinstance(c, list):
                text = ' '.join((item.get('text', '') for item in c if isinstance(item, dict) and item.get('type') in {'text', 'input_text'})).strip()
                if text:
                    return text[:1200]
        return ''

    def _session_key(self, openai_req: dict, messages: list) -> str:
        model = openai_req.get('model', '')
        task = self._first_user_task(messages)
        hint = str(openai_req.get('_session_hint') or '')
        if hint:
            return self._short_hash(hint + '\n' + model + '\n' + task)
        return self._short_hash(model + '\n' + task)

    def _iter_tool_records(self, messages: list) -> list[dict]:
        records = []
        pending = {}
        for m in messages:
            if m.get('role') == 'assistant':
                for tc in m.get('tool_calls') or []:
                    fn = tc.get('function', {})
                    name = fn.get('name', '')
                    args_text = str(fn.get('arguments', '') or '')
                    sig_name, args_hash, normalized = self._tool_signature(name, args_text)
                    rec = {'id': tc.get('id', ''), 'name': sig_name, 'args_text': args_text, 'args_hash': args_hash, 'args_norm': normalized, 'result': None, 'result_hash': None, 'result_preview': ''}
                    pending[rec['id']] = len(records)
                    records.append(rec)
            elif m.get('role') == 'tool':
                call_id = m.get('tool_call_id', '')
                idx = pending.get(call_id)
                if idx is None:
                    continue
                rh, preview = self._result_fingerprint(m.get('content', ''))
                records[idx]['result'] = self._tool_result_text(m.get('content', ''))
                records[idx]['result_hash'] = rh
                records[idx]['result_preview'] = preview
        return records[-self.LOOP_GUARD_CONFIG['window_calls']:]

    def _bump(self, counter: dict, key, rec: dict, idx: int, streak_gap: int) -> None:
        item = counter.get(key)
        if item is None:
            item = {'count': 0, 'records': [], 'args': set(), 'last_idx': -1}
            counter[key] = item
        if item['last_idx'] >= 0 and idx - item['last_idx'] > streak_gap:
            item['count'] = 0
            item['records'] = []
            item['args'] = set()
        item['count'] += 1
        item['records'].append(rec)
        item['args'].add(rec.get('args_hash'))
        item['last_idx'] = idx

    def _trigger(self, kind: str, name: str, target: str, count: int, warn_at: int, kill_at: int, detail: str='') -> dict | None:
        if count < warn_at:
            return None
        return {'kind': kind, 'name': name, 'target': target, 'count': count, 'warn_at': warn_at, 'kill_at': kill_at, 'hard': count >= kill_at, 'detail': detail, 'score': count / max(1, kill_at)}

    def _collect_loopguard_triggers(self, openai_req: dict, records: list[dict]) -> tuple[list[dict], dict]:
        cfg = self.LOOP_GUARD_CONFIG
        gap = cfg['streak_gap']
        generic_counts, zero_counts, bash_counts, web_counts, grep_counts = ({}, {}, {}, {}, {})
        for idx, rec in enumerate(records):
            name = rec['name']
            args_obj = self._load_tool_args(rec['args_text'])
            if name == 'Bash':
                command = self._tool_arg_value(args_obj, 'command')
                exit_code = self._bash_exit_code(rec.get('result') or '')
                if command and exit_code is not None:
                    if exit_code == 0:
                        self._bump(bash_counts, ('bash-zero', command), rec, idx, gap)
                    else:
                        out_hash, _ = self._result_fingerprint(rec.get('result') or '')
                        self._bump(bash_counts, ('bash-nonzero', command, exit_code, out_hash), rec, idx, gap)
                continue
            self._bump(generic_counts, ('signature', name, rec['args_hash']), rec, idx, gap)
            if rec.get('result_hash') and name not in self.PROGRESS_TOOLS:
                self._bump(zero_counts, ('zero-progress', name, self._zero_progress_scope(name, args_obj), rec['result_hash']), rec, idx, gap)
            if name == 'Grep':
                path = self._tool_arg_value(args_obj, 'path')
                pattern = self._tool_arg_value(args_obj, 'pattern')
                self._bump(grep_counts, ('grep', path, pattern), rec, idx, gap)
            elif name in self.LOOP_GUARD_TOOLS:
                target = self._tool_arg_value(args_obj, 'url') or self._tool_arg_value(args_obj, 'query')
                if target:
                    self._bump(web_counts, ('web', name, target), rec, idx, gap)
        triggers = []
        counters = {'generic': {str(k): v['count'] for k, v in generic_counts.items()}, 'zero_progress': {str(k): v['count'] for k, v in zero_counts.items()}, 'bash': {str(k): v['count'] for k, v in bash_counts.items()}, 'web': {str(k): v['count'] for k, v in web_counts.items()}, 'grep': {str(k): v['count'] for k, v in grep_counts.items()}}
        for (kind, name, args_hash), item in generic_counts.items():
            triggers.append(self._trigger('signature', name, args_hash, item['count'], cfg['generic_warn'], cfg['generic_kill']))
        for (kind, name, scope, result_hash), item in zero_counts.items():
            if len(item['args']) > 1:
                triggers.append(self._trigger('zero-progress', name, f"{scope or '*'}:{result_hash}", item['count'], cfg['zero_progress_warn'], cfg['zero_progress_kill'], item['records'][-1].get('result_preview', '')))
        for (kind, name, target), item in web_counts.items():
            triggers.append(self._trigger('web', name, target, item['count'], cfg['web_warn'], cfg['web_kill']))
        for (kind, path, pattern), item in grep_counts.items():
            triggers.append(self._trigger('grep', 'Grep', f'{path}|{pattern}', item['count'], cfg['grep_warn'], cfg['grep_kill']))
        for key, item in bash_counts.items():
            if key[0] == 'bash-nonzero':
                _, command, exit_code, out_hash = key
                triggers.append(self._trigger('bash-nonzero', 'Bash', command, item['count'], cfg['bash_nonzero_warn'], cfg['bash_nonzero_kill'], f'exit={exit_code} output={out_hash}'))
            else:
                _, command = key
                triggers.append(self._trigger('bash-zero', 'Bash', command, item['count'], cfg['bash_zero_warn'], cfg['bash_zero_kill'], 'exit=0'))
        return ([t for t in triggers if t], counters)

    def _stagnation_trigger(self, openai_req: dict, records: list[dict], counters: dict, key: str, state: dict) -> dict | None:
        if not records:
            return None
        cfg = self.LOOP_GUARD_CONFIG
        now = time.time()
        sigs = {(r['name'], r['args_hash']) for r in records}
        has_write_edit = any((r['name'] in self.PROGRESS_TOOLS for r in records))
        elapsed = now - state['started']
        counters['session'] = {'key': key, 'age_sec': int(elapsed), 'stagnant_turns': state['stagnant_turns'], 'signatures': len(sigs)}
        new_sigs = sigs - state['last_sigs']
        grew = len(records) > state['last_count']
        if grew and (not new_sigs) and (not has_write_edit):
            state['stagnant_turns'] += 1
        elif new_sigs or has_write_edit:
            state['stagnant_turns'] = 0
        state['last_sigs'] = sigs
        state['last_count'] = len(records)
        counters['session']['stagnant_turns'] = state['stagnant_turns']
        if state['stagnant_turns'] >= cfg['stagnant_kill_turns']:
            return self._trigger('stagnation', 'session', key, state['stagnant_turns'], cfg['stagnant_warn_turns'], cfg['stagnant_kill_turns'])
        if state['stagnant_turns'] >= cfg['stagnant_warn_turns']:
            return self._trigger('stagnation', 'session', key, state['stagnant_turns'], cfg['stagnant_warn_turns'], cfg['stagnant_kill_turns'])
        return None

    def _loopguard_hint(self, trigger: dict, original_task: str) -> str:
        task = f'\n\nOriginal task: {original_task}' if original_task else ''
        return f"[proxy loop-guard] {trigger['kind']}: {trigger['name']} repeated {trigger['count']} times for {trigger['target']}. Stop repeating this action. Change the method or data source, fix the underlying error, or finish with the information already available. Be explicit about missing information. Do only necessary wrap-up work." + task

    def _loopguard_abort_detail(self, trigger: dict) -> str:
        return (
            f"[proxy loop-guard] aborted: {trigger['kind']} repeated "
            f"{trigger['count']}x for {trigger['name']} ({trigger['target']}). "
            "The hard threshold was reached; this request was blocked to prevent a runaway loop."
        )

    def _purge_idle_sessions(self, max_entries: int=50, idle_sec: int=6 * 3600) -> None:
        """Drop sessions idle past idle_sec once the dict grows past max_entries.
        This removes stale entries, but does not impose a hard size limit."""
        if len(self._loopguard_sessions) <= max_entries:
            return
        now = time.time()
        stale = [k for k, v in self._loopguard_sessions.items() if now - v.get('last_seen', v.get('started', now)) > idle_sec]
        for k in stale:
            self._loopguard_sessions.pop(k, None)

    def _loop_guard(self, openai_req: dict) -> dict:
        try:
            messages = openai_req.get('messages', [])
            records = self._iter_tool_records(messages)
            key = self._session_key(openai_req, messages)
            now = time.time()
            hinted = bool(openai_req.get('_session_hint'))
            state = self._loopguard_sessions.setdefault(key, {'started': now, 'last_sigs': set(), 'last_count': 0, 'stagnant_turns': 0, 'warned': {}, 'hinted': hinted, 'last_seen': now})
            state['hinted'] = state.get('hinted') or hinted
            state['last_seen'] = now
            self._purge_idle_sessions()
            if not records:
                self._loopguard_last['events'] = []
                self._loopguard_last['counters'] = {}
                self._loopguard_last['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
                return openai_req
            triggers, counters = self._collect_loopguard_triggers(openai_req, records)
            st = self._stagnation_trigger(openai_req, records, counters, key, state)
            if st:
                triggers.append(st)
            self._loopguard_last['events'] = triggers
            self._loopguard_last['counters'] = counters
            self._loopguard_last['updated'] = time.strftime('%Y-%m-%d %H:%M:%S')
            if not triggers:
                return openai_req
            trigger = max(triggers, key=lambda t: (t['hard'], t['score'], t['count']))
            log_msg = f"{trigger['kind']} {trigger['name']} target={trigger['target']!r} count={trigger['count']} warn={trigger['warn_at']} kill={trigger['kill_at']}"
            if trigger['hard']:
                self.logger.error(f'[loop-guard] KILL: {log_msg}')
                raise LoopDetected(status_code=400, detail=self._loopguard_abort_detail(trigger))
            tkey = f"{trigger['kind']}:{trigger['name']}:{trigger['target']}"
            if state['warned'].get(tkey, 0) >= trigger['count']:
                return openai_req
            state['warned'][tkey] = trigger['count']
            hint = self._loopguard_hint(trigger, self._first_user_task(messages))
            openai_req['messages'].append({'role': 'user', 'content': hint})
            self.logger.warning(f'[loop-guard] WARN: {log_msg}; injected wrap-up directive')
            return openai_req
        finally:
            openai_req.pop('_session_hint', None)

