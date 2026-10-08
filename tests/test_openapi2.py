"""Tests for jobpilot.openapi2: the OpenAPI 3.1 -> Swagger 2.0 conversion for the Power Platform connector.
Offline: small inline 3.1 fixtures for the edge cases, the real spec (built in code) for the rest."""
import copy
import json

import pytest

from jobpilot import api, openapi2

OPS = {"/health": "health", "/jobs": "list_jobs", "/jobs/{job_id}": "get_job", "/skills": "top_skills",
       "/runs": "recent_runs", "/applications": "list_applications"}


@pytest.fixture(scope="module")
def spec3():
    return api.create_app(docs=True).openapi()


@pytest.fixture(scope="module")
def v2(spec3):
    return openapi2.to_swagger2(spec3)


def params(v2, path="/jobs"):
    return {p["name"]: p for p in v2["paths"][path]["get"]["parameters"]}


def mini(schemas=None, parameters=None, response=None):
    """A minimal 3.1 spec with one operation, for the unit cases."""
    op = {"operationId": "op", "responses": {"200": {
        "description": "ok", "content": {"application/json": {"schema": response or {"type": "object"}}}}}}
    if parameters:
        op["parameters"] = parameters
    return {"openapi": "3.1.0", "info": {"title": "t", "version": "1"},
            "components": {"schemas": schemas or {}}, "paths": {"/x": {"get": op}}}


def test_top_level(v2):
    assert v2["swagger"] == "2.0" and "openapi" not in v2
    assert v2["info"]["title"] == "JobPilot API" and v2["info"]["version"] and v2["info"]["description"]
    assert v2["host"] == openapi2.HOST and v2["basePath"] == "/" and v2["schemes"] == ["https"]
    assert v2["consumes"] == v2["produces"] == ["application/json"]
    assert "components" not in v2


def test_security(v2):
    scheme = v2["securityDefinitions"]["APIKeyHeader"]
    assert {k: scheme[k] for k in ("type", "in", "name")} == {"type": "apiKey", "in": "header", "name": "X-API-Key"}
    for path, item in v2["paths"].items():
        if path == "/health":
            assert "security" not in item["get"]
        else:
            assert item["get"]["security"] == [{"APIKeyHeader": []}], path


def test_operations(v2):
    assert {path: item["get"]["operationId"] for path, item in v2["paths"].items()} == OPS
    for path, item in v2["paths"].items():
        op = item["get"]
        assert op["summary"] and op["description"], path
        assert len(op["summary"]) <= 80, path  # Power Platform shows summaries as action names
        for p in op.get("parameters", []):
            assert p["description"] and p["x-ms-summary"], (path, p["name"])


def test_query_params_flattened(v2):
    p = params(v2)
    assert p["open_to_you"]["type"] == "boolean" and p["open_to_you"]["required"] is False
    assert "anyOf" not in p["open_to_you"] and "schema" not in p["open_to_you"]
    assert (p["min_score"]["minimum"], p["min_score"]["maximum"]) == (0, 100)
    assert (p["skill"]["minLength"], p["skill"]["maxLength"]) == (1, 60)
    assert (p["since"]["type"], p["since"]["format"]) == ("string", "date")
    assert p["work_type"]["type"] == "string" and p["work_type"]["enum"] == ["Remote", "Remote?", "Hybrid", "On-site"]
    assert p["source"]["type"] == "string" and p["source"]["enum"] == ["linkedin", "himalayas", "jooble"]
    assert p["limit"]["default"] == 20 and p["limit"]["x-ms-summary"] == "Limit"


def test_path_param(v2):
    job_id = params(v2, "/jobs/{job_id}")["job_id"]
    assert job_id["in"] == "path" and job_id["required"] is True
    assert job_id["type"] == "integer" and job_id["minimum"] == 1
    assert "maximum" not in job_id  # 2**63-1 is beyond what the JavaScript designer can hold exactly


def test_responses(v2):
    for path, item in v2["paths"].items():
        for code, resp in item["get"]["responses"].items():
            if code == "200":
                assert resp["description"]
                if path != "/health":
                    assert resp["schema"]["$ref"].startswith("#/definitions/"), path
            else:
                assert set(resp) == {"description"}, (path, code)
    assert set(v2["paths"]["/jobs/{job_id}"]["get"]["responses"]) == {"200", "404", "422"}


def test_definitions(v2):
    defs = v2["definitions"]
    company = defs["JobSummary"]["properties"]["company"]
    assert company["type"] == "string" and company["x-nullable"] is True and company["title"] == "Company"
    assert "company" in defs["JobSummary"]["required"]
    score = defs["JobDetail"]["properties"]["match_score"]
    assert score["type"] == "integer" and score["x-nullable"] and "0-100" in score["description"]
    assert "x-nullable" not in defs["JobSummary"]["properties"]["id"]
    assert defs["JobList"]["properties"]["items"]["items"] == {"$ref": "#/definitions/JobSummary"}
    for gone in ("HTTPValidationError", "ValidationError", "WorkType", "Source"):
        assert gone not in defs


def test_every_ref_resolves_and_no_31_leftovers(v2):
    text = json.dumps(v2)
    refs = openapi2._refs(v2)
    assert refs and all(r.startswith("#/definitions/") and r[len("#/definitions/"):] in v2["definitions"] for r in refs)
    assert "anyOf" not in text and "#/components/" not in text and '"null"' not in text


@pytest.mark.parametrize("schema, where", [
    ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, "#/components/schemas/M/properties/f"),
    ({"anyOf": [{"type": "string"}, {"type": "integer"}]}, "#/components/schemas/M/properties/f/anyOf"),
    ({"type": ["string", "null"]}, "#/components/schemas/M/properties/f"),
    ({"const": "x"}, "#/components/schemas/M/properties/f"),
    ({"$ref": "#/components/schemas/Missing"}, "#/components/schemas/M/properties/f/$ref"),
])
def test_unsupported_schema_raises_with_path(schema, where):
    spec = mini({"M": {"type": "object", "properties": {"f": schema}}}, response={"$ref": "#/components/schemas/M"})
    with pytest.raises(ValueError) as e:
        openapi2.to_swagger2(spec)
    assert where in str(e.value)


def test_dangling_ref_in_response_raises():
    with pytest.raises(ValueError, match="#/paths/~1x/get/responses/200/schema/\\$ref"):
        openapi2.to_swagger2(mini(response={"$ref": "#/components/schemas/Nope"}))


def test_unsupported_param_raises_with_path():
    p = {"name": "a", "in": "query", "schema": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}]}}
    with pytest.raises(ValueError, match="#/paths/~1x/get/parameters/0/schema/anyOf"):
        openapi2.to_swagger2(mini(parameters=[p]))


def test_mini_conversion():
    schemas = {"M": {"type": "object", "required": ["n"], "properties": {
        "n": {"anyOf": [{"type": "integer", "maximum": 2**63 - 1}, {"type": "null"}], "title": "N"},
        "tags": {"type": "array", "items": {"$ref": "#/components/schemas/T"}}}},
        "T": {"type": "object", "properties": {"v": {"type": "string"}}},
        "Unused": {"type": "object"}}
    out = openapi2.to_swagger2(mini(schemas, response={"$ref": "#/components/schemas/M"}), host="example.test")
    assert out["host"] == "example.test"
    assert set(out["definitions"]) == {"M", "T"}  # T reached through M's array items; Unused pruned
    assert out["definitions"]["M"]["properties"]["n"] == {"type": "integer", "title": "N", "x-nullable": True}
    assert out["definitions"]["M"]["required"] == ["n"]


def test_input_not_mutated(spec3):
    before = copy.deepcopy(spec3)
    openapi2.to_swagger2(spec3)
    assert spec3 == before


def test_snapshot_is_current(v2):
    # compared as parsed JSON, so a CRLF checkout on Windows doesn't fail it
    with open(openapi2.SPEC_V2_FILE, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved == json.loads(json.dumps(v2)), (
        "the 2.0 connector spec is stale: run `python -m jobpilot.openapi2 --write` and commit docs/openapi-v2.json")


def test_write_spec_v2_is_utf8_lf(tmp_path):
    path = tmp_path / "openapi-v2.json"
    openapi2.write_spec_v2(str(path))
    raw = path.read_bytes()
    assert raw.endswith(b"}\n") and b"\r\n" not in raw  # not UTF-16, not CRLF
    assert raw.decode("utf-8") == openapi2.spec_v2_json()
