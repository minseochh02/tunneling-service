"""
Sheet-to-SQLite Sync Endpoints

Add these endpoints to your existing tunnel service (main.py).
These handle bidirectional sync between Google Sheets and SQLite.

Usage:
1. Import this module in your main.py
2. Include the router: app.include_router(sheet_sync_router)

Or copy these endpoints directly into your existing FastAPI app.
"""

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime
import httpx
import json
import os

from gateway_security import (
    validate_google_drive_id,
    validate_outbound_webhook_url,
    verify_gateway_request_auth,
)

# Optional: Google API support
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    GOOGLE_APIS_AVAILABLE = True
except ImportError:
    GOOGLE_APIS_AVAILABLE = False
    print("⚠️ Google API libraries not installed. Template creation disabled.")

# =============================================================================
# Router
# =============================================================================

sheet_sync_router = APIRouter(prefix="/sheet-sync", tags=["Sheet Sync"])

# =============================================================================
# State Management (for Sheet Sync)
# =============================================================================

# Store Electron callback URLs per spreadsheet
electron_callbacks: dict[str, str] = {}

# Store pending changes when Electron is not connected
pending_changes: dict[str, list] = {}

# Store spreadsheet -> script ID mapping (cache)
script_id_cache: dict[str, str] = {}

# Store sync status per spreadsheet
sheet_sync_status: dict[str, dict] = {}

# =============================================================================
# Models
# =============================================================================

class ElectronRegistration(BaseModel):
    callback_url: str
    spreadsheet_id: str


class SheetChange(BaseModel):
    id: str
    timestamp: str
    sheet: str
    row: int
    col: int
    oldValue: Optional[Any] = None
    newValue: Optional[Any] = None
    source: Optional[str] = "edit"


class SheetChangesPayload(BaseModel):
    spreadsheetId: str
    changes: list[dict]


class FunctionExecutedEvent(BaseModel):
    spreadsheetId: str
    event: dict


class AppsScriptCall(BaseModel):
    function: str
    parameters: list = []
    spreadsheetId: str


class TemplateCreationRequest(BaseModel):
    template_id: str
    new_name: str
    tunnel_url: str
    folder_id: Optional[str] = None


class BatchUpdateRequest(BaseModel):
    spreadsheetId: str
    updates: list[dict]  # [{sheet, row, col, value}, ...]


# =============================================================================
# Electron Registration Endpoints
# =============================================================================

def _require_sheet_sync_auth(request: Request) -> None:
    """Enforce authentication on privileged sheet-sync endpoints."""
    allow_unauth = (os.getenv("SHEET_SYNC_ALLOW_UNAUTHENTICATED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    ok, err = verify_gateway_request_auth(
        request,
        env_keys=("SHEET_SYNC_API_KEY", "GATEWAY_API_KEY"),
        require_by_default=not allow_unauth,
    )
    if not ok:
        raise HTTPException(status_code=401, detail=err or "Unauthorized")


@sheet_sync_router.post("/register-electron")
async def register_electron(data: ElectronRegistration, request: Request):
    """
    Register Electron app's callback URL for a specific spreadsheet.
    Called by Electron when it starts up or connects to a spreadsheet.
    """
    _require_sheet_sync_auth(request)
    try:
        spreadsheet_id = validate_google_drive_id(data.spreadsheet_id, "spreadsheet_id")
        validated_callback_url = validate_outbound_webhook_url(data.callback_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    electron_callbacks[spreadsheet_id] = validated_callback_url
    
    # Initialize sync status
    if spreadsheet_id not in sheet_sync_status:
        sheet_sync_status[spreadsheet_id] = {
            "registered_at": datetime.utcnow().isoformat(),
            "last_push": None,
            "last_pull": None,
            "changes_received": 0,
            "changes_forwarded": 0
        }
    
    # Send any pending changes
    pending = pending_changes.pop(spreadsheet_id, [])
    forwarded = 0
    
    if pending:
        try:
            async with httpx.AsyncClient(follow_redirects=False) as client:
                response = await client.post(
                    validated_callback_url,
                    json={"changes": pending, "type": "pending_flush"},
                    timeout=10.0
                )
                if response.status_code == 200:
                    forwarded = len(pending)
                    print(f"📤 Flushed {forwarded} pending changes to {spreadsheet_id}")
        except Exception as e:
            print(f"⚠️ Failed to flush pending changes: {e}")
            # Re-queue the changes
            pending_changes[spreadsheet_id] = pending
    
    print(f"✅ Registered Electron callback for {spreadsheet_id}: {validated_callback_url}")
    
    return {
        "status": "registered",
        "spreadsheet_id": spreadsheet_id,
        "callback_url": validated_callback_url,
        "pending_flushed": forwarded
    }


@sheet_sync_router.post("/unregister-electron/{spreadsheet_id}")
async def unregister_electron(spreadsheet_id: str, request: Request):
    """Unregister Electron callback (e.g., on app close)"""
    _require_sheet_sync_auth(request)
    electron_callbacks.pop(spreadsheet_id, None)
    print(f"🔌 Unregistered Electron callback for {spreadsheet_id}")
    return {"status": "unregistered", "spreadsheet_id": spreadsheet_id}


@sheet_sync_router.get("/electron-status/{spreadsheet_id}")
async def electron_status(spreadsheet_id: str):
    """Check if Electron is connected for a spreadsheet"""
    is_connected = spreadsheet_id in electron_callbacks
    status = sheet_sync_status.get(spreadsheet_id, {})
    
    return {
        "spreadsheet_id": spreadsheet_id,
        "connected": is_connected,
        "callback_url": electron_callbacks.get(spreadsheet_id),
        "pending_changes": len(pending_changes.get(spreadsheet_id, [])),
        **status
    }


# =============================================================================
# Sheet Changes (from Apps Script onEdit)
# =============================================================================

@sheet_sync_router.post("/sheet-changes")
async def receive_sheet_changes(data: SheetChangesPayload):
    """
    Webhook endpoint for realtime mode.
    Called by Google Apps Script on each edit (via onEdit trigger).
    """
    spreadsheet_id = data.spreadsheetId
    changes = data.changes
    
    # Update stats
    if spreadsheet_id not in sheet_sync_status:
        sheet_sync_status[spreadsheet_id] = {
            "registered_at": datetime.utcnow().isoformat(),
            "changes_received": 0,
            "changes_forwarded": 0
        }
    sheet_sync_status[spreadsheet_id]["changes_received"] = \
        sheet_sync_status[spreadsheet_id].get("changes_received", 0) + len(changes)
    sheet_sync_status[spreadsheet_id]["last_push"] = datetime.utcnow().isoformat()
    
    callback_url = electron_callbacks.get(spreadsheet_id)
    
    if not callback_url:
        # Queue for later
        if spreadsheet_id not in pending_changes:
            pending_changes[spreadsheet_id] = []
        pending_changes[spreadsheet_id].extend(changes)
        
        # Limit pending queue size (keep last 500)
        if len(pending_changes[spreadsheet_id]) > 1000:
            pending_changes[spreadsheet_id] = pending_changes[spreadsheet_id][-500:]
        
        print(f"📥 Queued {len(changes)} changes for {spreadsheet_id} (Electron not connected)")
        return {"status": "queued", "count": len(changes)}
    
    # Forward to Electron
    try:
        validated_callback_url = validate_outbound_webhook_url(callback_url)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await client.post(
                validated_callback_url,
                json={"changes": changes, "type": "sheet_changes"},
                timeout=10.0
            )
            
            sheet_sync_status[spreadsheet_id]["changes_forwarded"] = \
                sheet_sync_status[spreadsheet_id].get("changes_forwarded", 0) + len(changes)
            
            print(f"📤 Forwarded {len(changes)} changes to Electron for {spreadsheet_id}")
            return {
                "status": "forwarded",
                "count": len(changes),
                "electron_response": response.status_code
            }
    except httpx.TimeoutException:
        print(f"⏱️ Timeout forwarding to Electron for {spreadsheet_id}")
        if spreadsheet_id not in pending_changes:
            pending_changes[spreadsheet_id] = []
        pending_changes[spreadsheet_id].extend(changes)
        return {"status": "timeout", "queued": True}
    except Exception as e:
        print(f"❌ Forward failed for {spreadsheet_id}: {e}")
        if spreadsheet_id not in pending_changes:
            pending_changes[spreadsheet_id] = []
        pending_changes[spreadsheet_id].extend(changes)
        return {"status": "forward_failed", "error": str(e), "queued": True}


# =============================================================================
# Function Execution Notifications
# =============================================================================

@sheet_sync_router.post("/function-executed")
async def function_executed(data: FunctionExecutedEvent):
    """
    Notification endpoint when any Apps Script function executes.
    Called by withSync() and triggerSync() in db.gs.
    """
    spreadsheet_id = data.spreadsheetId
    event = data.event
    
    print(f"📣 Function executed in {spreadsheet_id}: {event.get('function', 'unknown')}")
    
    callback_url = electron_callbacks.get(spreadsheet_id)
    
    if not callback_url:
        return {"status": "no_callback", "event_logged": True}
    
    try:
        validated_callback_url = validate_outbound_webhook_url(callback_url)
        async with httpx.AsyncClient(follow_redirects=False) as client:
            response = await client.post(
                validated_callback_url,
                json={"event": event, "type": "function_executed"},
                timeout=10.0
            )
            return {"status": "forwarded", "electron_response": response.status_code}
    except Exception as e:
        print(f"⚠️ Failed to notify Electron of function execution: {e}")
        return {"status": "forward_failed", "error": str(e)}


# =============================================================================
# Apps Script Proxy
# =============================================================================

@sheet_sync_router.post("/apps-script")
async def proxy_apps_script(data: AppsScriptCall, request: Request):
    """
    Proxy calls to Google Apps Script Execution API.
    Electron calls this → we call Apps Script.
    """
    _require_sheet_sync_auth(request)
    function_name = data.function
    parameters = data.parameters
    try:
        spreadsheet_id = validate_google_drive_id(data.spreadsheetId, "spreadsheetId")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    
    print(f"📞 Apps Script call: {function_name}({len(parameters)} params) for {spreadsheet_id}")
    
    if not GOOGLE_APIS_AVAILABLE:
        raise HTTPException(
            status_code=501,
            detail="Google API libraries not installed. Run: pip install google-auth google-api-python-client"
        )
    
    try:
        credentials = get_google_credentials()
        service = build('script', 'v1', credentials=credentials)
        
        script_id = await get_script_id_for_spreadsheet(spreadsheet_id, credentials)
        
        response = service.scripts().run(
            scriptId=script_id,
            body={
                'function': function_name,
                'parameters': parameters
            }
        ).execute()
        
        if 'error' in response:
            error_details = response['error'].get('details', [{}])[0]
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "Apps Script execution error",
                    "message": error_details.get('errorMessage', str(response['error'])),
                    "type": error_details.get('errorType', 'UNKNOWN')
                }
            )
        
        result = response.get('response', {}).get('result')
        print(f"✅ Apps Script result: {type(result).__name__}")
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Apps Script proxy error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Template Creation with db.gs Injection
# =============================================================================

@sheet_sync_router.post("/create-from-template")
async def create_from_template(data: TemplateCreationRequest, request: Request):
    """
    Create a new spreadsheet from template and inject db.gs.
    """
    _require_sheet_sync_auth(request)
    if not GOOGLE_APIS_AVAILABLE:
        raise HTTPException(status_code=501, detail="Google API libraries not installed")
    
    try:
        template_id = validate_google_drive_id(data.template_id, "template_id")
        folder_id = validate_google_drive_id(data.folder_id, "folder_id") if data.folder_id else None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        credentials = get_google_credentials()
        drive = build('drive', 'v3', credentials=credentials)
        script = build('script', 'v1', credentials=credentials)
        
        # Step 1: Copy template
        copy_body = {'name': data.new_name}
        if folder_id:
            copy_body['parents'] = [folder_id]
            
        copy_response = drive.files().copy(
            fileId=template_id,
            body=copy_body
        ).execute()
        
        spreadsheet_id = copy_response['id']
        print(f"📋 Created spreadsheet from template: {spreadsheet_id}")
        
        # Step 2: Get or create bound script
        script_id = await get_or_create_bound_script(drive, script, spreadsheet_id)
        
        # Step 3: Inject db.gs
        await inject_db_script(script, script_id)
        
        # Step 4: Initialize sync
        try:
            script.scripts().run(
                scriptId=script_id,
                body={
                    'function': 'initializeSync',
                    'parameters': [data.tunnel_url]
                }
            ).execute()
        except Exception as init_error:
            print(f"⚠️ Warning: Failed to initialize sync: {init_error}")
        
        # Cache the script ID
        script_id_cache[spreadsheet_id] = script_id
        
        return {
            "success": True,
            "spreadsheetId": spreadsheet_id,
            "scriptId": script_id,
            "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        }
        
    except Exception as e:
        print(f"❌ Template creation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Batch Update (SQLite → Sheet push)
# =============================================================================

@sheet_sync_router.post("/batch-update")
async def batch_update_cells(data: BatchUpdateRequest, request: Request):
    """
    Batch update cells in a spreadsheet.
    Called by Electron to push local SQLite changes to Sheet.
    """
    _require_sheet_sync_auth(request)
    if not GOOGLE_APIS_AVAILABLE:
        raise HTTPException(status_code=501, detail="Google API libraries not installed")
    
    try:
        spreadsheet_id = validate_google_drive_id(data.spreadsheetId, "spreadsheetId")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        credentials = get_google_credentials()
        script_id = await get_script_id_for_spreadsheet(spreadsheet_id, credentials)
        service = build('script', 'v1', credentials=credentials)
        
        response = service.scripts().run(
            scriptId=script_id,
            body={
                'function': 'batchUpdateCells',
                'parameters': [data.updates]
            }
        ).execute()
        
        if 'error' in response:
            raise HTTPException(status_code=500, detail=response['error'])
        
        result = response.get('response', {}).get('result', {})
        
        # Update sync status
        if spreadsheet_id in sheet_sync_status:
            sheet_sync_status[spreadsheet_id]["last_pull"] = datetime.utcnow().isoformat()
        
        return result
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Batch update error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Stats Endpoint
# =============================================================================

@sheet_sync_router.get("/stats")
async def sheet_sync_stats(request: Request):
    """Get sheet sync statistics"""
    _require_sheet_sync_auth(request)
    return {
        "registered_spreadsheets": list(electron_callbacks.keys()),
        "pending_changes": {k: len(v) for k, v in pending_changes.items()},
        "sync_status": sheet_sync_status,
        "cached_script_ids": len(script_id_cache),
        "google_apis_available": GOOGLE_APIS_AVAILABLE
    }


# =============================================================================
# Helper Functions
# =============================================================================

def get_google_credentials():
    """Load Google credentials from environment"""
    
    service_account_json = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON')
    if service_account_json:
        info = json.loads(service_account_json)
        return service_account.Credentials.from_service_account_info(
            info,
            scopes=[
                'https://www.googleapis.com/auth/userinfo.email',
                'https://www.googleapis.com/auth/drive.file',
                'https://www.googleapis.com/auth/script.projects',
                'https://www.googleapis.com/auth/script.scriptapp',
                'https://www.googleapis.com/auth/gmail.send',
                'https://www.googleapis.com/auth/script.deployments',
                'https://www.googleapis.com/auth/script.external_request',
                'https://www.googleapis.com/auth/script.webapp.deploy',
                'https://www.googleapis.com/auth/drive.scripts'
            ]
        )
    
    service_account_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE')
    if service_account_file and os.path.exists(service_account_file):
        return service_account.Credentials.from_service_account_file(
            service_account_file,
            scopes=[
                'https://www.googleapis.com/auth/userinfo.email',
                'https://www.googleapis.com/auth/script.metrics',
                'https://www.googleapis.com/auth/script.storage',
                'https://www.googleapis.com/auth/drive.file',
                'https://www.googleapis.com/auth/script.projects',
                'https://www.googleapis.com/auth/script.scriptapp',
                'https://www.googleapis.com/auth/gmail.send',
                'https://www.googleapis.com/auth/script.deployments',
                'https://www.googleapis.com/auth/script.external_request',
                'https://www.googleapis.com/auth/script.webapp.deploy',
                'https://www.googleapis.com/auth/drive.scripts'
            ]
        )
    
    raise HTTPException(
        status_code=500,
        detail="No Google credentials configured. Set GOOGLE_SERVICE_ACCOUNT_JSON"
    )


async def get_script_id_for_spreadsheet(spreadsheet_id: str, credentials) -> str:
    """Get the Apps Script ID bound to a spreadsheet"""
    spreadsheet_id = validate_google_drive_id(spreadsheet_id, "spreadsheet_id")
    if spreadsheet_id in script_id_cache:
        return script_id_cache[spreadsheet_id]
    
    drive = build('drive', 'v3', credentials=credentials)
    
    files = drive.files().list(
        q=f"mimeType='application/vnd.google-apps.script' and '{spreadsheet_id}' in parents",
        fields='files(id, name)'
    ).execute()
    
    if files.get('files'):
        script_id = files['files'][0]['id']
        script_id_cache[spreadsheet_id] = script_id
        return script_id
    
    raise HTTPException(
        status_code=404,
        detail=f"No Apps Script found for spreadsheet: {spreadsheet_id}"
    )


async def get_or_create_bound_script(drive, script, spreadsheet_id: str) -> str:
    """Get existing or create new bound script project"""
    spreadsheet_id = validate_google_drive_id(spreadsheet_id, "spreadsheet_id")
    try:
        files = drive.files().list(
            q=f"mimeType='application/vnd.google-apps.script' and '{spreadsheet_id}' in parents",
            fields='files(id)'
        ).execute()
        
        if files.get('files'):
            return files['files'][0]['id']
    except Exception:
        pass
    
    response = script.projects().create(
        body={
            'title': 'EGDesk Sync',
            'parentId': spreadsheet_id
        }
    ).execute()
    
    return response['scriptId']


async def inject_db_script(script, script_id: str):
    """Inject db.gs content into the script project"""
    
    try:
        content = script.projects().getContent(scriptId=script_id).execute()
        existing_files = content.get('files', [])
    except Exception:
        existing_files = []
    
    db_gs_content = get_db_gs_content()
    
    db_file = {
        'name': 'db',
        'type': 'SERVER_JS',
        'source': db_gs_content
    }
    
    db_index = next((i for i, f in enumerate(existing_files) if f['name'] == 'db'), None)
    
    if db_index is not None:
        existing_files[db_index] = db_file
    else:
        existing_files.append(db_file)
    
    manifest_index = next((i for i, f in enumerate(existing_files) if f['name'] == 'appsscript'), None)
    if manifest_index is None:
        existing_files.append({
            'name': 'appsscript',
            'type': 'JSON',
            'source': json.dumps({
                'timeZone': 'Asia/Seoul',
                'dependencies': {},
                'exceptionLogging': 'STACKDRIVER',
                'runtimeVersion': 'V8'
            })
        })
    
    script.projects().updateContent(
        scriptId=script_id,
        body={'files': existing_files}
    ).execute()


def get_db_gs_content() -> str:
    """Return the full db.gs content for injection"""
    
    db_gs_path = os.getenv('DB_GS_PATH', 'db.gs')
    if os.path.exists(db_gs_path):
        with open(db_gs_path, 'r') as f:
            return f.read()
    
    # Embedded fallback - see db.gs file for full version
    return '''
const DB_CONFIG = {
  CHANGELOG_SHEET: '_changelog',
  IGNORED_SHEETS: ['_changelog', '_config'],
  TUNNEL_URL_KEY: 'TUNNEL_URL',
  SYNC_MODE_KEY: 'SYNC_MODE'
};

function initializeSync(tunnelUrl) {
  const props = PropertiesService.getScriptProperties();
  props.setProperty(DB_CONFIG.TUNNEL_URL_KEY, tunnelUrl);
  props.setProperty(DB_CONFIG.SYNC_MODE_KEY, 'manual');
  ensureChangelogSheet_();
  setupEditTrigger_();
  return { success: true, spreadsheetId: SpreadsheetApp.getActiveSpreadsheet().getId() };
}

function setTunnelUrl(url) {
  PropertiesService.getScriptProperties().setProperty(DB_CONFIG.TUNNEL_URL_KEY, url);
  return { success: true, url };
}

function setSyncMode(mode) {
  PropertiesService.getScriptProperties().setProperty(DB_CONFIG.SYNC_MODE_KEY, mode);
  return { success: true, mode };
}

function onSheetEdit(e) {
  if (!e || !e.range) return;
  const sheetName = e.source.getActiveSheet().getName();
  if (DB_CONFIG.IGNORED_SHEETS.includes(sheetName)) return;
  
  const change = {
    id: Utilities.getUuid(),
    timestamp: new Date().toISOString(),
    sheet: sheetName,
    row: e.range.getRow(),
    col: e.range.getColumn(),
    oldValue: e.oldValue || null,
    newValue: e.value || null,
    synced: false
  };
  
  appendToChangelog_(change);
  
  const mode = PropertiesService.getScriptProperties().getProperty(DB_CONFIG.SYNC_MODE_KEY);
  if (mode === 'realtime') {
    pushChangesToTunnel_([change]);
  }
}

function getUnsynced() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const cl = ss.getSheetByName(DB_CONFIG.CHANGELOG_SHEET);
  if (!cl) return [];
  
  const data = cl.getDataRange().getValues();
  const unsyncedRows = [];
  
  for (let i = 1; i < data.length; i++) {
    if (data[i][7] === false) {
      unsyncedRows.push({
        rowIndex: i + 1,
        id: data[i][0],
        timestamp: data[i][1],
        sheet: data[i][2],
        row: data[i][3],
        col: data[i][4],
        oldValue: data[i][5],
        newValue: data[i][6]
      });
    }
  }
  return unsyncedRows;
}

function markSynced(ids) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const cl = ss.getSheetByName(DB_CONFIG.CHANGELOG_SHEET);
  if (!cl) return { success: false };
  
  const data = cl.getDataRange().getValues();
  const idSet = new Set(ids);
  
  for (let i = 1; i < data.length; i++) {
    if (idSet.has(data[i][0])) {
      cl.getRange(i + 1, 8).setValue(true);
    }
  }
  return { success: true, count: ids.length };
}

function getColumnHeaders(sheetName, headerRow) {
  headerRow = headerRow || 1;
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const sheet = ss.getSheetByName(sheetName);
  if (!sheet) return [];
  
  const lastCol = sheet.getLastColumn();
  if (lastCol === 0) return [];
  
  const headers = sheet.getRange(headerRow, 1, 1, lastCol).getValues()[0];
  return headers.map((h, i) => ({ index: i + 1, header: h || 'Column_' + (i + 1) }));
}

function batchUpdateCells(updates) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const originalMode = PropertiesService.getScriptProperties().getProperty(DB_CONFIG.SYNC_MODE_KEY);
  PropertiesService.getScriptProperties().setProperty(DB_CONFIG.SYNC_MODE_KEY, 'paused');
  
  try {
    const bySheet = {};
    for (const u of updates) {
      if (!bySheet[u.sheet]) bySheet[u.sheet] = [];
      bySheet[u.sheet].push(u);
    }
    
    for (const sheetName of Object.keys(bySheet)) {
      const sheet = ss.getSheetByName(sheetName);
      if (!sheet) continue;
      for (const update of bySheet[sheetName]) {
        sheet.getRange(update.row, update.col).setValue(update.value);
      }
    }
    
    SpreadsheetApp.flush();
    return { success: true, updated: updates.length };
  } finally {
    PropertiesService.getScriptProperties().setProperty(DB_CONFIG.SYNC_MODE_KEY, originalMode || 'manual');
  }
}

function ensureChangelogSheet_() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  let cl = ss.getSheetByName(DB_CONFIG.CHANGELOG_SHEET);
  if (!cl) {
    cl = ss.insertSheet(DB_CONFIG.CHANGELOG_SHEET);
    cl.appendRow(['id', 'timestamp', 'sheet', 'row', 'col', 'oldValue', 'newValue', 'synced']);
    cl.hideSheet();
  }
  return cl;
}

function appendToChangelog_(change) {
  const cl = ensureChangelogSheet_();
  cl.appendRow([change.id, change.timestamp, change.sheet, change.row, change.col, change.oldValue, change.newValue, false]);
}

function pushChangesToTunnel_(changes) {
  const tunnelUrl = PropertiesService.getScriptProperties().getProperty(DB_CONFIG.TUNNEL_URL_KEY);
  if (!tunnelUrl) return;
  try {
    UrlFetchApp.fetch(tunnelUrl + '/sheet-sync/sheet-changes', {
      method: 'POST',
      contentType: 'application/json',
      payload: JSON.stringify({ spreadsheetId: SpreadsheetApp.getActiveSpreadsheet().getId(), changes: changes }),
      muteHttpExceptions: true
    });
  } catch (err) { console.error('Push to tunnel failed:', err); }
}

function setupEditTrigger_() {
  const triggers = ScriptApp.getProjectTriggers();
  for (const trigger of triggers) {
    if (trigger.getHandlerFunction() === 'onSheetEdit') {
      ScriptApp.deleteTrigger(trigger);
    }
  }
  ScriptApp.newTrigger('onSheetEdit').forSpreadsheet(SpreadsheetApp.getActiveSpreadsheet()).onEdit().create();
}

function getSyncStatus() {
  const props = PropertiesService.getScriptProperties();
  return {
    tunnelUrl: props.getProperty(DB_CONFIG.TUNNEL_URL_KEY),
    syncMode: props.getProperty(DB_CONFIG.SYNC_MODE_KEY),
    spreadsheetId: SpreadsheetApp.getActiveSpreadsheet().getId()
  };
}
'''.strip()