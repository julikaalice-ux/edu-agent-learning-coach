"""Concurrent errors remain diagnosable without exposing provider payloads."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
from learning_coach.domain.contracts import ContractError
from learning_coach.service.execution import StageFailure, failure_codes

class FailureDiagnosticsTests(unittest.TestCase):
    def test_nested_group_keeps_fixed_codes(self):
        error = ExceptionGroup('private group', [
            StageFailure('quiz_question_not_accepted'),
            ExceptionGroup('private child', [ContractError('tool_reason_mismatch')])])
        self.assertEqual(failure_codes(error),
                         'StageFailure:quiz_question_not_accepted,ContractError:tool_reason_mismatch')

    def test_provider_and_invalid_application_messages_are_not_logged(self):
        for error in (ValueError('secret payload'), StageFailure('secret payload https://private')):
            self.assertEqual(failure_codes(error), f'{type(error).__name__}:unclassified')
