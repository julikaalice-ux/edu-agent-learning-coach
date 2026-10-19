"""What a turn's handlers work with, shared by reference.

The pipeline exposes these as its own attributes; replacing one there
(pipeline.image_finder = ...) reaches every handler.
"""

from dataclasses import dataclass

from learning_coach.domain.context import Budgets


@dataclass
class Deps:
    runner: object
    research: object
    store: object
    artifacts: object
    image_generator: object
    image_finder: object
    budgets: Budgets
    response_schema: dict
