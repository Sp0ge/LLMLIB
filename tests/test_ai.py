import unittest
from unittest.mock import patch
from ai import LocalModel, hybrid_search


class AITest(unittest.TestCase):
    def test_embedding_normalization_and_bad_values(self):
        model = LocalModel('/example/server', '/example/model', embeddings=True)
        with patch.object(model, '_request', return_value={'data': [{'embedding': [3, 4]}]}):
            self.assertEqual(model.embed('query'), [0.6, 0.8])
        with patch.object(model, '_request', return_value={'data': [{'embedding': [float('nan')]}]}):
            with self.assertRaises(ValueError): model.embed('query')

    def test_answer_rejects_missing_and_unknown_sources(self):
        model = LocalModel('/example/server', '/example/model')
        hit = [{'path': 'a.txt', 'text': 'Проверенный текст'}]
        for answer in ['текст без ссылки', 'текст [2]']:
            with patch.object(model, '_request', return_value={'choices': [{'message': {'content': answer}}]}):
                self.assertIn('Не удалось проверить', model.answer('вопрос', hit))
        with patch.object(model, '_request', return_value={'choices': [{'message': {'content': 'Ответ [1]'}}]}):
            self.assertEqual(model.answer('вопрос', hit), 'Ответ [1]')

    def test_hybrid_search(self):
        chunks = [{'id':'1', 'embedding':[1,0]}, {'id':'2', 'embedding':[0,1]}]
        self.assertEqual(hybrid_search(chunks, [1,0], [chunks[1]])[0]['id'], '2')
        self.assertEqual({x['id'] for x in hybrid_search(chunks, [1,0], [chunks[1]])}, {'1','2'})


if __name__ == '__main__': unittest.main()
