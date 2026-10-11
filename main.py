import json
import re
import time
import uuid

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request

app = Flask(__name__)
setattr(app.json, "sort_keys", False)
setattr(app.json, "ensure_ascii", False)
MODEL_NAME = "gemini-web"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


class GeminiSessionError(RuntimeError):
    pass


def extract_token(html: str):
    patterns = [
        r'"SNlM0e":"([^"]+)"',
        r"'SNlM0e':'([^']+)'",
        r'SNlM0e["\']?\s*[:=]\s*["\']([^"\']+)',
        r'"FdrFJe":"([^"]+)"',
        r'"cfb2h":"([^"]+)"',
        r'"at":"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html, re.IGNORECASE)
        if match and len(match.group(1)) > 20:
            return match.group(1)

    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        content = script.string or script.get_text()
        if any(key in content for key in ("SNlM0e", "FdrFJe", "cfb2h")):
            for pattern in patterns:
                match = re.search(pattern, content, re.IGNORECASE)
                if match and len(match.group(1)) > 20:
                    return match.group(1)
    return None


def extract_params(html: str):
    def first(patterns, default=None):
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                return match.group(1)
        return default

    return {
        "bl": first([
            r'bl["\']?\s*[:=]\s*["\']([^"\']+)',
            r'buildLabel["\']?\s*[:=]\s*["\']([^"\']+)',
        ], "boq_assistant-bard-web-server_20251231.00_p0"),
        "fsid": first([
            r'f\.sid["\']?\s*[:=]\s*["\']?([^"\'\s&]+)',
            r'"fsid":"([^"]+)"',
        ], str(-int(time.time() * 1000))),
        "reqid": int(first([r'_reqid["\']?\s*[:=]\s*["\']?(\d+)'], str(int(time.time() * 1000) % 1_000_000))),
    }


def create_session():
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; GemChat/1.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    page = session.get("https://gemini.google.com/app", headers=headers, timeout=30)
    page.raise_for_status()
    token = extract_token(page.text)
    if not token:
        raise GeminiSessionError("No se encontró el token de sesión de Gemini")
    params = extract_params(page.text)
    return session, token, params


def build_payload(prompt, token):
    prompt_esc = prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    session_id = uuid.uuid4().hex
    request_id = str(uuid.uuid4()).upper()
    data = [
        [prompt_esc, 0, None, None, None, None, 0],
        ["en-US"],
        ["", "", "", None, None, None, None, None, None, ""],
        token,
        session_id,
        None,
        [0],
        1,
        None,
        None,
        1,
        0,
        None,
        None,
        None,
        None,
        None,
        [[0]],
        0,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        1,
        None,
        None,
        [4],
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        [2],
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        0,
        None,
        None,
        None,
        None,
        None,
        request_id,
        None,
        []
    ]
    payload_str = json.dumps(data, separators=(",", ":"))
    esc_payload = payload_str.replace("\\", "\\\\").replace('"', '\\"')
    return {"f.req": f'[null,"{esc_payload}"]', "": ""}

def parse_response(text: str):
    answer = ""
    for line in text.strip().splitlines():
        if not line or line.startswith(")]}") or line.isdigit():
            continue
        try:
            outer = json.loads(line)
            if not isinstance(outer, list) or not outer or outer[0][0] != "wrb.fr":
                continue
            inner = outer[0][2]
            parsed = json.loads(inner)
            if len(parsed) <= 4 or not isinstance(parsed[4], list) or not parsed[4]:
                continue
            item = parsed[4][0]
            if isinstance(item, list) and len(item) > 1 and isinstance(item[1], list):
                candidate = item[1][0] if item[1] else ""
                if isinstance(candidate, str) and len(candidate) > len(answer):
                    answer = candidate
        except (ValueError, IndexError, KeyError, TypeError):
            continue
    return answer.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\") or None


def ask_gemini(prompt: str):
    session, token, params = create_session()
    url = (
        "https://gemini.google.com/_/BardChatUi/data/assistant.lamda.BardFrontendService/"
        f"StreamGenerate?bl={params['bl']}&f.sid={params['fsid']}&hl=en-US&"
        f"_reqid={params['reqid']}&rt=c"
    )
    cookies = "; ".join(f"{key}={value}" for key, value in session.cookies.items())
    response = session.post(
        url,
        data=build_payload(prompt, token),
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; GemChat/1.0)",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": "https://gemini.google.com",
            "Referer": "https://gemini.google.com/",
            "X-Same-Domain": "1",
            "Cookie": cookies,
        },
        timeout=60,
    )
    response.raise_for_status()
    answer = parse_response(response.text)
    if not answer:
        raise GeminiSessionError("Gemini no devolvió una respuesta interpretable")
    return answer


def json_response(data, status=200):
    response = jsonify(data)
    response.status_code = status
    response.headers.update(CORS)
    return response


@app.get("/health")
def health():
    return json_response({"ok": True, "model": MODEL_NAME, "service": "GemChat"})


@app.route("/chat", methods=["GET", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return json_response({}, 204)
    prompt = request.args.get("prompt", "").strip()
    if not prompt:
        return json_response({"success": False, "message": "Prompt parameter required"}, 400)
    if len(prompt) > 8_000:
        return json_response({"success": False, "message": "Prompt is too long"}, 413)
    try:
        answer = ask_gemini(prompt)
        return json_response({
            "success": True,
            "response": answer,
        })
    except Exception:
        return json_response({"success": False, "message": "Error processing request"}, 400)