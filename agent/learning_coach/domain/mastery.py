"""Pure mastery transition policy. Scores are heuristics, not measurements."""

RESTATEMENT_START = 0.3   # first correct restatement of a concept
QUIZ_GAIN = {"简单": 0.1, "中等": 0.15, "困难": 0.2}
QUIZ_LOSS = 0.15
REPEATED_ERROR_LOSS = 0.1  # extra loss when the same error cause repeats
# Display bands for a mastery level: below LEARNING is 还不熟, below SOLID is 正在巩固.
LEARNING, SOLID = 0.3, 0.7


def update_mastery(current, *, kind, correct, difficulty, category, previous=None):
    if current is None:
        if kind == "restatement":
            current = RESTATEMENT_START if correct else None
        else:
            current = 0.0
    if current is not None and kind == "quiz":
        if correct:
            current += QUIZ_GAIN[difficulty]
        else:
            current -= QUIZ_LOSS
            if (previous is not None and not previous["correct"]
                    and category != "unclassified" and previous["error_category"] == category):
                current -= REPEATED_ERROR_LOSS
        current = round(max(0.0, min(1.0, current)), 4)
    return current
