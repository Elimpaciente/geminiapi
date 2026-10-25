import json
import mimetypes
import os
import re
import time
import uuid
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)
setattr(app.json, "sort_keys", False)
setattr(app.json, "ensure_ascii", False)

MODEL_NAME = "gemini-web"
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS, HEAD",
    "Access-Control-Allow-Headers": "Content-Type",
}

TEMP_DIR = Path(os.environ.get("TEMP_DIR", "/tmp/gemini_media"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)
FILE_INDEX = {}


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
        "bl": first(
            [
                r'bl["\']?\s*[:=]\s*["\']([^"\']+)',
                r'buildLabel["\']?\s*[:=]\s*["\']([^"\']+)',
                r"(boq_gemini-web-uiserver_[^\"'\s]+)",
            ],
            "boq_gemini-web-uiserver_20261023.01_p0",
        ),
        "fsid": first(
            [
                r'f\.sid["\']?\s*[:=]\s*["\']?([^"\'\s&]+)',
                r'"FdrFJe":"([^"]+)"',
                r'"fsid":"([^"]+)"',
            ],
            str(-int(time.time() * 1000)),
        ),
        "reqid": int(
            first(
                [r'_reqid["\']?\s*[:=]\s*["\']?(\d+)'],
                str(int(time.time() * 1000) % 1_000_000),
            )
        ),
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
        token = extract_params(page.text).get("fsid") or uuid.uuid4().hex
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
        [],
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
        timeout=90,
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


def public_base_url():
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.host
    return f"{proto}://{host}".rstrip("/")


def save_temp_file(data: bytes, filename: str = "file.bin"):
    ext = Path(filename).suffix.lower() or ""
    if not ext:
        if data[:3] == b"\xff\xd8\xff":
            ext = ".jpg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            ext = ".png"
        elif len(data) > 8 and data[4:8] == b"ftyp":
            ext = ".mp4"
        else:
            ext = ".bin"
    file_id = uuid.uuid4().hex
    path = TEMP_DIR / f"{file_id}{ext}"
    path.write_bytes(data)
    mime = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    FILE_INDEX[file_id] = {"path": str(path), "mime": mime, "name": filename}
    return file_id


def read_multipart(field_names):
    for name in field_names:
        if name in request.files and request.files[name].filename:
            f = request.files[name]
            data = f.read()
            if data:
                return data, f.filename or name
    return None, None


@app.get("/health")
def health():
    return json_response({"ok": True, "model": MODEL_NAME, "service": "GemChat"})


@app.route("/file/<file_id>", methods=["GET", "HEAD", "OPTIONS"])
def serve_file(file_id):
    if request.method == "OPTIONS":
        return json_response({}, 204)
    meta = FILE_INDEX.get(file_id)
    path = None
    mime = "application/octet-stream"
    if meta:
        path = meta["path"]
        mime = meta.get("mime") or mime
    else:
        matches = list(TEMP_DIR.glob(f"{file_id}.*"))
        if matches:
            path = str(matches[0])
            mime = mimetypes.guess_type(path)[0] or mime
    if not path or not os.path.isfile(path):
        return json_response({"success": False, "response": "File not found"}, 404)
    if request.method == "HEAD":
        resp = jsonify({})
        resp.status_code = 200
        resp.headers.update(CORS)
        resp.headers["Content-Type"] = mime
        resp.headers["Content-Length"] = str(os.path.getsize(path))
        return resp
    return send_file(path, mimetype=mime, conditional=True)


@app.route("/chat", methods=["GET", "POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return json_response({}, 204)
    prompt = ""
    if request.method == "GET":
        prompt = (request.args.get("prompt") or "").strip()
    else:
        prompt = (
            (request.form.get("prompt") or request.form.get("message") or "")
            or ((request.json or {}).get("prompt") if request.is_json else "")
            or ((request.json or {}).get("message") if request.is_json else "")
            or (request.args.get("prompt") or "")
        ).strip()
    if not prompt:
        return json_response({"success": False, "response": "Prompt parameter required"}, 400)
    if len(prompt) > 8_000:
        return json_response({"success": False, "response": "Prompt is too long"}, 413)
    try:
        answer = ask_gemini(prompt)
        return json_response({"success": True, "response": answer})
    except Exception:
        return json_response({"success": False, "response": "Error processing request"}, 400)


@app.route("/mitch", methods=["POST", "OPTIONS"])
def mitch():
    if request.method == "OPTIONS":
        return json_response({}, 204)
    prompt = (request.form.get("prompt") or "").strip() or "Describe esta imagen con detalle."
    media, filename = read_multipart(["image", "file", "photo"])
    url_in = (request.form.get("url") or "").strip()
    try:
        if media:
            file_id = save_temp_file(media, filename or "image.jpg")
            media_url = f"{public_base_url()}/file/{file_id}"
        elif url_in:
            media_url = url_in
        else:
            return json_response({"success": False, "response": "Missing file field: image or url"}, 400)
        full_prompt = f"{prompt}\n\n{media_url}"
        answer = ask_gemini(full_prompt)
        return json_response({"success": True, "response": answer})
    except Exception:
        return json_response({"success": False, "response": "Error processing request"}, 400)


@app.route("/video", methods=["POST", "OPTIONS"])
def video():
    if request.method == "OPTIONS":
        return json_response({}, 204)
    prompt = (request.form.get("prompt") or "").strip() or "Describe este video con detalle."
    media, filename = read_multipart(["video", "file", "media"])
    url_in = (request.form.get("url") or "").strip()
    try:
        if media:
            file_id = save_temp_file(media, filename or "video.mp4")
            media_url = f"{public_base_url()}/file/{file_id}"
        elif url_in:
            media_url = url_in
        else:
            return json_response({"success": False, "response": "Missing file field: video or url"}, 400)
        full_prompt = f"{prompt}\n\n{media_url}"
        answer = ask_gemini(full_prompt)
        return json_response({"success": True, "response": answer})
    except Exception:
        return json_response({"success": False, "response": "Error processing request"}, 400)


if __name__ == "__main__":
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="0.0.0.0", port=5000)
