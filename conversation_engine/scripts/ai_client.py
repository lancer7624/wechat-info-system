"""Small OpenAI-compatible client for an explicitly configured loopback proxy."""
import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit

from runtime_paths import DATA_ROOT

CONFIG_PATH = DATA_ROOT / 'private' / 'ai.json'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class AIError(ValueError):
    """A sanitized configuration or transport error, without request contents."""


def strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError('non-finite number')

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def validate_config(value):
    if not isinstance(value, dict):
        raise AIError('模型配置必须是 JSON 对象。')
    base_url = value.get('base_url', 'http://127.0.0.1:8000/v1')
    if not isinstance(base_url, str):
        raise AIError('模型地址格式无效。')
    try:
        url = urlsplit(base_url)
        port = url.port
    except ValueError:
        raise AIError('模型地址格式无效。') from None
    if (url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or url.username or url.password or url.query or url.fragment
            or url.path.rstrip('/') != '/v1' or not port):
        raise AIError('请使用明确端口的本机地址，例如 http://127.0.0.1:8000/v1。')
    model = value.get('model')
    if not isinstance(model, str) or not model.strip() or len(model) > 200 or any(ord(c) < 32 for c in model):
        raise AIError('请填写模型列表中的准确模型 ID。')
    key_env = value.get('api_key_env', 'WECHAT_AI_API_KEY')
    if not isinstance(key_env, str):
        raise AIError('API 密钥环境变量名称无效。')
    api_key = os.environ.get(key_env) or value.get('api_key')
    if not isinstance(api_key, str) or not api_key or len(api_key) > 8192 or any(ord(c) < 32 or ord(c) > 126 for c in api_key):
        raise AIError('缺少有效 API 密钥；请在外部配置或 WECHAT_AI_API_KEY 中设置。')
    timeout = value.get('timeout_seconds', 120)
    if type(timeout) not in (int, float) or not 1 <= timeout <= 600:
        raise AIError('模型超时必须在 1—600 秒之间。')
    allowed = value.get('allow_chat_upload', False)
    if type(allowed) is not bool:
        raise AIError('allow_chat_upload 必须是布尔值。')
    return {**value, 'base_url': base_url.rstrip('/'), 'model': model, 'api_key': api_key,
            'timeout_seconds': timeout, 'allow_chat_upload': allowed}


def load_config(path=None):
    try:
        value = strict_json(Path(path or CONFIG_PATH).read_text(encoding='utf-8-sig'))
    except OSError:
        raise AIError('模型配置无法读取，请先配置外部数据目录中的 private/ai.json。') from None
    except (ValueError, UnicodeError, RecursionError):
        raise AIError('模型配置不是有效的 JSON。') from None
    return validate_config(value)


class AIClient:
    def __init__(self, config):
        self.config = validate_config(config)
        self.last_response_model = None
        self.last_usage = None
        self.http_requests = 0
        self.request_limit = None

    def _request(self, method, suffix, body=None):
        url = urlsplit(self.config['base_url'])
        # Direct HTTPConnection intentionally bypasses system proxies and redirects.
        host = '127.0.0.1' if url.hostname == 'localhost' else url.hostname
        raw_body = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
        for attempt in range(2):
            con = http.client.HTTPConnection(host, url.port, timeout=self.config['timeout_seconds'])
            retry = False
            try:
                if self.request_limit is not None and self.http_requests >= self.request_limit:
                    raise AIError('已达到本轮模型请求上限，包含暂时失败后的重试。')
                self.http_requests += 1
                con.request(method, url.path.rstrip('/') + suffix, body=raw_body,
                            headers={'Authorization': 'Bearer ' + self.config['api_key'],
                                     'Content-Type': 'application/json; charset=utf-8'})
                response = con.getresponse()
                if response.status != 200:
                    retry = response.status in {429, 500, 502, 503, 504} and attempt == 0
                    if not retry:
                        raise AIError(f'模型服务返回 HTTP {response.status}；未记录服务响应正文。')
                else:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise AIError('模型响应超过允许大小，请缩小分析批次。')
                    try:
                        result = strict_json(raw)
                    except (ValueError, UnicodeError, RecursionError):
                        raise AIError('模型接口返回了无效 JSON。') from None
                    if not isinstance(result, dict):
                        raise AIError('模型接口响应必须是对象。')
                    return result
            except (OSError, http.client.HTTPException, socket.timeout):
                if attempt:
                    raise AIError('模型连接失败或超时；可稍后重试，已完成的会话会复用缓存。') from None
                retry = True
            finally:
                con.close()
            if retry:
                time.sleep(1)
        raise AIError('模型请求失败。')

    def models(self):
        result = self._request('GET', '/models')
        rows = result.get('data')
        if not isinstance(rows, list):
            raise AIError('模型列表格式无效。')
        return [row['id'] for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)]

    def complete(self, messages, max_tokens=4096):
        result = self._request('POST', '/chat/completions',
                               {'model': self.config['model'], 'messages': messages,
                                'stream': False, 'max_tokens': max_tokens, 'temperature': 0.2})
        choices = result.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise AIError('模型没有返回唯一的完整结果。')
        choice = choices[0]
        message = choice.get('message')
        if choice.get('finish_reason') != 'stop':
            raise AIError('模型输出未正常结束，不能作为完整分析发布。')
        if not isinstance(message, dict) or message.get('tool_calls') or message.get('function_call'):
            raise AIError('分析只接受文字结果，不能包含工具调用。')
        content = message.get('content')
        if not isinstance(content, str) or not content.strip():
            raise AIError('模型返回了空的分析结果。')
        self.last_response_model = result.get('model') if isinstance(result.get('model'), str) else None
        self.last_usage = result.get('usage') if isinstance(result.get('usage'), dict) else None
        return content


def main():
    parser = argparse.ArgumentParser(description='检查本机模型接口；仅发送虚构测试文本。')
    parser.add_argument('--config', type=Path, default=CONFIG_PATH)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        client = AIClient(config)
        if config['model'] not in client.models():
            raise AIError('配置中的模型 ID 不在当前服务模型列表中。')
        answer = client.complete([
            {'role': 'system', 'content': 'Return only the exact JSON object {"ok":true}.'},
            {'role': 'user', 'content': 'Fictional connectivity test. No personal messages are supplied.'}], max_tokens=1024)
        candidate = answer.strip()
        if candidate.startswith('```json') and candidate.endswith('```'):
            candidate = candidate[7:-3].strip()
        if strict_json(candidate) != {'ok': True}:
            raise AIError('服务可连接，但未通过结构化输出测试。')
        print(json.dumps({'connected': True, 'requested_model': config['model'],
                          'reported_model': client.last_response_model,
                          'chat_upload_enabled': config['allow_chat_upload'],
                          'test_data': 'fictional_only'}, ensure_ascii=False, indent=2))
    except (AIError, ValueError):
        # Avoid printing configuration, remote error bodies or response contents.
        parser.exit(1, '模型检查未通过。请检查本机服务、配置、模型可用性与结构化输出支持。\n')


if __name__ == '__main__':
    main()
