"""Atlassian Assets (JSM) tools — object schemas, object types, attributes, objects, AQL.

Assets is the store for relational reference data synced from Orchard (Products,
Content Codes, and their Product->Default Content Code relationship). The API is a
separate host (api.atlassian.com/jsm/assets/workspace/{workspaceId}/v1); the client
handles workspace-id discovery and auth.

Attribute/object payloads are fiddly, so the create/update tools accept a raw JSON
string for full control, with convenience params for the common cases.

Attribute `type` (POST /objecttypeattribute/{id}):
  0 = Default  (with defaultTypeId: 0 Text, 1 Integer, 2 Bool, 3 Double, 4 Date,
                6 DateTime, 7 URL, 8 Email, 9 Textarea, 10 Select, 11 IP)
  1 = Object reference (typeValue = target objectTypeId)
  2 = User, 4 = Group, 6 = Project, 7 = Status
"""

import json
from jira_client import JiraCloudClient


def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _load(js: str, what: str):
    try:
        return json.loads(js)
    except Exception as exc:
        raise ValueError(f"Invalid JSON for {what}: {exc}")


def register_assets_tools(mcp, client: JiraCloudClient):

    # ---------- workspace / schema (read) ----------
    @mcp.tool()
    async def get_assets_workspace() -> str:
        """Get the Assets workspace id (confirms JSM Assets connectivity)."""
        wsid = await client.get_assets_workspace_id()
        return _fmt({"workspaceId": wsid})

    @mcp.tool()
    async def list_object_schemas() -> str:
        """List all Assets object schemas (id, name, key)."""
        data = await client.assets_get("/objectschema/list")
        vals = data.get("values", data) if isinstance(data, dict) else data
        return _fmt(vals)

    @mcp.tool()
    async def get_object_schema(schema_id: str) -> str:
        """Get one Assets object schema by id."""
        return _fmt(await client.assets_get(f"/objectschema/{schema_id}"))

    @mcp.tool()
    async def list_assets_icons() -> str:
        """List global Assets icons (needed as iconId when creating object types)."""
        return _fmt(await client.assets_get("/icon/global"))

    # ---------- object types (read/write) ----------
    @mcp.tool()
    async def list_object_types(schema_id: str) -> str:
        """List object types in a schema (flat), with ids and parent ids."""
        return _fmt(await client.assets_get(f"/objectschema/{schema_id}/objecttypes/flat"))

    @mcp.tool()
    async def list_object_type_attributes(object_type_id: str) -> str:
        """List an object type's attributes (id, name, type) — needed to build objects."""
        return _fmt(await client.assets_get(f"/objecttype/{object_type_id}/attributes"))

    # ---------- write: schema / type / attribute ----------
    @mcp.tool()
    async def create_object_schema(name: str, schema_key: str, description: str = "") -> str:
        """Create an Assets object schema. schema_key is the short prefix (e.g. 'ORCH')."""
        body = {"name": name, "objectSchemaKey": schema_key, "description": description}
        return _fmt(await client.assets_post("/objectschema/create", body))

    @mcp.tool()
    async def create_object_type(schema_id: str, name: str, icon_id: str,
                                 description: str = "", parent_object_type_id: str = "") -> str:
        """Create an object type in a schema. icon_id from list_assets_icons."""
        body = {"name": name, "objectSchemaId": schema_id, "iconId": icon_id,
                "description": description}
        if parent_object_type_id:
            body["parentObjectTypeId"] = parent_object_type_id
        return _fmt(await client.assets_post("/objecttype/create", body))

    @mcp.tool()
    async def create_object_type_attribute(object_type_id: str, attribute_json: str) -> str:
        """Create an attribute on an object type. attribute_json is the raw payload, e.g.
        text:      {"name":"Code","type":0,"defaultTypeId":0}
        select:    {"name":"Experience Type","type":0,"defaultTypeId":10,"options":"PHOTOSTRIP,VIDEO"}
        reference: {"name":"Default Content Code","type":1,"typeValue":"<targetObjectTypeId>",
                    "additionalValue":"<referenceTypeId>"}  # additionalValue REQUIRED; 4="Reference"
        Set a label attr with "label":true so objects show a human name."""
        body = _load(attribute_json, "attribute")
        return _fmt(await client.assets_post(f"/objecttypeattribute/{object_type_id}", body))

    # ---------- objects (read/write) ----------
    @mcp.tool()
    async def aql_find_objects(aql: str, limit: int = 50, include_attributes: bool = True) -> str:
        """Find objects with AQL, e.g. 'objectType = "Product" AND Code = "DLX"'."""
        body = {"qlQuery": aql}
        data = await client.assets_post(
            f"/object/aql?page=1&resultPerPage={int(limit)}"
            f"&includeAttributes={'true' if include_attributes else 'false'}", body)
        return _fmt(data)

    @mcp.tool()
    async def get_object(object_id: str) -> str:
        """Get one Assets object (attributes + values) by id."""
        return _fmt(await client.assets_get(f"/object/{object_id}"))

    @mcp.tool()
    async def create_object(object_type_id: str, attributes_json: str) -> str:
        """Create an object. attributes_json is the attributes array, e.g.
        [{"objectTypeAttributeId":"143","objectAttributeValues":[{"value":"Deluxe"}]},
         {"objectTypeAttributeId":"144","objectAttributeValues":[{"value":"DLX"}]}]
        For a reference attribute, value is the target object's id or key."""
        attrs = _load(attributes_json, "attributes")
        body = {"objectTypeId": object_type_id, "attributes": attrs}
        return _fmt(await client.assets_post("/object/create", body))

    @mcp.tool()
    async def update_object(object_id: str, attributes_json: str) -> str:
        """Update an object's attributes (same array shape as create_object)."""
        attrs = _load(attributes_json, "attributes")
        return _fmt(await client.assets_put(f"/object/{object_id}", {"attributes": attrs}))

    @mcp.tool()
    async def delete_object(object_id: str) -> str:
        """Delete an Assets object by id."""
        ok = await client.assets_delete(f"/object/{object_id}")
        return _fmt({"deleted": ok, "objectId": object_id})
