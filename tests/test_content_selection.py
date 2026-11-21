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

    def test_story_then_copy_only_allowlisted_tags(self):
        frames = []
        for i in range(3):
            p = Path(self.tmp.name) / f'DO_NOT_USE_{i}.jpg'
            p.write_bytes(b'fake-image')
            frames.append(str(p))
        story = {'summary': 'Uma pessoa prepara um bolo.', 'uncertainty': '', 'sufficient': True}
        copy = {'title': 'O preparo deste bolo em detalhes', 'caption_sentences': ['Uma pessoa prepara um bolo.'], 'hashtags': ['#Culinaria']}
        with patch.object(autopost, '_content_json', side_effect=[story, copy]) as request:
            result = autopost.gerar_textos_conteudo(frames, 'Agora misturamos a farinha.', self.trends)
        self.assertEqual(result['hashtags'], ['#Culinaria'])
        self.assertNotIn('DO_NOT_USE', repr(request.call_args_list))
        self.assertEqual(len(request.call_args_list[0].args[1]), 4)
        invalid = {**copy, 'hashtags': ['#Inventada']}
        with patch.object(autopost, '_content_json', side_effect=[story, invalid]):
            with self.assertRaises(RuntimeError):
                autopost.gerar_textos_conteudo(frames, '', self.trends)

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
