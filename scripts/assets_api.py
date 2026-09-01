#!/usr/bin/env python3
"""Jira Assets (JSM) REST helper for the Access Registry — OPS-35/OPS-36.

Thin wrapper over the Assets API, plus the schema constants for the ACCESS schema.
Written for the connector framework (OPS-37) to import.

API GOTCHAS, all discovered the hard way while building the schema. Every one of these
returns an unhelpful error, so they are documented rather than rediscovered:

  1. Select options live in "options" (comma-separated), NOT "additionalValue".
     Creating a Select with additionalValue silently produces an attribute with NO
     options, and every later object create fails with "Invalid values (High)".
     Fix by PUT /objecttypeattribute/{objectTypeId}/{attrId} with {"options": "a,b,c"}.

  2. Object REFERENCE attributes (type=1) require additionalValue = the REFERENCE TYPE
     id (GET /config/referencetype; 1=Dependency 2=Financial 3=Link 4=Reference
     5=Technical). Omit it and you get HTTP 400 with a completely EMPTY errors object.

  3. Object type description max 70 chars. Object schema description is shorter still
     (~75 failed, 73 worked). Both fail with an i18n constraint key, not a readable message.

  4. POST /object/aql returns attributes WITHOUT the nested objectTypeAttribute.name, so
     you cannot read values by name from an AQL result. Use GET /object/{id}/attributes
     when you need named values; use AQL to find ids.

  5. Reference values are written as the target object's numeric id (not objectKey), and
     read back under objectAttributeValues[].referencedObject.

  6. AQL pages at 25 rows by default and does NOT warn — the honest count is in "total".
     aql() below paginates; a single un-paged call made 164 people look like 25.

Env: reads JIRA_EMAIL / JIRA_API_TOKEN from jira-cloud-mcp/.env
"""
import base64
import json
import os
import urllib.error
import urllib.request

WORKSPACE = "54312ac0-661d-4578-ab7f-81c799c73010"
BASE = f"https://api.atlassian.com/jsm/assets/workspace/{WORKSPACE}/v1"

# ACCESS schema (id 136) — object type ids
SCHEMA_ID = 136
PERSON, SYSTEM, ENTITLEMENT, ROLE_PROFILE, ACCESS_GRANT = 161, 162, 163, 164, 165

REF_TYPE_REFERENCE = "4"          # see gotcha 2
DEFAULT_TYPE = {"text": 0, "int": 1, "bool": 2, "double": 3, "date": 4, "time": 5,
                "datetime": 6, "url": 7, "email": 8, "textarea": 9, "select": 10}


def _auth():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {}
    for line in open(os.path.join(repo, ".env")):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return base64.b64encode(f"{env['JIRA_EMAIL']}:{env['JIRA_API_TOKEN']}".encode()).decode()


_HEADERS = {"Authorization": f"Basic {_auth()}", "Content-Type": "application/json",
            "Accept": "application/json"}


def call(method, path, body=None, timeout=40):
    """Returns (status, parsed_body). Never raises on HTTP error — mirrors the error
    discipline the connector endpoints use."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=_HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:300]}


def attribute_ids(object_type_id):
    """{attribute name -> id} for an object type."""
    code, attrs = call("GET", f"/objecttype/{object_type_id}/attributes")
    if code != 200:
        raise RuntimeError(f"GET attributes failed: {code} {attrs}")
    return {a["name"]: a["id"] for a in attrs}


def create_object(object_type_id, attr_ids, **values):
    """Create an object. Reference attributes take the target object's numeric id."""
    payload = {"objectTypeId": object_type_id, "attributes": [
        {"objectTypeAttributeId": attr_ids[k], "objectAttributeValues": [{"value": str(v)}]}
        for k, v in values.items() if v is not None]}
    return call("POST", "/object/create", payload)


def update_object(object_id, attr_ids, **values):
    payload = {"attributes": [
        {"objectTypeAttributeId": attr_ids[k], "objectAttributeValues": [{"value": str(v)}]}
        for k, v in values.items() if v is not None]}
    return call("PUT", f"/object/{object_id}", payload)


def aql(query, page_size=100, **kw):
    """Find every matching object — ids and labels only (gotcha 4).

    PAGINATES. The API returns 25 rows by default and reports the real count in "total";
    a naive single call silently truncates, which made 164 people look like 25.
    """
    out, start = [], 0
    while True:
        code, body = call("POST", "/object/aql",
                          {"qlQuery": query, "startAt": start, "maxResults": page_size, **kw})
        if code != 200:
            raise RuntimeError(f"AQL failed: {code} {body}")
        vals = body.get("values", [])
        out.extend(vals)
        if body.get("isLast") or not vals or len(out) >= body.get("total", 0):
            return out
        start += len(vals)


def object_values(object_id):
    """{attribute name -> value} for one object, resolving references to their label."""
    code, attrs = call("GET", f"/object/{object_id}/attributes")
    if code != 200:
        raise RuntimeError(f"GET object attributes failed: {code} {attrs}")
    out = {}
    for a in attrs:
        name = a["objectTypeAttribute"]["name"]
        vals = []
        for v in a["objectAttributeValues"]:
            ref = v.get("referencedObject")
            vals.append({"id": ref["id"], "key": ref["objectKey"], "label": ref["label"]}
                        if ref else (v.get("displayValue") or v.get("value")))
        out[name] = vals[0] if len(vals) == 1 else vals
    return out
