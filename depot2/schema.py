"""The structured answers' JSON schemas, and a small validator for the subset they use.

The CLI validates `--json-schema` too; the code validates again and never trusts the answer.
A pass's schema is identical for every character: the schema is part of the cached prompt
prefix (it becomes a tool definition), so nothing per-character may go into it.
"""

POSITIONS = ("agree", "disagree", "none")
VERDICTS = ("on_track", "drifting", "off_course")

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _is_type(value, typ):
    if typ == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if typ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[typ])


def validate(value, schema, path="$"):
    """Return a list of error strings (empty when valid)."""
    errs = []
    typ = schema.get("type")
    if typ is not None:
        types = typ if isinstance(typ, list) else [typ]
        if not any(_is_type(value, t) for t in types):
            return ["{}: expected {}, got {}".format(path, "/".join(types), type(value).__name__)]
    if "enum" in schema and value not in schema["enum"]:
        errs.append("{}: {!r} not in {}".format(path, value, schema["enum"]))
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append("%s: longer than %d" % (path, schema["maxLength"]))  # noqa: UP031
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append("%s: shorter than %d" % (path, schema["minLength"]))  # noqa: UP031
    if _is_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append("{}: below {}".format(path, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            errs.append("{}: above {}".format(path, schema["maximum"]))
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append("%s: more than %d items" % (path, schema["maxItems"]))  # noqa: UP031
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append("%s: fewer than %d items" % (path, schema["minItems"]))  # noqa: UP031
        if "items" in schema:
            for i, item in enumerate(value):
                errs.extend(validate(item, schema["items"], "%s[%d]" % (path, i)))  # noqa: UP031
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errs.append(f"{path}: missing {key}")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errs.append(f"{path}: unexpected {key}")
        for key, sub in props.items():
            if key in value:
                errs.extend(validate(value[key], sub, f"{path}.{key}"))
    return errs


def _str(max_len):
    return {"type": "string", "maxLength": max_len}


def _obj(props, required=None):
    return {"type": "object", "properties": props,
            "required": list(required if required is not None else props),
            "additionalProperties": False}


def _position_item(point_ids, comment_max):
    return _obj({
        "id": {"type": "string", "enum": list(point_ids)},
        "position": {"type": "string", "enum": list(POSITIONS)},
        "stake": {"type": "integer", "minimum": 0},
        "comment": _str(comment_max),
    })


def _order():
    return _obj({"command": _str(300), "seeds": _str(120), "hardware": _str(40), "threshold": _str(300),
                  "forecast": _str(300), "why": _str(300)})


def _proposal(cfg):
    """An optional proposal of the team's own brainstorm: a topic, why now, the questions."""
    return _obj({"topic": _str(200), "why": _str(500),
                 "questions": {"type": "array", "maxItems": int(cfg["brainstorm"]["questions_max"]),
                               "items": _str(400)}})


OPTIONAL = ("brainstorm_proposal", "order")


def round1(kind, point_ids, cfg, solo=False):
    """solo: a solo round has no brainstorm proposal (a brainstorm needs the team's quorum)."""
    r = cfg["round"]
    props = {}
    if point_ids:
        props["points"] = {"type": "array", "items": _position_item(point_ids, r["comment_max_chars"]),
                           "maxItems": len(point_ids)}
    props["main_point"] = _str(300)
    props["inner_thought"] = _str(300)
    if kind in ("review", "discuss", "brainstorm"):
        props["proposals"] = {"type": "array", "maxItems": r["proposals_max"], "items": _obj({
            "title": _str(120),
            "question": _str(700),
            "stake": {"type": "integer", "minimum": 0},
        })}
    if kind == "review":
        props["verdict"] = {"type": "string", "enum": list(VERDICTS)}
    if not solo:
        props["brainstorm_proposal"] = _proposal(cfg)
    props["order"] = _order()
    return _obj(props, [k for k in props if k not in OPTIONAL])


def round2(point_ids, cfg, char_ids, proposal_ids=()):
    """proposal_ids: the brainstorm proposals of round 1; each character votes on every one."""
    r = cfg["round"]
    props = {
        "points": {"type": "array", "items": _position_item(point_ids, r["comment_max_chars"]),
                   "maxItems": max(1, len(point_ids))},
        "rebuttals": {"type": "array", "maxItems": r["rebuttals_max"], "items": _obj({
            "to": {"type": "string", "enum": list(char_ids)},
            "point": {"type": "string", "enum": list(point_ids)},
            "text": _str(400),
        })},
        "inner_thought": _str(300),
    }
    if proposal_ids:
        props["brainstorm_votes"] = {"type": "array", "maxItems": len(proposal_ids), "items": _obj({
            "proposal": {"type": "string", "enum": list(proposal_ids)},
            "vote": {"type": "string", "enum": ["yes", "no"]},
            "line": _str(200),
        })}
    props["order"] = _order()
    return _obj(props, [k for k in props if k not in OPTIONAL])


def lock_pass(object_ids):
    """Round 3, the lock pass: an optional lock on one stack object. The ids are the stack's, the
    same for every character; the code still checks the object and the rest."""
    return _obj({
        "lock": _obj({"object": {"type": "string", "enum": list(object_ids)}, "condition": _str(300),
                      "why": _str(300)}),
        "inner_thought": _str(300),
    }, ["inner_thought"])


def reflection(cfg):
    return _obj({
        "portrait": _str(cfg["reflect"]["portrait_max_chars"]),
        "summary_en": _str(cfg["reflect"]["summary_en_max_chars"]),
        "what_changed": _str(400),
        "inner_thought": _str(300),
    })
