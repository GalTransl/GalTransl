import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import yaml

from GalTransl.CacheDictionary import inspect_cache_dictionaries
from GalTransl.CSentense import CSentense
from GalTransl.Dictionary import CNormalDic, CGptDict
from GalTransl.server import JobRegistry, build_handler
from GalTransl.server_runtime import encode_project_dir


class CacheDictionaryMatchesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.write('pre.txt', '猫\t犬\nっ\t\n')
        self.write('post.txt', 'post_src\t犬\t哦\t喔\nぉ\t\nっ\t\nmono\t♥\t心\n')
        self.write('gpt.txt', '犬\t狗\t动物\n不存在\t无\n')
        self.config = {
            'common': {'language': 'zh-cn'},
            'plugin': {'filePlugin': 'file_galtransl_json', 'textPlugins': []},
            'dictionary': {
                'preDict': ['(project_dir)pre.txt'], 'postDict': ['(project_dir)post.txt'],
                'gpt.dict': ['(project_dir)gpt.txt'], 'sortDict': False, 'usePreDictInName': True,
            },
        }
        self.save_config()
        self.entry = {'index': 163, 'name': '猫', 'pre_src': '「猫っ」',
                      'post_src': '犬', 'pre_dst': '嗯っ♥', 'proofread_dst': '哦ぉっ♥'}

    def write(self, name, text):
        (self.project / name).write_text(text, encoding='utf-8')

    def save_config(self, name='config.yaml'):
        self.write(name, yaml.safe_dump(self.config, allow_unicode=True))

    def inspect(self, entries=None, **options):
        return inspect_cache_dictionaries(str(self.project), 'config.yaml', entries or [self.entry], **options)['results']

    def test_counts_replay_conditions_and_deduplicate_speaker_text_matches(self):
        before = copy.deepcopy(self.entry)
        result = self.inspect(details=True)[0]
        self.assertEqual(result['counts'], {'pre': 2, 'post': 3, 'gpt': 1})
        self.assertEqual(self.entry, before)
        self.assertEqual([(item['kind'], item['line']) for item in result['entries']],
                         [('pre', 1), ('pre', 2), ('post', 1), ('post', 2), ('post', 3), ('gpt', 1)])
        self.assertEqual(result['entries'][3]['rule'], 'ぉ\t')
        self.assertEqual(result['entries'][-1]['note'], '动物')
        self.assertEqual(Path(result['entries'][-1]['file']).resolve(), (self.project / 'gpt.txt').resolve())
        # Proofread text takes precedence; editing it changes the matching rules.
        entry = {**self.entry, 'proofread_dst': '中文'}
        self.assertEqual(self.inspect([entry])[0]['counts']['post'], 0)

    def test_missing_files_are_skipped_while_existing_dictionaries_are_inspected(self):
        self.config['dictionary']['preDict'].insert(0, '(project_dir)项目字典_译前.txt')
        self.config['dictionary']['postDict'].append('(project_dir)missing.txt')
        self.config['dictionary']['gpt.dict'].append('(project_dir)missing-gpt.txt')
        self.save_config()
        with patch('GalTransl.Dictionary.LOGGER.warning') as warning:
            self.assertEqual(self.inspect()[0]['counts'], {'pre': 2, 'post': 3, 'gpt': 1})
            self.assertEqual(self.inspect(details=True)[0]['counts'], {'pre': 2, 'post': 3, 'gpt': 1})
            warning.assert_not_called()
            # The normal translation loader still reports configuration issues.
            CNormalDic([str(self.project / 'missing.txt')])
            warning.assert_called_once()

    def test_invalid_existing_dictionary_is_not_silenced(self):
        (self.project / 'pre.txt').write_bytes(b'\xff')
        with self.assertRaises(UnicodeDecodeError):
            self.inspect()

    def test_legacy_fields_and_source_only_entries(self):
        legacy = {'index': 1, 'name': '', 'pre_jp': '猫っ', 'post_jp': '犬', 'pre_zh': '嗯っ'}
        self.assertEqual(self.inspect([legacy])[0]['counts'], {'pre': 2, 'post': 1, 'gpt': 1})
        self.assertEqual(self.inspect([{'pre_src': '猫っ'}])[0]['counts'], {'pre': 2, 'post': 0, 'gpt': 1})

    def test_split_dialogue_context_is_consistent_for_summary_and_details(self):
        self.write('pre.txt', 'diag\t中\t中间\nmono\t中\t独白\n')
        entry = {'index': 2, 'name': '', 'pre_src': '中', 'post_src': '中间',
                 'context_before': [{'name': '猫', 'pre_src': '「开始'}],
                 'context_after': [{'pre_src': '结束」'}]}
        result = self.inspect([entry], details=True)[0]
        self.assertEqual(result['counts']['pre'], 1)
        self.assertEqual(result['entries'][0]['line'], 1)
        self.assertEqual(self.inspect([entry])[0]['counts'], result['counts'])

    def test_details_are_paginated_without_losing_counts(self):
        self.write('gpt.txt', ''.join(f'犬\t狗{i}\t注{i}\n' for i in range(220)))
        first, second, third = [self.inspect(details=True, offset=offset)[0] for offset in (0, 100, 200)]
        self.assertEqual(first['counts']['gpt'], 220)
        self.assertEqual([len(page['entries']) for page in (first, second, third)], [100, 100, 25])
        self.assertEqual(len({(item['kind'], item['file'], item['line']) for page in (first, second, third) for item in page['entries']}), 225)

    def test_recording_preserves_prefix_onetime_and_chained_replacements(self):
        self.write('rules.txt', '^^a\tb\nb\tc\n1^c\td\nmono\td\te\n')
        dictionary = CNormalDic([str(self.project / 'rules.txt')])
        tran = CSentense('aa')
        hits = []
        self.assertEqual(dictionary.do_replace('aa', tran, matches=hits), 'ea')
        self.assertEqual(dictionary.do_replace('aa', tran), 'ea')
        self.assertEqual([hit.line_number for hit in hits], [1, 2, 3, 4])
        hits = []
        self.assertEqual(dictionary.do_replace('xa', tran, matches=hits), 'xa')
        self.assertEqual(hits, [])

    def test_gpt_selection_matches_prompt_overlap_rules(self):
        self.write('gpt.txt', '東京\t东京\n東京駅\t东京站\n京\t京\n')
        dictionary = CGptDict([str(self.project / 'gpt.txt')])
        tran = CSentense('東京駅')
        self.assertEqual([item.search_word for item in dictionary.matching_entries([tran])], ['東京', '東京駅'])
        prompt = dictionary.gen_prompt([tran])
        self.assertIn('| 東京 | 东京 |', prompt)
        self.assertIn('| 東京駅 | 东京站 |', prompt)
        self.assertNotIn('| 京 | 京 |', prompt)

    def test_endpoint_is_read_only_and_rejects_unbounded_requests(self):
        snapshot = {path: path.read_bytes() for path in self.project.iterdir()}
        server = ThreadingHTTPServer(('127.0.0.1', 0), build_handler(JobRegistry()))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}/api/projects/{encode_project_dir(str(self.project))}/cache/dictionary-matches'
        try:
            for entries, status in [([self.entry], 200), ([], 400), ([self.entry] * 101, 400)]:
                request = urllib.request.Request(url, data=json.dumps({'entries': entries, 'details': False}).encode(),
                                                 headers={'Content-Type': 'application/json'}, method='POST')
                try:
                    with urllib.request.urlopen(request, timeout=10) as response:
                        self.assertEqual(response.status, status)
                        self.assertEqual(json.load(response)['results'][0]['counts'], {'pre': 2, 'post': 3, 'gpt': 1})
                except urllib.error.HTTPError as error:
                    self.assertEqual(error.code, status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        self.assertEqual({path: path.read_bytes() for path in self.project.iterdir()}, snapshot)


if __name__ == '__main__':
    unittest.main()
