"""Read-only analytics clients for the official TikTok and Instagram APIs."""

from datetime import datetime, timezone
import os
import time

import requests


META_API_VERSION = os.environ.get("META_GRAPH_API_VERSION", "v27.0").strip() or "v27.0"
META_GRAPH_BASE = f"https://graph.facebook.com/{META_API_VERSION}"
TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
REQUEST_TIMEOUT = 25
MAX_TIKTOK_PAGES = 500


class SocialAnalyticsError(Exception):
    pass


def _error_message(payload, fallback):
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if isinstance(error, dict):
        return error.get("error_user_msg") or error.get("message") or error.get("error_description") or fallback
    if isinstance(error, str):
        return payload.get("error_description") or error
    return fallback


def _request_json(method, url, *, params=None, json_body=None, headers=None):
    try:
        response = requests.request(
            method,
            url,
            params=params,
            json=json_body,
            headers=headers or {},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        raise SocialAnalyticsError("Falha de conexão com a API oficial da rede social.") from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not response.ok:
        raise SocialAnalyticsError(_error_message(payload, f"A API retornou HTTP {response.status_code}."))
    if not isinstance(payload, dict):
        raise SocialAnalyticsError("A API retornou uma resposta inválida.")
    error = payload.get("error")
    if error and not (
        isinstance(error, dict) and error.get("code") in {"ok", 0, "0"}
    ):
        raise SocialAnalyticsError(_error_message(payload, "A API recusou a consulta."))
    return payload


def _facebook_get(path_or_url, access_token, params=None):
    if path_or_url.startswith("https://"):
        url = path_or_url
        query = None
    else:
        url = f"{META_GRAPH_BASE}/{path_or_url.lstrip('/')}"
        query = dict(params or {})
        query["access_token"] = access_token
    return _request_json("GET", url, params=query)


def list_instagram_accounts(user_access_token):
    """Return professional Instagram accounts linked to Pages visible to this user."""
    params = {
        "fields": "id,name,access_token,instagram_business_account{id,username}",
        "limit": 100,
        "access_token": user_access_token,
    }
    url = f"{META_GRAPH_BASE}/me/accounts"
    accounts = []
    while url:
        payload = _request_json("GET", url, params=params)
        params = None
        for page in payload.get("data", []):
            if not isinstance(page, dict):
                continue
            instagram = page.get("instagram_business_account") or {}
            page_token = page.get("access_token")
            if instagram.get("id") and page_token:
                accounts.append({
                    "account_id": str(instagram["id"]),
                    "username": str(instagram.get("username") or ""),
                    "page_id": str(page.get("id") or ""),
                    "page_name": str(page.get("name") or ""),
                    "page_access_token": str(page_token),
                })
        url = (payload.get("paging") or {}).get("next")
    return accounts


def _tiktok_get(path, access_token, params=None):
    return _request_json(
        "GET",
        f"{TIKTOK_API_BASE}/{path.lstrip('/')}",
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
    )


def _tiktok_post(path, access_token, body, params=None):
    payload = _request_json(
        "POST",
        f"{TIKTOK_API_BASE}/{path.lstrip('/')}",
        params=params,
        json_body=body,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
    )
    error = payload.get("error")
    if isinstance(error, dict) and error.get("code") not in {None, "ok", 0, "0"}:
        raise SocialAnalyticsError(error.get("message") or "A API do TikTok recusou a consulta.")
    return payload


def _as_int(value):
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sum_metric(rows, key):
    values = [_as_int(row.get(key)) for row in rows]
    if not values or any(value is None for value in values):
        return None
    return sum(values)


def fetch_tiktok_analytics(access_token):
    profile = _tiktok_get(
        "user/info/",
        access_token,
        {"fields": "open_id,display_name,username,follower_count,likes_count,video_count"},
    ).get("data", {}).get("user", {})
    if not profile.get("open_id"):
        raise SocialAnalyticsError("O TikTok não retornou a identificação da conta autorizada.")

    videos = []
    cursor = 0
    for _ in range(MAX_TIKTOK_PAGES):
        response = _tiktok_post(
            "video/list/",
            access_token,
            {"max_count": 20, "cursor": cursor},
            params={
                "fields": (
                    "id,create_time,title,video_description,share_url,like_count,"
                    "comment_count,share_count,view_count"
                )
            },
        )
        data = response.get("data", {})
        page_videos = data.get("videos", [])
        if not isinstance(page_videos, list):
            raise SocialAnalyticsError("A API do TikTok retornou uma lista de vídeos inválida.")
        videos.extend(video for video in page_videos if isinstance(video, dict))
        if not data.get("has_more"):
            break
        next_cursor = _as_int(data.get("cursor"))
        if next_cursor is None or next_cursor == cursor:
            raise SocialAnalyticsError("A paginação do TikTok não avançou; os dados não foram atualizados.")
        cursor = next_cursor
    else:
        raise SocialAnalyticsError("O TikTok excedeu o limite seguro de páginas nesta atualização.")

    media = []
    for video in videos:
        try:
            posted_at = datetime.fromtimestamp(int(video.get("create_time", 0)), timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError, OverflowError):
            posted_at = None
        media.append({
            "id": str(video.get("id") or ""),
            "title": str(video.get("title") or video.get("video_description") or "Vídeo sem título"),
            "posted_at": posted_at,
            "views": _as_int(video.get("view_count")),
            "likes": _as_int(video.get("like_count")),
            "comments": _as_int(video.get("comment_count")),
            "shares": _as_int(video.get("share_count")),
            "permalink": str(video.get("share_url") or ""),
        })

    user_likes = _as_int(profile.get("likes_count"))
    return {
        "platform": "tiktok",
        "account_id": str(profile["open_id"]),
        "username": str(profile.get("username") or profile.get("display_name") or ""),
        "followers": _as_int(profile.get("follower_count")),
        "views": _sum_metric(media, "views"),
        "likes": user_likes if user_likes is not None else _sum_metric(media, "likes"),
        "comments": _sum_metric(media, "comments"),
        "shares": _sum_metric(media, "shares"),
        "media": media,
        "daily_views": {},
    }


def _instagram_daily_views(access_token, account_id, days=30):
    until = int(time.time())
    since = until - (days + 2) * 86400
    payload = _facebook_get(
        f"{account_id}/insights",
        access_token,
        {"metric": "views", "period": "day", "since": since, "until": until},
    )
    result = {}
    for metric in payload.get("data", []):
        if metric.get("name") != "views":
            continue
        for value in metric.get("values", []):
            if value.get("value") is None or not value.get("end_time"):
                continue
            try:
                day = datetime.fromisoformat(value["end_time"].replace("Z", "+00:00")).date().isoformat()
                count = _as_int(value["value"])
            except (TypeError, ValueError):
                continue
            if count is not None:
                result[day] = count
    return result


def fetch_instagram_analytics(access_token, account_id):
    if not account_id:
        raise SocialAnalyticsError("Reconecte o Instagram para selecionar a conta profissional vinculada.")
    profile = _facebook_get(
        str(account_id),
        access_token,
        {"fields": "id,username,name,followers_count"},
    )
    fields = (
        "id,caption,media_type,media_product_type,timestamp,like_count,comments_count,"
        "total_like_count,total_comments_count,total_views_count,shares_count,permalink"
    )
    url = f"{META_GRAPH_BASE}/{account_id}/media"
    params = {"fields": fields, "limit": 100, "access_token": access_token}
    raw_media = []
    while url:
        payload = _request_json("GET", url, params=params)
        params = None
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            raise SocialAnalyticsError("A API do Instagram retornou uma lista de publicações inválida.")
        raw_media.extend(row for row in rows if isinstance(row, dict))
        url = (payload.get("paging") or {}).get("next")

    media = []
    for row in raw_media:
        timestamp = row.get("timestamp")
        try:
            posted_at = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00")).date().isoformat() if timestamp else None
        except ValueError:
            posted_at = None
        caption = str(row.get("caption") or "").strip()
        media_type = str(row.get("media_product_type") or row.get("media_type") or "").upper()
        video_like = media_type in {"VIDEO", "REELS"}
        media.append({
            "id": str(row.get("id") or ""),
            "title": caption[:180] or "Publicação sem legenda",
            "posted_at": posted_at,
            "views": _as_int(row.get("total_views_count", row.get("view_count"))) if video_like else 0,
            "likes": _as_int(row.get("total_like_count", row.get("like_count"))),
            "comments": _as_int(row.get("total_comments_count", row.get("comments_count"))),
            "shares": _as_int(row.get("shares_count")),
            "permalink": str(row.get("permalink") or ""),
        })

    videos = [row for row in media if row.get("views") is not None]
    try:
        daily_views = _instagram_daily_views(access_token, str(account_id))
    except SocialAnalyticsError:
        # Media counters still provide server-side daily deltas when Insights is unavailable.
        daily_views = {}
    return {
        "platform": "instagram",
        "account_id": str(profile.get("id") or account_id),
        "username": str(profile.get("username") or profile.get("name") or ""),
        "followers": _as_int(profile.get("followers_count")),
        "views": _sum_metric(videos, "views") if videos else 0,
        "likes": _sum_metric(media, "likes"),
        "comments": _sum_metric(media, "comments"),
        "shares": _sum_metric(media, "shares"),
        "media": media,
        "daily_views": daily_views,
    }
