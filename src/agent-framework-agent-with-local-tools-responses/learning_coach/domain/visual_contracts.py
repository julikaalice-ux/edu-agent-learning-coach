"""Declarative visual data contract; no rendering or persistence dependencies."""

from learning_coach.domain.scene_contracts import SCENE_PROPERTIES

def _text(limit: int) -> dict:
    return {"type": "string", "minLength": 1, "maxLength": limit, "pattern": "\\S"}


def _object(properties: dict) -> dict:
    return {
        "type": "object", "properties": properties,
        "required": list(properties), "additionalProperties": False,
    }


def _variant(kind: str, properties: dict) -> dict:
    return _object({"kind": {"const": kind}, "title": _text(120), **properties})


VISUAL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TeachingVisual",
    "oneOf": [
        _variant("comparison", {
            "left_label": _text(80), "right_label": _text(80),
            "rows": {"type": "array", "minItems": 1, "maxItems": 8,
                     "items": _object({"aspect": _text(80), "left": _text(600), "right": _text(600)})},
        }),
        _variant("process", {
            "steps": {"type": "array", "minItems": 2, "maxItems": 8,
                      "items": _object({"title": _text(80), "description": _text(600)})},
        }),
        _variant("flashcards", {
            "cards": {"type": "array", "minItems": 1, "maxItems": 12,
                      "items": _object({"front": _text(300), "back": _text(800)})},
        }),
        _variant("linear_function", {
            "slope": {"type": "number", "minimum": -10, "maximum": 10},
            "intercept": {"type": "number", "minimum": -20, "maximum": 20},
            "x_min": {"type": "number", "minimum": -20, "maximum": 20},
            "x_max": {"type": "number", "minimum": -20, "maximum": 20},
        }),
        _variant("scene", SCENE_PROPERTIES),
        _variant("illustration", {
            "prompt": _text(800),
            "alt": _text(200),
        }),
        _variant("diagram", {
            "nodes": {"type": "array", "minItems": 1, "maxItems": 12,
                      "items": _object({"id": _text(40), "label": _text(80),
                                         "x": {"type": "number", "minimum": 0, "maximum": 100},
                                         "y": {"type": "number", "minimum": 0, "maximum": 100}})},
            "edges": {"type": "array", "minItems": 0, "maxItems": 24,
                      "items": {"type": "object",
                                "properties": {"from": _text(40), "to": _text(40),
                                               "label": {"type": "string", "maxLength": 60}},
                                "required": ["from", "to"], "additionalProperties": False}},
        }),
    ],
}

