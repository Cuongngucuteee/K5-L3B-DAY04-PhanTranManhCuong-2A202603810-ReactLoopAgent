"""Bốn provider cùng một interface. Không chứa agent loop."""
import json
import os
import urllib.error
import time
import urllib.request
import uuid

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
}


class HTTPStatusError(RuntimeError):
    def __init__(self, code, retry_after=None):
        super().__init__(f'Provider trả HTTP {code}; kiểm tra model, key và quota.')
        self.code, self.retry_after = code, retry_after


_key_index = {}  # key đang dùng của mỗi provider, chia sẻ giữa các Provider


def post_json(url, headers, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', **headers})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not print response bodies: they may contain account or request data.
        raise HTTPStatusError(exc.code, exc.headers.get('Retry-After') if exc.headers else None) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f'Không kết nối được provider: {exc.reason}') from exc


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, self.url = CONFIG[name]
        self.extra_headers = {}
        if name == 'openai' and os.getenv('API_ENDPOINT'):  # endpoint OpenAI-compatible (opencode)
            self.url = os.getenv('API_ENDPOINT').rstrip('/') + '/chat/completions'
            # Mỗi Provider (mỗi run/case) dùng một session ID cố định.
            self.extra_headers = {'User-Agent': 'StatusAgentLab/1.0', 'x-opencode-session': str(uuid.uuid4())}
        # Nhiều key cách nhau dấu phẩy: hết quota (429) thì chuyển key kế tiếp.
        self.key_name = key_name
        self.keys = [k.strip() for k in os.getenv(key_name, '').split(',') if k.strip()]
        if not self.keys:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def _post(self, make_headers, payload):
        """429 -> đổi key, hết vòng key thì nghỉ; 5xx/timeout -> nghỉ rồi thử lại."""
        delay = float(os.getenv('REQUEST_DELAY', '0'))      # nghỉ trước mỗi request
        wait = float(os.getenv('RATE_LIMIT_WAIT', '30'))     # nghỉ khi mọi key 429 hoặc server lỗi
        rounds = int(os.getenv('RATE_LIMIT_ROUNDS', '3'))    # số lần nghỉ-rồi-thử-lại tối đa
        pauses, tried = 0, 0
        while True:
            if delay:
                time.sleep(delay)
            i = _key_index.get(self.key_name, 0) % len(self.keys)
            try:
                return post_json(self.url, make_headers(self.keys[i]), payload)
            except HTTPStatusError as exc:
                if exc.code == 429:
                    _key_index[self.key_name] = i + 1
                    tried += 1
                    if tried < len(self.keys):
                        print(f'[provider] 429, chuyển sang key #{(i + 1) % len(self.keys) + 1}', flush=True)
                        continue
                    reason = '429 trên mọi key'
                elif exc.code in (500, 502, 503, 504):
                    reason = f'server lỗi {exc.code}'
                else:
                    raise
                error = exc
            except TimeoutError as exc:
                reason, error = 'timeout', exc
            if pauses >= rounds:
                raise error
            pauses, tried = pauses + 1, 0
            pause = wait
            if isinstance(error, HTTPStatusError) and (error.retry_after or '').isdigit():
                pause = float(error.retry_after)
            print(f'[provider] {reason}, nghỉ {pause:.0f}s rồi thử lại ({pauses}/{rounds})', flush=True)
            time.sleep(pause)

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        data = self._post(lambda key: {'Authorization': f'Bearer {key}', **self.extra_headers}, payload)
        choice = data['choices'][0]
        message = choice['message']
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in message.get('tool_calls') or []]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls') or []:
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = self._post(lambda key: {'x-api-key': key, 'anthropic-version': '2023-06-01'}, payload)
        blocks = data['content']
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
