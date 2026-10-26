"""Pi-style local tools, exposed through GalTransl's existing function-calling loop."""
from GalTransl.Agent.tool_schemas import AGENT_TOOLS


def tool(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}}


PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
TIMEOUT = {"type": "integer", "minimum": 1, "maximum": 900}
EXTRACTION_TOOLS = [
    tool("read", "Read the contents of a file. Supports text files and images (jpg, png, gif, webp, bmp). Images are sent as attachments. For text files, output is truncated to 2000 lines or 50KB (whichever is hit first). Use offset/limit for large files. When you need the full file, continue with offset until complete.",
         {"path": {"type": "string", "description": "Path to the file to read (relative or absolute)"},
          "offset": {"type": "number", "description": "Line number to start reading from (1-indexed)"},
          "limit": {"type": "number", "description": "Maximum number of lines to read"}}, ["path"]),
    tool("write", "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Automatically creates parent directories.",
         {"path": {"type": "string", "description": "Path to the file to write (relative or absolute)"},
          "content": {"type": "string", "description": "Content to write to the file"}}, ["path", "content"]),
    tool("edit", "Edit a single file using exact text replacement. Every edits[].oldText must match a unique, non-overlapping region of the original file. If two changes affect the same block or nearby lines, merge them into one edit instead of emitting overlapping edits. Do not include large unchanged regions just to connect distant changes.",
         {"path": {"type": "string", "description": "Path to the file to edit (relative or absolute)"},
          "edits": {"type": "array", "description": "One or more targeted replacements. Each edit is matched against the original file, not incrementally. Do not include overlapping or nested edits. If two changes touch the same block or nearby lines, merge them into one edit instead.", "items": {
              "type": "object", "required": ["oldText", "newText"], "properties": {
                  "oldText": {"type": "string", "description": "Exact text for one targeted replacement. It must be unique in the original file and must not overlap with any other edits[].oldText in the same call."},
                  "newText": {"type": "string", "description": "Replacement text for this targeted edit."}}}}}, ["path", "edits"]),
    tool("run_python", "Execute Python in the output workspace. Supply exactly one of code, script, module. Defaults to the existing backend's bundled Python; the Skill root is importable. Explicitly choose runtime=system (optionally python_path) if a dependency is unavailable; never auto-install packages or silently fall back. Commands run as the local user, not in a sandbox. Never execute game EXE/DLLs or overwrite source files. Output is bounded; full output is in log_path. Nonzero exit code means failure.",
         {"code": {"type": "string", "maxLength": 60000}, "script": PATH,
          "module": {"type": "string", "minLength": 1, "maxLength": 256},
          "runtime": {"type": "string", "enum": ["bundled", "system"]}, "python_path": PATH,
          "args": {"type": "array", "maxItems": 16, "items": {"type": "string", "maxLength": 1024}},
          "timeout_seconds": TIMEOUT}),
    tool("bash", "Execute a bash command in the current working directory. Returns stdout and stderr. Output is truncated to last 2000 lines or 50KB (whichever is hit first). If truncated, full output is saved to a temp file. Optionally provide a timeout in seconds.",
         {"command": {"type": "string", "description": "Shell command to execute"},
          "timeout": {"type": "number", "description": "Timeout in seconds (optional, no default timeout)"}}, ["command"]),
    tool("ask_user", "Ask the user for a required choice that cannot be inferred. Supply 1-4 questions, each with options and a recommended option matching one of them. The user can also enter a custom answer. In full-auto no-interruption mode the recommended option is selected automatically; without a recommendation the first option is used.",
         next(item for item in AGENT_TOOLS if item["function"]["name"] == "ask_user")["function"]["parameters"]["properties"], ["questions"]),
    next(item for item in AGENT_TOOLS if item["function"]["name"] == "read_history_archive"),
]

PROMPT_SNIPPETS = {
    "read": "Read file contents",
    "bash": "Execute bash commands (ls, grep, find, etc.)",
    "edit": "Make precise file edits with exact text replacement, including multiple disjoint edits in one call",
    "write": "Create or overwrite files",
}
# Pi's bash guideline about PI_* variables is omitted: this port does not export them.
PROMPT_GUIDELINES = {
    "read": ["Use read to examine files instead of cat or sed."],
    "bash": [],
    "edit": [
        "Use edit for precise changes (edits[].oldText must match exactly)",
        "When changing multiple separate locations in one file, use one edit call with multiple entries in edits[] instead of multiple edit calls",
        "Each edits[].oldText is matched against the original file, not after earlier edits are applied. Do not emit overlapping or nested edits. Merge nearby changes into one edit.",
        "Keep edits[].oldText as small as possible while still being unique in the file. Do not pad with large unchanged regions.",
    ],
    "write": ["Use write only for new files or complete rewrites."],
}


def tool_prompt_section():
    """Pi's 'Available tools' and 'Guidelines' blocks, built from the tool contributions."""
    names = [name for name in ("read", "bash", "edit", "write") if PROMPT_SNIPPETS.get(name)]
    # Same order as Pi: file-exploration hint (bash without grep/find/ls), tool guidelines, fixed trailer.
    guidelines = ["Use bash for file operations like ls, rg, find"]
    for name in names:
        guidelines.extend(PROMPT_GUIDELINES[name])
    guidelines.extend(["Be concise in your responses", "Show file paths clearly when working with files"])
    unique = []
    for guideline in (item.strip() for item in guidelines):
        if guideline and guideline not in unique:
            unique.append(guideline)
    tools = "\n".join(f"- {name}: {PROMPT_SNIPPETS[name]}" for name in names)
    return (f"Available tools:\n{tools}\n\nIn addition to the tools above, you may have access to other custom tools "
            "depending on the project.\n\nGuidelines:\n" + "\n".join(f"- {item}" for item in unique))


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
