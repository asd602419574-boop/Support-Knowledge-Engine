"""MVP-2 source/type guards; these checks are not a semantic proof of translation."""
from __future__ import annotations

import json
import unittest

from support_knowledge_engine.assistant import _validate_answer
from support_knowledge_engine.assistant_llm import ModelError


FACT = 'Error code AM2-17 indicates that the gimbal home sensor needs inspection.'
CAUTION = 'Do not apply procedures written for AeroCam Pro 2.'
ACTION = 'Run propeller calibration after replacing a motor or vibration damper.'


def explanation(quote=FACT, text='AM2-17 表示需要检查云台归位传感器，不能据此认定传感器已经损坏。', kind='fact', **overrides):
    answer = {'items': [{'type': kind, 'text': text, 'quote': quote, 'evidence_id': 'ev1-test'}],
              'coverage': 'partial', 'missing_information': ['inspection_procedure', 'symptoms']}
    answer.update(overrides)
    return json.dumps(answer, ensure_ascii=False)


class ExplanationTests(unittest.TestCase):
    sources = [{'evidence_id': 'ev1-test', 'excerpts': [FACT, CAUTION, ACTION,
                'Firmware diagnostics', 'Supported firmware range: 2.4.0 through 2.9.x.']}]

    def test_fact_only_is_partial_and_cannot_claim_a_complete_procedure(self):
        result = _validate_answer(explanation(coverage='sufficient', missing_information=[]), self.sources)
        self.assertEqual(result['coverage'], 'partial')
        self.assertEqual(result['items'][0]['type'], 'fact')
        self.assertIn('inspection_procedure', result['missing_information'])
        self.assertEqual(result['items'][0]['quote'], FACT)

    def test_damage_denial_is_not_mistaken_for_a_confirmed_damage_claim(self):
        for text in ('AM2-17 表示云台归位传感器需要检查，但不代表该传感器已经损坏。',
                     'AM2-17 提示检查云台归位传感器，并不意味着它已经损坏。'):
            with self.subTest(text=text):
                result = _validate_answer(explanation(text=text), self.sources)
                self.assertEqual(result['items'][0]['text'], text)
        with self.assertRaises(ModelError):
            _validate_answer(explanation(text='AM2-17 表示云台归位传感器已经损坏。'), self.sources)
        with self.assertRaises(ModelError):
            _validate_answer(explanation(text='AM2-17 不代表它已经损坏，但该传感器已经损坏。'), self.sources)

    def test_metadata_and_diagnostic_meaning_cannot_masquerade_as_actions(self):
        for quote in (FACT, 'Firmware diagnostics', 'Supported firmware range: 2.4.0 through 2.9.x.'):
            with self.subTest(quote=quote), self.assertRaises(ModelError):
                _validate_answer(explanation(quote, '执行检查并重置设备。', 'action'), self.sources)

    def test_negation_model_and_action_conditions_must_be_preserved(self):
        good = explanation(CAUTION, '不得套用为 AeroCam Pro 2 编写的流程。', 'caution')
        self.assertEqual(_validate_answer(good, self.sources)['items'][0]['type'], 'caution')
        for text in ('可以套用 AeroCam Pro 2 的流程。', '不得套用其他设备的流程。'):
            with self.subTest(text=text), self.assertRaises(ModelError):
                _validate_answer(explanation(CAUTION, text, 'caution'), self.sources)
        good = explanation(ACTION, '更换电机或减振器后，按资料进行螺旋桨校准。', 'action')
        self.assertEqual(_validate_answer(good, self.sources)['items'][0]['type'], 'action')
        with self.assertRaises(ModelError):
            _validate_answer(explanation(ACTION, '立即进行螺旋桨校准。', 'action'), self.sources)

    def test_schema_reference_and_length_rejections(self):
        for answer in (
            explanation(coverage='definitely_fixed'),
            explanation(missing_information=['sensor_damage']),
            explanation(items=[{'type':'fact','text':'中文解释','quote':FACT,'evidence_id':'fake'}]),
            explanation(items=[{'type':'fact','text':'中文解释','quote':FACT[:-1],'evidence_id':'ev1-test'}]),
            explanation(items=[{'type':'fact','text':'Inspect sensor.','quote':FACT,'evidence_id':'ev1-test'}]),
            explanation(items=[{'type':'fact','text':'中'*501,'quote':FACT,'evidence_id':'ev1-test'}]),
            explanation(source_url='https://invented.example'),
            explanation(items=[{'type':'fact','text':'AM2-17 的传感器已经损坏。','quote':FACT,'evidence_id':'ev1-test'}]),
            explanation(items=[{'type':'fact','text':'AM2-17 表示应拆机检查传感器。','quote':FACT,'evidence_id':'ev1-test'}]),
            explanation(items=[{'type':'fact','text':'AM2-17 可以重置设备后解决。','quote':FACT,'evidence_id':'ev1-test'}]),
        ):
            with self.subTest(answer=answer), self.assertRaises(ModelError):
                _validate_answer(answer, self.sources)

    def test_diagnostics_distinguish_evidence_quote_schema_and_lexical_failures(self):
        cases = [
            (explanation(items=[{'type':'fact','text':'AM2-17 需要检查传感器。','quote':FACT,'evidence_id':'fake'}]), 'evidence_invalid'),
            (explanation(quote=FACT[:-1]), 'quote_mismatch'),
            (explanation(source_url='https://invented.example'), 'answer_schema'),
            ('not JSON', 'answer_json'),
            (explanation(text='AM2-17 表示传感器已经损坏。'), 'damage_claim'),
        ]
        for answer, reason in cases:
            with self.subTest(reason=reason), self.assertRaises(ModelError) as ctx:
                _validate_answer(answer, self.sources)
            self.assertEqual(ctx.exception.validation_reason, reason)


if __name__ == '__main__':
    unittest.main()
