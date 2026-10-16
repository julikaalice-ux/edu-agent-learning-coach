"""Offline contract/tool-body checks; these do not call Foundry or grade model behavior."""

import ast
import copy
import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'agent'


class ResponseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        schema = json.loads((SOURCE / 'response.schema.json').read_text())
        Draft202012Validator.check_schema(schema)
        cls.validator = Draft202012Validator(schema)

    def setUp(self):
        self.response = {
            'schema_version': '1.0',
            'intent': {'primary': 'A', 'is_implicit': False},
            'message': '可以先从一个具体例子理解。',
            'next_step': {'kind': 'question', 'text': '你会怎样分这两组物品？'},
        }

    def test_all_primary_intents_and_next_step_shapes(self):
        for primary in 'ABCDEFG':
            for step in (None, {'kind': 'question', 'text': '选哪一个？'}, {'kind': 'task', 'text': '匹配一组。'}):
                with self.subTest(primary=primary, step=step):
                    self.response['intent']['primary'] = primary
                    self.response['next_step'] = step
                    self.validator.validate(self.response)

    def test_rejects_missing_fields(self):
        for field in self.response:
            with self.subTest(field=field):
                response = copy.deepcopy(self.response)
                del response[field]
                self.assertFalse(self.validator.is_valid(response))

    def test_rejects_undefined_fields_at_each_level(self):
        for section in (None, 'intent', 'next_step'):
            with self.subTest(section=section):
                response = copy.deepcopy(self.response)
                target = response if section is None else response[section]
                target['extra'] = True
                self.assertFalse(self.validator.is_valid(response))

    def test_rejects_compound_labels_and_wrong_types(self):
        for field, value in (('primary', 'D+E'), ('primary', 'H'), ('is_implicit', 'false'), ('is_implicit', 1)):
            with self.subTest(field=field, value=value):
                self.response['intent'] = {'primary': 'A', 'is_implicit': False}
                self.response['intent'][field] = value
                self.assertFalse(self.validator.is_valid(self.response))

    def test_rejects_blank_visible_content(self):
        for value in ('', '  \n\t'):
            for field in ('message', 'next_step'):
                with self.subTest(value=value, field=field):
                    response = copy.deepcopy(self.response)
                    if field == 'message':
                        response['message'] = value
                    else:
                        response['next_step']['text'] = value
                    self.assertFalse(self.validator.is_valid(response))

    def test_rejects_wrong_version_or_step_kind(self):
        response = copy.deepcopy(self.response)
        response['schema_version'] = '1.5'
        self.assertFalse(self.validator.is_valid(response))
        self.response['next_step']['kind'] = 'quiz'
        self.assertFalse(self.validator.is_valid(self.response))


class RuntimePromptTests(unittest.TestCase):
    def test_loads_prompt_relative_to_source(self):
        factory = SOURCE / 'learning_coach/server.py'
        tree = ast.parse(factory.read_text())
        namespace = {'Path': Path, '__file__': str(factory)}
        assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id in ('SRC_DIR', 'INSTRUCTIONS_PATH')
                               for target in node.targets)]
        exec(compile(ast.fix_missing_locations(ast.Module(body=assignments, type_ignores=[])),
                     '<prompt-path>', 'exec'), namespace)
        self.assertEqual(namespace['INSTRUCTIONS_PATH'], SOURCE / 'prompts/instructions.md')
        actual = namespace['INSTRUCTIONS_PATH'].read_text(encoding='utf-8')
        self.assertIn('# 根据画像选择教学方式', actual)
        self.assertIn('## 意图单一来源与复合需求', actual)
        self.assertNotIn('log_intent', actual)


if __name__ == '__main__':
    unittest.main()
