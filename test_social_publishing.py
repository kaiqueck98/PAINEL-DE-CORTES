import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet

import social_publishing


class SocialPublishingTests(unittest.TestCase):
    def test_missing_encryption_key_is_generated_and_saved_to_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = os.path.join(directory, ".env")
            with open(dotenv_path, "w", encoding="utf-8") as file:
                file.write("DRIVE_API_KEY=preserve-me\n")
            with patch.dict(os.environ, {"SOCIAL_TOKEN_ENCRYPTION_KEY": ""}), patch.object(
                social_publishing,
                "SOCIAL_TOKENS_FILE",
                os.path.join(directory, "social_tokens.enc"),
            ):
                key = social_publishing.ensure_token_encryption_key(dotenv_path)
                self.assertEqual(os.environ["SOCIAL_TOKEN_ENCRYPTION_KEY"], key)

            cipher = Fernet(key.encode("ascii"))
            self.assertEqual(cipher.decrypt(cipher.encrypt(b"ok")), b"ok")
            values = social_publishing.dotenv_values(dotenv_path)
            self.assertEqual(values["SOCIAL_TOKEN_ENCRYPTION_KEY"], key)
            self.assertEqual(values["DRIVE_API_KEY"], "preserve-me")

    def test_invalid_key_is_replaced_only_when_no_tokens_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = os.path.join(directory, ".env")
            with open(dotenv_path, "w", encoding="utf-8") as file:
                file.write("SOCIAL_TOKEN_ENCRYPTION_KEY=not-a-fernet-key\n")
            tokens_path = os.path.join(directory, "social_tokens.enc")
            with patch.dict(os.environ, {"SOCIAL_TOKEN_ENCRYPTION_KEY": ""}), patch.object(
                social_publishing, "SOCIAL_TOKENS_FILE", tokens_path
            ):
                key = social_publishing.ensure_token_encryption_key(dotenv_path)

            self.assertNotEqual(key, "not-a-fernet-key")
            self.assertEqual(
                social_publishing.dotenv_values(dotenv_path)["SOCIAL_TOKEN_ENCRYPTION_KEY"],
                key,
            )

    def test_invalid_key_does_not_replace_existing_token_store(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = os.path.join(directory, ".env")
            tokens_path = os.path.join(directory, "social_tokens.enc")
            with open(dotenv_path, "w", encoding="utf-8") as file:
                file.write("SOCIAL_TOKEN_ENCRYPTION_KEY=not-a-fernet-key\n")
            with open(tokens_path, "wb") as file:
                file.write(b"existing tokens")
            with patch.dict(os.environ, {"SOCIAL_TOKEN_ENCRYPTION_KEY": ""}), patch.object(
                social_publishing, "SOCIAL_TOKENS_FILE", tokens_path
            ):
                with self.assertRaisesRegex(
                    social_publishing.SocialPublishingError,
                    "substituição automática foi interrompida",
                ):
                    social_publishing.ensure_token_encryption_key(dotenv_path)

            with open(tokens_path, "rb") as file:
                self.assertEqual(file.read(), b"existing tokens")

    def test_tiktok_authorization_uses_official_login_and_requested_scope(self):
        with patch.dict(
            os.environ,
            {
                "TIKTOK_CLIENT_KEY": "client-key",
                "TIKTOK_CLIENT_SECRET": "client-secret",
                "TIKTOK_REDIRECT_URI": "http://localhost:5000/oauth/tiktok/callback",
            },
        ):
            code_verifier = social_publishing.generate_tiktok_code_verifier()
            code_challenge = social_publishing.tiktok_code_challenge(code_verifier)
            authorization = social_publishing.authorization_url(
                "tiktok",
                "state-value",
                code_challenge=code_challenge,
            )

        parsed = urlparse(authorization)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "www.tiktok.com")
        self.assertEqual(params["state"], ["state-value"])
        self.assertEqual(params["scope"], ["user.info.basic,video.upload"])
        self.assertEqual(params["code_challenge"], [code_challenge])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(
            params["redirect_uri"],
            ["http://localhost:5000/oauth/tiktok/callback"],
        )

    def test_tiktok_authorization_code_exchange_sends_verifier_and_local_redirect_uri(self):
        response_body = json.dumps({
            "access_token": "access",
            "refresh_token": "refresh",
        }).encode("utf-8")
        with patch.dict(
            os.environ,
            {
                "TIKTOK_CLIENT_KEY": "client-key",
                "TIKTOK_CLIENT_SECRET": "client-secret",
                "TIKTOK_REDIRECT_URI": "http://localhost:5000/oauth/tiktok/callback",
            },
        ), patch.object(
            social_publishing,
            "_http_request",
            return_value=(200, {}, response_body),
        ) as http_request, patch.object(
            social_publishing, "store_authorized_tokens"
        ) as store_tokens:
            social_publishing.exchange_authorization_code(
                "tiktok",
                "authorization-code",
                code_verifier="verifier-value",
            )

        fields = parse_qs(http_request.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(fields["code"], ["authorization-code"])
        self.assertEqual(fields["code_verifier"], ["verifier-value"])
        self.assertEqual(
            fields["redirect_uri"],
            ["http://localhost:5000/oauth/tiktok/callback"],
        )
        store_tokens.assert_called_once_with(
            "tiktok",
            {"access_token": "access", "refresh_token": "refresh"},
        )

    def test_youtube_authorization_requests_offline_upload_permission(self):
        with patch.dict(
            os.environ,
            {
                "YOUTUBE_CLIENT_ID": "client-id",
                "YOUTUBE_CLIENT_SECRET": "client-secret",
                "YOUTUBE_REDIRECT_URI": "http://localhost:5000/oauth/youtube/callback",
            },
        ):
            authorization = social_publishing.authorization_url("youtube", "state-value")

        parsed = urlparse(authorization)
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "accounts.google.com")
        self.assertEqual(params["state"], ["state-value"])
        self.assertEqual(params["access_type"], ["offline"])
        self.assertEqual(
            params["scope"],
            ["https://www.googleapis.com/auth/youtube.upload"],
        )

    def test_oauth_tokens_are_encrypted_before_persistence(self):
        key = Fernet.generate_key().decode("ascii")
        token = "sensitive-access-token"
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "tokens.enc")
            with patch.dict(os.environ, {"SOCIAL_TOKEN_ENCRYPTION_KEY": key}), patch.object(
                social_publishing, "SOCIAL_TOKENS_FILE", path
            ):
                social_publishing.store_authorized_tokens(
                    "youtube",
                    {
                        "access_token": token,
                        "refresh_token": "sensitive-refresh-token",
                        "expires_in": 3600,
                    },
                )
                with open(path, "rb") as file:
                    encrypted = file.read()
                loaded = social_publishing.load_social_tokens()

            self.assertNotIn(token.encode("ascii"), encrypted)
            self.assertEqual(loaded["youtube"]["access_token"], token)
            self.assertEqual(
                loaded["youtube"]["refresh_token"],
                "sensitive-refresh-token",
            )

    def test_tiktok_video_upload_creates_inbox_draft_without_direct_post_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            video_path = os.path.join(directory, "corte.mp4")
            with open(video_path, "wb") as file:
                file.write(b"small video")
            api_responses = [
                {
                    "publish_id": "publish-123",
                    "upload_url": "https://open-upload.tiktokapis.com/upload?token=test",
                },
                {"status": "SEND_TO_USER_INBOX"},
            ]
            with patch.object(
                social_publishing, "_tiktok_request", side_effect=api_responses
            ) as tiktok_request, patch.object(
                social_publishing,
                "_http_request",
                return_value=(201, {}, b""),
            ) as upload_request:
                publish_id, result_status = social_publishing.upload_tiktok_inbox(
                    video_path,
                    "access-token",
                )

        self.assertEqual(publish_id, "publish-123")
        self.assertEqual(result_status, "inbox")
        self.assertEqual(tiktok_request.call_args_list[0].args[0], "/post/publish/inbox/video/init/")
        init_payload = tiktok_request.call_args_list[0].args[2]
        self.assertEqual(
            init_payload,
            {
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": len(b"small video"),
                    "chunk_size": len(b"small video"),
                    "total_chunk_count": 1,
                }
            },
        )
        self.assertEqual(init_payload["source_info"]["video_size"], len(b"small video"))
        self.assertEqual(
            tiktok_request.call_args_list[1].args[0],
            "/post/publish/status/fetch/",
        )
        upload_headers = upload_request.call_args.kwargs["headers"]
        self.assertEqual(upload_headers["Content-Range"], "bytes 0-10/11")

    def test_tiktok_inbox_upload_reports_api_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            video_path = os.path.join(directory, "corte.mp4")
            with open(video_path, "wb") as file:
                file.write(b"video")
            with patch.object(
                social_publishing,
                "_tiktok_request",
                side_effect=[
                    {
                        "publish_id": "publish-123",
                        "upload_url": "https://open-upload.tiktokapis.com/upload?token=test",
                    },
                    {"status": "FAILED", "fail_reason": "file_format_check_failed"},
                ],
            ), patch.object(
                social_publishing, "_http_request", return_value=(201, {}, b"")
            ):
                with self.assertRaisesRegex(
                    social_publishing.SocialPublishingRejected,
                    "não conseguiu enviar o rascunho",
                ):
                    social_publishing.upload_tiktok_inbox(video_path, "token")

    def test_youtube_upload_uses_private_status_and_hashtags_as_description(self):
        with tempfile.TemporaryDirectory() as directory:
            video_path = os.path.join(directory, "corte.mp4")
            with open(video_path, "wb") as file:
                file.write(b"small video")
            responses = [
                (
                    200,
                    {"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test"},
                    b"",
                ),
                (201, {}, b'{"id":"youtube-video-id"}'),
            ]
            with patch.object(
                social_publishing,
                "_http_request",
                side_effect=responses,
            ) as http_request:
                video_id = social_publishing.publish_youtube(
                    video_path,
                    "Título salvo",
                    "#cortes #viral",
                    "access-token",
                )

        self.assertEqual(video_id, "youtube-video-id")
        init_payload = json.loads(http_request.call_args_list[0].kwargs["data"])
        self.assertEqual(init_payload["snippet"]["title"], "Título salvo")
        self.assertEqual(init_payload["snippet"]["description"], "#cortes #viral")
        self.assertEqual(init_payload["status"]["privacyStatus"], "private")
        self.assertEqual(
            http_request.call_args_list[1].kwargs["headers"]["Content-Range"],
            "bytes 0-10/11",
        )


if __name__ == "__main__":
    unittest.main()
