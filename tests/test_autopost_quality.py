"""Local tests: temporary SQLite/media only; no accounts or uploads."""
import ast
import hashlib
import os
import json
import re
import secrets
import unicodedata
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import autopost


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = autopost.AutopostStore(os.path.join(self.temp.name, 'test.sqlite3'))
        self.store.initialize()
        self.args = ('folder', 'Ciência', 4, ['08:00', '12:30', '18:00', '21:00'],
                     ['youtube', 'tiktok'], 'America/Sao_Paulo', '2026-11-21T08:00:00-03:00')

    def test_automatic_repeat_reuses_schedule_and_preserves_edit(self):
        first = self.store.create_schedule(*self.args, title_template='Meu título', reuse=True)
        second = self.store.create_schedule(*self.args, title_template='Editado', reuse=True)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.list_schedules()), 1)
        self.assertEqual(self.store.get_schedule(first)['title_template'], 'Editado')

    def test_manual_scheduling_still_creates_independent_records(self):
        self.assertNotEqual(self.store.create_schedule(*self.args), self.store.create_schedule(*self.args))

    def test_claim_once_progress_and_next_video(self):
        sid = self.store.create_schedule(*self.args)
        now = self.args[-1]
        self.assertIsNotNone(self.store.claim_slot(sid, '2026-11-21', '08:00', now))
        self.assertIsNone(self.store.claim_slot(sid, '2026-11-21', '08:00', now))
        candidates = [{'id': 'a', 'name': '01.mp4'}, {'id': 'b', 'name': '02.mp4'}]
        rows = self.store.reserve_next_video(sid, '2026-11-21', '08:00', candidates, [], {}, now)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.store.update_history(row['id'], 'analisando', 'Analisando 01.mp4', None, now)
            self.store.update_history(row['id'], 'melhorando', 'RUIM', None, now)
            self.store.update_history(row['id'], 'enviando', 'Melhoria aplicada', None, now)
            self.store.update_history(row['id'], 'postado' if row['platform'] == 'youtube' else 'rascunho', 'Concluído', 'remote', now)
        self.store.claim_slot(sid, '2026-11-21', '12:30', now)
        following = self.store.reserve_next_video(sid, '2026-11-21', '12:30', candidates, [], {}, now)
        self.assertTrue(all(row['video_id'] == 'b' for row in following))


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def create_media(self, name, poor=False, silent=False):
        target = os.path.join(self.temp.name, name)
        args = ['ffmpeg', '-hide_banner', '-y', '-f', 'lavfi', '-i',
                'testsrc2=size=1280x720:rate=24' if not poor else 'testsrc2=size=320x240:rate=24']
        if not silent:
            args += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
        args += ['-t', '2', '-c:v', 'libx264', '-crf', '14']
        if poor:
            args += ['-vf', 'gblur=sigma=5']
            if not silent:
                args += ['-af', 'volume=0.03']
        autopost._media_command(args + ['-c:a', 'aac', target])
        return target

    def test_good_untouched(self):
        source = self.create_media('good.mp4')
        digest = hashlib.sha256(Path(source).read_bytes()).digest()
        self.assertEqual(autopost.verificar_qualidade(source), 'BOA')
        self.assertEqual(autopost.melhorar_video(source), source)
        self.assertEqual(hashlib.sha256(Path(source).read_bytes()).digest(), digest)

    def test_dark_video_gets_brightness_correction(self):
        source = os.path.join(self.temp.name, 'dark.mp4')
        autopost._media_command([
            'ffmpeg', '-hide_banner', '-y', '-f', 'lavfi', '-i',
            'color=c=black:size=1280x720:rate=24', '-t', '2',
            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', source,
        ])
        before = autopost.analisar_qualidade(source)
        self.assertLess(before['brightness'], 35)
        output = autopost.melhorar_video(source, analise=before)
        self.assertNotEqual(output, source)
        after = autopost.analisar_qualidade(output)
        self.assertGreater(after['brightness'], before['brightness'])

    def test_improvement_plan_only_applies_needed_video_and_audio_filters(self):
        good = {
            'brightness': 100, 'blur': 2, 'audio_db': -20, 'has_audio': True,
        }
        dark_blurry_quiet = {
            'brightness': 20, 'blur': 8, 'audio_db': -35, 'has_audio': True,
        }
        loud = {'brightness': 100, 'blur': 2, 'audio_db': -10, 'has_audio': True}
        with patch.dict(os.environ, {
            'AUTOPOST_MIN_BRIGHTNESS': '35',
            'AUTOPOST_MAX_BLUR': '5',
            'AUTOPOST_MIN_AUDIO_DB': '-30',
        }):
            self.assertEqual(autopost._planejar_melhorias(good), ([], [], []))
            video_filters, audio_filters, changes = autopost._planejar_melhorias(
                dark_blurry_quiet
            )
            self.assertIn('eq=brightness=0.08:contrast=1.1:saturation=1.1', video_filters)
            self.assertIn('unsharp=5:5:0.8:5:5:0.0', video_filters)
            self.assertEqual(audio_filters, ['volume=8dB', 'alimiter=limit=0.95'])
            self.assertEqual(len(changes), 3)
            _, loud_filters, loud_changes = autopost._planejar_melhorias(loud)
        self.assertIn('loudnorm=I=-14:TP=-1.5:LRA=11', loud_filters)
        self.assertIn('Áudio normalizado para -14 LUFS', loud_changes)

    def test_bad_converted_original_intact_and_audio_louder(self):
        source = self.create_media('bad.mp4', poor=True)
        digest = hashlib.sha256(Path(source).read_bytes()).digest()
        report = autopost.analisar_qualidade(source)
        self.assertEqual(report['qualidade'], 'RUIM')
        self.assertIn('possível desfoque', report['motivos'])
        output = autopost.melhorar_video(source, analise=report)
        self.assertEqual(Path(output).name, 'video_melhorado.mp4')
        self.assertEqual(hashlib.sha256(Path(source).read_bytes()).digest(), digest)
        after = autopost.analisar_qualidade(output)
        self.assertGreater(after['audio_db'], report['audio_db'])

    def test_missing_audio_is_bad_but_conversion_valid(self):
        source = self.create_media('silent.mp4', poor=True, silent=True)
        report = autopost.analisar_qualidade(source)
        self.assertFalse(report['has_audio'])
        self.assertEqual(report['qualidade'], 'RUIM')
        self.assertTrue(Path(autopost.melhorar_video(source, analise=report)).is_file())

    def test_invalid_input_does_not_get_approved(self):
        path = Path(self.temp.name) / 'invalid.mp4'
        path.write_bytes(b'invalid')
        with self.assertRaises(RuntimeError):
            autopost.verificar_qualidade(path)

    def test_extracts_three_real_frames_and_signatures(self):
        source = self.create_media('frames.mp4')
        frames, hashes = autopost.extrair_frames(source, 2, self.temp.name)
        self.assertEqual(len(frames), 3)
        self.assertTrue(all(Path(p).is_file() and Path(p).stat().st_size > 0 for p in frames))
        self.assertTrue(all(len(value) == 16 for value in hashes))

    def test_metadata_cleanup_watermarks_and_removes_source(self):
        source = self.create_media('tagged.mp4')
        autopost._media_command([
            'ffmpeg', '-hide_banner', '-y', '-i', source, '-map_metadata', '-1',
            '-metadata', 'title=original title', '-metadata:s:v:0',
            'comment=stream tag', '-c', 'copy',
            os.path.join(self.temp.name, 'tagged_with_metadata.mp4'),
        ])
        source_with_metadata = os.path.join(self.temp.name, 'tagged_with_metadata.mp4')
        media_command = autopost._media_command
        with patch.object(autopost, '_media_command', wraps=media_command) as command:
            output = autopost.limpar_metadados_e_adicionar_marca(
                source_with_metadata, '@diverso_clips', self.temp.name
            )
        remux_args = command.call_args_list[0].args[0]
        watermark_args = command.call_args_list[1].args[0]
        self.assertIn('-map_metadata', remux_args)
        self.assertIn('-c', remux_args)
        self.assertEqual(remux_args[remux_args.index('-c') + 1], 'copy')
        drawtext = watermark_args[watermark_args.index('-vf') + 1]
        self.assertIn('fontcolor=white@0.6', drawtext)
        self.assertIn('fontsize=h/28', drawtext)
        self.assertIn('x=20:y=h-th-30', drawtext)
        self.assertIn('shadowcolor=black@0.35', drawtext)
        self.assertNotIn('enable=', drawtext)
        self.assertTrue(Path(output).is_file())
        self.assertFalse(Path(source_with_metadata).exists())
        metadata = json.loads(autopost._media_command([
            'ffprobe', '-v', 'error', '-show_format', '-show_streams', '-of', 'json', output,
        ]).stdout)
        self.assertNotIn('title', metadata.get('format', {}).get('tags', {}))
        self.assertNotIn('comment', metadata['streams'][0].get('tags', {}))


class PipelineTests(unittest.TestCase):
    def test_scheduled_upload_uses_improved_path_both_platforms(self):
        # Extract only the worker: never import app or initialize its live database.
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'process_social_post_job')
        for platform in ('youtube', 'tiktok'):
            with self.subTest(platform=platform), tempfile.TemporaryDirectory() as directory:
                social = Mock()
                for name in ('SocialPublishingPending', 'SocialPublishingRejected', 'SocialPublishingError'):
                    setattr(social, name, type(name, (Exception,), {}))
                social.upload_tiktok_inbox.return_value = ('remote', 'inbox')
                social.publish_youtube.return_value = 'remote'
                auto = Mock()
                auto.analisar_qualidade.return_value = {
                    'qualidade': 'RUIM', 'motivos': ['áudio baixo'], 'duration': 60,
                }
                auto.video_bloqueado.return_value = False
                auto.prepared_media.return_value = None
                auto.extrair_frames.return_value = ([], [])
                auto.verificar_riscos.return_value = {'checked': True}
                output = os.path.join(directory, 'automatico', 'video_melhorado.mp4')
                auto.melhorar_video.return_value = output
                auto.limpar_metadados_e_adicionar_marca.side_effect = (
                    lambda *args, **kwargs: (kwargs['progress_callback'](), output)[1]
                )
                callback = Mock()
                ns = dict(os=os, tempfile=types.SimpleNamespace(mkdtemp=lambda **kw: directory),
                          subprocess=subprocess, social_publishing=social, autopost=auto,
                          CANAL_NOME='@diverso_clips',
                          SocialPostsError=type('SocialPostsError', (Exception,), {}),
                          PostedVideosError=type('PostedVideosError', (Exception,), {}),
                          app=Mock())
                for name in ('update_social_post_job', 'copy_completed_improvement',
                             'download_video_for_social_post', 'convert_video_for_tiktok',
                             'record_social_post', 'mark_video_posted', 'remove_improvement_files'):
                    ns[name] = Mock()
                exec(compile(ast.Module(body=[node], type_ignores=[]), '<worker>', 'exec'), ns)
                ns['process_social_post_job'](
                    'job', platform, 'video', 'original.mp4', 'Título editado',
                    '#ciencia', 'mock-key', quality_callback=callback,
                    video_number=1, video_total=4,
                )
                uploader = social.publish_youtube if platform == 'youtube' else social.upload_tiktok_inbox
                self.assertEqual(uploader.call_args.args[0], output)
                if platform == 'youtube':
                    self.assertEqual(uploader.call_args.args[1], 'Título editado')
                self.assertIn('Limpando metadados do vídeo 1 de 4...', [c.args[1] for c in callback.call_args_list])
                self.assertIn(
                    'Adicionando @diverso_clips no canto inferior esquerdo do vídeo 1 de 4...',
                    [c.args[1] for c in callback.call_args_list],
                )
                self.assertIn('Postando...', [c.args[1] for c in callback.call_args_list])
                if platform == 'youtube':
                    social.publish_youtube.reset_mock()
                    callback.reset_mock()
                    auto.limpar_metadados_e_adicionar_marca.side_effect = RuntimeError('FFmpeg falhou')
                    ns['process_social_post_job'](
                        'job-falhou', platform, 'video', 'original.mp4',
                        'Título editado', '#ciencia', 'mock-key',
                        quality_callback=callback,
                        video_number=1, video_total=4,
                    )
                    social.publish_youtube.assert_not_called()
                    callback.assert_any_call(
                        'falhou', 'Falha ao limpar metadados, pulando'
                    )


class ScreenTests(StoreTests):
    def test_home_shows_only_connections_and_folder_form_until_drive_loads(self):
        from flask import Flask, render_template, request

        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
        index_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "index"
        )
        index_node.decorator_list = []
        folder_link = "https://drive.google.com/drive/folders/folder"
        load_link = Mock(return_value="")
        save_link = Mock()
        folder_view = {
            "folder_name": "Clipes",
            "video_count": 4,
            "videos": [],
            "folders": [{"id": "subfolder", "name": "50 MIL cortes virais", "video_count": 4}],
            "warnings": [],
        }
        get_folder_view = Mock(return_value=folder_view)
        namespace = {
            "request": request,
            "render_template": render_template,
            "load_drive_folder_link": load_link,
            "save_drive_folder_link": save_link,
            "extract_folder_id": lambda value: "folder",
            "parse_folder_path": lambda value: value.split(","),
            "get_folder_view": get_folder_view,
            "load_good_video_ids": Mock(return_value=set()),
            "load_posted_video_ids": Mock(return_value=set()),
            "load_social_posts": Mock(return_value={}),
            "load_folder_models": Mock(return_value={}),
            "AUTPOST_STORE": Mock(),
            "video_quality": Mock(),
            "video_duration_milliseconds": Mock(),
            "score_video": Mock(),
            "apply_folder_model": Mock(),
            "gerar_titulo": Mock(),
            "os": os,
            "re": re,
            "DriveError": type("DriveError", (Exception,), {}),
            "GoodVideosError": type("GoodVideosError", (Exception,), {}),
            "FolderModelsError": type("FolderModelsError", (Exception,), {}),
            "PostedVideosError": type("PostedVideosError", (Exception,), {}),
            "SocialPostsError": type("SocialPostsError", (Exception,), {}),
            "AutomationSettingsError": type("AutomationSettingsError", (Exception,), {}),
        }
        exec(
            compile(ast.Module(body=[index_node], type_ignores=[]), "<home-route>", "exec"),
            namespace,
        )
        web = Flask("isolated_home_test", template_folder=str(ROOT / "templates"))
        web.secret_key = "isolated-test-only"
        web.testing = True
        web.jinja_env.filters["duration"] = lambda value: ""
        web.context_processor(
            lambda: {
                "csrf_token": "test-token",
                "tiktok_connected": True,
                "youtube_connected": True,
                "social_token_error": None,
            }
        )
        web.add_url_rule("/", endpoint="index", view_func=namespace["index"], methods=["GET", "POST"])
        web.add_url_rule("/agendamento", endpoint="agendamento_page", view_func=lambda: "")
        web.add_url_rule("/oauth/<platform>", endpoint="start_social_oauth", view_func=lambda platform: "")
        web.add_url_rule(
            "/disconnect/<platform>",
            endpoint="disconnect_social",
            view_func=lambda platform: "",
            methods=["POST"],
        )
        web.add_url_rule("/privacy", endpoint="privacy", view_func=lambda: "")
        web.add_url_rule("/terms", endpoint="terms", view_func=lambda: "")
        client = web.test_client()

        with patch.dict(os.environ, {"DRIVE_API_KEY": "test-key"}):
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn("<h1>Painel de Cortes Prontos</h1>", page)
            self.assertIn("TikTok conectado", page)
            self.assertIn("YouTube conectado", page)
            self.assertIn("Desconectar", page)
            self.assertIn("O vídeo será enviado como rascunho", page)
            self.assertIn("Link da pasta do Google Drive", page)
            self.assertIn("Listar pastas", page)
            self.assertIn("A pasta precisa estar acessível", page)
            self.assertIn('placeholder="Cole o link aqui..."', page)
            self.assertNotIn(folder_link, page)
            self.assertNotIn("Organize seus vídeos", page)
            self.assertNotIn("Postagem Automática", page)
            self.assertNotIn("50 MIL cortes virais", page)
            self.assertNotIn('class="content-panel"', page)
            self.assertNotIn("Informações legais", page)

            get_folder_view.side_effect = namespace["DriveError"]("Drive indisponível")
            response = client.post("/", data={"folder_link": folder_link})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Postagem Automática", response.get_data(as_text=True))
            self.assertNotIn(folder_link, response.get_data(as_text=True))
            save_link.assert_not_called()

            get_folder_view.side_effect = None
            response = client.post("/", data={"folder_link": folder_link})
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn("50 MIL cortes virais", page)
            self.assertIn("Postagem Automática", page)
            self.assertNotIn(folder_link, page)
            save_link.assert_called_once_with(folder_link)

            load_link.return_value = folder_link
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertNotIn("50 MIL cortes virais", page)
            self.assertNotIn("Postagem Automática", page)
            self.assertNotIn(folder_link, page)
            load_link.assert_not_called()

            response = client.get("/?path=folder")
            self.assertEqual(response.status_code, 200)
            page = response.get_data(as_text=True)
            self.assertIn("50 MIL cortes virais", page)
            self.assertIn("Postagem Automática", page)
            self.assertNotIn(folder_link, page)

    def test_processed_preview_route_serves_only_the_prepared_report_media(self):
        from flask import Flask, request, send_file, session

        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8-sig'))
        node = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == 'automatic_cover'
        )
        web = Flask('isolated_preview_test')
        web.secret_key = 'isolated-test-only'
        web.testing = True
        sha256 = 'a' * 64
        prepared = Path(self.temp.name) / 'processed.mp4'
        prepared.write_bytes(b'processed preview')
        self.store.reviews = Mock(return_value=[{
            'state': {'videos': [{'sha256': sha256, 'prepared_path': 'stored/path'}]},
        }])
        ns = {
            'app': web, 'request': request, 'session': session, 'send_file': send_file,
            're': re, 'os': os, 'AUTPOST_STORE': self.store, 'autopost': autopost,
            'schedule_now': datetime.now,
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<preview-route>', 'exec'), ns)
        web.add_url_rule(
            '/agendamento/capa/<sha256>', endpoint='automatic_cover',
            view_func=ns['automatic_cover'],
        )
        client = web.test_client()
        with client.session_transaction() as state:
            state['csrf_token'] = 'test-token'
        with patch.object(autopost, 'prepared_media', return_value=str(prepared)):
            response = client.get(f'/agendamento/capa/{sha256}?video=1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'video/mp4')
        self.assertEqual(response.get_data(), b'processed preview')
        response.close()

    def test_automatic_button_route_defaults_and_status(self):
        from flask import Flask, jsonify, render_template, request, session, url_for
        web = Flask('isolated_test', template_folder=str(ROOT / 'templates'))
        web.secret_key = 'isolated-test-only'
        web.testing = True
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8-sig'))
        names = {'agendamento_page', 'progresso_automatico', '_folder_subject',
                 'gerar_titulo', 'suggest_social_copy'}
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
        script = next(n for n in tree.body if isinstance(n, ast.Assign)
                      and any(isinstance(t, ast.Name) and t.id == 'TITLE_EDITOR_SCRIPT' for t in n.targets))
        social = Mock()
        social.load_social_tokens.return_value = {'youtube': {'refresh_token': 'mock'}, 'tiktok': {'refresh_token': 'mock'}}
        ns = dict(app=web, request=request, session=session, render_template=render_template,
                  jsonify=jsonify, url_for=url_for, os=os, re=re, secrets=secrets, json=json,
                  unicodedata=unicodedata, autopost=autopost, AUTPOST_STORE=self.store,
                  social_publishing=social, AUTPOST_TIMEZONE_NAME='America/Sao_Paulo',
                  schedule_now=lambda: datetime.fromisoformat(self.args[-1]),
                  get_csrf_token=lambda: 'test-token', extract_folder_id=lambda link: 'folder',
                  get_folder_name=lambda *a: 'Ciência VIDEOS 2', register_autopost_schedule=Mock(),
                  threading=Mock(), prepare_schedule_content=Mock())
        web.add_url_rule('/suggestions', endpoint='sugestoes_titulo_agendamento', view_func=lambda: '')
        exec(compile(ast.Module(body=[script] + nodes, type_ignores=[]), '<routes>', 'exec'), ns)
        client = web.test_client()
        with client.session_transaction() as sess:
            sess['csrf_token'] = 'test-token'
        with patch.object(autopost, 'validar_analise_conteudo'):
            for _ in range(2):
                response = client.post('/agendamento', data={'action': 'automatico', 'csrf_token': 'test-token', 'folder_link': 'folder'})
                self.assertEqual(response.status_code, 200)
                self.assertIn('RODAR AUTOMÁTICO', response.get_data(as_text=True))
                self.assertIn('Agendamento ativo!', response.get_data(as_text=True))
        schedules = self.store.list_schedules()
        self.assertEqual(len(schedules), 1)
        self.assertEqual(schedules[0]['times'], self.args[3])
        self.assertEqual(schedules[0]['destinations'], ['youtube', 'tiktok'])
        self.assertEqual(schedules[0]['title_template'], '')
        self.assertEqual(client.get('/agendamento/progresso').status_code, 403)
        self.assertEqual(client.get('/agendamento/progresso', headers={'X-CSRF-Token': 'test-token'}).json['history'], [])


if __name__ == '__main__':
    unittest.main()
