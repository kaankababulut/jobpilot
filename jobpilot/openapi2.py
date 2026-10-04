"""Converts the API's OpenAPI 3.1 spec (what FastAPI emits) to Swagger 2.0 (what a Power Platform
custom connector imports). Stdlib only, and only the subset our API uses: anything it can't convert
faithfully raises ValueError naming the JSON path, so a new model shape fails a test instead of
silently producing a connector that lies about the API.
Run: python -m jobpilot.openapi2 [--write]   (writes docs/openapi-v2.json)"""
import copy
import json
import os
import sys

# the deployed API's public hostname (already in the README); the connector calls it over HTTPS
HOST = "ca-jobpilot-api.kindpebble-9d30ee27.swedencentral.azurecontainerapps.io"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC_V2_FILE = os.path.join(ROOT, "docs", "openapi-v2.json")

REF3, REF2 = "#/components/schemas/", "#/definitions/"
NULL = {"type": "null"}
# keywords 2.0 lacks, or (exclusive*) where 3.1 changed the meaning from a flag to a number
UNSUPPORTED = ("oneOf", "allOf", "not", "const", "prefixItems", "exclusiveMinimum", "exclusiveMaximum")
PARAM_KEYS = ("type", "format", "enum", "minimum", "maximum", "minLength", "maxLength", "pattern", "default")
# the connector designer is JavaScript, where integers above 2**53 lose precision; a bound like
# BIGINT's 2**63-1 would be stored rounded or rejected, and the server still enforces it anyway
MAX_SAFE = 2**53


def _join(path: str, key) -> str:
    # JSON Pointer escaping, so "/jobs/{job_id}" stays one segment in error messages
    return f"{path}/{str(key).replace('~', '~0').replace('/', '~1')}"


def _check(s, path: str) -> None:
    if not isinstance(s, dict):
        raise ValueError(f"{path}: expected a schema object")
    for key in UNSUPPORTED:
        if key in s:
            raise ValueError(f"{path}: '{key}' has no Swagger 2.0 equivalent")
    if isinstance(s.get("type"), list):
        raise ValueError(f"{path}: 'type' lists are not supported in Swagger 2.0")


def _unwrap_nullable(s: dict, path: str) -> tuple[dict, bool]:
    """anyOf[T, null] -> (outer keys merged with T, True); a schema without anyOf -> (s, False)."""
    if "anyOf" not in s:
        return s, False
    branches = s["anyOf"]
    rest = [b for b in branches if b != NULL]
    if len(rest) != 1 or len(branches) != 2:
        raise ValueError(f"{_join(path, 'anyOf')}: only anyOf[T, null] converts to Swagger 2.0")
    _check(rest[0], _join(_join(path, "anyOf"), branches.index(rest[0])))
    outer = {k: v for k, v in s.items() if k != "anyOf"}
    return {**outer, **rest[0]}, True


def _ref_name(ref: str, components: dict, path: str) -> str:
    if not ref.startswith(REF3) or ref[len(REF3):] not in components:
        raise ValueError(f"{path}: dangling or unsupported $ref {ref}")
    return ref[len(REF3):]


def _schema(s, components: dict, path: str) -> dict:
    """One 3.1 schema -> 2.0, recursively. Refs point at #/definitions; nullable becomes x-nullable."""
    _check(s, path)
    s, nullable = _unwrap_nullable(s, path)
    if "$ref" in s:
        if nullable:  # 2.0 ignores siblings of $ref, so x-nullable there would be silently lost
            raise ValueError(f"{path}: a nullable $ref can't be expressed in Swagger 2.0")
        return {"$ref": REF2 + _ref_name(s["$ref"], components, _join(path, "$ref"))}
    out = {}
    for key, value in s.items():
        if key == "properties":
            out[key] = {name: _schema(sub, components, _join(_join(path, key), name)) for name, sub in value.items()}
        elif key == "items":
            out[key] = _schema(value, components, _join(path, key))
        elif key == "additionalProperties" and isinstance(value, dict):
            out[key] = _schema(value, components, _join(path, key))
        elif key in ("minimum", "maximum") and abs(value) > MAX_SAFE:
            continue
        else:
            out[key] = copy.deepcopy(value)
    if nullable:
        out["x-nullable"] = True
    return out


def _param(p: dict, components: dict, path: str) -> dict:
    """2.0 parameters carry type/format/bounds directly instead of in a nested schema."""
    spath = _join(path, "schema")
    schema = p.get("schema", {})
    _check(schema, spath)
    schema, _ = _unwrap_nullable(schema, spath)  # optional is already said by required: false
    if "$ref" in schema:  # an Enum parameter: 2.0 parameters can't $ref, so inline the values
        target = components[_ref_name(schema["$ref"], components, _join(spath, "$ref"))]
        if "enum" not in target:
            raise ValueError(f"{spath}: only enum schemas can be inlined into a parameter")
        schema = {**{k: v for k, v in schema.items() if k != "$ref"}, **{k: target[k] for k in ("type", "enum")}}
    _check(schema, spath)
    if schema.get("type") not in ("string", "integer", "number", "boolean"):
        raise ValueError(f"{spath}: parameters must be a simple type, got {schema.get('type')!r}")
    out = {"name": p["name"], "in": p["in"],
           "required": True if p["in"] == "path" else bool(p.get("required", False))}  # 2.0 requires path params
    description = p.get("description") or schema.get("description")
    if description:
        out["description"] = description
    if schema.get("title"):
        out["x-ms-summary"] = schema["title"]  # the display name Power Platform shows for the field
    for key in PARAM_KEYS:
        if key in schema and not (key in ("minimum", "maximum") and abs(schema[key]) > MAX_SAFE):
            out[key] = copy.deepcopy(schema[key])
    return out


def _operation(op: dict, components: dict, path: str) -> dict:
    out = {k: op[k] for k in ("operationId", "summary", "description") if k in op}
    if op.get("parameters"):
        out["parameters"] = [_param(p, components, _join(_join(path, "parameters"), i))
                             for i, p in enumerate(op["parameters"])]
    responses = {}
    for code, resp in op.get("responses", {}).items():
        responses[code] = {"description": resp.get("description", "")}
        schema = resp.get("content", {}).get("application/json", {}).get("schema")
        # only the success body is described: Power Platform builds outputs from it, and the 422
        # body (HTTPValidationError) uses anyOf[string, integer], which 2.0 can't express
        if code == "200" and schema is not None:
            responses[code]["schema"] = _schema(
                schema, components, _join(_join(_join(path, "responses"), code), "schema"))
    out["responses"] = responses
    if "security" in op:
        out["security"] = copy.deepcopy(op["security"])
    return out


def _refs(node) -> set[str]:
    """Every $ref string anywhere under node."""
    if isinstance(node, dict):
        found = {node["$ref"]} if isinstance(node.get("$ref"), str) else set()
        return found.union(*(_refs(v) for v in node.values()))
    if isinstance(node, list):
        return set().union(*(_refs(v) for v in node))
    return set()


def to_swagger2(spec: dict, host: str = HOST) -> dict:
    """The 3.1 spec as a Swagger 2.0 dict. Pure: the input is never modified."""
    spec = copy.deepcopy(spec)
    components = spec.get("components", {}).get("schemas", {})
    info = spec.get("info", {})
    out = {
        "swagger": "2.0",
        "info": {k: info[k] for k in ("title", "version", "description") if k in info},
        "host": host,
        "basePath": "/",
        "schemes": ["https"],
        "consumes": ["application/json"],
        "produces": ["application/json"],
    }
    security = {}
    for name, scheme in spec.get("components", {}).get("securitySchemes", {}).items():
        if scheme.get("type") != "apiKey":
            raise ValueError(f"#/components/securitySchemes/{name}: only apiKey schemes are supported")
        security[name] = {k: scheme[k] for k in ("type", "in", "name", "description") if k in scheme}
    if security:
        out["securityDefinitions"] = security
    paths = {}
    for route, item in spec.get("paths", {}).items():
        ipath = _join("#/paths", route)
        paths[route] = {method: _operation(op, components, _join(ipath, method)) for method, op in item.items()}
    out["paths"] = paths

    # only definitions the paths reach, followed transitively: the 422 error models and the inlined
    # enums would otherwise be dragged in, and ValidationError can't even be converted
    todo = [r[len(REF2):] for r in _refs(paths)]
    reached = set()
    while todo:
        name = todo.pop()
        if name in reached:
            continue
        reached.add(name)
        # a bad ref is skipped here and raised by _schema below, which knows its exact path
        todo += [r[len(REF3):] for r in _refs(components[name]) if r.startswith(REF3) and r[len(REF3):] in components]
    out["definitions"] = {name: _schema(components[name], components, REF3 + name) for name in sorted(reached)}
    return out


def spec_v2_json() -> str:
    """The 2.0 spec as stable text, formatted like docs/openapi.json (sorted keys, ASCII escapes)."""
    from jobpilot import api  # imported here, so the converter itself stays importable without FastAPI
    return json.dumps(to_swagger2(api.create_app(docs=True).openapi()), indent=2, sort_keys=True) + "\n"


def write_spec_v2(path: str = SPEC_V2_FILE) -> None:
    # written here rather than with a shell `>`, because PowerShell's `>` writes UTF-16
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(spec_v2_json())


if __name__ == "__main__":  # python -m jobpilot.openapi2 [--write]
    if sys.argv[1:] == ["--write"]:
        write_spec_v2()
        print(f"wrote {SPEC_V2_FILE}")
    elif sys.argv[1:]:
        sys.exit("usage: python -m jobpilot.openapi2 [--write]")
    else:
        sys.stdout.write(spec_v2_json())
