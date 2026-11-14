"""ComfyUI worker executed by Windows Python through WSL interop.

Only JSON enters stdin/stdout. ComfyUI stays on Windows loopback, and image
bytes return through the subprocess rather than a new network listener.
"""
from __future__ import annotations
import base64
import copy
import json
import secrets
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

def run(payload: dict) -> dict:
    """Inspect ComfyUI or submit one rendered workflow and retrieve its image.

    Args:
        payload: Trusted host configuration and a validated image plan.

    Returns:
        JSON-safe status or base64 image with its prompt ID.

    Raises:
        RuntimeError: Configuration, queue, execution, or image retrieval fails.
    """
    base = str(payload.get('base_url', __import__('share_runtime').resolve('{{AB_CAPABILITY_AGENT_ANIMA_COMFY_URL}}'))).rstrip('/')
    url = urlsplit(base)
    if url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise RuntimeError('ComfyUI worker requires a local loopback HTTP endpoint.')
    timeout = max(15, int(payload.get('timeout', 240)))
    max_response_bytes = max(1, int(payload.get('max_response_bytes', 128 * 1024 * 1024)))
    deadline = time.monotonic() + timeout
    opener = build_opener(ProxyHandler({}))

    def request(path: str, body=None, image=False):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('ComfyUI request deadline exceeded.')
        data = None if body is None else json.dumps(body).encode('utf-8')
        req = Request(base + path, data=data, headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=max(0.1, min(15, remaining))) as response:
            raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise RuntimeError('ComfyUI response exceeds configured limit.')
            if image:
                if not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise RuntimeError('ComfyUI did not return a PNG image.')
                return raw
            return json.loads(raw)
    stats = request('/system_stats')
    if payload.get('action') == 'status':
        queue = request('/queue')
        return {'ok': True, 'version': stats['system'].get('comfyui_version'), 'devices': [d['name'] for d in stats.get('devices', [])], 'running': len(queue.get('queue_running', [])), 'pending': len(queue.get('queue_pending', []))}
    document = json.loads(Path(payload['workflow']).read_text(encoding='utf-8-sig'))
    prompt = copy.deepcopy(document.get('prompt', document))
    replacements = {'{{CHATARCHIVE_STYLE}}': str(payload.get('style', '')), '{{CHATARCHIVE_CHARACTERS}}': str(payload.get('characters', '')), '{{CHATARCHIVE_SCENE}}': str(payload.get('scene', '')), '{{CHATARCHIVE_PROMPT}}': str(payload.get('prompt', ''))}
    positive_nodes = []
    latent_nodes = []
    for node_id, node in prompt.items():
        inputs = node.get('inputs', {})
        for key, value in list(inputs.items()):
            if isinstance(value, str):
                for marker, replacement in replacements.items():
                    value = value.replace(marker, replacement)
                if '{{CHATARCHIVE_' in value:
                    raise RuntimeError('An unresolved workflow placeholder remains.')
                inputs[key] = value
        if node.get('class_type') == 'MoyuuDeepSeekRewrite':
            raise RuntimeError('Use the original Anima template, without a rewrite node.')
        source_text = str(document.get('prompt', document)[node_id].get('inputs', {}).get('text', ''))
        if node.get('class_type') == 'CLIPTextEncode' and ('{{CHATARCHIVE_SCENE}}' in source_text or '{{CHATARCHIVE_PROMPT}}' in source_text):
            positive_nodes.append(node)
        if node.get('class_type') in {'EmptyLatentImage', 'EmptySD3LatentImage'}:
            inputs.update(width=int(payload['width']), height=int(payload['height']), batch_size=1)
            latent_nodes.append(node)
        if node.get('class_type') == 'KSampler':
            inputs['seed'] = secrets.randbelow(2 ** 48)
        if node.get('class_type') == 'SaveImage':
            inputs['filename_prefix'] = 'AstrBot_Anima'
    if not positive_nodes or not latent_nodes:
        raise RuntimeError('The Anima template must contain a scene/prompt node and a latent node.')
    if payload.get('mode') == 'direct':
        direct_prompt = str(payload.get('prompt', '')).strip()
        if not direct_prompt:
            raise RuntimeError('Direct mode requires a final positive prompt.')
        positive_nodes[0]['inputs']['text'] = direct_prompt
    if payload.get('action') == 'validate':
        return {'ok': True, 'nodes': len(prompt), 'width': int(payload['width']), 'height': int(payload['height']), 'positive': positive_nodes[0]['inputs']['text']}
    queue = request('/queue')
    max_queue = int(payload.get('max_queue', 0) or 0)
    queue_depth = len(queue.get('queue_running', [])) + len(queue.get('queue_pending', []))
    if max_queue > 0 and queue_depth >= max_queue:
        raise RuntimeError('ComfyUI is busy; please retry after the existing jobs finish.')
    submitted = request('/prompt', {'prompt': prompt, 'client_id': str(uuid.uuid4())})
    prompt_id = submitted.get('prompt_id')
    if not prompt_id or submitted.get('node_errors'):
        errors = json.dumps(submitted.get('node_errors') or {}, ensure_ascii=True)[:400]
        raise RuntimeError('ComfyUI rejected the workflow: ' + errors)
    completed = False
    try:
        while time.monotonic() < deadline:
            history = request('/history/' + prompt_id).get(prompt_id)
            if history:
                status = history.get('status', {})
                if status.get('status_str') == 'error':
                    raise RuntimeError('ComfyUI execution failed; prompt_id=' + prompt_id)
                if status.get('completed'):
                    for output in history.get('outputs', {}).values():
                        for image in output.get('images', []):
                            if image.get('type') == 'output':
                                raw = request('/view?' + urlencode(image), image=True)
                                completed = True
                                return {'ok': True, 'prompt_id': prompt_id, 'image': base64.b64encode(raw).decode('ascii')}
                    raise RuntimeError('ComfyUI completed without an output image.')
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise TimeoutError('ComfyUI image deadline exceeded; prompt_id=' + prompt_id)
    finally:
        if not completed:
            try:
                cleanup = Request(base + '/queue', data=json.dumps({'delete': [prompt_id]}).encode('utf-8'), headers={'Content-Type': 'application/json'})
                opener.open(cleanup, timeout=3).close()
            except Exception:
                pass
if __name__ == '__main__':
    try:
        result = run(json.loads(sys.stdin.buffer.read().decode('utf-8-sig')))
    except Exception as error:
        result = {'ok': False, 'error': str(error)[:500]}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode('utf-8'))
