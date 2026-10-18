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


# Program-drawn kinds that can also accompany a gallery page as its
# "companion" (a picture page explains appearance; these explain how and why).
_DRAWN = [
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
]
_DIAGRAM = _variant("diagram", {
    "nodes": {"type": "array", "minItems": 1, "maxItems": 12,
              "items": _object({"id": _text(40), "label": _text(80),
                                 "x": {"type": "number", "minimum": 0, "maximum": 100},
                                 "y": {"type": "number", "minimum": 0, "maximum": 100}})},
    "edges": {"type": "array", "minItems": 0, "maxItems": 24,
              "items": {"type": "object",
                        "properties": {"from": _text(40), "to": _text(40),
                                       "label": {"type": "string", "maxLength": 60}},
                        "required": ["from", "to"], "additionalProperties": False}},
})
GALLERY_MAX_PANELS = 6
GALLERY_ARRANGEMENTS = ("auto", "stack", "side_by_side", "timeline", "tabs")
# A page of web pictures (found by the program, chosen by the reviewer) with
# the designer's labels, points and guided steps; the designer never sees the
# pictures, so nothing here may point at a position inside a picture.
_GALLERY = _variant("gallery", {
    "arrange": {"enum": list(GALLERY_ARRANGEMENTS)},
    "panels": {"type": "array", "minItems": 1, "maxItems": GALLERY_MAX_PANELS,
               "items": _object({
                   "label": _text(40),
                   "find": _text(200),
                   "query_zh": _text(60),
                   "query_en": _text(80),
                   "time": {"type": ["string", "null"], "maxLength": 30},
                   "points": {"type": "array", "minItems": 1, "maxItems": 4, "items": _text(160)},
               })},
    "steps": {"type": "array", "minItems": 0, "maxItems": 6,
              "items": _object({"panel": {"type": ["integer", "null"], "minimum": 0,
                                          "maximum": GALLERY_MAX_PANELS - 1},
                                "text": _text(200)})},
    "companion": {"anyOf": [{"type": "null"}, *[variant for variant in _DRAWN
                                                 if variant["properties"]["kind"]["const"] != "flashcards"],
                            _DIAGRAM]},
})

VISUAL_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "TeachingVisual",
    "oneOf": [
        *_DRAWN,
        _variant("illustration", {
            "prompt": _text(800),
            "alt": _text(200),
        }),
        _DIAGRAM,
        _GALLERY,
    ],
}
