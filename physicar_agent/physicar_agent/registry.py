"""
Tool registry — single-file tool loading, metadata extraction

Tools file : /home/physicar/physicar_ws/userdata/agent/tools.py
Builtin src: builtin_tools.py (shipped with the package)

Tools are discovered as public functions (no leading ``_``) inside
``tools.py``.  The function name becomes the tool name, the docstring
becomes the description, and ``Annotated[type, Field(description=...)]``
parameters become the tool schema.
"""

import sys
import ast
import types
import inspect
import shutil
import importlib
import importlib.util
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, get_type_hints, get_origin, get_args

try:
    from typing import Annotated
except ImportError:
    Annotated = None

try:
    from pydantic import Field
    from pydantic.fields import FieldInfo
except ImportError:
    Field = None
    FieldInfo = None


AGENT_PATH = Path("/home/physicar/physicar_ws/userdata/agent")
TOOLS_FILE = AGENT_PATH / "tools.py"
BUILTIN_FILE = Path(__file__).parent / "builtin_tools.py"

# Reserved system tool names (cannot be used as function names)
SYSTEM_TOOL_NAMES = frozenset([
    "tool_list",
    "tool_get",
    "tool_set",
    "tool_reload",
    "tool_reset",
])

# System tool metadata
SYSTEM_TOOLS_METADATA = [
    {
        "name": "tool_get",
        "description": "Look up details and source code of a registered tool",
        "properties": [
            {"name": "name", "type": "string", "description": "Tool name", "required": True}
        ]
    },
    {
        "name": "tool_set",
        "description": """Register or update tools by writing the entire tools.py file.

## Code structure
```python
\"\"\"Optional module docstring.\"\"\"

from typing import Annotated, Optional
from pydantic import Field
from physicar_agent import api, text, image
import time, math, base64

def my_tool(arg1: Annotated[str, Field(description="description")], ...) -> list:
    \"\"\"Tool description (used as the tool description).\"\"\"
    odom = api.get('/state/odom')
    api.post('/control/speed', value=0.5)
    time.sleep(0.1)
    return [text("done")]

def _helper():
    \"\"\"Private helper (underscore prefix = not registered as tool).\"\"\"
    pass
```

## Rules
- Each public function (no leading ``_``) becomes a tool
- Function name = tool name
- Docstring = tool description
- Parameters use ``Annotated[type, Field(description=...)]``
- Private helpers start with ``_``
- Return: list of text/image dicts, or str/dict/bytes (auto-converted)

## Available API (from physicar_agent import ...)
- api.get('/state/odom'): odometry (dict)
- api.get('/state/battery'): battery info
- api.get('/state/imu'): IMU data
- api.get('/state/lidar'): LiDAR scan
- api.get('/state/camera'): camera image (JPEG bytes)
- api.post('/control/speed', value=0.5): set speed (m/s)
- api.post('/control/steering', value=0.1): set steering (radians)
- api.post('/control/camera/pan', value=0.0): camera pan (radians)
- api.post('/control/camera/tilt', value=0.0): camera tilt (radians)
- api.post('/control/audio', ...): play audio
- text("content"): create text response
- image(data, mime): create image response""",
        "properties": [
            {"name": "code", "type": "string", "description": "Full Python source code for tools.py", "required": True}
        ]
    },
    {
        "name": "tool_reload",
        "description": "Reload tools from disk (reimport tools.py after external edits)",
        "properties": []
    },
    {
        "name": "tool_reset",
        "description": "Reset tools.py to builtin defaults and reload",
        "properties": []
    },
]

# Caches
_tool_cache: Dict[str, Callable] = {}
_metadata_cache: List[Dict] = []
_tools_module = None


# ============================================
# Type helpers
# ============================================

def _python_type_to_json(py_type) -> Optional[str]:
    if py_type is None:
        return None
    origin = get_origin(py_type)
    if origin is not None:
        if origin is list:
            return "array"
        if origin is dict:
            return "object"
        if Annotated and origin is Annotated:
            args = get_args(py_type)
            if args:
                return _python_type_to_json(args[0])
        import typing
        if origin is typing.Union:
            args = get_args(py_type)
            for arg in args:
                if arg is not type(None):
                    return _python_type_to_json(arg)
    if py_type is str:
        return "string"
    if py_type in (int, float):
        return "number"
    if py_type is bool:
        return "boolean"
    if py_type is list:
        return "array"
    if py_type is dict:
        return "object"
    return None


def _extract_field_description(annotation) -> Optional[str]:
    if not Annotated or not FieldInfo:
        return None
    origin = get_origin(annotation)
    if origin is not Annotated:
        return None
    args = get_args(annotation)
    for arg in args[1:]:
        if isinstance(arg, FieldInfo) and arg.description:
            return arg.description
    return None


# ============================================
# Metadata extraction from a function object
# ============================================

def _extract_func_metadata(name: str, func: Callable) -> Optional[Dict]:
    if not callable(func):
        return None

    metadata: Dict[str, Any] = {"name": name}

    if func.__doc__:
        doc = func.__doc__.strip()
        if doc:
            metadata["description"] = doc

    sig = inspect.signature(func)
    try:
        type_hints = get_type_hints(func, include_extras=True)
    except Exception:
        type_hints = {}

    properties = []
    for param_name, param in sig.parameters.items():
        if param_name in ('self', 'cls'):
            continue
        prop: Dict[str, Any] = {"name": param_name}

        annotation = type_hints.get(param_name, param.annotation)
        if annotation != inspect.Parameter.empty:
            json_type = _python_type_to_json(annotation)
            if json_type:
                prop["type"] = json_type
            desc = _extract_field_description(annotation)
            if desc:
                prop["description"] = desc

        prop["required"] = param.default is inspect.Parameter.empty
        properties.append(prop)

    if properties:
        metadata["properties"] = properties

    return metadata


# ============================================
# Module loading
# ============================================

_MODULE_NAME = "_physicar_agent_tools"


def _load_module(filepath: Path):
    """Load (or reload) a Python file as a module and return it."""
    global _tools_module

    # Ensure physicar_agent is importable
    if 'physicar_agent.core' not in sys.modules:
        try:
            from physicar_agent import core
            sys.modules['physicar_agent.core'] = core
            sys.modules['physicar_agent'] = sys.modules[__name__.rsplit('.', 1)[0]]
        except ImportError:
            pass

    # Remove old module to force fresh import
    sys.modules.pop(_MODULE_NAME, None)

    spec = importlib.util.spec_from_file_location(_MODULE_NAME, filepath)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot create module spec for {filepath}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    _tools_module = module
    return module


def _discover_tools(module) -> Dict[str, Callable]:
    """Return {name: func} for all public functions in the module."""
    tools = {}
    for name in dir(module):
        if name.startswith('_'):
            continue
        obj = getattr(module, name)
        if inspect.isfunction(obj) and obj.__module__ == _MODULE_NAME:
            tools[name] = obj
    return tools


# ============================================
# Public API
# ============================================

def _ensure_tools_file():
    """Create tools.py from builtin if it doesn't exist."""
    if TOOLS_FILE.exists():
        return
    AGENT_PATH.mkdir(parents=True, exist_ok=True)
    if BUILTIN_FILE.exists():
        shutil.copy2(BUILTIN_FILE, TOOLS_FILE)


def reload_tools() -> int:
    """Reload tools.py from disk, refresh caches.

    Returns number of tools loaded.
    """
    global _tool_cache, _metadata_cache

    _tool_cache.clear()
    _metadata_cache.clear()

    _ensure_tools_file()

    if not TOOLS_FILE.exists():
        return 0

    try:
        module = _load_module(TOOLS_FILE)
    except Exception as e:
        print(f"[registry] Failed to load {TOOLS_FILE}: {e}")
        return 0

    tools = _discover_tools(module)
    for name, func in sorted(tools.items()):
        _tool_cache[name] = func
        meta = _extract_func_metadata(name, func)
        if meta:
            _metadata_cache.append(meta)

    return len(_tool_cache)


def init_tools() -> int:
    """Initialise: sync builtin -> tools.py, load all tools."""
    AGENT_PATH.mkdir(parents=True, exist_ok=True)

    # Always overwrite tools.py with latest builtin on startup
    if BUILTIN_FILE.exists():
        shutil.copy2(BUILTIN_FILE, TOOLS_FILE)

    return reload_tools()


def reset_tools() -> int:
    """Reset tools.py to builtin defaults and reload."""
    if TOOLS_FILE.exists():
        TOOLS_FILE.unlink()
    if BUILTIN_FILE.exists():
        shutil.copy2(BUILTIN_FILE, TOOLS_FILE)
    return reload_tools()


def list_tools(include_system: bool = False) -> List[Dict]:
    if not _metadata_cache:
        reload_tools()
    result = _metadata_cache.copy()
    if include_system:
        result = SYSTEM_TOOLS_METADATA + result
    return result


def is_system_tool(name: str) -> bool:
    return name in SYSTEM_TOOL_NAMES


def get_tool_code(name: str) -> Optional[str]:
    """Return the source code of a single tool function."""
    if is_system_tool(name):
        return None
    func = _tool_cache.get(name)
    if func is None:
        if not _tool_cache:
            reload_tools()
        func = _tool_cache.get(name)
    if func is None:
        return None
    try:
        return inspect.getsource(func)
    except (OSError, TypeError):
        return None


def get_tools_file() -> Optional[str]:
    """Return the entire tools.py source code."""
    if not TOOLS_FILE.exists():
        return None
    try:
        return TOOLS_FILE.read_text(encoding='utf-8')
    except Exception:
        return None


def get_tool_info(name: str) -> Optional[Dict]:
    if is_system_tool(name):
        for meta in SYSTEM_TOOLS_METADATA:
            if meta.get("name") == name:
                return meta.copy()
        return None

    if not _metadata_cache:
        reload_tools()

    for meta in _metadata_cache:
        if meta.get("name") == name:
            result = meta.copy()
            code = get_tool_code(name)
            if code:
                result["code"] = code
            return result
    return None


def get_tool(name: str) -> Optional[Callable]:
    if not _tool_cache:
        reload_tools()
    return _tool_cache.get(name)


# ============================================
# set_tools — save entire tools.py + reload
# ============================================

class SetToolResult:
    def __init__(self, success: bool, message: str = "", tool_count: int = 0):
        self.success = success
        self.message = message
        self.tool_count = tool_count

    def __bool__(self):
        return self.success


def _validate_tools_code(code: str) -> Optional[str]:
    """Validate the entire tools.py source code.

    Returns error message or None on success.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error at line {e.lineno}: {e.msg}"

    public_funcs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and not node.name.startswith('_')
    ]
    if not public_funcs:
        return "No public functions found (need at least one function without leading underscore)"

    for node in public_funcs:
        if node.name in SYSTEM_TOOL_NAMES:
            return f"Function '{node.name}' uses a reserved system tool name"

    return None


def set_tools(code: str) -> SetToolResult:
    """Validate, test-load in memory, then save to tools.py and reload."""
    # 1. Static validation
    error = _validate_tools_code(code)
    if error:
        return SetToolResult(False, error)

    AGENT_PATH.mkdir(parents=True, exist_ok=True)

    # 2. Import test in memory
    try:
        compiled = compile(code, 'tools.py', 'exec')
        module = types.ModuleType(_MODULE_NAME)
        module.__file__ = str(TOOLS_FILE)

        # Ensure physicar_agent is importable
        if 'physicar_agent.core' not in sys.modules:
            try:
                from physicar_agent import core
                sys.modules['physicar_agent.core'] = core
                sys.modules['physicar_agent'] = sys.modules[__name__.rsplit('.', 1)[0]]
            except ImportError:
                pass

        exec(compiled, module.__dict__)
        tools = {
            name: obj for name, obj in vars(module).items()
            if not name.startswith('_') and inspect.isfunction(obj)
            and obj.__module__ == _MODULE_NAME
        }
    except Exception as e:
        return SetToolResult(False, f"Load error: {e}")

    if not tools:
        return SetToolResult(False, "No public functions found after loading")

    # 3. All checks passed — save and reload
    TOOLS_FILE.write_text(code, encoding='utf-8')
    count = reload_tools()
    return SetToolResult(True, f"Saved and loaded {count} tools", count)


# ============================================
# call_tool
# ============================================

def _is_text_object(obj) -> bool:
    return isinstance(obj, dict) and obj.get('type') == 'text' and 'text' in obj


def _is_image_object(obj) -> bool:
    return isinstance(obj, dict) and obj.get('type') == 'image' and 'base64' in obj


def _normalize_text(obj) -> Dict:
    return {"type": "text", "text": str(obj.get('text', ''))}


def _normalize_image(obj) -> Dict:
    return {
        "type": "image",
        "mime": str(obj.get('mime', 'image/jpeg')),
        "base64": str(obj.get('base64', ''))
    }


def _to_image_object(data) -> Optional[Dict]:
    import base64 as b64

    if isinstance(data, bytes):
        return {"type": "image", "mime": "image/jpeg", "base64": b64.b64encode(data).decode()}

    if hasattr(data, 'data') and hasattr(data, 'format'):
        fmt = data.format.lower() if data.format else 'jpeg'
        return {"type": "image", "mime": f"image/{fmt}", "base64": b64.b64encode(bytes(data.data)).decode()}

    try:
        from PIL import Image as PILImage
        if isinstance(data, PILImage.Image):
            import io
            buf = io.BytesIO()
            fmt = 'PNG' if data.mode == 'RGBA' else 'JPEG'
            data.save(buf, format=fmt)
            return {"type": "image", "mime": f"image/{fmt.lower()}", "base64": b64.b64encode(buf.getvalue()).decode()}
    except ImportError:
        pass

    try:
        import numpy as np
        if isinstance(data, np.ndarray):
            from PIL import Image as PILImage
            import io
            img = PILImage.fromarray(data)
            buf = io.BytesIO()
            fmt = 'PNG' if len(data.shape) > 2 and data.shape[2] == 4 else 'JPEG'
            img.save(buf, format=fmt)
            return {"type": "image", "mime": f"image/{fmt.lower()}", "base64": b64.b64encode(buf.getvalue()).decode()}
    except ImportError:
        pass

    return None


def _to_text_object(data) -> Dict:
    import json
    if isinstance(data, str):
        return {"type": "text", "text": data}
    elif isinstance(data, dict):
        return {"type": "text", "text": json.dumps(data, ensure_ascii=False)}
    else:
        return {"type": "text", "text": str(data)}


def _normalize_item(item) -> Dict:
    if _is_text_object(item):
        return _normalize_text(item)
    if _is_image_object(item):
        return _normalize_image(item)
    img = _to_image_object(item)
    if img:
        return img
    return _to_text_object(item)


def _wrap_result(result) -> List[Dict]:
    if result is None:
        return [{"type": "text", "text": "null"}]
    if isinstance(result, list):
        if not result:
            return [{"type": "text", "text": "[]"}]
        return [_normalize_item(item) for item in result]
    return [_normalize_item(result)]


def call_tool(name: str, args: Dict[str, Any]) -> List[Dict]:
    """Call a tool by name with given arguments.

    Raises ValueError if tool not found.
    """
    func = get_tool(name)
    if func is None:
        raise ValueError(f"Tool '{name}' not found")
    result = func(**args)
    return _wrap_result(result)
