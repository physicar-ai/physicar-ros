"""
Tool registry — single-file tool loading, metadata extraction

Tools file : /opt/physicar/userdata/agent/tools.py
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
import linecache
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


AGENT_PATH = Path("/opt/physicar/userdata/agent")
TOOLS_FILE = AGENT_PATH / "tools.py"
BUILTIN_FILE = Path(__file__).parent / "builtin_tools.py"

# Reserved system tool names (cannot be used as function names)
SYSTEM_TOOL_NAMES = frozenset([
    "tool_list",
    "tool_code",
    "tool_set",
    "tool_load",
    "tool_init",
])

# System tool metadata
SYSTEM_TOOLS_METADATA = [
    {
        "name": "tool_code",
        "description": "Get the full tools.py source code.",
        "properties": []
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
]

class LoadResult:
    def __init__(self, success: bool, message: str = "", tool_count: int = 0):
        self.success = success
        self.message = message
        self.tool_count = tool_count

    def __bool__(self):
        return self.success


# Caches — only updated on successful load
_tool_cache: Dict[str, Callable] = {}
_metadata_cache: List[Dict] = []
_loaded_source: Optional[str] = None


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


_MODULE_NAME = "_physicar_agent_tools"


def _clear():
    global _loaded_source
    _tool_cache.clear()
    _metadata_cache.clear()
    _loaded_source = None


def _try_load_code(code: str, source_path: str = "tools.py") -> LoadResult:
    """Validate and load a code string. Commits to caches on success only."""
    global _loaded_source

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return LoadResult(False, f"Syntax error at line {e.lineno}: {e.msg}")

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
            if node.name in SYSTEM_TOOL_NAMES:
                return LoadResult(False, f"Function '{node.name}' uses a reserved system tool name")

    try:
        compiled = compile(code, source_path, 'exec')
        module = types.ModuleType(_MODULE_NAME)
        module.__file__ = source_path

        if 'physicar_agent.core' not in sys.modules:
            try:
                from physicar_agent import core
                sys.modules['physicar_agent.core'] = core
                sys.modules['physicar_agent'] = sys.modules[__name__.rsplit('.', 1)[0]]
            except ImportError:
                pass

        exec(compiled, module.__dict__)
    except SyntaxError as e:
        return LoadResult(False, f"Syntax error at line {e.lineno}: {e.msg}")
    except Exception as e:
        import traceback
        tb = traceback.extract_tb(e.__traceback__)
        for frame in reversed(tb):
            if frame.filename == source_path:
                return LoadResult(False, f"line {frame.lineno}: {type(e).__name__}: {e}")
        return LoadResult(False, f"{type(e).__name__}: {e}")

    # ── Discover functions in definition order ──
    tools = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith('_'):
            name = node.name
            obj = getattr(module, name, None)
            if obj is not None and inspect.isfunction(obj) and obj.__module__ == _MODULE_NAME:
                tools[name] = obj

    if not tools:
        return LoadResult(False, "No public functions found")

    # ── Success — commit ──
    sys.modules[_MODULE_NAME] = module
    linecache.cache[source_path] = (len(code), None, code.splitlines(True), source_path)

    _tool_cache.clear()
    _metadata_cache.clear()
    _loaded_source = code

    for name, func in tools.items():
        _tool_cache[name] = func
        meta = _extract_func_metadata(name, func)
        if meta:
            _metadata_cache.append(meta)

    return LoadResult(True, f"Loaded {len(_tool_cache)} tools", len(_tool_cache))


def _try_load_file(source_file: Path) -> LoadResult:
    """Read a file and try loading it."""
    if not source_file.exists():
        return LoadResult(False, f"{source_file.name} not found")
    try:
        code = source_file.read_text(encoding='utf-8')
    except Exception as e:
        return LoadResult(False, f"Read error: {e}")
    return _try_load_code(code, str(source_file))


def load_tools() -> LoadResult:
    """Load tools from tools.py, fallback to builtin on first load failure.

    First load  (_loaded_source is None): tools.py fail → try builtin.
    Reload (_loaded_source set)         : fail → keep previous state.
    """
    first_load = _loaded_source is None

    source = TOOLS_FILE if TOOLS_FILE.exists() else BUILTIN_FILE
    result = _try_load_file(source)

    if result.success:
        return result

    # Failed — builtin fallback on first load only
    if first_load and source != BUILTIN_FILE and BUILTIN_FILE.exists():
        fallback = _try_load_file(BUILTIN_FILE)
        if fallback.success:
            fallback.message = f"tools.py error ({result.message}), loaded builtin"
            return fallback

    # First load total failure → clear
    if first_load:
        _clear()

    # Reload failure → caches untouched (previous state kept)
    return result


def set_tools(code: str) -> LoadResult:
    """Validate, load, then save to tools.py."""
    result = _try_load_code(code, str(TOOLS_FILE))
    if result.success:
        AGENT_PATH.mkdir(parents=True, exist_ok=True)
        TOOLS_FILE.write_text(code, encoding='utf-8')
    return result


def init_tools() -> LoadResult:
    """Delete agent folder and load builtin defaults."""
    import shutil
    if AGENT_PATH.exists():
        shutil.rmtree(AGENT_PATH)
    return load_tools()


def list_tools(include_system: bool = False) -> List[Dict]:
    result = _metadata_cache.copy()
    if include_system:
        result = SYSTEM_TOOLS_METADATA + result
    return result


def is_system_tool(name: str) -> bool:
    return name in SYSTEM_TOOL_NAMES


def get_tool_code(name: Optional[str] = None) -> Optional[str]:
    """Return source code from last successful load.

    name=None → full file, name given → single function.
    """
    if name is None:
        return _loaded_source
    if is_system_tool(name):
        return None
    func = _tool_cache.get(name)
    if func is None:
        return None
    try:
        return inspect.getsource(func)
    except (OSError, TypeError):
        return None


def get_tool_info(name: str) -> Optional[Dict]:
    if is_system_tool(name):
        for meta in SYSTEM_TOOLS_METADATA:
            if meta.get("name") == name:
                return meta.copy()
        return None
    for meta in _metadata_cache:
        if meta.get("name") == name:
            result = meta.copy()
            code = get_tool_code(name)
            if code:
                result["code"] = code
            return result
    return None


def get_tool(name: str) -> Optional[Callable]:
    return _tool_cache.get(name)


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
