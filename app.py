import atexit
import json
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
DOTENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=DOTENV_FILE)

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, send_file, session, stream_with_context, url_for
from werkzeug.utils import secure_filename

import social_publishing
import autopost
social_publishing.ensure_token_encryption_key(DOTENV_FILE)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "").lower() in {
    "1",
    "true",
    "yes",
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv"}
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
GOOD_VIDEOS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bons.json")
GOOD_VIDEOS_LOCK = threading.Lock()
FOLDER_MODELS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modelos.json")
FOLDER_MODELS_LOCK = threading.Lock()
POSTED_VIDEOS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "postados.json")
POSTED_VIDEOS_LOCK = threading.Lock()
SOCIAL_POSTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publicados.json")
SOCIAL_POSTS_LOCK = threading.Lock()
SOCIAL_POST_JOBS = {}
SOCIAL_POST_JOBS_LOCK = threading.Lock()
SOCIAL_POST_JOB_TTL_SECONDS = 7200
IMPROVEMENT_JOBS = {}
IMPROVEMENT_JOBS_LOCK = threading.Lock()
IMPROVEMENT_JOB_TTL_SECONDS = 3600
AUTPOST_TIMEZONE_NAME = "America/Sao_Paulo"
AUTPOST_TIMEZONE = pytz.timezone(AUTPOST_TIMEZONE_NAME)
AUTPOST_STORE = autopost.AutopostStore(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "agendamentos.sqlite3")
)
AUTPOST_STORE.initialize()
AUTPOST_SCHEDULER = BackgroundScheduler(timezone=AUTPOST_TIMEZONE)
AUTPOST_SCHEDULER_LOCK = threading.Lock()
AUTPOST_SCHEDULER_STARTED = False


class DriveError(Exception):
    pass


class GoodVideosError(Exception):
    pass


class FolderModelsError(Exception):
    pass


class PostedVideosError(Exception):
    pass


class SocialPostsError(Exception):
    pass


def extract_folder_id(link):
    value = link.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value

    parsed = urlparse(value)
    if parsed.hostname not in {"drive.google.com", "www.drive.google.com"}:
        raise ValueError("Cole um link válido de uma pasta do Google Drive.")

    match = re.search(r"/folders/([A-Za-z0-9_-]+)", parsed.path)
    folder_id = match.group(1) if match else parse_qs(parsed.query).get("id", [None])[0]
    if not folder_id or not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
        raise ValueError("Não foi possível encontrar o ID da pasta nesse link.")
    return folder_id


def drive_api_get(path, params, api_key):
    query = dict(params)
    query["key"] = api_key
    url = f"{DRIVE_API_BASE}/{path}?{urlencode(query)}"
    try:
        with urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8"))
            message = details.get("error", {}).get("message", "Falha ao consultar o Google Drive.")
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = "Falha ao consultar o Google Drive."
        raise DriveError(message) from error
    except (URLError, TimeoutError) as error:
        raise DriveError("Não foi possível conectar à API do Google Drive.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise DriveError("A API do Google Drive retornou uma resposta inválida.") from error


def get_folder_name(folder_id, api_key):
    folder = drive_api_get(
        f"files/{quote(folder_id, safe='')}",
        {"fields": "name,mimeType"},
        api_key,
    )
    if folder.get("mimeType") != "application/vnd.google-apps.folder":
        raise DriveError("O link informado não aponta para uma pasta do Google Drive.")
    return folder.get("name", "Pasta sem nome")


def list_folder_contents(folder_id, api_key):
    files = []
    page_token = None
    query = f"'{folder_id}' in parents and trashed = false"

    while True:
        params = {
            "q": query,
            "pageSize": 1000,
            "fields": "nextPageToken,files(id,name,mimeType,webViewLink,thumbnailLink,videoMediaMetadata)",
        }
        if page_token:
            params["pageToken"] = page_token
        data = drive_api_get("files", params, api_key)
        files.extend(data.get("files", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return files


def get_folder_view(folder_id, api_key):
    root_name = get_folder_name(folder_id, api_key)
    nodes = {
        folder_id: {
            "id": folder_id,
            "name": root_name,
            "children": [],
            "videos": [],
        }
    }
    pending = [folder_id]
    visited = set()
    warnings = []

    while pending:
        current_id = pending.pop()
        if current_id in visited:
            continue
        visited.add(current_id)

        try:
            files = list_folder_contents(current_id, api_key)
        except DriveError as exc:
            if current_id == folder_id:
                raise
            warnings.append(f"Não foi possível acessar a pasta “{nodes[current_id]['name']}”: {exc}")
            continue

        for file in files:
            if file.get("mimeType") == "application/vnd.google-apps.folder":
                child_id = file["id"]
                if child_id not in nodes:
                    nodes[child_id] = {
                        "id": child_id,
                        "name": file.get("name", "Pasta sem nome"),
                        "children": [],
                        "videos": [],
                    }
                    nodes[current_id]["children"].append(child_id)
                    pending.append(child_id)
            elif os.path.splitext(file.get("name", ""))[1].lower() in VIDEO_EXTENSIONS:
                file["folderName"] = nodes[current_id]["name"]
                nodes[current_id]["videos"].append(file)

    for node in reversed(list(nodes.values())):
        node["video_count"] = len(node["videos"]) + sum(
            nodes[child_id]["video_count"] for child_id in node["children"]
        )

    current = nodes[folder_id]
    return {
        "folder_name": current["name"],
        "all_videos": [
            video
            for node in nodes.values()
            for video in node["videos"]
        ],
        "folders": [
            {
                "id": child_id,
                "name": nodes[child_id]["name"],
                "video_count": nodes[child_id]["video_count"],
            }
            for child_id in current["children"]
        ],
        "videos": current["videos"],
        "video_count": current["video_count"],
        "warnings": warnings,
    }


def format_duration(metadata):
    if not isinstance(metadata, dict):
        return ""
    try:
        total_seconds = int(metadata.get("durationMillis", 0)) // 1000
    except (AttributeError, TypeError, ValueError):
        return ""
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def video_duration_milliseconds(video):
    metadata = video.get("videoMediaMetadata") or {}
    try:
        return max(0, int(metadata.get("durationMillis", 0)))
    except (AttributeError, TypeError, ValueError):
        return 0


def video_quality(video):
    metadata = video.get("videoMediaMetadata") or {}
    try:
        width = int(metadata.get("width", 0))
        height = int(metadata.get("height", 0))
    except (AttributeError, TypeError, ValueError):
        width = height = 0
    if width <= 0 or height <= 0:
        return "Indisponível", False
    resolution = min(width, height)
    return f"{resolution}p", resolution < 720


def score_video(video, marked_good=False):
    duration_seconds = video_duration_milliseconds(video) / 1000

    if 30 <= duration_seconds <= 90:
        score = 5
    elif 20 <= duration_seconds < 30 or 90 < duration_seconds <= 120:
        score = 4
    elif 10 <= duration_seconds < 20 or 120 < duration_seconds <= 180:
        score = 3
    elif duration_seconds > 0:
        score = 1.5
    else:
        score = 0

    filename = os.path.splitext(video.get("name", ""))[0].casefold()
    words = {word for word in re.findall(r"[^\W_]+", filename) if len(word) > 2 and not word.isdigit()}
    title_points = min(len(words), 4) * 0.45
    title_points += min(len(words & {"corte", "final", "pronto", "reel", "short", "aprovado"}), 2) * 0.35
    score += min(title_points, 2.5)
    if video.get("thumbnailLink"):
        score += 0.5
    if marked_good:
        score += 2
    return round(min(score, 10), 1)


def suggest_social_copy(folder_name, filename):
    file_title = re.sub(r"[_\-.]+", " ", os.path.splitext(filename)[0]).strip()
    file_title = re.sub(r"\s+", " ", file_title)
    if file_title:
        file_title = file_title[0].upper() + file_title[1:]
    folder_title = re.sub(r"[_\-.]+", " ", folder_name or "").strip()
    folder_title = re.sub(r"\s+", " ", folder_title)

    title_parts = [file_title or "Novo corte"]
    if folder_title and folder_title.casefold() not in title_parts[0].casefold():
        title_parts.append(folder_title)
    suggested_title = " | ".join(title_parts)[:120]

    stop_words = {"com", "para", "por", "uma", "uns", "das", "dos", "que", "the", "and", "from"}
    words = re.findall(r"[^\W_]+", f"{folder_title} {file_title}".casefold(), flags=re.UNICODE)
    hashtags = ["#TikTok", "#Reels"]
    seen = {tag.casefold() for tag in hashtags}
    for word in words:
        if len(word) < 3 or word.isdigit() or word in stop_words:
            continue
        normalized = unicodedata.normalize("NFKD", word)
        ascii_word = normalized.encode("ascii", "ignore").decode("ascii")
        if not ascii_word:
            continue
        tag = f"#{ascii_word}"
        if tag.casefold() not in seen:
            hashtags.append(tag)
            seen.add(tag.casefold())
        if len(hashtags) == 8:
            break
    return suggested_title, " ".join(hashtags)


def _read_good_video_ids():
    try:
        with open(GOOD_VIDEOS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        raise GoodVideosError("Não foi possível ler o arquivo bons.json.") from exc

    if not isinstance(data, list) or any(not isinstance(video_id, str) for video_id in data):
        raise GoodVideosError("O arquivo bons.json tem um formato inválido.")
    return set(data)


def load_good_video_ids():
    with GOOD_VIDEOS_LOCK:
        return _read_good_video_ids()


def _write_good_video_ids(video_ids):
    directory = os.path.dirname(GOOD_VIDEOS_FILE)
    temporary_path = None
    try:
        handle, temporary_path = tempfile.mkstemp(
            prefix="bons-",
            suffix=".json.tmp",
            dir=directory,
        )
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(sorted(video_ids), file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_path, GOOD_VIDEOS_FILE)
    except OSError as exc:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)
        raise GoodVideosError("Não foi possível salvar o arquivo bons.json.") from exc


def mark_good_video(video_id):
    with GOOD_VIDEOS_LOCK:
        video_ids = _read_good_video_ids()
        video_ids.add(video_id)
        _write_good_video_ids(video_ids)


def _read_posted_video_ids():
    try:
        with open(POSTED_VIDEOS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return set()
    except (OSError, json.JSONDecodeError) as exc:
        raise PostedVideosError("Não foi possível ler o arquivo postados.json.") from exc

    if not isinstance(data, list) or any(not isinstance(video_id, str) for video_id in data):
        raise PostedVideosError("O arquivo postados.json tem um formato inválido.")
    return set(data)


def load_posted_video_ids():
    with POSTED_VIDEOS_LOCK:
        return _read_posted_video_ids()


def mark_video_posted(video_id):
    with POSTED_VIDEOS_LOCK:
        video_ids = _read_posted_video_ids()
        video_ids.add(video_id)
        temporary_path = None
        try:
            handle, temporary_path = tempfile.mkstemp(
                prefix="postados-",
                suffix=".json.tmp",
                dir=os.path.dirname(POSTED_VIDEOS_FILE),
            )
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(sorted(video_ids), file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temporary_path, POSTED_VIDEOS_FILE)
        except OSError as exc:
            if temporary_path and os.path.exists(temporary_path):
                os.remove(temporary_path)
            raise PostedVideosError("Não foi possível salvar o arquivo postados.json.") from exc


def _read_social_posts():
    try:
        with open(SOCIAL_POSTS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SocialPostsError("Não foi possível ler o arquivo publicados.json.") from exc
    if not isinstance(data, dict):
        raise SocialPostsError("O arquivo publicados.json tem um formato inválido.")
    for video_id, platforms in data.items():
        if not isinstance(video_id, str) or not isinstance(platforms, dict):
            raise SocialPostsError("O arquivo publicados.json tem um formato inválido.")
        if any(
            platform not in {"tiktok", "youtube"} or not isinstance(status, dict)
            for platform, status in platforms.items()
        ):
            raise SocialPostsError("O arquivo publicados.json tem um formato inválido.")
    return data


def load_social_posts():
    with SOCIAL_POSTS_LOCK:
        return _read_social_posts()


def record_social_post(video_id, platform, remote_id, status="posted"):
    with SOCIAL_POSTS_LOCK:
        posts = _read_social_posts()
        record = {
            "id": remote_id,
            "status": status,
            "updated_at": time.time(),
        }
        if status == "posted":
            record["posted_at"] = record["updated_at"]
        posts.setdefault(video_id, {})[platform] = record
        temporary_path = None
        try:
            handle, temporary_path = tempfile.mkstemp(
                prefix="publicados-",
                suffix=".json.tmp",
                dir=os.path.dirname(SOCIAL_POSTS_FILE),
            )
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(posts, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temporary_path, SOCIAL_POSTS_FILE)
        except OSError as exc:
            if temporary_path and os.path.exists(temporary_path):
                os.remove(temporary_path)
            raise SocialPostsError("Não foi possível salvar o arquivo publicados.json.") from exc


def _read_folder_models():
    try:
        with open(FOLDER_MODELS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise FolderModelsError("Não foi possível ler o arquivo modelos.json.") from exc

    if not isinstance(data, dict):
        raise FolderModelsError("O arquivo modelos.json tem um formato inválido.")
    for folder_id, model in data.items():
        if (
            not isinstance(folder_id, str)
            or not isinstance(model, dict)
            or not isinstance(model.get("title_template", ""), str)
            or not isinstance(model.get("hashtags_template", ""), str)
        ):
            raise FolderModelsError("O arquivo modelos.json tem um formato inválido.")
    return data


def load_folder_models():
    with FOLDER_MODELS_LOCK:
        return _read_folder_models()


def save_folder_model(folder_id, title_template, hashtags_template):
    with FOLDER_MODELS_LOCK:
        models = _read_folder_models()
        models[folder_id] = {
            "title_template": title_template,
            "hashtags_template": hashtags_template,
        }
        temporary_path = None
        try:
            handle, temporary_path = tempfile.mkstemp(
                prefix="modelos-",
                suffix=".json.tmp",
                dir=os.path.dirname(FOLDER_MODELS_FILE),
            )
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump(models, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temporary_path, FOLDER_MODELS_FILE)
        except OSError as exc:
            if temporary_path and os.path.exists(temporary_path):
                os.remove(temporary_path)
            raise FolderModelsError("Não foi possível salvar o arquivo modelos.json.") from exc


def file_number(filename, fallback_number):
    match = re.search(r"\d+", os.path.splitext(filename)[0])
    return match.group(0) if match else str(fallback_number)


def apply_folder_model(video, folder_name, model, fallback_number):
    suggested_title, suggested_hashtags = suggest_social_copy(folder_name, video.get("name", ""))
    number = file_number(video.get("name", ""), fallback_number)
    title_template = model.get("title_template", "").strip()
    hashtags_template = model.get("hashtags_template", "").strip()
    if title_template:
        suggested_title = title_template.replace("{numero}", number)
    if hashtags_template:
        suggested_hashtags = hashtags_template.replace("{numero}", number)
    return suggested_title, suggested_hashtags


def parse_folder_path(value):
    path_ids = value.split(",") if value else []
    if not path_ids or any(not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id) for folder_id in path_ids):
        raise ValueError("Caminho de pasta inválido.")
    return path_ids


def redirect_to_folder(path_ids, sort_order, good_filter, posted_filter):
    return redirect(
        url_for(
            "index",
            path=",".join(path_ids),
            sort_order=sort_order if sort_order == "score" else "",
            good_filter=good_filter if good_filter in {"all", "good"} else "all",
            posted_filter=posted_filter if posted_filter in {"all", "pending", "posted"} else "all",
        )
    )


@app.template_filter("duration")
def duration_filter(metadata):
    return format_duration(metadata)


def get_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def social_template_context():
    try:
        tokens = social_publishing.load_social_tokens()
        token_error = None
    except social_publishing.SocialPublishingError as exc:
        tokens = {}
        token_error = str(exc)
    return {
        "csrf_token": get_csrf_token(),
        "tiktok_connected": bool(tokens.get("tiktok", {}).get("refresh_token")),
        "youtube_connected": bool(tokens.get("youtube", {}).get("refresh_token")),
        "social_token_error": token_error,
    }


@app.route("/oauth/<platform>")
def start_social_oauth(platform):
    if platform not in {"tiktok", "youtube"}:
        return Response("Plataforma inválida.", status=404, mimetype="text/plain")
    try:
        social_publishing.validate_token_encryption()
        state = secrets.token_urlsafe(32)
        if platform == "tiktok":
            code_verifier = social_publishing.generate_tiktok_code_verifier()
            code_challenge = social_publishing.tiktok_code_challenge(code_verifier)
            authorization = social_publishing.authorization_url(
                platform,
                state,
                code_challenge=code_challenge,
            )
            session["oauth_code_verifier_tiktok"] = code_verifier
        else:
            authorization = social_publishing.authorization_url(platform, state)
        session[f"oauth_state_{platform}"] = state
        return redirect(authorization)
    except social_publishing.SocialPublishingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))


@app.route("/oauth/<platform>/callback")
def social_oauth_callback(platform):
    if platform not in {"tiktok", "youtube"}:
        return Response("Plataforma inválida.", status=404, mimetype="text/plain")
    expected_state = session.pop(f"oauth_state_{platform}", "")
    code_verifier = (
        session.pop("oauth_code_verifier_tiktok", "") if platform == "tiktok" else None
    )
    returned_state = request.args.get("state", "")
    if not expected_state or not returned_state or not secrets.compare_digest(
        expected_state, returned_state
    ):
        flash("Não foi possível validar a autorização. Tente conectar novamente.", "error")
        return redirect(url_for("index"))
    if request.args.get("error"):
        flash(f"A autorização foi recusada: {request.args['error']}", "error")
        return redirect(url_for("index"))
    code = request.args.get("code", "")
    if not code:
        flash("A plataforma não retornou um código de autorização.", "error")
        return redirect(url_for("index"))
    if platform == "tiktok" and not code_verifier:
        flash("A sessão PKCE do TikTok expirou. Tente conectar novamente.", "error")
        return redirect(url_for("index"))
    try:
        if platform == "tiktok":
            social_publishing.exchange_authorization_code(
                platform,
                code,
                code_verifier=code_verifier,
            )
        else:
            social_publishing.exchange_authorization_code(platform, code)
    except social_publishing.SocialPublishingError as exc:
        flash(str(exc), "error")
        return redirect(url_for("index"))
    flash(f"{social_publishing.platform_display_name(platform)} conectado.", "success")
    return redirect(url_for("index"))


@app.route("/tiktok-developers-site-verification.txt")
def tiktok_developers_site_verification():
    verification_code = os.environ.get(
        "TIKTOK_DEVELOPERS_SITE_VERIFICATION_CODE", ""
    ).strip()
    if not verification_code:
        return Response(
            "Configure TIKTOK_DEVELOPERS_SITE_VERIFICATION_CODE no arquivo .env.",
            status=503,
            mimetype="text/plain",
        )
    return Response(verification_code, mimetype="text/plain")


@app.route("/privacy")
def privacy():
    return render_template(
        "legal.html",
        title="Política de Privacidade",
        sections=[
            {
                "heading": "Dados utilizados",
                "paragraphs": [
                    "O painel processa links e identificadores de pastas do Google Drive, metadados dos vídeos e os títulos e hashtags que você editar.",
                    "Se você conectar TikTok ou YouTube, o painel recebe tokens de autorização para acessar os recursos necessários. Ao solicitar um envio ao TikTok, somente o vídeo é transferido como rascunho; título, hashtags e publicação são concluídos por você no TikTok. Para o YouTube, o vídeo, título e hashtags são enviados para criar um vídeo privado.",
                ],
            },
            {
                "heading": "Uso e armazenamento",
                "paragraphs": [
                    "Os dados são usados para listar e organizar vídeos, manter suas marcações e modelos e executar as ações solicitadas. O painel não publica conteúdo sem uma ação sua.",
                    "As marcações e modelos ficam em arquivos locais do aplicativo. Os tokens sociais são armazenados criptografados em social_tokens.enc; a chave fica no arquivo .env. Quem administra o computador ou servidor que hospeda o painel pode ter acesso aos arquivos e aos vídeos processados.",
                ],
            },
            {
                "heading": "Serviços externos e controle",
                "paragraphs": [
                    "O acesso ao Drive e as autorizações e publicações sociais dependem do Google, TikTok e YouTube e seguem os termos e políticas desses serviços. O painel não vende seus dados.",
                    "Você pode revogar a autorização nas configurações da plataforma e apagar os arquivos locais do aplicativo para remover dados armazenados. A revogação pode impedir funções do painel.",
                ],
            },
        ],
    )


@app.route("/terms")
def terms():
    return render_template(
        "legal.html",
        title="Termos de Uso",
        sections=[
            {
                "heading": "Uso do painel",
                "paragraphs": [
                    "Use o painel apenas com contas, pastas e vídeos aos quais você tem autorização de acesso. Você é responsável por manter suas credenciais e o arquivo .env protegidos.",
                    "Você deve possuir os direitos e obter as permissões necessárias para armazenar, processar e publicar cada vídeo, imagem, música, título e hashtag.",
                ],
            },
            {
                "heading": "Publicações e serviços externos",
                "paragraphs": [
                    "Os envios são iniciados por você e estão sujeitos às regras, limites, aprovações e disponibilidade do Google, TikTok e YouTube. No TikTok, o painel envia um rascunho à caixa de entrada; você deve abrir a notificação e concluir a edição e publicação no aplicativo. Confira o conteúdo e as declarações necessárias antes de publicar.",
                    "Notas, títulos, hashtags e ajustes de qualidade gerados pelo painel são sugestões automatizadas, não garantem desempenho, qualidade final ou aceitação pelas plataformas.",
                ],
            },
            {
                "heading": "Disponibilidade e responsabilidade",
                "paragraphs": [
                    "O painel é fornecido como está, sem garantia de disponibilidade contínua ou de que serviços externos funcionarão sem interrupções. Você é responsável por cópias de segurança e por verificar os resultados antes de usar ou publicar arquivos.",
                    "Não use o painel para violar leis, direitos de terceiros ou os termos dos serviços conectados. O uso do Google Drive, TikTok e YouTube também está sujeito aos termos próprios de cada serviço.",
                ],
            },
        ],
    )


@app.route("/", methods=["GET", "POST"])
def index():
    view = None
    error = None
    folder_link = ""
    path_ids = []
    sort_order = ""
    good_filter = "all"
    posted_filter = "all"
    folder_model = {}

    if request.method == "POST" and request.form.get("action") in {
        "mark_good",
        "save_model",
        "mark_posted",
    }:
        action = request.form.get("action")
        sort_order = request.form.get("sort_order", "")
        good_filter = request.form.get("good_filter", "all")
        posted_filter = request.form.get("posted_filter", "all")
        try:
            path_ids = parse_folder_path(request.form.get("path", ""))
            if action == "mark_good":
                video_id = request.form.get("video_id", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
                    raise ValueError("ID de vídeo inválido.")
                mark_good_video(video_id)
            elif action == "mark_posted":
                video_id = request.form.get("video_id", "")
                if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
                    raise ValueError("ID de vídeo inválido.")
                mark_video_posted(video_id)
            else:
                save_folder_model(
                    path_ids[-1],
                    request.form.get("title_template", ""),
                    request.form.get("hashtags_template", ""),
                )
            return redirect_to_folder(path_ids, sort_order, good_filter, posted_filter)
        except (ValueError, GoodVideosError, FolderModelsError, PostedVideosError) as exc:
            error = str(exc)
            if path_ids:
                folder_link = f"https://drive.google.com/drive/folders/{path_ids[0]}"
    elif request.method == "POST":
        folder_link = request.form.get("folder_link", "").strip()
        try:
            if not folder_link:
                raise ValueError("Cole o link da pasta do Google Drive.")
            folder_id = extract_folder_id(folder_link)
            path_ids = [folder_id]
        except ValueError as exc:
            error = str(exc)
    elif request.args.get("path"):
        sort_order = request.args.get("sort_order", "")
        good_filter = request.args.get("good_filter", "all")
        posted_filter = request.args.get("posted_filter", "all")
        if sort_order != "score":
            sort_order = ""
        if good_filter not in {"all", "good"}:
            good_filter = "all"
        if posted_filter not in {"all", "pending", "posted"}:
            posted_filter = "all"
        try:
            path_ids = parse_folder_path(request.args["path"])
        except ValueError as exc:
            error = str(exc)

    if path_ids:
        folder_link = folder_link or f"https://drive.google.com/drive/folders/{path_ids[0]}"
        api_key = os.environ.get("DRIVE_API_KEY", "").strip()
        try:
            if not api_key:
                raise DriveError("Configure a variável DRIVE_API_KEY para consultar o Google Drive.")
            view = get_folder_view(path_ids[-1], api_key)
            good_video_ids = load_good_video_ids()
            posted_video_ids = load_posted_video_ids()
            social_posts = load_social_posts()
            folder_model = load_folder_models().get(path_ids[-1], {})
            ordered_videos = sorted(view["videos"], key=lambda video: video.get("name", "").casefold())
            fallback_numbers = {
                video.get("id"): position
                for position, video in enumerate(ordered_videos, start=1)
            }
            for video in view["videos"]:
                video["marked_good"] = video.get("id") in good_video_ids
                platform_posts = social_posts.get(video.get("id"), {})
                video["social_posted"] = {
                    platform: platform_posts.get(platform, {}).get("status") == "posted"
                    for platform in ("tiktok", "youtube")
                }
                video["social_inbox"] = (
                    platform_posts.get("tiktok", {}).get("status") == "inbox"
                )
                video["social_processing"] = {
                    platform: platform_posts.get(platform, {}).get("status") == "processing"
                    for platform in ("tiktok", "youtube")
                }
                video["posted"] = video.get("id") in posted_video_ids or any(
                    video["social_posted"].values()
                )
                video["resolution"], video["quality_low"] = video_quality(video)
                video["duration_ms"] = video_duration_milliseconds(video)
                video["score"] = score_video(video, video["marked_good"])
                video["suggested_title"], video["suggested_hashtags"] = apply_folder_model(
                    video,
                    video.get("folderName", view["folder_name"]),
                    folder_model,
                    fallback_numbers.get(video.get("id"), 1),
                )
            videos = view["videos"]
            if good_filter == "good":
                videos = [video for video in videos if video["marked_good"]]
            if posted_filter == "pending":
                videos = [video for video in videos if not video["posted"]]
            elif posted_filter == "posted":
                videos = [video for video in videos if video["posted"]]
            if sort_order == "score":
                videos = sorted(
                    videos,
                    key=lambda video: (video["posted"], -video["score"]),
                )
            else:
                videos = sorted(videos, key=lambda video: video["posted"])
            view["videos"] = videos
        except (
            DriveError,
            GoodVideosError,
            FolderModelsError,
            PostedVideosError,
            SocialPostsError,
        ) as exc:
            view = None
            error = str(exc)

    return render_template(
        "index.html",
        view=view,
        path_string=",".join(path_ids),
        parent_path_string=",".join(path_ids[:-1]),
        is_root_view=len(path_ids) == 1,
        sort_order=sort_order,
        good_filter=good_filter,
        posted_filter=posted_filter,
        folder_model=folder_model,
        error=error,
        folder_link=folder_link,
    )


def update_improvement_job(job_id, **updates):
    with IMPROVEMENT_JOBS_LOCK:
        job = IMPROVEMENT_JOBS.get(job_id)
        if job:
            job.update(updates)
            job["updated_at"] = time.time()


def remove_improvement_files(directory):
    if directory and os.path.isdir(directory):
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            app.logger.warning("Não foi possível limpar arquivos temporários do job: %s", exc)


def cleanup_expired_improvement_jobs():
    now = time.time()
    with IMPROVEMENT_JOBS_LOCK:
        expired = [
            job_id
            for job_id, job in IMPROVEMENT_JOBS.items()
            if job["status"] in {"complete", "error"}
            and now - job["updated_at"] > IMPROVEMENT_JOB_TTL_SECONDS
        ]
        jobs = [IMPROVEMENT_JOBS.pop(job_id) for job_id in expired]
    for job in jobs:
        remove_improvement_files(job.get("workdir"))


def download_drive_video_for_job(job_id, video_id, api_key, destination):
    params = urlencode({"alt": "media", "key": api_key})
    url = f"{DRIVE_API_BASE}/files/{quote(video_id, safe='')}?{params}"
    try:
        response = urlopen(url, timeout=60)
    except HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8"))
            message = details.get("error", {}).get("message", "Falha ao baixar vídeo do Google Drive.")
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = "Falha ao baixar vídeo do Google Drive."
        raise RuntimeError(message) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError("Não foi possível conectar ao Google Drive para processar o vídeo.") from error

    try:
        total_size = int(response.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        total_size = 0
    downloaded = 0
    with response, open(destination, "wb") as file:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            file.write(chunk)
            downloaded += len(chunk)
            if total_size:
                fraction = min(downloaded / total_size, 1)
                update_improvement_job(
                    job_id,
                    progress=round(fraction * 25),
                    message=f"Baixando vídeo: {round(fraction * 100)}%",
                )


def run_ffmpeg(ffmpeg_path, input_path, output_path, job_id, duration_ms):
    filters = (
        "scale=1080:1920:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
        "unsharp=5:5:0.8:3:3:0.35,eq=brightness=0.03,setsar=1"
    )
    command = [
        ffmpeg_path,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-i",
        input_path,
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-vf",
        filters,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        output_path,
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    error_lines = []
    duration_seconds = duration_ms / 1000 if duration_ms > 0 else 0

    for line in process.stdout:
        line = line.strip()
        if line.startswith("out_time_ms=") and duration_seconds:
            try:
                processed_seconds = int(line.partition("=")[2]) / 1_000_000
            except ValueError:
                continue
            ratio = min(processed_seconds / duration_seconds, 0.99)
            progress = 25 + round(ratio * 74)
            update_improvement_job(
                job_id,
                progress=progress,
                message=f"Melhorando qualidade: {progress}%",
            )
        elif line and "=" not in line:
            error_lines.append(line)
            error_lines = error_lines[-5:]

    return_code = process.wait()
    if return_code:
        details = " ".join(error_lines)
        message = "O FFmpeg não conseguiu processar o vídeo."
        if details:
            message = f"{message} {details}"
        raise RuntimeError(message)


def process_improvement_job(job_id, video_id, filename, duration_ms, api_key, ffmpeg_path):
    workdir = None
    try:
        workdir = tempfile.mkdtemp(prefix="painel-cortes-")
        input_path = os.path.join(workdir, filename)
        output_name = f"{os.path.splitext(filename)[0]}_melhorado.mp4"
        output_path = os.path.join(workdir, output_name)
        update_improvement_job(
            job_id,
            status="downloading",
            progress=0,
            message="Baixando vídeo do Drive...",
            workdir=workdir,
        )
        download_drive_video_for_job(job_id, video_id, api_key, input_path)
        update_improvement_job(
            job_id,
            status="processing",
            progress=25,
            message="Preparando melhoria de qualidade...",
        )
        run_ffmpeg(ffmpeg_path, input_path, output_path, job_id, duration_ms)
        if not os.path.isfile(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("O FFmpeg não gerou um arquivo de vídeo.")
        update_improvement_job(
            job_id,
            status="complete",
            progress=100,
            message="Vídeo melhorado pronto para baixar.",
            output_path=output_path,
            output_name=output_name,
            workdir=workdir,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        update_improvement_job(
            job_id,
            status="error",
            progress=0,
            message=str(exc),
            workdir=workdir,
        )
        remove_improvement_files(workdir)
        update_improvement_job(job_id, workdir=None)


@app.route("/improve/<video_id>", methods=["POST"])
def start_video_improvement(video_id):
    cleanup_expired_improvement_jobs()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        return jsonify(error="ID de vídeo inválido."), 400
    api_key = os.environ.get("DRIVE_API_KEY", "").strip()
    if not api_key:
        return jsonify(error="Configure a variável DRIVE_API_KEY para processar vídeos."), 503
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return jsonify(error="FFmpeg não encontrado no PATH do servidor."), 503

    filename = secure_filename(request.form.get("filename", ""))
    if not filename or os.path.splitext(filename)[1].lower() not in VIDEO_EXTENSIONS:
        return jsonify(error="Nome ou formato de vídeo inválido."), 400
    try:
        duration_ms = max(0, int(request.form.get("duration_ms", "0")))
    except ValueError:
        return jsonify(error="Duração do vídeo inválida."), 400

    job_id = uuid.uuid4().hex
    with IMPROVEMENT_JOBS_LOCK:
        active_jobs = sum(
            job["status"] in {"queued", "downloading", "processing"}
            for job in IMPROVEMENT_JOBS.values()
        )
        if active_jobs >= 2:
            return jsonify(error="Já há duas melhorias em andamento. Tente novamente em instantes."), 429
        IMPROVEMENT_JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Aguardando processamento...",
            "created_at": time.time(),
            "updated_at": time.time(),
            "workdir": None,
            "video_id": video_id,
        }

    worker = threading.Thread(
        target=process_improvement_job,
        args=(job_id, video_id, filename, duration_ms, api_key, ffmpeg_path),
        daemon=True,
    )
    try:
        worker.start()
    except RuntimeError as exc:
        with IMPROVEMENT_JOBS_LOCK:
            IMPROVEMENT_JOBS.pop(job_id, None)
        return jsonify(error=f"Não foi possível iniciar o processamento: {exc}"), 500
    return jsonify(
        job_id=job_id,
        status_url=url_for("improvement_status", job_id=job_id),
    ), 202


@app.route("/improvement-status/<job_id>")
def improvement_status(job_id):
    cleanup_expired_improvement_jobs()
    with IMPROVEMENT_JOBS_LOCK:
        job = IMPROVEMENT_JOBS.get(job_id)
        if not job:
            return jsonify(error="Processamento não encontrado ou expirado."), 404
        result = {
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
        }
        if job["status"] == "complete":
            result["download_url"] = url_for("download_improved_video", job_id=job_id)
            result["preview_url"] = url_for("preview_improved_video", job_id=job_id)
    return jsonify(result)


@app.route("/improved/<job_id>")
def download_improved_video(job_id):
    cleanup_expired_improvement_jobs()
    with IMPROVEMENT_JOBS_LOCK:
        job = IMPROVEMENT_JOBS.get(job_id)
        if not job or job["status"] != "complete":
            return Response("Vídeo melhorado indisponível ou expirado.", status=404, mimetype="text/plain")
        output_path = job["output_path"]
        output_name = job["output_name"]
    if not os.path.isfile(output_path):
        return Response("O arquivo melhorado não foi encontrado.", status=404, mimetype="text/plain")
    return send_file(
        output_path,
        as_attachment=True,
        download_name=output_name,
        mimetype="video/mp4",
        conditional=True,
        max_age=0,
    )


@app.route("/improved-preview/<job_id>")
def preview_improved_video(job_id):
    cleanup_expired_improvement_jobs()
    with IMPROVEMENT_JOBS_LOCK:
        job = IMPROVEMENT_JOBS.get(job_id)
        if not job or job["status"] != "complete":
            return Response("Pré-visualização indisponível ou expirada.", status=404, mimetype="text/plain")
        output_path = job["output_path"]
    if not os.path.isfile(output_path):
        return Response("O arquivo melhorado não foi encontrado.", status=404, mimetype="text/plain")
    return send_file(
        output_path,
        as_attachment=False,
        mimetype="video/mp4",
        conditional=True,
        max_age=0,
    )


def drive_video_response(video_id, as_attachment):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        return Response("ID de vídeo inválido.", status=400, mimetype="text/plain")
    api_key = os.environ.get("DRIVE_API_KEY", "").strip()
    if not api_key:
        return Response(
            "Configure a variável DRIVE_API_KEY para acessar vídeos.",
            status=503,
            mimetype="text/plain",
        )

    params = urlencode({"alt": "media", "key": api_key})
    url = f"{DRIVE_API_BASE}/files/{quote(video_id, safe='')}?{params}"
    range_header = request.headers.get("Range", "")
    if range_header and (
        not re.fullmatch(r"bytes=\d*-\d*(?:,\d*-\d*)*", range_header)
        or range_header == "bytes=-"
    ):
        return Response("Intervalo de bytes inválido.", status=416, mimetype="text/plain")
    headers = {"Range": range_header} if range_header else {}
    try:
        upstream = urlopen(Request(url, headers=headers), timeout=60)
    except HTTPError as error:
        if error.code == 416:
            response_headers = {"Accept-Ranges": "bytes"}
            content_range = error.headers.get("Content-Range")
            if content_range:
                response_headers["Content-Range"] = content_range
            return Response(status=416, headers=response_headers)
        try:
            details = json.loads(error.read().decode("utf-8"))
            message = details.get("error", {}).get("message", "Falha ao acessar vídeo do Google Drive.")
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = "Falha ao acessar vídeo do Google Drive."
        return Response(message, status=error.code, mimetype="text/plain")
    except (URLError, TimeoutError):
        return Response(
            "Não foi possível conectar ao Google Drive para acessar o vídeo.",
            status=502,
            mimetype="text/plain",
        )

    filename = secure_filename(request.args.get("filename", "")) or video_id
    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'{"attachment" if as_attachment else "inline"}; filename="{filename}"',
        "Cache-Control": "no-store",
    }
    for header in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
        value = upstream.headers.get(header)
        if value:
            response_headers[header] = value
    content_type = upstream.headers.get("Content-Type", "")
    if not content_type or content_type == "application/octet-stream":
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def stream_video():
        try:
            while True:
                chunk = upstream.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            upstream.close()

    return Response(
        stream_with_context(stream_video()),
        status=getattr(upstream, "status", 200),
        headers=response_headers,
        content_type=content_type,
    )


def update_social_post_job(job_id, **updates):
    with SOCIAL_POST_JOBS_LOCK:
        job = SOCIAL_POST_JOBS.get(job_id)
        if job:
            job.update(updates)
            job["updated_at"] = time.time()


def cleanup_expired_social_post_jobs():
    now = time.time()
    with SOCIAL_POST_JOBS_LOCK:
        expired = [
            job_id
            for job_id, job in SOCIAL_POST_JOBS.items()
            if job["status"] in {"complete", "error", "partial", "pending"}
            and now - job["updated_at"] > SOCIAL_POST_JOB_TTL_SECONDS
        ]
        jobs = [SOCIAL_POST_JOBS.pop(job_id) for job_id in expired]
    for job in jobs:
        remove_improvement_files(job.get("workdir"))


def copy_completed_improvement(video_id, destination_dir):
    cleanup_expired_improvement_jobs()
    with IMPROVEMENT_JOBS_LOCK:
        matches = [
            job
            for job in IMPROVEMENT_JOBS.values()
            if job.get("video_id") == video_id
            and job["status"] == "complete"
            and isinstance(job.get("output_path"), str)
            and os.path.isfile(job["output_path"])
        ]
    if not matches:
        return None
    latest = max(matches, key=lambda job: job["updated_at"])
    copied_path = os.path.join(destination_dir, latest["output_name"])
    shutil.copyfile(latest["output_path"], copied_path)
    return copied_path, latest["output_name"]


def download_video_for_social_post(job_id, video_id, api_key, destination):
    params = urlencode({"alt": "media", "key": api_key})
    url = f"{DRIVE_API_BASE}/files/{quote(video_id, safe='')}?{params}"
    try:
        response = urlopen(url, timeout=60)
    except HTTPError as error:
        try:
            details = json.loads(error.read().decode("utf-8"))
            message = details.get("error", {}).get("message", "Falha ao baixar vídeo do Google Drive.")
        except (json.JSONDecodeError, UnicodeDecodeError):
            message = "Falha ao baixar vídeo do Google Drive."
        raise RuntimeError(message) from error
    except (URLError, TimeoutError) as error:
        raise RuntimeError("Não foi possível conectar ao Google Drive para preparar a publicação.") from error

    try:
        total_size = int(response.headers.get("Content-Length", 0))
    except (TypeError, ValueError):
        total_size = 0
    downloaded = 0
    with response, open(destination, "wb") as file:
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            file.write(chunk)
            downloaded += len(chunk)
            if total_size:
                fraction = min(downloaded / total_size, 1)
                update_social_post_job(
                    job_id,
                    progress=round(fraction * 25),
                    message=f"Baixando vídeo: {round(fraction * 100)}%",
                )


def convert_video_for_tiktok(video_path, destination, job_id):
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError("TikTok não aceita MKV e o FFmpeg não está disponível para convertê-lo.")
    update_social_post_job(
        job_id,
        status="processing",
        progress=25,
        message="Convertendo o vídeo MKV para MP4 para o TikTok...",
    )
    process = subprocess.run(
        [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            video_path,
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            destination,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if process.returncode or not os.path.isfile(destination) or os.path.getsize(destination) == 0:
        details = process.stderr.strip()[-1000:]
        message = "O FFmpeg não conseguiu converter o vídeo para MP4."
        if details:
            message = f"{message} {details}"
        raise RuntimeError(message)


def process_social_post_job(
    job_id,
    platform,
    video_id,
    filename,
    title,
    hashtags,
    api_key,
):
    workdir = None
    published = False
    remote_id = None
    result_status = None
    upload_completed = False
    upload_started = False
    try:
        workdir = tempfile.mkdtemp(prefix="painel-cortes-post-")
        update_social_post_job(
            job_id,
            status="downloading",
            progress=0,
            message=(
                "Preparando o vídeo para envio..."
                if platform == "tiktok"
                else "Preparando o vídeo para publicação..."
            ),
            workdir=workdir,
        )
        improved = copy_completed_improvement(video_id, workdir)
        if improved:
            video_path, upload_name = improved
            update_social_post_job(
                job_id,
                progress=25,
                message="Usando o vídeo melhorado.",
            )
        else:
            upload_name = filename
            video_path = os.path.join(workdir, filename)
            download_video_for_social_post(job_id, video_id, api_key, video_path)

        if platform == "tiktok" and os.path.splitext(upload_name)[1].lower() == ".mkv":
            converted_path = os.path.join(workdir, f"{os.path.splitext(upload_name)[0]}.mp4")
            convert_video_for_tiktok(video_path, converted_path, job_id)
            video_path = converted_path

        token = social_publishing.get_access_token(platform)

        def progress_callback(status, progress, message):
            nonlocal upload_completed
            if platform == "tiktok" and status == "processing":
                upload_completed = True
            update_social_post_job(
                job_id,
                status="uploading" if status == "complete" else status,
                progress=progress,
                message=message,
            )

        def record_social_publish_id(publish_id):
            nonlocal remote_id
            remote_id = publish_id
            record_social_post(video_id, platform, publish_id, status="processing")

        def record_youtube_upload_started():
            nonlocal upload_started
            record_social_post(video_id, platform, None, status="processing")
            upload_started = True

        if platform == "tiktok":
            remote_id, result_status = social_publishing.upload_tiktok_inbox(
                video_path,
                token,
                progress_callback,
                lambda publish_id: record_social_publish_id(publish_id),
            )
        else:
            remote_id = social_publishing.publish_youtube(
                video_path,
                title,
                hashtags,
                token,
                progress_callback,
                record_youtube_upload_started,
            )
            result_status = "posted"
        published = result_status == "posted"
        try:
            if platform == "tiktok":
                record_social_post(video_id, platform, remote_id, status=result_status)
            else:
                record_social_post(video_id, platform, remote_id)
        except SocialPostsError as exc:
            if platform == "tiktok" and result_status == "inbox":
                message = (
                    "O rascunho foi enviado à caixa de entrada do TikTok, mas não foi possível "
                    f"salvar o status local: {exc} Não envie novamente antes de conferir o TikTok."
                )
            else:
                message = (
                    f"Concluído no {social_publishing.platform_display_name(platform)}, "
                    f"mas não foi possível salvar o status local: {exc} "
                    "Não tente novamente antes de conferir a conta."
                )
            update_social_post_job(
                job_id,
                status="partial",
                message=message,
                remote_id=remote_id,
            )
            return
        if platform == "tiktok" and result_status == "inbox":
            update_social_post_job(
                job_id,
                status="complete",
                progress=100,
                message=(
                    "Rascunho enviado à caixa de entrada do TikTok. "
                    "Abra a notificação no TikTok para editar e publicar."
                ),
                remote_id=remote_id,
                result_status=result_status,
            )
            return
        try:
            mark_video_posted(video_id)
            message = f"Postado no {social_publishing.platform_display_name(platform)}."
        except PostedVideosError as exc:
            app.logger.error("Vídeo publicado, mas não foi possível atualizar postados.json: %s", exc)
            message = (
                f"Postado no {social_publishing.platform_display_name(platform)}; "
                "o status da plataforma foi salvo, "
                "mas postados.json não pôde ser atualizado."
            )
        update_social_post_job(
            job_id,
            status="complete",
            progress=100,
            message=message,
            remote_id=remote_id,
            result_status=result_status,
        )
    except social_publishing.SocialPublishingPending as exc:
        update_social_post_job(
            job_id,
            status="pending",
            progress=95,
            message=str(exc),
            remote_id=remote_id,
        )
    except social_publishing.SocialPublishingRejected as exc:
        try:
            if remote_id:
                record_social_post(video_id, platform, remote_id, status="failed")
        except SocialPostsError as save_error:
            app.logger.error("Não foi possível salvar falha de publicação: %s", save_error)
        update_social_post_job(
            job_id,
            status="error",
            progress=0,
            message=str(exc),
            remote_id=remote_id,
        )
    except (
        OSError,
        RuntimeError,
        subprocess.SubprocessError,
        social_publishing.SocialPublishingError,
        SocialPostsError,
    ) as exc:
        if platform == "youtube" and upload_started:
            update_social_post_job(
                job_id,
                status="pending",
                progress=95,
                message=(
                    "O YouTube iniciou o envio, mas não confirmou o resultado. "
                    "Confira a conta antes de tentar novamente."
                ),
            )
            return
        if platform == "tiktok" and remote_id and upload_completed:
            update_social_post_job(
                job_id,
                status="pending",
                progress=95,
                message=(
                    "O TikTok recebeu o vídeo, mas não foi possível confirmar o resultado. "
                    "Confira a conta antes de tentar novamente."
                ),
                remote_id=remote_id,
            )
            return
        if platform == "tiktok" and remote_id:
            try:
                record_social_post(video_id, platform, remote_id, status="failed")
            except SocialPostsError as save_error:
                app.logger.error("Não foi possível salvar falha de publicação: %s", save_error)
        if published:
            status = "partial"
            message = (
                f"A plataforma confirmou a publicação, mas não foi possível concluir o registro local: {exc} "
                "Não publique novamente antes de conferir a conta."
            )
        else:
            status = "error"
            message = str(exc)
        update_social_post_job(
            job_id,
            status=status,
            progress=0,
            message=message,
            remote_id=remote_id,
        )
    finally:
        remove_improvement_files(workdir)
        update_social_post_job(job_id, workdir=None)


def schedule_now():
    return datetime.now(AUTPOST_TIMEZONE)


def schedule_job_id(schedule_id, slot_time):
    return f"autopost-{schedule_id}-{slot_time.replace(':', '')}"


def register_autopost_schedule(schedule):
    if not AUTPOST_SCHEDULER.running:
        return
    for slot_time in schedule["times"]:
        hour, minute = (int(value) for value in slot_time.split(":"))
        AUTPOST_SCHEDULER.add_job(
            run_schedule_slot,
            trigger=CronTrigger(
                hour=hour,
                minute=minute,
                timezone=AUTPOST_TIMEZONE,
            ),
            args=(schedule["id"], slot_time),
            id=schedule_job_id(schedule["id"], slot_time),
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )


def ensure_autopost_scheduler():
    global AUTPOST_SCHEDULER_STARTED
    with AUTPOST_SCHEDULER_LOCK:
        if AUTPOST_SCHEDULER_STARTED:
            return
        for schedule in AUTPOST_STORE.list_active_schedules():
            register_autopost_schedule(schedule)
        AUTPOST_SCHEDULER.start()
        AUTPOST_SCHEDULER_STARTED = True


def remove_autopost_schedule_jobs(schedule_id):
    prefix = f"autopost-{schedule_id}-"
    for job in AUTPOST_SCHEDULER.get_jobs():
        if job.id.startswith(prefix):
            AUTPOST_SCHEDULER.remove_job(job.id)


def start_scheduled_social_post(item, api_key):
    video_id = item["video_id"]
    filename = secure_filename(item["video_name"])
    if not filename or os.path.splitext(filename)[1].lower() not in VIDEO_EXTENSIONS:
        AUTPOST_STORE.update_history(
            item["id"],
            "falhou",
            "O nome ou formato do vídeo não pode ser usado para o envio.",
            None,
            schedule_now().isoformat(timespec="seconds"),
        )
        return

    title, hashtags = suggest_social_copy(item["folder_name"], filename)
    if item["platform"] == "tiktok":
        title = ""
        hashtags = ""
    job_id = uuid.uuid4().hex
    now = time.time()
    failure_message = None
    with SOCIAL_POST_JOBS_LOCK:
        active_jobs = sum(
            job["status"] in {"queued", "downloading", "uploading", "processing"}
            for job in SOCIAL_POST_JOBS.values()
        )
        duplicate_job = any(
            job.get("video_id") == video_id
            and job.get("platform") == item["platform"]
            and job["status"] in {"queued", "downloading", "uploading", "processing"}
            for job in SOCIAL_POST_JOBS.values()
        )
        if duplicate_job:
            failure_message = "Já existe um envio deste vídeo para esse destino."
        elif active_jobs >= 2:
            failure_message = (
                "O limite de dois envios simultâneos foi atingido; "
                "este vídeo não foi enviado."
            )
        else:
            SOCIAL_POST_JOBS[job_id] = {
                "status": "queued",
                "progress": 0,
                "message": "Aguardando o agendamento...",
                "created_at": now,
                "updated_at": now,
                "workdir": None,
                "platform": item["platform"],
                "video_id": video_id,
            }

    if failure_message:
        AUTPOST_STORE.update_history(
            item["id"],
            "falhou",
            failure_message,
            None,
            schedule_now().isoformat(timespec="seconds"),
        )
        return

    worker = threading.Thread(
        target=process_scheduled_social_post,
        args=(
            job_id,
            item,
            filename,
            title,
            hashtags,
            api_key,
        ),
        daemon=True,
    )
    try:
        worker.start()
    except RuntimeError as exc:
        with SOCIAL_POST_JOBS_LOCK:
            SOCIAL_POST_JOBS.pop(job_id, None)
        AUTPOST_STORE.update_history(
            item["id"],
            "falhou",
            f"Não foi possível iniciar o envio: {exc}",
            None,
            schedule_now().isoformat(timespec="seconds"),
        )


def process_scheduled_social_post(job_id, item, filename, title, hashtags, api_key):
    try:
        process_social_post_job(
            job_id,
            item["platform"],
            item["video_id"],
            filename,
            title,
            hashtags,
            api_key,
        )
    except (
        OSError,
        RuntimeError,
        ValueError,
        subprocess.SubprocessError,
        social_publishing.SocialPublishingError,
        SocialPostsError,
    ) as exc:
        update_social_post_job(
            job_id,
            status="error",
            progress=0,
            message=str(exc),
        )
    finally:
        with SOCIAL_POST_JOBS_LOCK:
            job = dict(SOCIAL_POST_JOBS.get(job_id, {}))
        if job.get("status") == "complete" and job.get("result_status") == "inbox":
            status = "rascunho"
        elif job.get("status") == "complete":
            status = "postado"
        else:
            status = "falhou"
        message = job.get("message") or "O envio não foi concluído."
        if job.get("status") == "pending":
            message = (
                f"{message} O resultado não foi confirmado; o painel não repetirá "
                "este vídeo automaticamente."
            )
        try:
            AUTPOST_STORE.update_history(
                item["id"],
                status,
                message,
                job.get("remote_id"),
                schedule_now().isoformat(timespec="seconds"),
            )
        except autopost.AutopostStoreError:
            app.logger.exception("Não foi possível salvar o resultado do agendamento.")


def run_schedule_slot(schedule_id, slot_time):
    now = schedule_now()
    run_date = now.strftime("%Y-%m-%d")
    now_text = now.isoformat(timespec="seconds")
    claimed = False
    try:
        claim = AUTPOST_STORE.claim_slot(schedule_id, run_date, slot_time, now_text)
        if not claim:
            return
        claimed = True
        if claim["limit_reached"]:
            return

        api_key = os.environ.get("DRIVE_API_KEY", "").strip()
        if not api_key:
            raise DriveError("Configure DRIVE_API_KEY para acessar os vídeos agendados.")
        schedule = claim["schedule"]
        view = get_folder_view(schedule["folder_id"], api_key)
        candidates = sorted(
            view.get("all_videos", view.get("videos", [])),
            key=lambda video: (
                video.get("name", "").casefold(),
                video.get("id", ""),
            ),
        )
        reserved = AUTPOST_STORE.reserve_next_video(
            schedule_id,
            run_date,
            slot_time,
            candidates,
            load_posted_video_ids(),
            load_social_posts(),
            now_text,
        )
        for item in reserved:
            start_scheduled_social_post(item, api_key)
    except (
        DriveError,
        PostedVideosError,
        SocialPostsError,
        autopost.AutopostStoreError,
    ) as exc:
        app.logger.error("Falha no agendamento %s (%s): %s", schedule_id, slot_time, exc)
        if claimed:
            try:
                AUTPOST_STORE.finish_slot(
                    schedule_id,
                    run_date,
                    slot_time,
                    "failed",
                    str(exc),
                    schedule_now().isoformat(timespec="seconds"),
                )
            except autopost.AutopostStoreError:
                app.logger.exception("Não foi possível salvar a falha do horário agendado.")


def shutdown_autopost_scheduler():
    if AUTPOST_SCHEDULER.running:
        AUTPOST_SCHEDULER.shutdown(wait=False)


atexit.register(shutdown_autopost_scheduler)


@app.before_request
def ensure_autopost_scheduler_for_request():
    if not app.testing:
        ensure_autopost_scheduler()


@app.route("/social-post/<platform>/<video_id>", methods=["POST"])
def start_social_post(platform, video_id):
    cleanup_expired_social_post_jobs()
    if platform not in {"tiktok", "youtube"}:
        return jsonify(error="Plataforma inválida."), 404
    csrf_token = session.get("csrf_token", "")
    supplied_token = request.headers.get("X-CSRF-Token", "")
    if not csrf_token or not supplied_token or not secrets.compare_digest(
        csrf_token, supplied_token
    ):
        return jsonify(error="A sessão expirou. Atualize a página e tente novamente."), 400
    if not re.fullmatch(r"[A-Za-z0-9_-]+", video_id):
        return jsonify(error="ID de vídeo inválido."), 400
    filename = secure_filename(request.form.get("filename", ""))
    if not filename or os.path.splitext(filename)[1].lower() not in VIDEO_EXTENSIONS:
        return jsonify(error="Nome ou formato de vídeo inválido."), 400
    title = request.form.get("title", "").strip()
    hashtags = request.form.get("hashtags", "").strip()
    if platform == "youtube" and not title:
        return jsonify(error="Preencha o título do vídeo antes de publicar."), 400
    if platform == "youtube" and len(title) > 100:
        return jsonify(error="O título do YouTube não pode passar de 100 caracteres."), 400
    if platform == "youtube" and len(hashtags) > 5000:
        return jsonify(error="A descrição do YouTube não pode passar de 5.000 caracteres."), 400
    api_key = os.environ.get("DRIVE_API_KEY", "").strip()
    if not api_key:
        return jsonify(error="Configure DRIVE_API_KEY para acessar o vídeo no Google Drive."), 503
    try:
        tokens = social_publishing.load_social_tokens()
        if not tokens.get(platform, {}).get("refresh_token"):
            action = "enviar um rascunho" if platform == "tiktok" else "publicar"
            return jsonify(
                error=(
                    f"Conecte a conta do {social_publishing.platform_display_name(platform)} "
                    f"antes de {action}."
                )
            ), 409
    except (social_publishing.SocialPublishingError, SocialPostsError) as exc:
        return jsonify(error=str(exc)), 503

    job_id = uuid.uuid4().hex
    with SOCIAL_POST_JOBS_LOCK:
        try:
            platform_status = load_social_posts().get(video_id, {}).get(platform, {}).get("status")
        except SocialPostsError as exc:
            return jsonify(error=str(exc)), 503
        if platform_status == "posted":
            return jsonify(
                error=f"Este vídeo já foi publicado no {social_publishing.platform_display_name(platform)}."
            ), 409
        if platform == "tiktok" and platform_status == "inbox":
            return jsonify(
                error="Este vídeo já foi enviado à caixa de entrada do TikTok. Confira a notificação antes de reenviar."
            ), 409
        if platform_status == "processing":
            message = (
                "O envio do rascunho ao TikTok ainda está sendo processado. Confira a caixa de entrada antes de tentar novamente."
                if platform == "tiktok"
                else "A publicação no YouTube ainda está sendo processada. Confira a conta antes de tentar novamente."
            )
            return jsonify(
                error=message
            ), 409
        active_jobs = sum(
            job["status"] in {"queued", "downloading", "uploading", "processing"}
            for job in SOCIAL_POST_JOBS.values()
        )
        duplicate_job = any(
            job["video_id"] == video_id
            and job["platform"] == platform
            and job["status"] in {"queued", "downloading", "uploading", "processing"}
            for job in SOCIAL_POST_JOBS.values()
        )
        if duplicate_job:
            return jsonify(
                error=f"Este vídeo já está sendo enviado ao {social_publishing.platform_display_name(platform)}."
            ), 409
        if active_jobs >= 2:
            return jsonify(error="Já há duas publicações em andamento. Tente novamente em instantes."), 429
        SOCIAL_POST_JOBS[job_id] = {
            "status": "queued",
            "progress": 0,
            "message": "Aguardando publicação...",
            "created_at": time.time(),
            "updated_at": time.time(),
            "workdir": None,
            "platform": platform,
            "video_id": video_id,
        }

    worker = threading.Thread(
        target=process_social_post_job,
        args=(
            job_id,
            platform,
            video_id,
            filename,
            title,
            hashtags,
            api_key,
        ),
        daemon=True,
    )
    try:
        worker.start()
    except RuntimeError as exc:
        with SOCIAL_POST_JOBS_LOCK:
            SOCIAL_POST_JOBS.pop(job_id, None)
        return jsonify(error=f"Não foi possível iniciar a publicação: {exc}"), 500
    return jsonify(
        job_id=job_id,
        status_url=url_for("social_post_status", job_id=job_id),
    ), 202


@app.route("/social-post-status/<job_id>")
def social_post_status(job_id):
    cleanup_expired_social_post_jobs()
    with SOCIAL_POST_JOBS_LOCK:
        job = SOCIAL_POST_JOBS.get(job_id)
        if not job:
            return jsonify(error="Publicação não encontrada ou expirada."), 404
        return jsonify(
            status=job["status"],
            progress=job["progress"],
            message=job["message"],
            result_status=job.get("result_status"),
        )


@app.route("/stream/<video_id>")
def stream_original_video(video_id):
    return drive_video_response(video_id, as_attachment=False)


@app.route("/download/<video_id>")
def download_video(video_id):
    return drive_video_response(video_id, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
