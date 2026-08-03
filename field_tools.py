"""Custom field tools for Jira Cloud."""

import json
from jira_client import JiraCloudClient

def _fmt(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


_SEARCHER_PREFIX = "com.atlassian.jira.plugin.system.customfieldtypes:"

# Default search template (searcherKey) per built-in custom field type. Jira creates a field
# with NO searcher when searcherKey is empty/omitted, which leaves it silently non-searchable:
# it can't be JQL-searched, filtered, or aggregated in dashboard gadgets. Verified against the
# searcherKey of existing fields of each type on the instance.
DEFAULT_SEARCHERS = {
    "select": _SEARCHER_PREFIX + "multiselectsearcher",
    "multiselect": _SEARCHER_PREFIX + "multiselectsearcher",
    "radiobuttons": _SEARCHER_PREFIX + "multiselectsearcher",
    "multicheckboxes": _SEARCHER_PREFIX + "multiselectsearcher",
    "cascadingselect": _SEARCHER_PREFIX + "cascadingselectsearcher",
    "textfield": _SEARCHER_PREFIX + "textsearcher",
    "textarea": _SEARCHER_PREFIX + "textsearcher",
    "readonlyfield": _SEARCHER_PREFIX + "textsearcher",
    "url": _SEARCHER_PREFIX + "exacttextsearcher",
    "datepicker": _SEARCHER_PREFIX + "daterange",
    "datetime": _SEARCHER_PREFIX + "datetimerange",
    "float": _SEARCHER_PREFIX + "exactnumber",
    "importid": _SEARCHER_PREFIX + "exactnumber",
    "userpicker": _SEARCHER_PREFIX + "userpickergroupsearcher",
    "multiuserpicker": _SEARCHER_PREFIX + "userpickergroupsearcher",
    "grouppicker": _SEARCHER_PREFIX + "grouppickersearcher",
    "multigrouppicker": _SEARCHER_PREFIX + "grouppickersearcher",
    "project": _SEARCHER_PREFIX + "projectsearcher",
    "version": _SEARCHER_PREFIX + "versionsearcher",
    "multiversion": _SEARCHER_PREFIX + "versionsearcher",
    "labels": _SEARCHER_PREFIX + "labelsearcher",
}


def _default_searcher(type_key: str) -> str:
    """Resolve the default searcherKey for a custom field type key. '' if unknown."""
    short = (type_key or "").split(":")[-1]
    return DEFAULT_SEARCHERS.get(short, "")


def register_field_tools(mcp, client: JiraCloudClient):

    @mcp.tool()
    async def list_custom_fields(search: str = "") -> str:
        """List all custom fields. Use 'search' to filter by name."""
        data = await client.get("/field")
        fields = [f for f in data if f.get("custom", False)]
        if search:
            s = search.lower()
            fields = [f for f in fields if s in (f.get("name", "") or "").lower()]
        return _fmt(fields)

    @mcp.tool()
    async def get_custom_field(field_id: str) -> str:
        """Get custom field details including contexts."""
        # Field info
        all_fields = await client.get("/field")
        field = next((f for f in all_fields if f.get("id") == field_id), None)
        if not field:
            return _fmt({"error": f"Field {field_id} not found"})
        # Contexts
        field_num = field_id.replace("customfield_", "")
        try:
            contexts = await client.get(f"/field/{field_id}/context")
            field["contexts"] = contexts.get("values", [])
        except Exception:
            field["contexts"] = []
        return _fmt(field)

    @mcp.tool()
    async def get_field_options(field_id: str, context_id: str = "") -> str:
        """Get options for a select/radio/checkbox field."""
        if context_id:
            data = await client.get(f"/field/{field_id}/context/{context_id}/option")
        else:
            # Get first context
            contexts = await client.get(f"/field/{field_id}/context")
            ctx_list = contexts.get("values", [])
            if not ctx_list:
                return _fmt({"error": "No contexts found"})
            ctx_id = ctx_list[0]["id"]
            data = await client.get(f"/field/{field_id}/context/{ctx_id}/option")
        return _fmt(data)

    @mcp.tool()
    async def create_custom_field(name: str, type_key: str, description: str = "",
                                  searcher_key: str = "") -> str:
        """Create a new custom field.
        type_key examples: 'com.atlassian.jira.plugin.system.customfieldtypes:textfield',
        'com.atlassian.jira.plugin.system.customfieldtypes:select'.

        searcher_key sets the field's search template — what makes it JQL-searchable,
        filterable, and aggregatable in dashboard gadgets. Leave it empty to auto-pick the
        correct default for the type (recommended); a field created with no searcher is
        silently non-searchable. Pass an explicit searcherKey only to override the default."""
        sk = searcher_key or _default_searcher(type_key)
        body = {"name": name, "type": type_key}
        if sk:
            body["searcherKey"] = sk
        if description:
            body["description"] = description
        try:
            data = await client.post("/field", body)
        except Exception as exc:
            # Surface the response body — app/managed field types (e.g. Assets object
            # fields, com.atlassian.jira.plugins.cmdb:cmdb-object-cf) are typically NOT
            # creatable via this REST endpoint and must be made in the Assets/field UI.
            detail = getattr(getattr(exc, "response", None), "text", "")
            raise RuntimeError(f"create field failed: {exc}. Detail: {detail[:600]}")
        if isinstance(data, dict):
            data["_searcherKeyUsed"] = sk or "(none — unknown type_key; field will NOT be searchable)"
        return _fmt(data)

    @mcp.tool()
    async def update_custom_field(field_id: str, name: str = "", description: str = "",
                                  searcher_key: str = "") -> str:
        """Update a custom field's name, description, and/or search template.
        Pass searcher_key to repair a non-searchable field, e.g.
        'com.atlassian.jira.plugin.system.customfieldtypes:multiselectsearcher'.
        Omitted name/description/searcherKey default to the field's current values, so a
        searcher-only repair won't clobber the name or description."""
        cur = {}
        try:
            r = await client.get("/field/search", type="custom", id=field_id, expand="searcherKey")
            vals = r.get("values", []) if isinstance(r, dict) else []
            cur = vals[0] if vals else {}
        except Exception:
            cur = {}
        body = {"name": name or cur.get("name", "")}
        desc = description or cur.get("description", "")
        if desc:
            body["description"] = desc
        sk = searcher_key or cur.get("searcherKey") or ""
        if sk:
            body["searcherKey"] = sk
        await client.put(f"/field/{field_id}", body)
        return _fmt({"status": "updated", "fieldId": field_id,
                     "name": body["name"], "searcherKey": sk or "(none)"})

    @mcp.tool()
    async def delete_custom_field(field_id: str) -> str:
        """Delete a custom field. IRREVERSIBLE."""
        await client.delete(f"/field/{field_id}")
        return _fmt({"status": "deleted", "fieldId": field_id})

    @mcp.tool()
    async def add_field_option(field_id: str, context_id: str, value: str) -> str:
        """Add option to a select/radio/checkbox field."""
        body = {"options": [{"value": value}]}
        data = await client.post(f"/field/{field_id}/context/{context_id}/option", body)
        return _fmt(data)

    @mcp.tool()
    async def list_system_fields() -> str:
        """List all fields (system + custom)."""
        data = await client.get("/field")
        system = [f for f in data if not f.get("custom", False)]
        return _fmt(system)
