"""Pure mastery transition policy. Scores are heuristics, not measurements."""


def update_mastery(current, *, kind, correct, difficulty, category, previous=None):
    if current is None:
        if kind == "restatement":
            current = 0.3 if correct else None
        else:
            current = 0.0
    if current is not None and kind == "quiz":
        if correct:
            current += {"简单": 0.1, "中等": 0.15, "困难": 0.2}[difficulty]
        else:
            current -= 0.15
            if (previous is not None and not previous["correct"]
                    and category != "unclassified" and previous["error_category"] == category):
                current -= 0.1
        current = round(max(0.0, min(1.0, current)), 4)
    return current
