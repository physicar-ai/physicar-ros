"""
DeepRacer API router.

Endpoints match ROS service paths:
    - /deepracer/load_model - Load a model for inference
    - /deepracer/control - Start/stop inference
    - /deepracer/status - Get current status
    - /deepracer/set_config - Set configuration

Model management (file-based):
    - /deepracer/models - List available models
    - /deepracer/models/import/* - Import a new model (chunked)
    - /deepracer/models/{model_name} - Get/delete a specific model

Model structure:
    /opt/physicar/userdata/deepracer/models/<model_name>/
        ├── model_metadata.json
        └── agent/
            ├── model.pb      # TensorFlow frozen graph
            └── model.tflite  # TFLite converted (created on first load)
"""
import json
import os
import shutil
import tarfile
import tempfile
import uuid
from pathlib import Path
from typing import Optional, List, Any, Dict

from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from physicar_webserver.ros_bridge import get_ros_bridge
from physicar_webserver.state_manager import get_state_manager

router = APIRouter(prefix="/deepracer", tags=["deepracer"])

# Constants
MODELS_DIR = Path("/opt/physicar/userdata/deepracer/models")
IMPORT_DIR = Path("/tmp/deepracer_imports")
REQUIRED_FILES = ["model_metadata.json", "model.pb"]
IMPORT_TIMEOUT_SEC = 600  # 10 minutes

# Pending chunked imports: import_id -> {filename, model_name, total_chunks, received_chunks, created_at}
_pending_imports: Dict[str, dict] = {}


def _cleanup_stale_imports():
    """Remove import sessions older than IMPORT_TIMEOUT_SEC."""
    import time
    now = time.time()
    stale = [uid for uid, sess in _pending_imports.items() 
             if now - sess.get("created_at", 0) > IMPORT_TIMEOUT_SEC]
    for uid in stale:
        del _pending_imports[uid]
        import_dir = IMPORT_DIR / uid
        if import_dir.exists():
            shutil.rmtree(import_dir, ignore_errors=True)


def _cancel_import(import_id: str):
    """Cancel and cleanup an import session."""
    if import_id in _pending_imports:
        del _pending_imports[import_id]
    import_dir = IMPORT_DIR / import_id
    if import_dir.exists():
        shutil.rmtree(import_dir, ignore_errors=True)


# ============================================================================
# Request/Response Models - aligned with ROS service definitions
# ============================================================================

# --- Load Model ---
class LoadModelRequest(BaseModel):
    """Request to load a model. Matches DeepracerLoadModel.srv"""
    model_name: str = Field(..., description="Name of model directory")


class LoadModelResponse(BaseModel):
    """Response from load model. Matches DeepracerLoadModel.srv"""
    success: bool
    message: str
    model_path: str = ""
    action_space_json: str = "[]"


# --- Unload Model ---
class UnloadModelRequest(BaseModel):
    """Request to unload a model. Matches DeepracerUnloadModel.srv"""
    model_name: str = Field("", description="Name of model to unload (empty = unload all)")


class UnloadModelResponse(BaseModel):
    """Response from unload model. Matches DeepracerUnloadModel.srv"""
    success: bool
    message: str


# --- Control ---
class ControlRequest(BaseModel):
    """Request to start/stop inference. Matches DeepracerControl.srv"""
    start: bool = Field(..., description="True to start inference, False to stop")


class ControlResponse(BaseModel):
    """Response from control. Matches DeepracerControl.srv"""
    success: bool
    message: str


# --- Status ---
class StatusResponse(BaseModel):
    """Response from status. Matches DeepracerStatus.srv"""
    success: bool = True
    model_loaded: bool = False
    inference_running: bool = False
    model_name: str = ""
    model_path: str = ""
    action_count: int = 0
    action_space_json: str = "[]"
    loaded_models_json: str = "[]"
    inference_rate: float = 0.0
    inference_count: int = 0
    last_action: str = ""
    # Configuration
    action_selection_mode: str = ""
    config_source: str = ""
    camera_pan: float = 0.0
    camera_tilt: float = 0.0
    speed_percent: float = 1.0


# --- Config ---
class SetConfigRequest(BaseModel):
    """Request to set config. Matches DeepracerSetConfig.srv"""
    key: str = Field(..., description="Config key: 'action_selection', 'pan', 'tilt', 'all'")
    string_value: str = Field("", description="String value (for action_selection: 'greedy' or 'stochastic')")
    float_value: float = Field(0.0, description="Float value (for pan/tilt in degrees)")
    save_to_file: bool = Field(False, description="Save config to file after applying")


class SetConfigResponse(BaseModel):
    """Response from set_config. Matches DeepracerSetConfig.srv"""
    success: bool
    message: str


# --- Model Management (file-based) ---
class ModelInfo(BaseModel):
    """Model file information."""
    name: str
    path: str
    has_metadata: bool
    has_model_pb: bool
    has_tflite: bool = False  # True if optimized tflite exists
    is_valid: bool = True  # True if model passes validation
    validation_error: str = ""  # Error message if validation failed
    sensors: List[str] = []  # e.g. ["FRONT_FACING_CAMERA"] or ["FRONT_FACING_CAMERA", "LIDAR"]


class ModelListResponse(BaseModel):
    """Response for model list."""
    success: bool
    models: List[ModelInfo]


class ModelDeleteResponse(BaseModel):
    """Response for model deletion."""
    success: bool
    message: str


# ============================================================================
# Helper Functions
# ============================================================================

def validate_model_archive(extract_path: Path) -> tuple[bool, str]:
    """Validate that the extracted model contains required files and valid content."""
    metadata_path = extract_path / "model_metadata.json"
    model_pb_path = extract_path / "model.pb"
    
    if not metadata_path.exists():
        return False, "Missing required file: model_metadata.json"
    
    if not model_pb_path.exists():
        return False, "Missing required file: model.pb"
    
    # Validate model_metadata.json
    try:
        with open(metadata_path, 'r') as f:
            metadata = json.load(f)
        
        # Check required fields
        if "action_space" not in metadata:
            return False, "model_metadata.json missing 'action_space' field"
        
        if not isinstance(metadata["action_space"], list) or len(metadata["action_space"]) == 0:
            return False, "action_space must be a non-empty list"
        
        for i, action in enumerate(metadata["action_space"]):
            if "speed" not in action or "steering_angle" not in action:
                return False, f"action_space[{i}] missing 'speed' or 'steering_angle'"
                
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON in model_metadata.json: {e}"
    except Exception as e:
        return False, f"Error reading model_metadata.json: {e}"
    
    # Validate model.pb - check file size and basic protobuf structure
    try:
        pb_size = model_pb_path.stat().st_size
        
        # DeepRacer models are typically 10MB+ when valid
        if pb_size < 10_000_000:  # Less than 10MB is suspicious
            return False, f"model.pb file too small ({pb_size} bytes), possibly truncated or corrupted"
        
        # Check protobuf header - TensorFlow GraphDef starts with field tag 0x0a
        with open(model_pb_path, 'rb') as f:
            header = f.read(100)
            
            # Basic protobuf structure check
            if len(header) < 10 or header[0] != 0x0a:
                return False, "model.pb does not appear to be a valid TensorFlow GraphDef"
            
            # Check for expected tensor names (DeepRacer specific)
            if b'main_level/agent' not in header:
                return False, "model.pb missing expected DeepRacer network structure"
                
    except Exception as e:
        return False, f"Error validating model.pb: {e}"
    
    return True, ""


def sanitize_model_name(model_name: str) -> bool:
    """Check if model name is safe."""
    return ".." not in model_name and "/" not in model_name and "\\" not in model_name


# ============================================================================
# ROS Service Endpoints
# ============================================================================

@router.post("/load_model", response_model=LoadModelResponse)
async def load_model(request: LoadModelRequest):
    """
    Load a DeepRacer model for inference.
    
    Loads the model into memory and sets it as the active model.
    If the model is already loaded, simply switches active (instant).
    Multiple models can be loaded simultaneously.
    
    Calls ROS service: /deepracer/load_model
    """
    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")
        
        result = await bridge.deepracer_load_model(request.model_name)
        _bump_status()
        return LoadModelResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DeepRacer service error: {str(e)}")


@router.post("/unload_model", response_model=UnloadModelResponse)
async def unload_model(request: UnloadModelRequest):
    """
    Unload a DeepRacer model from memory.
    
    If model_name is empty, unloads all models.
    Stops inference if the active model is unloaded.
    
    Calls ROS service: /deepracer/unload_model
    """
    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")
        
        result = await bridge.deepracer_unload_model(request.model_name)
        _bump_status()
        return UnloadModelResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DeepRacer service error: {str(e)}")


@router.post("/control", response_model=ControlResponse)
async def control(request: ControlRequest):
    """
    Start or stop DeepRacer inference.
    
    Calls ROS service: /deepracer/control
    """
    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")
        
        result = await bridge.deepracer_control(request.start)
        _bump_status()
        return ControlResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DeepRacer service error: {str(e)}")


@router.get("/status", response_model=StatusResponse)
async def status(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """
    Get current DeepRacer status.

    Default: one-shot JSON.
    With ?stream=true or Accept: text/event-stream — SSE stream that pushes
    on change (~1s poll interval, only emits when status changes).

    Calls ROS service: /deepracer/status
    """
    accept = request.headers.get("accept", "")
    wants_stream = bool(stream) or ("text/event-stream" in accept)

    if wants_stream:
        return StreamingResponse(
            _stream_status(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")

        result = await bridge.deepracer_status()
        return StatusResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DeepRacer service error: {str(e)}")


import asyncio as _asyncio


class _StatusBroadcaster:
    """
    Single shared poller for DeepRacer status.
    Many SSE clients can subscribe; only one ROS service call per poll.
    `bump()` forces an immediate poll (used after state-changing POSTs to
    propagate updates to other tabs in <100ms instead of waiting for
    the next interval).
    """
    POLL_INTERVAL = 2.0  # seconds (relaxed: bump() handles instantaneous updates)

    def __init__(self):
        self._last_payload: Optional[str] = None
        self._subscribers: set = set()  # set[asyncio.Queue]
        self._task: Optional[_asyncio.Task] = None
        self._wake = _asyncio.Event()

    def subscribe(self) -> "_asyncio.Queue":
        q: _asyncio.Queue = _asyncio.Queue(maxsize=8)
        self._subscribers.add(q)
        if self._last_payload is not None:
            try:
                q.put_nowait(self._last_payload)
            except _asyncio.QueueFull:
                pass
        self._ensure_running()
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)

    def bump(self):
        """Force immediate re-poll (call after state-changing operations)."""
        self._wake.set()

    def _ensure_running(self):
        if self._task is None or self._task.done():
            self._task = _asyncio.create_task(self._run())

    def _emit(self, payload: str):
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except _asyncio.QueueFull:
                pass

    async def _run(self):
        bridge = get_ros_bridge()
        try:
            while self._subscribers:
                try:
                    if bridge.is_ready:
                        result = await bridge.deepracer_status()
                        payload = json.dumps(result, default=str, sort_keys=True)
                        if payload != self._last_payload:
                            self._last_payload = payload
                            self._emit(payload)
                    elif self._last_payload != "__not_ready__":
                        self._last_payload = "__not_ready__"
                        self._emit('{"error": "ros_bridge_not_ready"}')
                except Exception as e:
                    self._emit(json.dumps({"error": str(e)}))
                # Sleep until next interval OR a bump() wake-up
                try:
                    await _asyncio.wait_for(self._wake.wait(), timeout=self.POLL_INTERVAL)
                except _asyncio.TimeoutError:
                    pass
                self._wake.clear()
        except _asyncio.CancelledError:
            pass


_status_broadcaster: Optional[_StatusBroadcaster] = None


def _get_broadcaster() -> _StatusBroadcaster:
    global _status_broadcaster
    if _status_broadcaster is None:
        _status_broadcaster = _StatusBroadcaster()
    return _status_broadcaster


def _bump_status():
    """Trigger an immediate status re-poll for all SSE subscribers."""
    try:
        _get_broadcaster().bump()
    except Exception:
        pass


async def _stream_status():
    """SSE generator: subscribe to shared broadcaster."""
    bcaster = _get_broadcaster()
    q = bcaster.subscribe()
    try:
        while True:
            payload = await q.get()
            yield f"data: {payload}\n\n"
    except _asyncio.CancelledError:
        pass
    finally:
        bcaster.unsubscribe(q)


@router.post("/set_config", response_model=SetConfigResponse)
async def set_config(request: SetConfigRequest):
    """
    Set DeepRacer configuration.
    
    Keys:
    - 'action_selection': 'greedy' or 'stochastic'
    - 'pan': camera pan angle in degrees (-30 to 30)
    - 'tilt': camera tilt angle in degrees (-30 to 30)
    - 'all': reload config from file
    
    Calls ROS service: /deepracer/set_config
    """
    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")
        
        result = await bridge.deepracer_set_config(
            key=request.key,
            string_value=request.string_value,
            float_value=request.float_value,
            save_to_file=request.save_to_file
        )
        _bump_status()
        return SetConfigResponse(**result)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DeepRacer service error: {str(e)}")


# ============================================================================
# Model File Management Endpoints
# ============================================================================

@router.get("/models", response_model=ModelListResponse)
async def list_models():
    """
    List all available DeepRacer models in /opt/physicar/userdata/deepracer/models/
    
    Returns validation status for each model.
    """
    models = []
    
    if not MODELS_DIR.exists():
        return ModelListResponse(success=True, models=[])
    
    for item in MODELS_DIR.iterdir():
        if item.is_dir():
            has_metadata = (item / "model_metadata.json").exists()
            has_model_pb = (item / "model.pb").exists()
            has_tflite = (item / "model.tflite").exists()
            
            # Read sensor list from metadata
            sensors = []
            if has_metadata:
                try:
                    with open(item / "model_metadata.json", 'r') as f:
                        meta = json.load(f)
                    sensors = meta.get("sensor", [])
                except Exception:
                    pass
            
            # Validate the model
            is_valid, validation_error = validate_model_archive(item)
            
            models.append(ModelInfo(
                name=item.name,
                path=str(item),
                has_metadata=has_metadata,
                has_model_pb=has_model_pb,
                has_tflite=has_tflite,
                is_valid=is_valid,
                validation_error=validation_error,
                sensors=sensors
            ))
    
    models.sort(key=lambda m: m.name)
    return ModelListResponse(success=True, models=models)


# ============================================================================
# Chunked Import Endpoints (to bypass Codespaces proxy size limits)
# ============================================================================

class ImportInitRequest(BaseModel):
    """Request to start a chunked import."""
    filename: str
    total_chunks: int
    model_name: Optional[str] = None


class ImportInitResponse(BaseModel):
    """Response with import session ID."""
    import_id: str


class ImportCompleteResponse(BaseModel):
    """Response after completing chunked import."""
    success: bool
    message: str
    model_name: Optional[str] = None
    model_path: Optional[str] = None


@router.post("/models/import/init", response_model=ImportInitResponse)
async def import_init(request: ImportInitRequest):
    """
    Initialize a chunked import session.
    Returns an import_id to use for subsequent chunk imports.
    """
    import time
    
    # Cleanup stale sessions first
    _cleanup_stale_imports()
    
    if not request.filename.endswith(".tar.gz"):
        raise HTTPException(status_code=400, detail="File must be a .tar.gz archive")
    
    # Determine model name
    model_name = request.model_name or request.filename[:-7]
    if not sanitize_model_name(model_name):
        raise HTTPException(status_code=400, detail="Invalid model name")
    
    import_id = str(uuid.uuid4())
    import_dir = IMPORT_DIR / import_id
    import_dir.mkdir(parents=True, exist_ok=True)
    
    _pending_imports[import_id] = {
        "filename": request.filename,
        "model_name": model_name,
        "total_chunks": request.total_chunks,
        "received_chunks": set(),
        "created_at": time.time()
    }
    
    return ImportInitResponse(import_id=import_id)


@router.post("/models/import/chunk")
async def import_chunk(
    import_id: str = Form(...),
    chunk_index: int = Form(...),
    chunk: UploadFile = File(...)
):
    """
    Import a single chunk of the file.
    Chunks can be sent in any order.
    """
    if import_id not in _pending_imports:
        raise HTTPException(status_code=404, detail="Import session not found")
    
    session = _pending_imports[import_id]
    if chunk_index < 0 or chunk_index >= session["total_chunks"]:
        raise HTTPException(status_code=400, detail="Invalid chunk index")
    
    import_dir = IMPORT_DIR / import_id
    chunk_path = import_dir / f"chunk_{chunk_index:04d}"
    
    try:
        content = await chunk.read()
        with open(chunk_path, "wb") as f:
            f.write(content)
        session["received_chunks"].add(chunk_index)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save chunk: {e}")
    
    return {
        "success": True,
        "chunk_index": chunk_index,
        "received": len(session["received_chunks"]),
        "total": session["total_chunks"]
    }


@router.post("/models/import/cancel")
async def import_cancel(import_id: str = Form(...)):
    """
    Cancel an import session and clean up temp files.
    """
    _cancel_import(import_id)
    return {"success": True, "message": "Import cancelled"}


@router.post("/models/import/complete", response_model=ImportCompleteResponse)
async def import_complete(import_id: str = Form(...)):
    """
    Complete the chunked import by assembling chunks and processing the model.
    """
    if import_id not in _pending_imports:
        raise HTTPException(status_code=404, detail="Import session not found")
    
    session = _pending_imports[import_id]
    
    # Check all chunks received
    if len(session["received_chunks"]) != session["total_chunks"]:
        missing = session["total_chunks"] - len(session["received_chunks"])
        raise HTTPException(status_code=400, detail=f"Missing {missing} chunks")
    
    import_dir = IMPORT_DIR / import_id
    final_model_name = session["model_name"]
    
    try:
        # Assemble chunks into final archive
        archive_path = import_dir / "model.tar.gz"
        with open(archive_path, "wb") as out_file:
            for i in range(session["total_chunks"]):
                chunk_path = import_dir / f"chunk_{i:04d}"
                with open(chunk_path, "rb") as chunk_file:
                    out_file.write(chunk_file.read())
        
        # Extract and validate
        extract_path = import_dir / "extracted"
        extract_path.mkdir()
        
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.startswith("/") or ".." in member.name:
                    raise HTTPException(status_code=400, detail="Archive contains unsafe paths")
            tar.extractall(path=extract_path)
        
        # Flatten agent/ subfolder: move agent/model.pb → model.pb
        agent_dir = extract_path / "agent"
        if agent_dir.is_dir():
            for f in agent_dir.iterdir():
                shutil.move(str(f), str(extract_path / f.name))
            agent_dir.rmdir()
        
        is_valid, error_msg = validate_model_archive(extract_path)
        if not is_valid:
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Install model
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        target_path = MODELS_DIR / final_model_name
        
        if target_path.exists():
            shutil.rmtree(target_path)
        
        shutil.move(str(extract_path), str(target_path))
        
    except HTTPException:
        raise
    except tarfile.TarError as e:
        raise HTTPException(status_code=400, detail=f"Failed to extract archive: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process import: {e}")
    finally:
        # Cleanup
        if import_id in _pending_imports:
            del _pending_imports[import_id]
        if import_dir.exists():
            shutil.rmtree(import_dir, ignore_errors=True)
    
    return ImportCompleteResponse(
        success=True,
        message=f"Model '{final_model_name}' imported successfully",
        model_name=final_model_name,
        model_path=str(target_path)
    )


@router.get("/models/{model_name}", response_model=ModelInfo)
async def get_model(model_name: str):
    """
    Get information about a specific model.
    """
    if not sanitize_model_name(model_name):
        raise HTTPException(status_code=400, detail="Invalid model name")
    
    target_path = MODELS_DIR / model_name
    
    if not target_path.exists():
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")
    
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail=f"'{model_name}' is not a valid model directory")
    
    has_metadata = (target_path / "model_metadata.json").exists()
    has_model_pb = (target_path / "model.pb").exists()
    has_tflite = (target_path / "model.tflite").exists()
    
    return ModelInfo(
        name=model_name,
        path=str(target_path),
        has_metadata=has_metadata,
        has_model_pb=has_model_pb,
        has_tflite=has_tflite
    )


@router.delete("/models/{model_name}", response_model=ModelDeleteResponse)
async def delete_model(model_name: str):
    """
    Delete a DeepRacer model.
    """
    if not sanitize_model_name(model_name):
        raise HTTPException(status_code=400, detail="Invalid model name")
    
    target_path = MODELS_DIR / model_name
    
    if not target_path.exists():
        raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found")
    
    if not target_path.is_dir():
        raise HTTPException(status_code=400, detail=f"'{model_name}' is not a valid model directory")
    
    # Delete model directory (includes model.pb and model.tflite)
    try:
        shutil.rmtree(target_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete model: {e}")
    
    return ModelDeleteResponse(
        success=True,
        message=f"Model '{model_name}' deleted successfully"
    )


# ============================================================================
# Inference Streaming Endpoint
# ============================================================================

def _wants_stream(request: Request, stream: Optional[bool]) -> bool:
    """Check if client wants SSE stream."""
    if stream:
        return True
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept


class InferenceResponse(BaseModel):
    """Response for inference result."""
    speed: float
    steering_angle: float
    probabilities: List[float]
    timestamp: Optional[float] = None


@router.get("/inference", response_model=InferenceResponse)
async def get_inference(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """
    Get latest inference result from DeepRacer.
    
    Use ?stream=true for continuous updates (SSE).
    
    Returns:
        - speed: m/s (actual output)
        - steering_angle: degrees (actual output)
        - probabilities: probability distribution over action space
        - timestamp: inference timestamp
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_sse("deepracer_inference"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    # One-shot read
    data = sm.get_once("deepracer_inference")
    if data is None:
        raise HTTPException(status_code=404, detail="No inference data available (inference may not be running)")
    
    return InferenceResponse(**data)
