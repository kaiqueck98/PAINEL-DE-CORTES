import ast
from contextlib import ExitStack
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import threading
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import autopost


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = autopost.AutopostStore(os.path.join(self.tmp.name, 'test.sqlite'))
        self.store.initialize()
        sid = self.store.create_schedule('folder', 'NAME_MUST_NOT_REACH_MODEL', 4,
                                        ['08:00', '12:30', '18:00', '21:00'], ['youtube', 'tiktok'],
                                        'America/Sao_Paulo', '2026-11-21')
        self.schedule = self.store.get_schedule(sid)
        self.quality = {'width': 1280, 'height': 720, 'bitrate': 2000000,
                        'blur': 2, 'duration': 30, 'audio_db': -20, 'has_audio': True}
        self.trends = {'source': 'test source', 'updated_at': datetime.now(timezone.utc).isoformat(),
                       'hashtags': ['#Culinaria', '#Ciencia']}

    def test_best_four_not_first_four_and_persistent_daily_plan(self):
        videos = [{'id': str(i), 'name': 'DO_NOT_USE.mp4', 'folderName': 'DO_NOT_USE'} for i in range(6)]
        analyzed = []
        def download(vid, path):
            Path(path).write_text(vid)
            analyzed.append(vid)
        def quality(path):
            i = int(Path(path).read_text())
            return {**self.quality, 'duration': 30 + i, 'height': 480 if i < 2 else 720}
        with ExitStack() as stack:
            stack.enter_context(patch.object(autopost, 'validar_analise_conteudo'))
            stack.enter_context(patch.object(autopost, 'carregar_tendencias', return_value=self.trends))
            stack.enter_context(patch.object(autopost, 'analisar_qualidade', side_effect=quality))
            stack.enter_context(patch.object(autopost, 'extrair_frames', return_value=(['a', 'b', 'c'], ['123', '456', '789'])))
            stack.enter_context(patch.object(autopost, 'transcrever_whisper', return_value='Uma pessoa prepara um bolo.'))
            stack.enter_context(patch.object(autopost, 'gerar_textos_conteudo', return_value={'summary': 'Preparo de bolo', 'title': 'Como esse bolo ganha forma', 'caption': 'Uma pessoa prepara um bolo.', 'hashtags': ['#Culinaria']}))
            result = autopost.preparar_melhores_do_dia(self.store, self.schedule, '2026-11-21', videos, download, set())
            again = autopost.preparar_melhores_do_dia(self.store, self.schedule, '2026-11-21', videos, download, set())
        self.assertEqual(analyzed, ['0', '1', '2', '3', '4', '5'])
        self.assertEqual([v['id'] for v in result], ['2', '3', '4', '5'])
        self.assertEqual([v['id'] for v in again], ['2', '3', '4', '5'])
        self.assertEqual(len(self.store.content_overview('2026-11-21')['videos']), 6)

    def test_duplicates_exact_and_visual(self):
        a = {'sha256': 'one', 'quality': self.quality, 'visual_hashes': ['0123456789abcdef'] * 3, 'transcription': 'A mesma fala'}
        self.assertTrue(autopost.conteudo_repetido(a, {**a, 'sha256': 'other'}))
        self.assertTrue(autopost.conteudo_repetido(a, {'sha256': 'one'}))
        self.assertFalse(autopost.conteudo_repetido(a, {**a, 'sha256': 'other', 'quality': {**self.quality, 'duration': 50}}))
        self.assertFalse(autopost.conteudo_repetido(a, {**a, 'sha256': 'other', 'transcription': 'Uma fala diferente'}))

    def test_analysis_quality_uses_drive_metadata_and_short_sample_signals(self):
        sample_quality = {
            **self.quality,
            "width": 640,
            "height": 360,
            "duration": 20,
            "bitrate": 900000,
            "qualidade": "RUIM",
            "motivos": ["resolução abaixo de 720p"],
        }
        video = {
            "size": "15000000",
            "videoMediaMetadata": {
                "durationMillis": "60000",
                "width": "1920",
                "height": "1080",
            },
        }
        with patch.object(autopost, "analisar_qualidade", return_value=sample_quality):
            quality = autopost.analisar_qualidade_amostra("sample.mp4", video)

        self.assertEqual(quality["duration"], 60)
        self.assertEqual(quality["sample_duration"], 20)
        self.assertEqual((quality["width"], quality["height"]), (1920, 1080))
        self.assertEqual(quality["bitrate"], 2_000_000)
        self.assertEqual(quality["qualidade"], "BOA")

    def test_progress_updates_and_finishes_at_one_hundred_percent(self):
        lease = self.store.begin_day(self.schedule["id"], "2026-11-21", total=4)
        self.store.update_day_progress(
            self.schedule["id"],
            "2026-11-21",
            lease,
            2,
            4,
            38,
            "Criando título e legenda em japonês...",
        )
        day = self.store.content_overview("2026-11-21")["days"][0]
        self.assertEqual(day["progress"], 38)
        self.assertEqual(day["current_index"], 2)
        self.assertEqual(day["total"], 4)
        self.assertEqual(day["message"], "Criando título e legenda em japonês...")

        self.store.finish_day(
            self.schedule["id"],
            "2026-11-21",
            lease,
            [],
            success_count=4,
        )
        day = self.store.content_overview("2026-11-21")["days"][0]
        self.assertEqual(day["progress"], 100)
        self.assertEqual(
            day["message"], "Pronto! 4 de 4. 4 deram certo, 0 deram erro."
        )

    def test_bad_sample_is_skipped_and_final_progress_counts_successes_and_errors(self):
        videos = [
            {"id": "bad", "name": "bad.mp4", "size": "1000",
             "videoMediaMetadata": {"durationMillis": "60000", "width": "1280", "height": "720"}},
            {"id": "good", "name": "good.mp4", "size": "2000",
             "videoMediaMetadata": {"durationMillis": "60000", "width": "1280", "height": "720"}},
        ]
        frame_paths = []
        for index in range(3):
            frame = Path(self.tmp.name) / f"sample-frame-{index}.jpg"
            frame.write_bytes(b"small frame")
            frame_paths.append(str(frame))

        def asset_dir(sha256):
            directory = Path(self.tmp.name) / "assets" / sha256
            directory.mkdir(parents=True, exist_ok=True)
            return directory

        def download_sample(video, destination, progress):
            if video["id"] == "bad":
                raise autopost.VideoDownloadError("width not divisible by 2")
            Path(destination).write_bytes(b"short sample for video two")
            progress(1.0)

        def prepare_media(report, download):
            report["improved"] = False

        with ExitStack() as stack:
            stack.enter_context(patch.object(autopost, "validar_analise_conteudo"))
            stack.enter_context(patch.object(autopost, "carregar_tendencias", return_value=self.trends))
            stack.enter_context(patch.object(autopost, "ler_bloqueados", return_value=[]))
            stack.enter_context(patch.object(autopost, "ler_historico", return_value=[]))
            stack.enter_context(patch.object(autopost, "video_bloqueado", return_value=False))
            stack.enter_context(patch.object(autopost, "analisar_qualidade_amostra", return_value=self.quality))
            stack.enter_context(patch.object(autopost, "extrair_frames", return_value=(frame_paths, ["1", "2", "3"])))
            stack.enter_context(patch.object(autopost, "verificar_riscos", return_value={"checked": True, "reasons": []}))
            stack.enter_context(patch.object(autopost, "salvar_primeiro_frame", return_value=frame_paths[0]))
            stack.enter_context(patch.object(autopost, "transcrever_whisper", return_value="Uma fala curta."))
            stack.enter_context(patch.object(
                autopost,
                "gerar_textos_conteudo",
                return_value={"summary": "Uma história", "title": "Um título", "hashtags": []},
            ))
            stack.enter_context(patch.object(autopost, "asset_directory", side_effect=asset_dir))
            stack.enter_context(patch.object(autopost, "preparar_midia_selecionada", side_effect=prepare_media))
            stack.enter_context(patch.object(autopost, "gerar_capa", return_value="cover.jpg"))
            selected = autopost.preparar_melhores_do_dia(
                self.store,
                self.schedule,
                "2026-11-21",
                videos,
                Mock(),
                set(),
                sample_download=download_sample,
            )

        day = self.store.content_overview("2026-11-21")["days"][0]
        reports = self.store.content_overview("2026-11-21")["videos"]
        failed_report = next(report for report in reports if report["id"] == "bad")
        self.assertEqual([report["id"] for report in selected], ["good"])
        self.assertIn("Vídeo 1 pulado - erro de tamanho, tentando vídeo 2", failed_report["message"])
        self.assertIn("1 deram certo, 1 deram erro", day["message"])
        self.assertEqual(day["progress"], 100)
        self.assertEqual(day["status"], "ready")

    def test_story_then_copy_only_allowlisted_tags(self):
        frames = []
        for i in range(3):
            p = Path(self.tmp.name) / f'DO_NOT_USE_{i}.jpg'
            p.write_bytes(b'fake-image')
            frames.append(str(p))
        story = {'summary': 'Uma pessoa prepara um bolo.', 'uncertainty': '', 'sufficient': True}
        copy = {'title': 'O preparo deste bolo em detalhes', 'caption_sentences': ['Uma pessoa prepara um bolo.'],
                'hashtags': ['#Culinaria'], 'hashtag_reasons': ['A receita envolve culinária.']}
        japanese = {'text': '爆笑', 'translation': 'muito engraçado',
                    'reason': 'A pessoa ri enquanto prepara o bolo.', 'sufficient': True}
        with patch.object(autopost, '_content_json', side_effect=[story, copy, japanese]) as request:
            result = autopost.gerar_textos_conteudo(frames, 'Agora misturamos a farinha.', self.trends)
        self.assertEqual(result['hashtags'], ['#Culinaria'])
        self.assertEqual(result['japanese'], japanese)
        self.assertNotIn('DO_NOT_USE', repr(request.call_args_list))
        self.assertEqual(len(request.call_args_list[0].args[1]), 4)
        japanese_evidence = request.call_args_list[2].args[1]
        self.assertEqual(len(japanese_evidence), 4)
        self.assertEqual(json.loads(japanese_evidence[0]['text'])['transcription'],
                         'Agora misturamos a farinha.')
        invalid = {**copy, 'hashtags': ['#Inventada']}
        with patch.object(autopost, '_content_json', side_effect=[story, invalid, japanese]):
            fallback = autopost.gerar_textos_conteudo(frames, 'Agora misturamos a farinha.', self.trends)
        self.assertEqual(fallback['mode'], 'simple')
        self.assertNotIn('#Inventada', fallback['hashtags'])
        self.assertEqual(fallback['japanese'], japanese)

    def test_japanese_title_validation_accepts_any_well_formed_japanese_title(self):
        valid = {'text': '日常', 'translation': 'cotidiano', 'reason': 'Resume o assunto do vídeo.'}
        autopost.validar_titulo_japones(valid)
        with self.assertRaisesRegex(RuntimeError, '2 a 4 caracteres'):
            autopost.validar_titulo_japones({**valid, 'text': 'funny'})
        with self.assertRaisesRegex(RuntimeError, 'repetiu'):
            autopost.validar_titulo_japones(valid, ['日常'])

    def test_japanese_title_swap_excludes_previous_titles_and_renders_new_one(self):
        frames = []
        for index in range(3):
            path = Path(self.tmp.name) / f'frame-{index}.jpg'
            path.write_bytes(b'frame')
            frames.append(str(path))
        current = {'text': '爆笑', 'translation': 'muito engraçado',
                   'reason': 'A pessoa ri.'}
        replacement = {'text': '驚愕', 'translation': 'surpresa',
                       'reason': 'A expressão muda para espanto.'}
        report = {
            'content': {'summary': 'A pessoa ri e se espanta.', 'japanese': current},
            'previous_japanese_titles': ['悲哀'],
            'analysis_frames': frames,
            'transcription': 'Não acredito!',
        }
        with patch.object(autopost, 'gerar_titulo_japones', return_value=replacement) as generate, \
                patch.object(autopost, 'gravar_titulo_japones') as render:
            autopost.trocar_titulo_japones(report)
        generate.assert_called_once_with(
            frames, 'Não acredito!', 'A pessoa ri e se espanta.', ['悲哀', '爆笑']
        )
        render.assert_called_once_with(report, replacement)
        self.assertEqual(report['previous_japanese_titles'], ['悲哀', '爆笑'])
        self.assertTrue(report['content']['japanese_required'])

    def test_expired_trends_rejected(self):
        path = Path(self.tmp.name) / 'trends.json'
        path.write_text(json.dumps({**self.trends, 'updated_at': (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}))
        with patch.dict(os.environ, {'AUTOPOST_TRENDS_FILE': str(path)}):
            with self.assertRaises(RuntimeError):
                autopost.carregar_tendencias()
        path.write_text(json.dumps(self.trends))
        with patch.dict(os.environ, {'AUTOPOST_TRENDS_FILE': str(path)}):
            self.assertEqual(autopost.carregar_tendencias()['hashtags'], self.trends['hashtags'])

    def test_no_filename_or_folder_fallback(self):
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'gerar_titulo')
        ns = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<titles>', 'exec'), ns)
        self.assertEqual(ns['gerar_titulo']('FOLDER 6', 'FILE.mp4'), [])
        self.assertEqual(ns['gerar_titulo']({'summary': 'Preparo de bolo', 'title': 'Um bolo ganha forma'}), ['Um bolo ganha forma'])

    def test_incomplete_analysis_records_error_and_does_not_select(self):
        with patch.object(autopost, 'validar_analise_conteudo', side_effect=RuntimeError('No key')):
            with self.assertRaises(RuntimeError):
                autopost.preparar_melhores_do_dia(self.store, self.schedule, '2026-11-21', [], Mock(), set())
        day, selected = self.store.get_day(self.schedule['id'], '2026-11-21')
        self.assertEqual(day['status'], 'error')
        self.assertEqual(selected, [])

    def test_direct_post_payload_requires_scope_and_privacy(self):
        video = Path(self.tmp.name) / 'upload.mp4'
        video.write_bytes(b'data')
        copy = {'title': 'Bolo pronto', 'hashtags': ['#Culinaria']}
        info = {'privacy_level_options': ['SELF_ONLY']}
        with self.assertRaises(RuntimeError):
            autopost.payload_tiktok_publish(str(video), copy, info, 'SELF_ONLY', {'video.upload'})
        payload = autopost.payload_tiktok_publish(str(video), copy, info, 'SELF_ONLY', {'video.publish'})
        self.assertEqual(payload['post_info']['title'], 'Bolo pronto\n#Culinaria')

    def test_future_direct_post_uses_title_and_hashtags(self):
        video = Path(self.tmp.name) / 'video.mp4'
        video.write_bytes(b'fake-media')
        client = Mock()
        client._tiktok_request.side_effect = [
            {'privacy_level_options': ['SELF_ONLY'], 'max_video_post_duration_sec': 60},
            {'publish_id': 'id', 'upload_url': 'https://upload.tiktokapis.com/video'},
            {'status': 'PUBLISH_COMPLETE'},
        ]
        client._http_request.return_value = (201, {}, b'')
        content = {'title': 'Bolo pronto', 'hashtags': ['#Culinaria']}
        with patch.object(autopost, '_media_tools', return_value=('ffmpeg', 'ffprobe')), patch.object(
                autopost, '_media_command', return_value=Mock(stdout='{"format":{"duration":"30"}}')):
            result = autopost.publicar_tiktok_direto(str(video), content, 'mock', {'video.publish'}, 'SELF_ONLY', True, client)
        self.assertEqual(result, ('id', 'posted'))
        payload = client._tiktok_request.call_args_list[1].args[2]
        self.assertEqual(payload['post_info']['title'], 'Bolo pronto\n#Culinaria')

    def test_scheduler_starts_before_restoring_jobs(self):
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'ensure_autopost_scheduler')
        scheduler = Mock(running=False)
        scheduler.start.side_effect = lambda: setattr(scheduler, 'running', True)
        restored = []
        def register(schedule):
            self.assertTrue(scheduler.running)
            restored.append(schedule['id'])
        ns = {'AUTPOST_SCHEDULER': scheduler, 'AUTPOST_SCHEDULER_LOCK': threading.Lock(),
              'AUTPOST_SCHEDULER_STARTED': False, 'AUTPOST_STORE': self.store,
              'register_autopost_schedule': register}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<scheduler>', 'exec'), ns)
        ns['ensure_autopost_scheduler']()
        ns['ensure_autopost_scheduler']()
        self.assertEqual(restored, [self.schedule['id']])
        scheduler.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
