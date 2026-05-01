"""
Tool registry — tool loading, metadata extraction, dependency management

Tools path: /opt/physicar/agent/tools/
Virtualenv: /opt/physicar/agent/venv/
Dependencies: /opt/physicar/agent/deps.json

Supports PEP 723 inline script metadata:
# /// script
# dependencies = ["requests>=2.28", "pillow"]
# ///
"""

import os
import sys
import re
import json
import shutil
import inspect
import subprocess
import importlib.util
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, get_type_hints, get_origin, get_args

# Annotated / Field support
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


AGENT_PATH = Path("/opt/physicar/agent")
TOOLS_PATH = AGENT_PATH / "tools"
VENV_PATH = AGENT_PATH / "venv"
DEPS_FILE = AGENT_PATH / "deps.json"
BUILTIN_PATH = Path(__file__).parent / "builtin"

# Reserved system tool names (cannot be registered)
SYSTEM_TOOL_NAMES = frozenset([
    "tool_list",
    "tool_get",
    "tool_set", 
    "tool_delete",
    "tool_reset",
])

# System tool metadata (tool_list, tool_reset excluded — recursive)
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
        "description": """Register or update a tool (Python code)

## Code structure
```python
# /// script
# dependencies = ["package1", "package2"]  # PEP 723 (optional)
# ///
\"\"\"Tool description (used as the tool description)\"\"\"

from typing import Annotated
from pydantic import Field
from physicar_agent import topic, service, action
from std_msgs.msg import Float64
import time

def tool(arg1: Annotated[str, Field(description="description")], ...) -> list:
    \"\"\"The docstring is also included in the description\"\"\"
    
    # Topic read/write
    odom = topic.get('/odom', {})
    topic.pub('/speed', Float64(data=0.5))
    
    # Call service / action
    result = service('/some_service', {'param': 1})
    
    # Wait
    time.sleep(0.1)
    
    # Return
    return [{"type": "text", "text": "done"}]
```

## Available API (from physicar_agent import ...)
- topic['/name'] / topic.get('/name', default): topic data (dict)
- topic.pub('/name', msg): publish a ROS2 message
- topic.list(): list of (topic_name, type) tuples
- service('/name', {req}): call a service → dict
- action('/name', {goal}): call an action (blocking) → dict

## Return value (list)
- text: {"type": "text", "text": "content"}
- image: {"type": "image", "mime": "image/jpeg", "base64": "..."}
- auto conversions: str → [{"type": "text", ...}], dict → [{"type": "text", "text": JSON}], bytes → [{"type": "image", ...}]

## Validation: Python syntax → def tool() exists → reserved-name check → install deps → load test""",
        "properties": [
            {"name": "name", "type": "string", "description": "Tool name (reserved words not allowed: tool_list, tool_get, tool_set, tool_delete, tool_reset)", "required": True},
            {"name": "code", "type": "string", "description": "Python source code", "required": True}
        ]
    },
    {
        "name": "tool_delete",
        "description": "Delete a registered tool and clean up dependencies",
        "properties": [
            {"name": "name", "type": "string", "description": "Name of the tool to delete", "required": True}
        ]
    },
]

# Tool cache: {name: callable}
_tool_cache: Dict[str, Callable] = {}
_metadata_cache: List[Dict] = []


# ============================================
# Virtualenv and dependency management
# ============================================

def _ensure_venv():
    """Create the tools-dedicated virtualenv (if missing)"""
    if VENV_PATH.exists():
        return
    
    AGENT_PATH.mkdir(parents=True, exist_ok=True)
    
    # Create with system-site-packages so ROS2 packages (rclpy etc.) are visible
    subprocess.run(
        [sys.executable, "-m", "venv", "--system-site-packages", str(VENV_PATH)],
        check=True
    )


def _get_venv_pip() -> str:
    """Path to the venv's pip"""
    return str(VENV_PATH / "bin" / "pip")


def _get_venv_python() -> str:
    """Path to the venv's python"""
    return str(VENV_PATH / "bin" / "python")


def _load_deps() -> Dict[str, List[str]]:
    """Load deps.json: {package: [tool1, tool2, ...]}"""
    if not DEPS_FILE.exists():
        return {}
    try:
        return json.loads(DEPS_FILE.read_text())
    except Exception:
        return {}


def _save_deps(deps: Dict[str, List[str]]):
    """Save deps.json"""
    AGENT_PATH.mkdir(parents=True, exist_ok=True)
    DEPS_FILE.write_text(json.dumps(deps, indent=2))


def _parse_pep723(code: str) -> List[str]:
    """
    Extract dependencies from PEP 723 script metadata
    
    # /// script
    # dependencies = ["requests", "pillow>=9.0"]
    # ///
    """
    pattern = r'# /// script\s*\n((?:# .*\n)*?)# ///'
    match = re.search(pattern, code)
    if not match:
        return []
    
    block = match.group(1)
    # Parse dependencies = [...]
    dep_pattern = r'# dependencies\s*=\s*\[(.*?)\]'
    dep_match = re.search(dep_pattern, block, re.DOTALL)
    if not dep_match:
        return []
    
    # Parse the list of strings
    deps_str = dep_match.group(1)
    deps = re.findall(r'["\']([^"\']+)["\']', deps_str)
    return deps


def _normalize_package_name(dep: str) -> str:
    """Extract just the package name (drop the version)"""
    # requests>=2.28 → requests
    return re.split(r'[<>=!~\[]', dep)[0].strip().lower().replace('-', '_')


def _install_dependencies(tool_name: str, dependencies: List[str]) -> bool:
    """
    Install dependencies + update reference counts
    
    Returns:
        success flag
    """
    if not dependencies:
        return True
    
    _ensure_venv()
    deps = _load_deps()
    
    to_install = []
    for dep in dependencies:
        pkg_name = _normalize_package_name(dep)
        
        # Check whether it's already installed
        if pkg_name not in deps:
            to_install.append(dep)
        
        # Update reference counts
        if pkg_name not in deps:
            deps[pkg_name] = []
        if tool_name not in deps[pkg_name]:
            deps[pkg_name].append(tool_name)
    
    # Install
    if to_install:
        try:
            subprocess.run(
                [_get_venv_pip(), "install", "--quiet"] + to_install,
                check=True,
                capture_output=True
            )
        except subprocess.CalledProcessError as e:
            return False
    
    _save_deps(deps)
    return True


def _uninstall_dependencies(tool_name: str):
    """
    Decrement refcount on tool deletion; uninstall when it reaches 0
    """
    deps = _load_deps()
    
    to_remove = []
    for pkg_name, tools in list(deps.items()):
        if tool_name in tools:
            tools.remove(tool_name)
            if not tools:
                to_remove.append(pkg_name)
                del deps[pkg_name]
    
    # Remove unreferenced packages
    if to_remove and VENV_PATH.exists():
        try:
            subprocess.run(
                [_get_venv_pip(), "uninstall", "-y", "--quiet"] + to_remove,
                check=True,
                capture_output=True
            )
        except subprocess.CalledProcessError:
            pass  # Ignore uninstall failures
    
    _save_deps(deps)


def _add_venv_to_path():
    """Add the venv's site-packages to sys.path"""
    if not VENV_PATH.exists():
        return
    
    # venv site-packages path
    venv_site = VENV_PATH / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    if venv_site.exists() and str(venv_site) not in sys.path:
        sys.path.insert(0, str(venv_site))


def _python_type_to_json(py_type) -> Optional[str]:
    """Convert a Python type to a JSON type-name string"""
    if py_type is None:
        return None
    
    origin = get_origin(py_type)
    if origin is not None:
        # Generic types (List, Dict, …)
        if origin is list:
            return "array"
        if origin is dict:
            return "object"
        # Annotated[T, ...] → extract T
        if Annotated and origin is Annotated:
            args = get_args(py_type)
            if args:
                return _python_type_to_json(args[0])
        # Union (incl. Optional) → first non-None type
        import typing
        if origin is typing.Union:
            args = get_args(py_type)
            for arg in args:
                if arg is not type(None):
                    return _python_type_to_json(arg)
    
    # Primitive types
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
    """Extract description from Annotated[..., Field(description=...)]"""
    if not Annotated or not FieldInfo:
        return None
    
    origin = get_origin(annotation)
    if origin is not Annotated:
        return None
    
    args = get_args(annotation)
    for arg in args[1:]:  # first arg is the type itself
        if isinstance(arg, FieldInfo) and arg.description:
            return arg.description
    
    return None


def extract_metadata(filepath: str) -> Optional[Dict]:
    """
    Extract metadata from a tool file
    
    Returns:
        {
            "name": "control",
            "description": "Robot motion control",
            "properties": [
                {"name": "speed", "type": "number", "description": "...", "required": false},
                ...
            ]
        }
    """
    filepath = Path(filepath)
    if not filepath.exists() or filepath.suffix != '.py':
        return None
    
    name = filepath.stem
    if name.startswith('_'):
        return None
    
    # Load module
    spec = importlib.util.spec_from_file_location(name, filepath)
    if spec is None or spec.loader is None:
        return None
    
    module = importlib.util.module_from_spec(spec)
    
    # Add the core module to sys.modules (so imports work)
    if 'physicar_agent.core' not in sys.modules:
        try:
            from . import core
            sys.modules['physicar_agent.core'] = core
            sys.modules['physicar_agent'] = sys.modules[__name__.rsplit('.', 1)[0]]
        except ImportError:
            pass
    
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    
    # Find the `tool` function
    if not hasattr(module, 'tool'):
        return None
    
    func = module.tool
    if not callable(func):
        return None
    
    # Extract metadata
    metadata = {"name": name}
    
    # Description from docstring (full docstring included)
    if func.__doc__:
        doc = func.__doc__.strip()
        if doc:
            metadata["description"] = doc
    
    # Extract parameters
    sig = inspect.signature(func)
    
    try:
        type_hints = get_type_hints(func, include_extras=True)
    except Exception:
        type_hints = {}
    
    properties = []
    for param_name, param in sig.parameters.items():
        if param_name in ('self', 'cls'):
            continue
        
        prop = {"name": param_name}
        
        # Extract type
        annotation = type_hints.get(param_name, param.annotation)
        if annotation != inspect.Parameter.empty:
            json_type = _python_type_to_json(annotation)
            if json_type:
                prop["type"] = json_type
            
            # Extract description from Annotated
            desc = _extract_field_description(annotation)
            if desc:
                prop["description"] = desc
        
        # required?
        prop["required"] = param.default is inspect.Parameter.empty
        
        properties.append(prop)
    
    if properties:
        metadata["properties"] = properties
    
    return metadata


def _copy_builtin_tools(overwrite: bool = True) -> int:
    """Copy builtin tools into TOOLS_PATH.

    With overwrite=False, existing files are kept (user edits preserved).
    """
    count = 0
    if not BUILTIN_PATH.exists():
        return count

    for src in BUILTIN_PATH.glob("*.py"):
        if src.stem.startswith('_'):
            continue
        dst = TOOLS_PATH / src.name
        if dst.exists() and not overwrite:
            continue
        shutil.copy2(src, dst)
        count += 1

    return count


def _load_tool(name: str) -> Optional[Callable]:
    """Load a tool from a file (install dependencies if missing)"""
    filepath = TOOLS_PATH / f"{name}.py"
    if not filepath.exists():
        return None
    
    # Read source code
    try:
        code = filepath.read_text(encoding='utf-8')
    except Exception as e:
        print(f"[registry] Failed to read {filepath}: {e}")
        return None
    
    # Parse PEP 723 dependencies and install (only if not yet installed)
    dependencies = _parse_pep723(code)
    if dependencies:
        deps = _load_deps()
        # Skip already-registered tools (already handled in set_tool)
        # deps = {package: [tool1, tool2, ...]}
        tool_registered = any(name in tools for tools in deps.values())
        if not tool_registered:
            _install_dependencies(name, dependencies)
    
    # Add the venv's site-packages to sys.path
    _add_venv_to_path()
    
    spec = importlib.util.spec_from_file_location(name, filepath)
    if spec is None or spec.loader is None:
        print(f"[registry] Failed to create spec for {filepath}")
        return None
    
    module = importlib.util.module_from_spec(spec)
    
    # Add the core module to sys.modules (so imports work)
    if 'physicar_agent.core' not in sys.modules:
        try:
            from . import core
            sys.modules['physicar_agent.core'] = core
            sys.modules['physicar_agent'] = sys.modules[__name__.rsplit('.', 1)[0]]
        except ImportError:
            pass
    
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        print(f"[registry] Failed to load tool '{name}': {e}")
        return None
    
    tool_func = getattr(module, 'tool', None)
    if tool_func is None:
        print(f"[registry] Tool '{name}' has no 'tool' function")
    
    return tool_func


def reload_tools() -> int:
    """
    Reload all tools (refresh the cache)
    
    Returns:
        number of tools loaded
    """
    global _tool_cache, _metadata_cache
    
    # Initialise the folder if it doesn't exist
    if not TOOLS_PATH.exists():
        TOOLS_PATH.mkdir(parents=True, exist_ok=True)
        _copy_builtin_tools()
    
    # Clear cache
    _tool_cache.clear()
    _metadata_cache.clear()
    
    # Load all tools
    for filepath in sorted(TOOLS_PATH.glob("*.py")):
        if filepath.stem.startswith('_'):
            continue
        
        name = filepath.stem
        
        # Cache the function
        func = _load_tool(name)
        if func:
            _tool_cache[name] = func
        
        # Cache the metadata
        metadata = extract_metadata(str(filepath))
        if metadata:
            _metadata_cache.append(metadata)
    
    return len(_tool_cache)


def _verify_and_install_dependencies() -> Dict[str, str]:
    """
    Validate every tool's dependencies and install anything missing
    
    Returns:
        {tool_name: error_message} — tools with issues
    """
    errors = {}
    
    if not TOOLS_PATH.exists():
        return errors
    
    _ensure_venv()
    
    for filepath in sorted(TOOLS_PATH.glob("*.py")):
        if filepath.stem.startswith('_'):
            continue
        
        name = filepath.stem
        
        try:
            code = filepath.read_text(encoding='utf-8')
        except Exception as e:
            errors[name] = f"Failed to read: {e}"
            continue
        
        # Parse PEP 723 dependencies
        dependencies = _parse_pep723(code)
        if not dependencies:
            continue
        
        # Install dependencies (skips ones already installed)
        if not _install_dependencies(name, dependencies):
            errors[name] = f"Failed to install dependencies: {dependencies}"
    
    return errors


def init_tools() -> int:
    """
    Initialise the tools folder + validate/install dependencies + load all tools
    - If the folder is missing: create it + copy builtins
    - Validate dependencies for all tools and install anything missing
    - Load all tools into the cache
    
    Returns:
        number of tools loaded
    """
    # Initialise the folder + auto-copy missing builtins (existing files preserved)
    TOOLS_PATH.mkdir(parents=True, exist_ok=True)
    _copy_builtin_tools(overwrite=False)
    
    # Create the venv up-front
    _ensure_venv()
    
    # Validate and install dependencies for existing tools
    dep_errors = _verify_and_install_dependencies()
    if dep_errors:
        for tool_name, error in dep_errors.items():
            print(f"[registry] Warning: Tool '{tool_name}' dependency issue: {error}")
    
    # Load tools
    return reload_tools()


def reset_tools() -> int:
    """
    Reset tools (factory reset) + refresh the cache
    - Remove tools folder + recreate + copy builtins
    - Reset venv and deps.json
    
    Returns:
        number of tools copied
    """
    # Reset tools folder
    if TOOLS_PATH.exists():
        shutil.rmtree(TOOLS_PATH)
    
    # Reset venv
    if VENV_PATH.exists():
        shutil.rmtree(VENV_PATH)
    
    # Reset deps.json
    if DEPS_FILE.exists():
        DEPS_FILE.unlink()
    
    TOOLS_PATH.mkdir(parents=True, exist_ok=True)
    _copy_builtin_tools()
    return reload_tools()


def list_tools(include_system: bool = False) -> List[Dict]:
    """
    Return the full list of tools (uses cache)
    
    Args:
        include_system: whether to include system tools (default: False)
    
    Returns:
        Tool[] array
    """
    if not _metadata_cache:
        reload_tools()
    
    result = _metadata_cache.copy()
    
    if include_system:
        result = SYSTEM_TOOLS_METADATA + result
    
    return result


def is_system_tool(name: str) -> bool:
    """Check whether this is a reserved system tool"""
    return name in SYSTEM_TOOL_NAMES


def get_tool_code(name: str) -> Optional[str]:
    """
    Return the source code of a tool
    
    Args:
        name: tool name
    
    Returns:
        source-code string, or None
    """
    # System tools have no source code
    if is_system_tool(name):
        return None
    
    filepath = TOOLS_PATH / f"{name}.py"
    if not filepath.exists():
        return None
    
    try:
        return filepath.read_text(encoding='utf-8')
    except Exception:
        return None


def get_tool_info(name: str) -> Optional[Dict]:
    """
    Return tool details (metadata + code)
    
    Args:
        name: tool name
    
    Returns:
        {"name", "description", "properties", "code"?} or None
    """
    # System tool
    if is_system_tool(name):
        for meta in SYSTEM_TOOLS_METADATA:
            if meta.get("name") == name:
                return meta.copy()
        return None
    
    # Regular tool
    if not _metadata_cache:
        reload_tools()
    
    for meta in _metadata_cache:
        if meta.get("name") == name:
            result = meta.copy()
            # Attach code
            code = get_tool_code(name)
            if code:
                result["code"] = code
            return result
    
    return None


def get_tool(name: str) -> Optional[Callable]:
    """
    Return the tool function (uses cache)
    
    Args:
        name: tool name
    
    Returns:
        tool function, or None
    """
    if not _tool_cache:
        reload_tools()
    
    return _tool_cache.get(name)


class SetToolResult:
    """set_tool result — success flag and detailed messages"""
    def __init__(self, success: bool, message: str = "", warnings: List[str] = None):
        self.success = success
        self.message = message
        self.warnings = warnings or []
    
    def __bool__(self):
        return self.success


def _validate_tool_syntax(code: str, name: str) -> Optional[str]:
    """
    Validate tool source-code syntax
    
    Returns:
        error message, or None on success
    """
    import ast
    import traceback
    
    # 1. Validate Python syntax
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"Syntax error at line {e.lineno}: {e.msg}"
    
    # 2. Confirm a `def tool()` function exists
    tool_found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == 'tool':
            tool_found = True
            break
    
    if not tool_found:
        return "Missing 'def tool(...)' function"
    
    return None


def _validate_tool_load(name: str, filepath: Path) -> Optional[str]:
    """
    Actually try loading the tool
    
    Returns:
        error message, or None on success
    """
    import traceback
    
    # Add the venv's site-packages to sys.path
    _add_venv_to_path()
    
    spec = importlib.util.spec_from_file_location(name, filepath)
    if spec is None or spec.loader is None:
        return f"Failed to create module spec for {filepath}"
    
    module = importlib.util.module_from_spec(spec)
    
    # Add the core module to sys.modules (so imports work)
    if 'physicar_agent.core' not in sys.modules:
        try:
            from . import core
            sys.modules['physicar_agent.core'] = core
            sys.modules['physicar_agent'] = sys.modules[__name__.rsplit('.', 1)[0]]
        except ImportError:
            pass
    
    try:
        spec.loader.exec_module(module)
    except ImportError as e:
        return f"Import error: {e} (missing dependency?)"
    except Exception as e:
        tb = traceback.format_exc()
        return f"Load error: {e}\n{tb}"
    
    if not hasattr(module, 'tool') or not callable(module.tool):
        return "Module loaded but 'tool' is not callable"
    
    return None


def set_tool(name: str, code: str) -> SetToolResult:
    """
    Set a tool (add/overwrite) + install dependencies + refresh the cache
    
    Args:
        name: tool name
        code: Python source code (PEP 723 dependencies supported)
    
    Returns:
        SetToolResult(success, message, warnings)
        - success: True if the tool loads successfully
        - message: success/failure message
        - warnings: warnings list (e.g. dependency-install failures)
    """
    warnings = []
    
    # 0. Validate reserved names
    if is_system_tool(name):
        return SetToolResult(False, f"'{name}' is a reserved system tool name and cannot be overwritten")
    
    if not TOOLS_PATH.exists():
        reload_tools()
    
    # 1. Syntax validation (before saving)
    syntax_error = _validate_tool_syntax(code, name)
    if syntax_error:
        return SetToolResult(False, syntax_error)
    
    filepath = TOOLS_PATH / f"{name}.py"
    
    try:
        # Remove existing dependencies (on update)
        if filepath.exists():
            _uninstall_dependencies(name)
        
        # 2. Save file
        filepath.write_text(code, encoding='utf-8')
        
        # 3. Install dependencies
        dependencies = _parse_pep723(code)
        if dependencies:
            if not _install_dependencies(name, dependencies):
                warnings.append(f"Failed to install some dependencies: {dependencies}")
        
        # 4. Actual load test (with dependencies)
        load_error = _validate_tool_load(name, filepath)
        if load_error:
            # Remove file (rollback)
            filepath.unlink(missing_ok=True)
            _uninstall_dependencies(name)
            return SetToolResult(False, load_error, warnings)
        
        # 5. Refresh cache
        reload_tools()
        
        msg = f"Tool '{name}' saved and loaded successfully"
        if warnings:
            msg += f" (with warnings)"
        
        return SetToolResult(True, msg, warnings)
        
    except Exception as e:
        import traceback
        return SetToolResult(False, f"Unexpected error: {e}\n{traceback.format_exc()}")


def delete_tool(name: str) -> bool:
    """
    Delete tool + clean up dependencies + refresh the cache
    
    Args:
        name: tool name
    
    Returns:
        success flag
    """
    filepath = TOOLS_PATH / f"{name}.py"
    
    if not filepath.exists():
        return False
    
    try:
        # Decrement dependency refcount and remove if needed
        _uninstall_dependencies(name)
        
        filepath.unlink()
        # Refresh cache
        reload_tools()
        return True
    except Exception:
        return False


def _is_text_object(obj) -> bool:
    """Check whether the value is a text object"""
    return isinstance(obj, dict) and obj.get('type') == 'text' and 'text' in obj


def _is_image_object(obj) -> bool:
    """Check whether the value is an image object"""
    return isinstance(obj, dict) and obj.get('type') == 'image' and 'base64' in obj


def _normalize_text(obj) -> Dict:
    """Normalise a text object — keep only allowed keys"""
    return {"type": "text", "text": str(obj.get('text', ''))}


def _normalize_image(obj) -> Dict:
    """Normalise an image object — keep only allowed keys"""
    return {
        "type": "image",
        "mime": str(obj.get('mime', 'image/jpeg')),
        "base64": str(obj.get('base64', ''))
    }


def _normalize_item(item) -> Dict:
    """Normalise a single item"""
    if _is_text_object(item):
        return _normalize_text(item)
    if _is_image_object(item):
        return _normalize_image(item)
    
    # Try image conversion
    img = _to_image_object(item)
    if img:
        return img
    
    # Otherwise, treat as text
    return _to_text_object(item)


def _to_image_object(data) -> Optional[Dict]:
    """Try to convert image data into an image object"""
    import base64 as b64
    
    # bytes → image
    if isinstance(data, bytes):
        return {"type": "image", "mime": "image/jpeg", "base64": b64.b64encode(data).decode()}
    
    # ROS2 CompressedImage
    if hasattr(data, 'data') and hasattr(data, 'format'):
        fmt = data.format.lower() if data.format else 'jpeg'
        return {"type": "image", "mime": f"image/{fmt}", "base64": b64.b64encode(bytes(data.data)).decode()}
    
    # PIL Image
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
    
    # numpy array
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
    """Convert data into a text object"""
    import json
    if isinstance(data, str):
        return {"type": "text", "text": data}
    elif isinstance(data, dict):
        return {"type": "text", "text": json.dumps(data, ensure_ascii=False)}
    else:
        return {"type": "text", "text": str(data)}


def _wrap_result(result) -> List[Dict]:
    """
    Wrap and normalise tool return values into the standard response format
    
    Standard format: [{"type": "text", "text": "..."}, {"type": "image", "mime": "...", "base64": "..."}]
    
    Conversion rules:
    - list → normalise each item
    - single text/image object → normalise and wrap in a list
    - image data (bytes, PIL, numpy, CompressedImage) → [image object]
    - otherwise → [text object]
    
    All outputs are normalised to include only allowed keys
    """
    # None
    if result is None:
        return [{"type": "text", "text": "null"}]
    
    # List input — normalise each item
    if isinstance(result, list):
        if not result:
            return [{"type": "text", "text": "[]"}]
        return [_normalize_item(item) for item in result]
    
    # Single-item normalisation
    return [_normalize_item(result)]


def call_tool(name: str, args: Dict[str, Any]) -> List[Dict]:
    """
    Call a tool
    
    Args:
        name: tool name
        args: argument dict
    
    Returns:
        Standard response format: [{"type": "text"|"image", ...}, ...]
    
    Raises:
        ValueError: tool not found
        Exception: error while running the tool
    """
    func = get_tool(name)
    if func is None:
        raise ValueError(f"Tool '{name}' not found")
    
    result = func(**args)
    return _wrap_result(result)
