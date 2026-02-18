import asyncio
import logging
from datetime import datetime
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

from .config import get_settings
from .services.docker_ops import docker_ops
from .services.github import github_client
from .services.version import is_update_available, format_version

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Companion Update Dashboard",
    description="Web dashboard to manage Companion updates",
    version="1.0.0"
)

# Store update state
update_in_progress = False


class StatusResponse(BaseModel):
    """Response model for status endpoint."""
    current_version: str
    latest_version: str
    update_available: bool
    container_status: str
    container_running: bool
    can_update: bool
    cooldown_remaining: int
    last_checked: str


class UpdateResponse(BaseModel):
    """Response model for update endpoint."""
    success: bool
    message: str


# HTML Template
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Companion Update Dashboard</title>
    <style>
        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #e0e0e0;
            padding: 2rem;
        }

        .container {
            max-width: 600px;
            margin: 0 auto;
        }

        header {
            text-align: center;
            margin-bottom: 2rem;
        }

        h1 {
            font-size: 2rem;
            color: #fff;
            margin-bottom: 0.5rem;
        }

        .subtitle {
            color: #888;
            font-size: 0.9rem;
        }

        .card {
            background: rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 1rem;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }

        .version-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
            margin-bottom: 1rem;
        }

        .version-box {
            background: rgba(0, 0, 0, 0.2);
            border-radius: 8px;
            padding: 1rem;
            text-align: center;
        }

        .version-label {
            font-size: 0.8rem;
            color: #888;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 0.5rem;
        }

        .version-number {
            font-size: 1.8rem;
            font-weight: bold;
            color: #fff;
        }

        .version-number.current {
            color: #4ecdc4;
        }

        .version-number.latest {
            color: #ff6b6b;
        }

        .version-number.up-to-date {
            color: #4ecdc4;
        }

        .status-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.75rem 0;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }

        .status-row:last-child {
            border-bottom: none;
        }

        .status-label {
            color: #888;
        }

        .status-value {
            font-weight: 500;
        }

        .status-badge {
            display: inline-block;
            padding: 0.25rem 0.75rem;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 500;
        }

        .badge-running {
            background: rgba(78, 205, 196, 0.2);
            color: #4ecdc4;
        }

        .badge-stopped {
            background: rgba(255, 107, 107, 0.2);
            color: #ff6b6b;
        }

        .badge-update {
            background: rgba(255, 193, 7, 0.2);
            color: #ffc107;
        }

        .badge-current {
            background: rgba(78, 205, 196, 0.2);
            color: #4ecdc4;
        }

        .update-btn {
            width: 100%;
            padding: 1rem;
            font-size: 1.1rem;
            font-weight: 600;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.3s ease;
            text-transform: uppercase;
            letter-spacing: 1px;
        }

        .update-btn.available {
            background: linear-gradient(135deg, #ff6b6b 0%, #ee5a5a 100%);
            color: white;
        }

        .update-btn.available:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 15px rgba(255, 107, 107, 0.4);
        }

        .update-btn.disabled {
            background: #333;
            color: #666;
            cursor: not-allowed;
        }

        .update-btn.in-progress {
            background: #444;
            color: #888;
            cursor: wait;
        }

        .progress-container {
            display: none;
            margin-top: 1rem;
        }

        .progress-container.active {
            display: block;
        }

        .progress-log {
            background: #000;
            border-radius: 8px;
            padding: 1rem;
            font-family: 'Monaco', 'Menlo', 'Ubuntu Mono', monospace;
            font-size: 0.85rem;
            max-height: 300px;
            overflow-y: auto;
            line-height: 1.6;
        }

        .progress-log .line {
            color: #4ecdc4;
        }

        .progress-log .error {
            color: #ff6b6b;
        }

        .progress-log .success {
            color: #4ecdc4;
            font-weight: bold;
        }

        .refresh-btn {
            background: transparent;
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: #888;
            padding: 0.5rem 1rem;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.85rem;
            transition: all 0.2s ease;
        }

        .refresh-btn:hover {
            border-color: rgba(255, 255, 255, 0.4);
            color: #fff;
        }

        .footer {
            text-align: center;
            margin-top: 2rem;
            color: #555;
            font-size: 0.8rem;
        }

        .loading {
            opacity: 0.5;
        }

        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }

        .updating {
            animation: pulse 1.5s infinite;
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Companion Update Dashboard</h1>
            <p class="subtitle">Bitfocus Companion Container Manager</p>
        </header>

        <div class="card">
            <div class="version-grid">
                <div class="version-box">
                    <div class="version-label">Current Version</div>
                    <div class="version-number current" id="current-version">--</div>
                </div>
                <div class="version-box">
                    <div class="version-label">Latest Version</div>
                    <div class="version-number" id="latest-version">--</div>
                </div>
            </div>

            <div class="status-row">
                <span class="status-label">Container Status</span>
                <span class="status-badge" id="container-status">--</span>
            </div>
            <div class="status-row">
                <span class="status-label">Update Status</span>
                <span class="status-badge" id="update-status">--</span>
            </div>
            <div class="status-row">
                <span class="status-label">Last Checked</span>
                <span class="status-value" id="last-checked">--</span>
            </div>
        </div>

        <div class="card">
            <button class="update-btn disabled" id="update-btn" disabled>
                Checking...
            </button>

            <div class="progress-container" id="progress-container">
                <div class="progress-log" id="progress-log"></div>
            </div>
        </div>

        <div style="text-align: center; margin-top: 1rem;">
            <button class="refresh-btn" id="refresh-btn" onclick="checkStatus()">
                Refresh Status
            </button>
        </div>

        <footer class="footer">
            <p>Companion Update Dashboard v1.0</p>
        </footer>
    </div>

    <script>
        let updateInProgress = false;

        async function checkStatus() {
            const btn = document.getElementById('update-btn');
            const refreshBtn = document.getElementById('refresh-btn');

            if (updateInProgress) return;

            refreshBtn.disabled = true;
            refreshBtn.textContent = 'Checking...';

            try {
                const response = await fetch('/api/status');
                const data = await response.json();

                // Update version displays
                document.getElementById('current-version').textContent = data.current_version;

                const latestEl = document.getElementById('latest-version');
                latestEl.textContent = data.latest_version;
                latestEl.className = 'version-number ' + (data.update_available ? 'latest' : 'up-to-date');

                // Update container status
                const containerStatus = document.getElementById('container-status');
                containerStatus.textContent = data.container_status;
                containerStatus.className = 'status-badge ' + (data.container_running ? 'badge-running' : 'badge-stopped');

                // Update update status
                const updateStatus = document.getElementById('update-status');
                if (data.update_available) {
                    updateStatus.textContent = 'Update Available';
                    updateStatus.className = 'status-badge badge-update';
                } else {
                    updateStatus.textContent = 'Up to Date';
                    updateStatus.className = 'status-badge badge-current';
                }

                // Update last checked
                document.getElementById('last-checked').textContent = data.last_checked;

                // Update button state
                if (data.update_available && data.can_update) {
                    btn.textContent = 'Update Now';
                    btn.className = 'update-btn available';
                    btn.disabled = false;
                } else if (data.update_available && !data.can_update) {
                    btn.textContent = `Cooldown: ${data.cooldown_remaining}s`;
                    btn.className = 'update-btn disabled';
                    btn.disabled = true;
                } else {
                    btn.textContent = 'Up to Date';
                    btn.className = 'update-btn disabled';
                    btn.disabled = true;
                }

            } catch (error) {
                console.error('Failed to check status:', error);
                document.getElementById('current-version').textContent = 'Error';
                document.getElementById('latest-version').textContent = 'Error';
            } finally {
                refreshBtn.disabled = false;
                refreshBtn.textContent = 'Refresh Status';
            }
        }

        async function performUpdate() {
            if (updateInProgress) return;

            const btn = document.getElementById('update-btn');
            const progressContainer = document.getElementById('progress-container');
            const progressLog = document.getElementById('progress-log');

            updateInProgress = true;
            btn.textContent = 'Updating...';
            btn.className = 'update-btn in-progress';
            btn.disabled = true;

            progressContainer.classList.add('active');
            progressLog.innerHTML = '';

            try {
                const eventSource = new EventSource('/api/update/stream');

                eventSource.onmessage = (event) => {
                    const data = JSON.parse(event.data);

                    const line = document.createElement('div');
                    line.className = 'line';

                    if (data.type === 'error') {
                        line.className = 'error';
                    } else if (data.type === 'complete') {
                        line.className = 'success';
                    }

                    line.textContent = data.message;
                    progressLog.appendChild(line);
                    progressLog.scrollTop = progressLog.scrollHeight;

                    if (data.type === 'complete' || data.type === 'error') {
                        eventSource.close();
                        updateInProgress = false;
                        setTimeout(checkStatus, 2000);
                    }
                };

                eventSource.onerror = () => {
                    eventSource.close();
                    updateInProgress = false;

                    const line = document.createElement('div');
                    line.className = 'error';
                    line.textContent = 'Connection lost. Please refresh status.';
                    progressLog.appendChild(line);

                    setTimeout(checkStatus, 2000);
                };

            } catch (error) {
                console.error('Update failed:', error);
                updateInProgress = false;

                const line = document.createElement('div');
                line.className = 'error';
                line.textContent = 'Update failed: ' + error.message;
                progressLog.appendChild(line);

                checkStatus();
            }
        }

        // Set up event listeners
        document.getElementById('update-btn').addEventListener('click', performUpdate);

        // Initial status check
        checkStatus();

        // Auto-refresh every 30 seconds when not updating
        setInterval(() => {
            if (!updateInProgress) {
                checkStatus();
            }
        }, 30000);
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard HTML page."""
    return DASHBOARD_HTML


@app.get("/api/status", response_model=StatusResponse)
async def get_status():
    """Get current version status."""
    # Get current version from running container
    current_version = docker_ops.get_running_version()

    # Get latest version from GitHub
    try:
        release = await github_client.get_latest_release()
        latest_version = release.get("tag_name", "").lstrip("v")
    except Exception as e:
        logger.error(f"Failed to fetch latest version: {e}")
        latest_version = None

    # Get container status
    container_status = docker_ops.get_container_status()

    # Check if update is available
    update_available = False
    if current_version and latest_version:
        update_available = is_update_available(current_version, latest_version)

    return StatusResponse(
        current_version=format_version(current_version),
        latest_version=format_version(latest_version),
        update_available=update_available,
        container_status=container_status["status"].capitalize(),
        container_running=container_status["running"],
        can_update=docker_ops.can_update() and not update_in_progress,
        cooldown_remaining=docker_ops.get_cooldown_remaining(),
        last_checked=datetime.now().strftime("%H:%M:%S")
    )


@app.post("/api/update", response_model=UpdateResponse)
async def trigger_update():
    """Trigger an update (non-streaming)."""
    global update_in_progress

    if update_in_progress:
        return UpdateResponse(success=False, message="Update already in progress")

    if not docker_ops.can_update():
        remaining = docker_ops.get_cooldown_remaining()
        return UpdateResponse(success=False, message=f"Please wait {remaining} seconds")

    update_in_progress = True

    try:
        for _ in docker_ops.perform_update():
            pass
        return UpdateResponse(success=True, message="Update completed successfully")
    except Exception as e:
        logger.error(f"Update failed: {e}")
        return UpdateResponse(success=False, message=str(e))
    finally:
        update_in_progress = False


@app.get("/api/update/stream")
async def stream_update():
    """Stream update progress via Server-Sent Events."""
    global update_in_progress

    async def event_generator() -> AsyncGenerator[str, None]:
        global update_in_progress

        if update_in_progress:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Update already in progress'})}\n\n"
            return

        if not docker_ops.can_update():
            remaining = docker_ops.get_cooldown_remaining()
            yield f"data: {json.dumps({'type': 'error', 'message': f'Cooldown active. Wait {remaining}s'})}\n\n"
            return

        update_in_progress = True
        logger.info("Starting streamed update")

        try:
            for message in docker_ops.perform_update():
                yield f"data: {json.dumps({'type': 'progress', 'message': message})}\n\n"
                await asyncio.sleep(0.1)  # Allow event to be sent

            yield f"data: {json.dumps({'type': 'complete', 'message': 'Update completed successfully!'})}\n\n"

        except Exception as e:
            logger.error(f"Update failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

        finally:
            update_in_progress = False

    # Need to import json for the generator
    import json

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.on_event("startup")
async def startup_event():
    """Log startup information."""
    settings = get_settings()
    logger.info("Companion Update Dashboard starting")
    logger.info(f"Monitoring container: {settings.companion_container_name}")
    logger.info(f"Companion path: {settings.companion_docker_path}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
