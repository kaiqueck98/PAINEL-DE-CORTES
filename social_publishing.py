import base64
import hashlib
import json
import mimetypes
import os
import secrets
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet, InvalidToken
from dotenv import dotenv_values, set_key


SOCIAL_TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "social_tokens.enc")
SOCIAL_TOKENS_LOCK = threading.Lock()

TIKTOK_AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
YOUTUBE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
YOUTUBE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"


class SocialPublishingError(Exception):
    pass


class SocialPublishingPending(SocialPublishingError):
    pass


class SocialPublishingRejected(SocialPublishingError):
    pass


def ensure_token_encryption_key(dotenv_path=None):
    dotenv_path = os.path.abspath(
        dotenv_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    )
    try:
        dotenv_key = (dotenv_values(dotenv_path).get("SOCIAL_TOKEN_ENCRYPTION_KEY") or "").strip()
    except (OSError, UnicodeError) as exc:
        raise SocialPublishingError("Não foi possível ler o arquivo .env.") from exc

    environment_key = os.environ.get("SOCIAL_TOKEN_ENCRYPTION_KEY", "").strip()
    key = ""
    dotenv_key_valid = False
    for candidate in (environment_key, dotenv_key):
        if not candidate:
            continue
        try:
            Fernet(candidate.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            continue
        if candidate == dotenv_key:
            dotenv_key_valid = True
        key = candidate
        break

    should_write_key = not dotenv_key_valid
    if not key:
        if (environment_key or dotenv_key) and os.path.isfile(SOCIAL_TOKENS_FILE):
            raise SocialPublishingError(
                "A chave de criptografia configurada é inválida e o arquivo de tokens existe; "
                "a substituição automática foi interrompida para preservar os dados."
            )
        key = Fernet.generate_key().decode("ascii")
        should_write_key = True

    if should_write_key:
        try:
            saved, _, _ = set_key(
                dotenv_path,
                "SOCIAL_TOKEN_ENCRYPTION_KEY",
                key,
                quote_mode="always",
            )
            if not saved:
                raise OSError("set_key não conseguiu gravar a chave.")
        except (OSError, UnicodeError, AssertionError) as exc:
            raise SocialPublishingError(
                "Não foi possível salvar automaticamente a chave no arquivo .env."
            ) from exc

    os.environ["SOCIAL_TOKEN_ENCRYPTION_KEY"] = key
    return key


def platform_display_name(platform):
    return "TikTok" if platform == "tiktok" else "YouTube"


def _platform_config(platform):
    if platform == "tiktok":
        client_id = os.environ.get("TIKTOK_CLIENT_KEY", "").strip()
        client_secret = os.environ.get("TIKTOK_CLIENT_SECRET", "").strip()
        redirect_uri = os.environ.get(
            "TIKTOK_REDIRECT_URI",
            "http://localhost:5000/oauth/tiktok/callback",
        ).strip()
    elif platform == "youtube":
        client_id = os.environ.get("YOUTUBE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET", "").strip()
        redirect_uri = os.environ.get(
            "YOUTUBE_REDIRECT_URI",
            "http://localhost:5000/oauth/youtube/callback",
        ).strip()
    else:
        raise SocialPublishingError("Plataforma de publicação inválida.")

    missing = [
        name
        for name, value in (
            ("client ID", client_id),
            ("client secret", client_secret),
            ("redirect URI", redirect_uri),
        )
        if not value
    ]
    if missing:
        raise SocialPublishingError(
            f"Configure as credenciais de {platform_display_name(platform)} no arquivo .env."
        )
    return client_id, client_secret, redirect_uri


def _fernet():
    key = os.environ.get("SOCIAL_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise SocialPublishingError(
            "Configure SOCIAL_TOKEN_ENCRYPTION_KEY no arquivo .env para proteger os tokens."
        )
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SocialPublishingError(
            "SOCIAL_TOKEN_ENCRYPTION_KEY não é uma chave Fernet válida."
        ) from exc


def validate_token_encryption():
    _fernet()


def load_social_tokens():
    if not os.path.isfile(SOCIAL_TOKENS_FILE):
        return {}
    cipher = _fernet()
    try:
        with open(SOCIAL_TOKENS_FILE, "rb") as file:
            encrypted = file.read()
        payload = json.loads(cipher.decrypt(encrypted).decode("utf-8"))
    except (OSError, InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SocialPublishingError(
            "Não foi possível ler os tokens sociais protegidos; confira a chave configurada."
        ) from exc
    if not isinstance(payload, dict):
        raise SocialPublishingError("O arquivo de tokens sociais tem um formato inválido.")
    if any(
        platform not in {"tiktok", "youtube"} or not isinstance(token_data, dict)
        for platform, token_data in payload.items()
    ):
        raise SocialPublishingError("O arquivo de tokens sociais tem um formato inválido.")
    if any(
        not isinstance(token_data.get("access_token"), str)
        or not isinstance(token_data.get("refresh_token"), str)
        for token_data in payload.values()
    ):
        raise SocialPublishingError("O arquivo de tokens sociais tem um formato inválido.")
    return payload


def save_social_tokens(tokens):
    cipher = _fernet()
    directory = os.path.dirname(SOCIAL_TOKENS_FILE)
    temporary_path = None
    try:
        os.makedirs(directory, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=".social-tokens-",
            delete=False,
        ) as file:
            temporary_path = file.name
            file.write(cipher.encrypt(json.dumps(tokens).encode("utf-8")))
        os.replace(temporary_path, SOCIAL_TOKENS_FILE)
    except OSError as exc:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError as cleanup_error:
                raise SocialPublishingError(
                    "Não foi possível salvar nem limpar o arquivo temporário de tokens."
                ) from cleanup_error
        raise SocialPublishingError("Não foi possível salvar os tokens sociais protegidos.") from exc


def store_authorized_tokens(platform, token_response):
    if platform not in {"tiktok", "youtube"}:
        raise SocialPublishingError("Plataforma de publicação inválida.")
    if platform == "tiktok":
        data = token_response.get("data", token_response)
        if not isinstance(data, dict) or not data.get("access_token"):
            raise SocialPublishingError("TikTok não retornou um token de acesso válido.")
    else:
        data = token_response
        if not isinstance(data, dict) or not data.get("access_token"):
            raise SocialPublishingError("YouTube não retornou um token de acesso válido.")

    with SOCIAL_TOKENS_LOCK:
        tokens = load_social_tokens()
        previous = tokens.get(platform, {})
        refresh_token = data.get("refresh_token") or previous.get("refresh_token")
        if not refresh_token:
            raise SocialPublishingError(
                f"{platform_display_name(platform)} não forneceu um token de renovação. Tente conectar novamente."
            )
        try:
            expires_in = int(data.get("expires_in") or 3600)
        except (TypeError, ValueError) as exc:
            raise SocialPublishingError("A plataforma retornou um prazo de token inválido.") from exc
        tokens[platform] = {
            "access_token": data["access_token"],
            "refresh_token": refresh_token,
            "expires_at": time.time() + expires_in,
            "open_id": data.get("open_id", previous.get("open_id", "")),
        }
        save_social_tokens(tokens)


def generate_tiktok_code_verifier():
    return secrets.token_urlsafe(64)


def tiktok_code_challenge(code_verifier):
    allowed_characters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    if (
        not isinstance(code_verifier, str)
        or not 43 <= len(code_verifier) <= 128
        or any(character not in allowed_characters for character in code_verifier)
    ):
        raise SocialPublishingError("O code_verifier do TikTok não está em um formato válido.")
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(platform, state, code_challenge=None):
    client_id, _, redirect_uri = _platform_config(platform)
    if platform == "tiktok":
        challenge_characters = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        if (
            not isinstance(code_challenge, str)
            or len(code_challenge) != 43
            or any(character not in challenge_characters for character in code_challenge)
        ):
            raise SocialPublishingError("O desafio PKCE do TikTok não está em um formato válido.")
        params = {
            "client_key": client_id,
            "response_type": "code",
            "scope": "user.info.basic,video.upload",
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        base_url = TIKTOK_AUTHORIZE_URL
    else:
        params = {
            "client_id": client_id,
            "response_type": "code",
            "scope": "https://www.googleapis.com/auth/youtube.upload",
            "redirect_uri": redirect_uri,
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
        }
        base_url = YOUTUBE_AUTHORIZE_URL
    return f"{base_url}?{urlencode(params)}"


def _http_request(url, data=None, headers=None, method=None, timeout=60):
    request = Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.headers, response.read()
    except HTTPError as error:
        return error.code, error.headers, error.read()
    except (URLError, TimeoutError, OSError) as exc:
        raise SocialPublishingError("Não foi possível conectar à API da plataforma.") from exc


def _response_message(body, fallback):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return fallback
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("message") or error.get("error_description") or fallback
    if isinstance(error, str):
        return payload.get("error_description") or error
    return fallback


def _json_request(url, payload, headers=None, method="POST"):
    request_headers = {"Content-Type": "application/json; charset=UTF-8"}
    request_headers.update(headers or {})
    status, _, body = _http_request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method=method,
    )
    if not 200 <= status < 300:
        raise SocialPublishingError(_response_message(body, f"A plataforma retornou HTTP {status}."))
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SocialPublishingError("A plataforma retornou uma resposta inválida.") from exc


def exchange_authorization_code(platform, code, code_verifier=None):
    client_id, client_secret, redirect_uri = _platform_config(platform)
    fields = {
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    if platform == "tiktok":
        if not code_verifier:
            raise SocialPublishingError("O TikTok não recebeu o code_verifier da sessão OAuth.")
        fields.update({"client_key": client_id, "client_secret": client_secret})
        fields["code_verifier"] = code_verifier
        token_url = TIKTOK_TOKEN_URL
    else:
        fields.update({"client_id": client_id, "client_secret": client_secret})
        token_url = YOUTUBE_TOKEN_URL
    status, _, body = _http_request(
        token_url,
        data=urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not 200 <= status < 300:
        raise SocialPublishingError(_response_message(body, f"A autorização retornou HTTP {status}."))
    try:
        token_response = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SocialPublishingError("A plataforma retornou uma resposta de autorização inválida.") from exc
    if not isinstance(token_response, dict):
        raise SocialPublishingError("A plataforma retornou uma resposta de autorização inválida.")
    if token_response.get("error"):
        raise SocialPublishingError(_response_message(body, "A plataforma recusou a autorização."))
    store_authorized_tokens(platform, token_response)


def _refresh_access_token(platform, refresh_token):
    client_id, client_secret, _ = _platform_config(platform)
    if platform == "tiktok":
        fields = {
            "client_key": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        token_url = TIKTOK_TOKEN_URL
    else:
        fields = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        token_url = YOUTUBE_TOKEN_URL
    status, _, body = _http_request(
        token_url,
        data=urlencode(fields).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if not 200 <= status < 300:
        raise SocialPublishingError(
            f"A sessão de {platform_display_name(platform)} expirou. Reconecte a conta para continuar."
        )
    try:
        response = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SocialPublishingError("A plataforma retornou uma resposta de renovação inválida.") from exc
    if not isinstance(response, dict):
        raise SocialPublishingError("A plataforma retornou uma resposta de renovação inválida.")
    if response.get("error"):
        raise SocialPublishingError(
            f"A sessão de {platform_display_name(platform)} expirou. Reconecte a conta para continuar."
        )
    data = response.get("data", response) if platform == "tiktok" else response
    if not isinstance(data, dict):
        raise SocialPublishingError("A plataforma retornou uma resposta de renovação inválida.")
    if not data.get("access_token"):
        raise SocialPublishingError("A plataforma não renovou o token de acesso.")
    return data


def get_access_token(platform):
    with SOCIAL_TOKENS_LOCK:
        tokens = load_social_tokens()
        token_data = tokens.get(platform)
        if not token_data or not token_data.get("access_token"):
            raise SocialPublishingError(
                f"Conecte a conta do {platform_display_name(platform)} antes de publicar."
            )
        try:
            expires_at = float(token_data.get("expires_at", 0))
        except (TypeError, ValueError) as exc:
            raise SocialPublishingError("O prazo do token salvo é inválido. Reconecte a conta.") from exc
        if expires_at > time.time() + 60:
            return token_data["access_token"]
        refresh_token = token_data.get("refresh_token")
        if not refresh_token:
            raise SocialPublishingError(
                f"Reconecte a conta do {platform_display_name(platform)} para publicar."
            )
        refreshed = _refresh_access_token(platform, refresh_token)
        try:
            expires_in = int(refreshed.get("expires_in") or 3600)
        except (TypeError, ValueError) as exc:
            raise SocialPublishingError("A plataforma retornou um prazo de token inválido.") from exc
        token_data.update(
            {
                "access_token": refreshed["access_token"],
                "refresh_token": refreshed.get("refresh_token", refresh_token),
                "expires_at": time.time() + expires_in,
            }
        )
        tokens[platform] = token_data
        save_social_tokens(tokens)
        return token_data["access_token"]


def _check_tiktok_response(payload):
    if not isinstance(payload, dict):
        raise SocialPublishingError("TikTok retornou uma resposta inválida.")
    error = payload.get("error")
    if error and (not isinstance(error, dict) or error.get("code") != "ok"):
        if isinstance(error, dict):
            message = error.get("message") or error.get("code")
        else:
            message = str(error)
        raise SocialPublishingError(message or "A API do TikTok recusou a solicitação.")
    data = payload.get("data", {})
    if not isinstance(data, dict):
        raise SocialPublishingError("TikTok retornou uma resposta inválida.")
    return data


def _tiktok_request(endpoint, token, payload):
    payload = _json_request(
        f"{TIKTOK_API_BASE}{endpoint}",
        payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    return _check_tiktok_response(payload)


def upload_tiktok_inbox(video_path, token, progress_callback=None, publish_id_callback=None):
    file_size = os.path.getsize(video_path)
    if file_size <= 0:
        raise SocialPublishingError("O arquivo de vídeo está vazio.")
    if file_size > 4 * 1024 * 1024 * 1024:
        raise SocialPublishingError("O TikTok aceita vídeos de até 4 GB.")

    chunk_size = min(10_000_000, file_size)
    chunk_count = max(1, (file_size + chunk_size - 1) // chunk_size)
    init_data = _tiktok_request(
        "/post/publish/inbox/video/init/",
        token,
        {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": chunk_size,
                "total_chunk_count": chunk_count,
            },
        },
    )
    publish_id = init_data.get("publish_id")
    upload_url = init_data.get("upload_url")
    if not publish_id or not upload_url:
        raise SocialPublishingError("TikTok não forneceu os dados necessários para enviar o vídeo.")
    if publish_id_callback:
        publish_id_callback(publish_id)
    upload_host = urlsplit(upload_url).hostname or ""
    if (
        urlsplit(upload_url).scheme != "https"
        or not (upload_host == "tiktokapis.com" or upload_host.endswith(".tiktokapis.com"))
    ):
        raise SocialPublishingError("TikTok retornou um endereço de upload inválido.")

    mime_type = mimetypes.guess_type(video_path)[0] or "video/mp4"
    if mime_type not in {"video/mp4", "video/quicktime", "video/webm"}:
        raise SocialPublishingError("O formato do vídeo não é aceito pelo TikTok.")
    with open(video_path, "rb") as video_file:
        start = 0
        for index in range(chunk_count):
            chunk_length = chunk_size if index < chunk_count - 1 else file_size - start
            chunk = video_file.read(chunk_length)
            if len(chunk) != chunk_length:
                raise SocialPublishingError("Não foi possível ler o arquivo completo para envio.")
            end = start + chunk_length - 1
            status, _, body = _http_request(
                upload_url,
                data=chunk,
                headers={
                    "Content-Type": mime_type,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                },
                method="PUT",
                timeout=300,
            )
            expected_status = 201 if index == chunk_count - 1 else 206
            if status != expected_status:
                raise SocialPublishingError(
                    _response_message(body, f"O TikTok recusou o bloco do vídeo (HTTP {status}).")
                )
            start += chunk_length
            if progress_callback:
                progress_callback("uploading", 25 + round(start / file_size * 60), "Enviando vídeo ao TikTok...")

    if progress_callback:
        progress_callback("processing", 90, "TikTok está preparando o rascunho...")
    for _ in range(360):
        status_data = _tiktok_request(
            "/post/publish/status/fetch/",
            token,
            {"publish_id": publish_id},
        )
        publish_status = status_data.get("status", "")
        if publish_status == "SEND_TO_USER_INBOX":
            if progress_callback:
                progress_callback(
                    "complete",
                    100,
                    "Rascunho enviado à caixa de entrada do TikTok.",
                )
            return publish_id, "inbox"
        if publish_status == "PUBLISH_COMPLETE":
            if progress_callback:
                progress_callback("complete", 100, "O vídeo foi publicado pelo TikTok.")
            return publish_id, "posted"
        if publish_status == "FAILED":
            reason = status_data.get("fail_reason", "O TikTok não conseguiu enviar o rascunho.")
            raise SocialPublishingRejected(f"O TikTok não conseguiu enviar o rascunho: {reason}")
        if progress_callback:
            progress_callback("processing", 95, "TikTok ainda está preparando o rascunho...")
        time.sleep(5)
    raise SocialPublishingPending(
        f"O TikTok ainda está preparando o rascunho (ID {publish_id}). Confira a caixa de entrada antes de tentar novamente."
    )


def publish_youtube(
    video_path,
    title,
    hashtags,
    token,
    progress_callback=None,
    upload_started_callback=None,
):
    file_size = os.path.getsize(video_path)
    if file_size <= 0:
        raise SocialPublishingError("O arquivo de vídeo está vazio.")
    mime_type = mimetypes.guess_type(video_path)[0] or "application/octet-stream"
    if not mime_type.startswith("video/"):
        mime_type = "application/octet-stream"
    metadata = {
        "snippet": {
            "title": title,
            "description": hashtags,
            "categoryId": "22",
        },
        "status": {"privacyStatus": "private"},
    }
    start_url = f"{YOUTUBE_UPLOAD_URL}?{urlencode({'uploadType': 'resumable', 'part': 'snippet,status'})}"
    status, headers, body = _http_request(
        start_url,
        data=json.dumps(metadata).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(file_size),
            "X-Upload-Content-Type": mime_type,
        },
        method="POST",
    )
    if status != 200:
        raise SocialPublishingError(_response_message(body, f"YouTube recusou o início do envio (HTTP {status})."))
    upload_url = headers.get("Location")
    if not upload_url or not upload_url.startswith("https://www.googleapis.com/"):
        raise SocialPublishingError("YouTube não forneceu uma sessão de upload válida.")
    if upload_started_callback:
        upload_started_callback()

    chunk_size = 8 * 1024 * 1024
    start = 0
    with open(video_path, "rb") as video_file:
        while start < file_size:
            chunk = video_file.read(min(chunk_size, file_size - start))
            if not chunk:
                raise SocialPublishingError("Não foi possível ler o arquivo completo para envio.")
            end = start + len(chunk) - 1
            status, _, body = _http_request(
                upload_url,
                data=chunk,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": mime_type,
                    "Content-Length": str(len(chunk)),
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                },
                method="PUT",
                timeout=300,
            )
            if end + 1 == file_size:
                if status not in {200, 201}:
                    raise SocialPublishingError(
                        _response_message(body, f"YouTube recusou o vídeo (HTTP {status}).")
                    )
                try:
                    response = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise SocialPublishingError("YouTube retornou uma resposta de envio inválida.") from exc
                video_id = response.get("id")
                if not video_id:
                    raise SocialPublishingError("YouTube não confirmou a criação do vídeo.")
                if progress_callback:
                    progress_callback("complete", 100, "Vídeo enviado ao YouTube.")
                return video_id
            if status != 308:
                raise SocialPublishingError(
                    _response_message(body, f"YouTube interrompeu o envio (HTTP {status}).")
                )
            start += len(chunk)
            if progress_callback:
                progress_callback(
                    "uploading",
                    25 + round(start / file_size * 70),
                    "Enviando vídeo ao YouTube...",
                )
    raise SocialPublishingError("YouTube encerrou o envio sem confirmar o vídeo.")
