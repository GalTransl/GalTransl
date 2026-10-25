"""Pi-style local tools, exposed through GalTransl's existing function-calling loop."""
from GalTransl.Agent.tool_schemas import AGENT_TOOLS


def tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}}


PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
TIMEOUT = {"type": "integer", "minimum": 1, "maximum": 900}
EXTRACTION_TOOLS = [
    tool("read", "Read text or images (jpg, png, gif, webp, bmp) from the game, Skill or workspace. Images are attachments. Text is truncated to 2000 lines or 50KB; follow offset/limit until complete. Offset is 1-based. Use run_python for binary analysis.",
         {"path": {"type": "string"}, "offset": {"type": "number"},
          "limit": {"type": "number"}}, ["path"]),
    tool("write", "Create or overwrite a UTF-8 file in the output workspace, creating parent directories. Use for new files or complete rewrites. Game files and the installed Skill cannot be edited.",
         {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    tool("edit", "Edit a workspace file with targeted replacements. Each oldText must uniquely match the original file, and regions must not overlap. Put multiple disjoint changes in one edits array. Returns diff, patch and firstChangedLine.",
         {"path": {"type": "string"}, "edits": {"type": "array", "items": {
             "type": "object", "properties": {"oldText": {"type": "string"}, "newText": {"type": "string"}},
             "required": ["oldText", "newText"]}}}, ["path", "edits"]),
    tool("run_python", "Execute Python in the output workspace. Supply exactly one of code, script, module. Defaults to the existing backend's bundled Python; the Skill root is importable. Explicitly choose runtime=system (optionally python_path) if a dependency is unavailable; never auto-install packages or silently fall back. Commands run as the local user, not in a sandbox. Never execute game EXE/DLLs or overwrite source files. Output is bounded; full output is in log_path. Nonzero exit code means failure.",
         {"code": {"type": "string", "maxLength": 60000}, "script": PATH,
          "module": {"type": "string", "minLength": 1, "maxLength": 256},
          "runtime": {"type": "string", "enum": ["bundled", "system"]}, "python_path": PATH,
          "args": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 1024}},
          "timeout_seconds": TIMEOUT}),
    tool("bash", "Execute a bash command in the output workspace as the current user. Output keeps the last 2000 lines or 50KB; full truncated output is saved to a log. Optional timeout in seconds, no default timeout. Never run game executables or modify source game files. Prefer run_python for Python.",
         {"command": {"type": "string"}, "timeout": {"type": "number"}}, ["command"]),
    tool("ask_user", "Ask the user for a required choice that cannot be inferred. Supply 1-4 questions, each with options and a recommended option matching one of them. The user can also enter a custom answer. In full-auto no-interruption mode the recommended option is selected automatically; without a recommendation the first option is used.",
         next(item for item in AGENT_TOOLS if item["function"]["name"] == "ask_user")["function"]["parameters"]["properties"], ["questions"]),
    next(item for item in AGENT_TOOLS if item["function"]["name"] == "read_history_archive"),
]

# Pi's TypeBox schemas allow extra properties; the retained custom tools remain strict.
for _definition in EXTRACTION_TOOLS:
    if _definition['function']['name'] in {'read', 'bash', 'edit', 'write'}:
        _definition['function']['parameters'].pop('additionalProperties', None)


def prepare_arguments(name, args):
    """Pi edit's model compatibility coercions, before schema validation."""
    import json
    if name != "edit" or not isinstance(args, dict):
        return args
    args = dict(args)
    edits = args.get("edits")
    if isinstance(edits, str):
        try:
            edits = json.loads(edits)
        except ValueError:
            pass
    if isinstance(edits, dict) and isinstance(edits.get("oldText"), str) and isinstance(edits.get("newText"), str):
        edits = [edits]
    if isinstance(args.get("oldText"), str) and isinstance(args.get("newText"), str):
        edits = [*(edits if isinstance(edits, list) else []),
                 {"oldText": args.pop("oldText"), "newText": args.pop("newText")}]
    if edits is not None:
        args["edits"] = edits
    return args


def validate_arguments(name, args):
    """Validate at the execution boundary even if a provider ignores the schema."""
    from GalTransl.Agent.models import AgentToolError
    schema = next((item["function"]["parameters"] for item in EXTRACTION_TOOLS
                   if item["function"]["name"] == name), None)
    if schema is None or not isinstance(args, dict):
        raise AgentToolError(f"Unknown tool or invalid arguments: {name}")

    def check(value, spec, key):
        import math
        kind = spec.get("type")
        valid = {"string": isinstance(value, str), "integer": type(value) is int,
                 "number": type(value) in (int, float) and math.isfinite(value),
                 "boolean": type(value) is bool, "array": isinstance(value, list), "object": isinstance(value, dict)}
        if kind in valid and not valid[kind]:
            raise AgentToolError(f"{key} must be {kind}")
        if isinstance(value, (str, list)):
            minimum = spec.get("minLength", spec.get("minItems", 0))
            maximum = spec.get("maxLength", spec.get("maxItems", float("inf")))
            if not minimum <= len(value) <= maximum:
                raise AgentToolError(f"{key} exceeds its length budget")
        if type(value) is int and not spec.get("minimum", value) <= value <= spec.get("maximum", value):
            raise AgentToolError(f"{key} is outside its allowed range")
        if "enum" in spec and value not in spec["enum"]:
            raise AgentToolError(f"Invalid {key}")
        if isinstance(value, list) and "items" in spec:
            for item in value:
                check(item, spec["items"], key)
        if kind == "object":
            if any(field not in value for field in spec.get("required", [])):
                raise AgentToolError(f"Missing required fields in {key}")
            for field, item in value.items():
                if field in spec.get("properties", {}):
                    check(item, spec["properties"][field], f"{key}.{field}")

    if any(key not in args for key in schema.get("required", [])):
        raise AgentToolError("Missing required tool arguments")
    for key, value in args.items():
        if key not in schema["properties"]:
            if schema.get("additionalProperties") is False:
                raise AgentToolError(f"Unknown argument: {key}")
        else:
            check(value, schema["properties"][key], key)
