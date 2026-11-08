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

    def test_answer_adds_retrieved_sources_when_model_omits_citations(self):
        model = LocalModel('/example/server', '/example/model')
        hit = [{'path': 'a.txt', 'text': 'Проверенный текст'}]
        with patch.object(model, '_request', return_value={'choices': [{'message': {'content': 'Текстовый ответ'}}]}):
            answer = model.answer('вопрос', hit)
        self.assertNotIn('Текстовый ответ', answer)
        self.assertIn('По найденным материалам', answer)
        self.assertIn('Проверенный текст [1]', answer)

    def test_answer_rejects_unknown_sources(self):
        model = LocalModel('/example/server', '/example/model')
        hit = [{'path': 'a.txt', 'text': 'Проверенный текст'}]
        with patch.object(model, '_request', return_value={'choices': [{'message': {'content': 'текст [2]'}}]}):
            self.assertIn('Не удалось проверить', model.answer('вопрос', hit))
        with patch.object(model, '_request', return_value={'choices': [{'message': {'content': 'Ответ [1]'}}]}):
            self.assertIn('Проверенный текст [1]', model.answer('вопрос', hit))

    def test_hybrid_search(self):
        chunks = [{'id':'1', 'embedding':[1,0]}, {'id':'2', 'embedding':[0,1]}]
        self.assertEqual(hybrid_search(chunks, [1,0], [chunks[1]])[0]['id'], '2')
        self.assertEqual({x['id'] for x in hybrid_search(chunks, [1,0], [chunks[1]])}, {'1','2'})

    def test_hybrid_search_discards_unrelated_vectors(self):
        chunks = [{'id':'near', 'embedding':[0.8, 0.6]}, {'id':'noise', 'embedding':[0.0, 1.0]}]
        hits = hybrid_search(chunks, [1.0, 0.0], [])
        self.assertEqual([x['id'] for x in hits], ['near'])

    def test_hybrid_search_returns_no_semantic_false_positive(self):
        chunks = [{'id':'budget', 'embedding':[0.72, 0.694]}, {'id':'travel', 'embedding':[0.70, 0.714]}]
        self.assertEqual(hybrid_search(chunks, [1.0, 0.0], []), [])

    def test_answer_prompt_includes_file_identity_and_detail_instructions(self):
        model = LocalModel('/example/server', '/example/model')
        hit = [{'path': 'reports/quarterly.txt', 'locator': 'строки 1–5', 'text': 'Доход вырос.'}]
        captured = {}
        def response(path, data):
            captured.update(data)
            return {'choices': [{'message': {'content': 'Доход вырос [1].'}}]}
        with patch.object(model, '_request', side_effect=response):
            model.answer('Что с доходом?', hit)
        prompt = captured['messages'][-1]['content']
        self.assertIn('reports/quarterly.txt', prompt)
        self.assertIn('развёрнуто', prompt)


if __name__ == '__main__': unittest.main()
