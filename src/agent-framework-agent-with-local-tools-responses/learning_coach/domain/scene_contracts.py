"""Topic-independent, bounded drawing primitives for interactive storyboards."""

import math
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
    element("polyline", {"points": {"type": "array", "minItems": 2, "maxItems": 100,
        "items": {"type": "array", "items": COORD, "minItems": 2, "maxItems": 2}},
        "color": COLOR, "opacity": OPACITY}),
]}

SCENE_PROPERTIES = {
    "learning_goal": text(300), "interaction_hint": text(200),
    "check_question": text(200), "limitations": text(500),
    "frames": {"type": "array", "minItems": 2, "maxItems": 6,
        "items": obj({"title": text(80), "explanation": text(600),
            "elements": {"type": "array", "minItems": 1, "maxItems": 32, "items": ELEMENT_SCHEMA}})},
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


def scene_layout(spec):
    """Reject invalid geometry/readability before publishing; not a fact check.

    Each scene uses an 860 by 440 SVG viewBox. The returned layout is computed
    by the application, so the model cannot insert styles or font settings.
    """
    layouts = []
    for frame in spec["frames"]:
        seen, frame_layout, text_boxes = set(), {}, []
        for item in frame["elements"]:
            if item["id"] in seen:
                raise ValueError("scene_duplicate_id")
            seen.add(item["id"])
            numbers = [v for v in item.values() if isinstance(v, (float, int))]
            numbers.extend(v for point in item.get("points", []) for v in point)
            if not all(math.isfinite(value) for value in numbers):
                raise ValueError("scene_nonfinite_number")
            if item["shape"] in ("line", "arrow") and item["x1"] == item["x2"] and item["y1"] == item["y2"]:
                raise ValueError("scene_zero_length_line")
            if item["shape"] == "polyline" and len({tuple(point) for point in item["points"]}) < 2:
                raise ValueError("scene_zero_length_line")
            if "width" in item:
                if item["x"] + item["width"] > 100.00001 or item["y"] + item["height"] > 100.00001:
                    raise ValueError("scene_out_of_bounds")
            label = item.get("label", item.get("text", ""))
            if not label:
                continue
            if "width" in item:
                width, height = item["width"] * 8.6, item["height"] * 4.4
                center_x = (item["x"] + item["width"] / 2) * 8.6
                center_y = (item["y"] + item["height"] / 2) * 4.4
            else:
                width, height = 160, 60
                center_x = (item["x1"] + item["x2"]) * 4.3
                center_y = (item["y1"] + item["y2"]) * 2.2 - 18
            fitted = None
            for size in (16, 14, 12):
                lines = wrap_label(label, max(1, int((width - 16) / (size * .62))))
                if max(len(line) for line in lines) and len(lines) * size * 1.35 <= height - 8:
                    # A minimum actual glyph width prevents tiny boxes passing
                    # after the wrapper is clamped to one character per line.
                    if width < size + 16:
                        continue
                    fitted = {"lines": lines, "font_size": size, "cx": center_x, "cy": center_y}
                    break
            if fitted is None:
                raise ValueError("scene_label_does_not_fit")
            line_width = max(sum(2 if unicodedata.east_asian_width(ch) in 'WF' else 1 for ch in line)
                             for line in fitted["lines"]) * fitted["font_size"] * .62
            label_height = len(fitted["lines"]) * fitted["font_size"] * 1.35
            bounds = (center_x-line_width/2, center_y-label_height/2,
                      center_x+line_width/2, center_y+label_height/2)
            if bounds[0] < 0 or bounds[1] < 0 or bounds[2] > 860 or bounds[3] > 440:
                raise ValueError("scene_label_out_of_bounds")
            for other in text_boxes:
                if min(bounds[2], other[2]) > max(bounds[0], other[0]) + 2 and min(bounds[3], other[3]) > max(bounds[1], other[1]) + 2:
                    raise ValueError("scene_labels_overlap")
            text_boxes.append(bounds)
            frame_layout[item["id"]] = fitted
        layouts.append(frame_layout)
    return layouts
