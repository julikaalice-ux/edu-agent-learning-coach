"""Topic-independent, bounded drawing primitives for interactive storyboards."""

import math
import re
import unicodedata


def obj(properties):
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


def text(limit, empty=False):
    return {"type": "string", "maxLength": limit, **({} if empty else {"minLength": 1, "pattern": r"\S"})}


COORD = {"type": "number", "minimum": 0, "maximum": 100}
SIZE = {"type": "number", "exclusiveMinimum": 0, "maximum": 100}
COLOR = {"enum": ["neutral", "water", "light", "accent", "particle", "warning", "ink"]}
OPACITY = {"type": "number", "minimum": 0, "maximum": 1}
BOX = {"x": COORD, "y": COORD, "width": SIZE, "height": SIZE}


def element(shape, properties):
    return obj({"id": text(40), "shape": {"const": shape}, **properties})


ELEMENT_SCHEMA = {"oneOf": [
    *[element(shape, {**BOX, "color": COLOR, "opacity": OPACITY, "label": text(60, True)})
      for shape in ("rect", "ellipse")],
    *[element(shape, {"x1": COORD, "y1": COORD, "x2": COORD, "y2": COORD,
                      "color": COLOR, "opacity": OPACITY, "label": text(40, True)})
      for shape in ("line", "arrow")],
    element("text", {**BOX, "color": COLOR, "text": text(200)}),
    element("polyline", {"points": {"type": "array", "minItems": 2, "maxItems": 40,
        "items": {"type": "array", "items": COORD, "minItems": 2, "maxItems": 2}},
        "color": COLOR, "opacity": OPACITY}),
]}

SCENE_PROPERTIES = {
    "learning_goal": text(300), "interaction_hint": text(200),
    "check_question": text(200), "limitations": text(500),
    # Kept small on purpose: the designer writes every element by hand, and
    # the time to draft a scene grows with its length.
    "frames": {"type": "array", "minItems": 2, "maxItems": 4,
        "items": obj({"title": text(80), "explanation": text(600),
            "elements": {"type": "array", "minItems": 1, "maxItems": 20, "items": ELEMENT_SCHEMA}})},
}


def wrap_label(value, max_units):
    """Conservative CJK-aware wrapping; all text still uses SVG text nodes."""
    lines, current, units = [], "", 0
    for char in value:
        if char == "\n":
            lines.append(current); current, units = "", 0
            continue
        weight = 2 if unicodedata.east_asian_width(char) in "WF" else 1
        if current and units + weight > max_units:
            lines.append(current); current, units = "", 0
        current += char; units += weight
    lines.append(current)
    return lines


# Scenes are drawn on an 860 x 440 SVG viewBox; specs place elements in 0-100
# percent coordinates, so one percent is X_SCALE by Y_SCALE pixels.
CANVAS_WIDTH, CANVAS_HEIGHT = 860, 440
X_SCALE, Y_SCALE = CANVAS_WIDTH / 100, CANVAS_HEIGHT / 100
EDGE_TOLERANCE = 1e-5            # float slack on the 100 % edge
LABEL_FONT_SIZES = (16, 14, 12)  # px, tried largest first
GLYPH_WIDTH = .62                # a half-width glyph, in font sizes; CJK counts double
LINE_HEIGHT = 1.35               # in font sizes
LABEL_PADDING_X = 16             # px of a box kept free beside its label
LABEL_PADDING_Y = 8              # px of a box kept free above and below its label
LINE_LABEL_BOX = (160, 60)       # px room for the label of a line or arrow
LINE_LABEL_LIFT = 18             # px a line label sits above the line's midpoint
OUTSIDE_LABEL_GAP = 4            # px between a small shape and its name placed outside it
OVERLAP_TOLERANCE = 2            # px two labels may share before they overlap


# A text made only of field marks (into / out of the page) is a background
# pattern: it may fill the field region, so other labels may sit on it.
FIELD_MARKS = re.compile(r"[×✕⊗·•⊙\s]*[×✕⊗·•⊙][×✕⊗·•⊙\s]*")


class SceneError(ValueError):
    """A render-check failure. str() is the code; where names the frame and the
    element ids (the designer's own ids), so a redraw can fix the exact spot."""

    def __init__(self, code, where=""):
        super().__init__(code)
        self.where = where


def _check_geometry(item):
    numbers = [v for v in item.values() if isinstance(v, (float, int))]
    numbers.extend(v for point in item.get("points", []) for v in point)
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("scene_nonfinite_number")
    if item["shape"] in ("line", "arrow") and item["x1"] == item["x2"] and item["y1"] == item["y2"]:
        raise ValueError("scene_zero_length_line")
    if item["shape"] == "polyline" and len({tuple(point) for point in item["points"]}) < 2:
        raise ValueError("scene_zero_length_line")
    if "width" in item and (item["x"] + item["width"] > 100 + EDGE_TOLERANCE
                            or item["y"] + item["height"] > 100 + EDGE_TOLERANCE):
        raise ValueError("scene_out_of_bounds")


def _label_box(item):
    """Pixel room and centre for an element's label."""
    if "width" in item:
        return (item["width"] * X_SCALE, item["height"] * Y_SCALE,
                (item["x"] + item["width"] / 2) * X_SCALE, (item["y"] + item["height"] / 2) * Y_SCALE)
    width, height = LINE_LABEL_BOX
    return (width, height, (item["x1"] + item["x2"]) / 2 * X_SCALE,
            (item["y1"] + item["y2"]) / 2 * Y_SCALE - LINE_LABEL_LIFT)


def _fit_label(label, width, height, center_x, center_y):
    for size in LABEL_FONT_SIZES:
        lines = wrap_label(label, max(1, int((width - LABEL_PADDING_X) / (size * GLYPH_WIDTH))))
        fits = max(len(line) for line in lines) and len(lines) * size * LINE_HEIGHT <= height - LABEL_PADDING_Y
        # A minimum actual glyph width prevents tiny boxes passing
        # after the wrapper is clamped to one character per line.
        if fits and width >= size + LABEL_PADDING_X:
            return {"lines": lines, "font_size": size, "cx": center_x, "cy": center_y}
    raise ValueError("scene_label_does_not_fit")


def _label_bounds(fitted):
    size = fitted["font_size"]
    line_width = max(sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in line)
                     for line in fitted["lines"]) * size * GLYPH_WIDTH
    label_height = len(fitted["lines"]) * size * LINE_HEIGHT
    bounds = (fitted["cx"] - line_width / 2, fitted["cy"] - label_height / 2,
              fitted["cx"] + line_width / 2, fitted["cy"] + label_height / 2)
    if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > CANVAS_WIDTH or bounds[3] > CANVAS_HEIGHT:
        raise ValueError("scene_label_out_of_bounds")
    return bounds


def _overlaps(a, b):
    return (min(a[2], b[2]) > max(a[0], b[0]) + OVERLAP_TOLERANCE
            and min(a[3], b[3]) > max(a[1], b[1]) + OVERLAP_TOLERANCE)


def _place_label(item, label):
    """Inside its shape when it fits; a shape too small for its name (a particle,
    a small ball) gets the name just below it, or above, instead of a redraw."""
    try:
        fitted = _fit_label(label, *_label_box(item))
        return fitted, _label_bounds(fitted)
    except ValueError as inside:
        if item["shape"] not in ("rect", "ellipse") or str(inside) != "scene_label_does_not_fit":
            raise
    width, height = LINE_LABEL_BOX
    center_x = (item["x"] + item["width"] / 2) * X_SCALE
    for side in (1, -1):
        edge = (item["y"] + item["height"]) * Y_SCALE if side == 1 else item["y"] * Y_SCALE
        try:
            fitted = _fit_label(label, width, height, center_x, 0)
            half = len(fitted["lines"]) * fitted["font_size"] * LINE_HEIGHT / 2
            fitted["cy"] = edge + side * (OUTSIDE_LABEL_GAP + half)
            fitted["outside"] = True   # drawn in the line-label colour, not on the shape
            return fitted, _label_bounds(fitted)
        except ValueError:
            continue
    raise ValueError("scene_label_does_not_fit")


def _frame_layout(frame, number):
    seen, layout, text_boxes = set(), {}, []
    for item in frame["elements"]:
        where = f"第 {number} 帧的 {item['id']}"
        if item["id"] in seen:
            raise SceneError("scene_duplicate_id", where)
        seen.add(item["id"])
        label = item.get("label", item.get("text", ""))
        try:
            _check_geometry(item)
            if not label:
                continue
            fitted, bounds = _place_label(item, label)
        except ValueError as error:
            raise SceneError(str(error), where) from None
        layout[item["id"]] = fitted
        if item["shape"] == "text" and FIELD_MARKS.fullmatch(label):
            fitted["background"] = True  # drawn under the shapes' labels
            continue
        clash = next((other for other, box in text_boxes if _overlaps(bounds, box)), None)
        if clash is not None:
            raise SceneError("scene_labels_overlap", f"第 {number} 帧的 {clash} 和 {item['id']}")
        text_boxes.append((item["id"], bounds))
    return layout


def scene_layout(spec):
    """Reject invalid geometry/readability before publishing; not a fact check.

    The returned layout is computed by the application, so the model cannot
    insert styles or font settings.
    """
    return [_frame_layout(frame, number) for number, frame in enumerate(spec["frames"], 1)]
