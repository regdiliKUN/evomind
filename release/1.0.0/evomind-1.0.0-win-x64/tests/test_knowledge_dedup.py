from unittest.mock import patch
from test_regressions import IsolatedTest
from backend import memory, cleanup, refinement
import math
from fastapi.testclient import TestClient


class KnowledgeDedupTests(IsolatedTest):
    def test_same_text_different_vectors_merges_richer_detail(self):
        pid = memory.add_point(topic='CAD', claim='模型打印之前需要检查壁厚。', detail='避免破损。', vec=[1., 0.])
        result = memory.add_points_batch([dict(topic='CAD', claim='模型打印之前需要检查壁厚。',
            detail='避免破损。尤其需要检查袖口等薄壁区域。', vec=[0., 1.])])
        self.assertEqual(memory.count_points(), 1)
        self.assertEqual(result['dup'], 1)
        self.assertIn('袖口', memory.get_point(pid)['detail'])

    def test_cleanup_preview_covers_old_points_and_is_reversible(self):
        for i in range(405):
            memory.add_point(topic='CAD', claim=f'fixture point number {i}', detail='detail', vec=[1., 0.], dedup=False)
        preview = cleanup.preview(.9)
        self.assertEqual(preview['removed'], 404)
        self.assertEqual(len(preview['items']), 404)
        self.assertEqual(memory.count_points(), 405)
        self.assertEqual(cleanup.clean(threshold=.9, token=preview['token'])['removed'], 404)
        self.assertEqual(cleanup.undo()['restored'], 404)

    def test_distillation_merges_duplicate_outputs_and_citations(self):
        evidence = {'a': {'claim':'原始有效知识内容A', 'detail':'原文条件'},
                    'b': {'claim':'原始有效知识内容B', 'detail':'原文条件'}}
        response = {'items': [dict(claim='打印之前需要检查模型壁厚', detail='条件', source_ids=[i]) for i in evidence]}
        refinement._stop.clear()
        with patch('backend.llm.chat_json', side_effect=[response, {'verified':[0,1]}]):
            result = refinement._distill(list(evidence.values()), evidence)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]['source_ids']), {'a','b'})

    def test_batch_completeness_is_independent_of_input_order_and_sources_survive(self):
        points = [dict(topic='CAD', claim='模型打印之前需要检查壁厚。', detail=d,
                       source_title=str(i), vec=[1.,0.]) for i,d in enumerate(['避免破损。', '避免破损。检查袖口。'])]
        result = memory.add_points_batch(points)
        self.assertEqual((result['added'], result['dup']), (1,1))
        pid = result['ids'][0]
        self.assertIn('袖口', memory.get_point(pid)['detail'])
        self.assertEqual({p['source_title'] for p in memory.get_point(pid)['merged_sources']}, {'0','1'})
        memory.add_points_batch(points[::-1])
        self.assertEqual(memory.count_points(), 1)
        self.assertEqual(len(memory.get_point(pid)['merged_sources']), 2)

    def test_preview_prefers_complete_then_earlier_and_rejects_stale_snapshot(self):
        old = memory.add_point(topic='CAD', claim='原始知识内容足够完整', detail='简短', vec=[1.,0.], dedup=False)
        rich = memory.add_point(topic='CAD', claim='另一个版本知识内容', detail='更完整的详细说明和边界条件', vec=[1.,0.], dedup=False)
        p = cleanup.preview(.9)
        self.assertEqual((p['items'][0]['keep']['id'], p['items'][0]['drop']['id']), (rich,old))
        memory.conn().execute('UPDATE knowledge SET detail=? WHERE id=?', ('new detail', old))
        memory.conn().commit()
        result = cleanup.clean(threshold=.9, token=p['token'])
        self.assertTrue(result['stale'])
        self.assertEqual(memory.count_points(),2)
        self.assertFalse(cleanup.clean(threshold=.9)['ok'])

    def test_nontransitive_similarity_and_threshold_boundary(self):
        # A~B and B~C at .9, but A is not similar enough to C.
        for i, angle in enumerate([0,20,40]):
            v = [math.cos(math.radians(angle)),math.sin(math.radians(angle))]
            memory.add_point(topic='CAD', claim=f'Original long knowledge claim {i}', detail='same', vec=v, dedup=False)
        p = cleanup.preview(.9)
        self.assertEqual(p['removed'],1)
        self.assertTrue(all(i['sim'] >= .9 for i in p['items']))
        self.assertEqual(cleanup.preview(1.)['removed'],0)

    def test_api_preview_pagination_validation_and_topic_scope(self):
        from backend.main import app
        for i in range(4):
            memory.add_point(topic='CAD', claim=f'Original knowledge point {i}', vec=[1.,0.], dedup=False)
        memory.add_point(topic='Other', claim='Another topic knowledge point', vec=[1.,0.], dedup=False)
        with TestClient(app) as client:
            p = client.get('/api/knowledge/cleanup/preview?threshold=.9&topic=CAD').json()
            self.assertEqual((p['removed'],len(p['items'])),(3,3))
            response = client.get('/api/knowledge/duplicates?limit=1&offset=1&topic=CAD').json()
            self.assertEqual((response['total'],len(response['pairs'])),(3,1))
            for value in ['.49','1.01','nan','inf']:
                self.assertEqual(client.get('/api/knowledge/cleanup/preview?threshold='+value).status_code,422)
            self.assertTrue(client.post('/api/knowledge/cleanup',json={'threshold':.9,'topic':'CAD','token':p['token']}).json()['ok'])
            self.assertEqual(memory.count_points(),2)
            self.assertEqual(client.post('/api/knowledge/cleanup/undo').json()['restored'],3)

    def test_refinement_merges_existing_points_and_history_but_keeps_source_links(self):
        rows = []
        for detail in ['原始的详细补充条件与说明。'*5, '原始的详细补充条件与说明。'*6]:
            pid = memory.add_point(topic='CAD',claim='模型打印前需要检查壁厚条件',detail=detail,vec=[1.,0.],dedup=False)
            rows.append(dict(memory.conn().execute('SELECT * FROM knowledge WHERE id=?',(pid,)).fetchone()))
        items = [dict(claim='打印前检查模型壁厚',detail='必要条件',source_ids=[p['id'] for p in rows])]
        refinement._stop.clear()
        refinement._save('CAD',rows,items)
        self.assertEqual(memory.count_points(),1)
        self.assertTrue(memory.get_point(rows[0]['id'])['archived'])
        refinement._save('CAD',rows,items)
        history = refinement.history('CAD')
        self.assertEqual(sum(len(h['items']) for h in history),1)
        self.assertEqual(set(history[1]['items'][0]['source_ids']), {p['id'] for p in rows})
        self.assertEqual(cleanup.undo()['restored'],1)

    def test_numeric_negation_and_code_differences_are_not_auto_merged(self):
        for i, detail in enumerate(['需要8 GB内存', '需要16 GB内存', '不需要8 GB内存',
                                    '```\nif a:\n    b()\n```', '```\nif a:\nb()\n```']):
            memory.add_point(topic='CAD',claim='运行此程序需要满足以下条件',detail=detail,vec=[1.,0.])
        self.assertEqual(memory.count_points(),5)

    def test_equal_completeness_keeps_older_and_archive_failure_is_atomic(self):
        first = memory.add_point(topic='CAD', claim='Original point version A', detail='same', vec=[1.,0.],dedup=False)
        second = memory.add_point(topic='CAD', claim='Original point version B', detail='same', vec=[1.,0.],dedup=False)
        p = cleanup.preview(.9)
        self.assertEqual(p['items'][0]['keep']['id'],first)
        memory.conn().execute("CREATE TRIGGER reject_semantic_delete BEFORE DELETE ON knowledge BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(Exception):
            cleanup.clean(threshold=.9,token=p['token'])
        self.assertEqual(memory.count_points(),2)
        self.assertFalse(cleanup.state()['available'])
