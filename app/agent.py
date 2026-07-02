# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import sys
import json
import re
import datetime
from zoneinfo import ZoneInfo
from typing import Any
from google.adk.agents import LlmAgent
from google.adk.tools import AgentTool, ToolContext
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.adk.workflow import Workflow, START
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.apps import App, ResumabilityConfig
from google.adk.models import Gemini
from google.adk.agents.context import Context
from google.genai import types
from .config import config

# --- CONFIG & PERSISTENCE ---

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER_PATH = os.path.join(CURRENT_DIR, "mcp_server.py")
DB_FILE = os.path.join(CURRENT_DIR, "elderly_care_db.json")

def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return {"medications": [], "appointments": [], "pending_actions": []}
    try:
        with open(DB_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"medications": [], "appointments": [], "pending_actions": []}

def save_db(db: dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)

# --- MCP CONFIGURATION ---

mcp_connection = StdioConnectionParams(
    server_params=StdioServerParameters(
        command=sys.executable,
        args=[MCP_SERVER_PATH]
    )
)

# --- SPECIALIZED SUB-AGENTS ---

medication_agent = LlmAgent(
    name="medication_agent",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="You are an expert medication manager. You can add and retrieve medications from the schedule. Use your tools to manage medications. Note that adding a medication requires caregiver approval and returns a pending status.",
    description="Manages patient medication schedules (adding, listing).",
    tools=[
        McpToolset(
            connection_params=mcp_connection,
            tool_filter=["get_medications", "request_add_medication"]
        )
    ]
)

appointment_agent = LlmAgent(
    name="appointment_agent",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="You are an expert doctor visit coordinator. You can schedule new doctor appointments and retrieve appointment records. Use your tools to manage appointments. Note that scheduling an appointment requires caregiver approval and returns a pending status.",
    description="Manages doctor visit scheduling and list queries.",
    tools=[
        McpToolset(
            connection_params=mcp_connection,
            tool_filter=["get_appointments", "request_schedule_appointment"]
        )
    ]
)


# --- ORCHESTRATOR / CARE COORDINATOR ---

care_coordinator = LlmAgent(
    name="care_coordinator",
    model=Gemini(
        model=config.model,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction="You are the primary elderly care assistant coordinator. You receive user queries and delegate medication tasks to medication_agent, and doctor appointment tasks to appointment_agent. Introduce yourself as the Care Coordinator. Ask clarifying questions if needed.",
    description="Coordinates overall care, routing queries to either the medication agent or appointment agent.",
    tools=[AgentTool(medication_agent), AgentTool(appointment_agent)]
)


# --- SECURITY CHECKPOINT & UTILITIES ---

def log_security_event(severity: str, action: str, details: dict):
    """Logs structured security checkpoint decisions to console and audit file."""
    audit_entry = {
        "timestamp": datetime.datetime.now(ZoneInfo("UTC")).isoformat(),
        "severity": severity,
        "action": action,
        "details": details
    }
    # Log to console
    print(f"[AUDIT LOG] {json.dumps(audit_entry)}")
    # Log to file
    try:
        audit_file = os.path.join(CURRENT_DIR, "security_audit.log")
        with open(audit_file, "a") as f:
            f.write(json.dumps(audit_entry) + "\n")
    except Exception:
        pass


def scrub_pii(text: str) -> tuple[str, bool, list]:
    """Detects and redacts common PII types such as SSN and phone numbers."""
    scrubbed = text
    modified = False
    scrubbed_types = []
    
    # 1. SSN pattern: XXX-XX-XXXX or XXXXXXXXX
    ssn_pattern = r'\b\d{3}-\d{2}-\d{4}\b'
    if re.search(ssn_pattern, scrubbed):
        scrubbed = re.sub(ssn_pattern, "[REDACTED SSN]", scrubbed)
        modified = True
        scrubbed_types.append("SSN")
        
    # 2. Phone number pattern (various formats)
    phone_pattern = r'\b(?:\+?1[-. ]?)?\(?([0-9]{3})\)?[-. ]?([0-9]{3})[-. ]?([0-9]{4})\b'
    if re.search(phone_pattern, scrubbed):
        scrubbed = re.sub(phone_pattern, "[REDACTED PHONE]", scrubbed)
        modified = True
        scrubbed_types.append("Phone")
        
    return scrubbed, modified, scrubbed_types


def security_checkpoint(ctx: Context, node_input: Any) -> Event:
    """Validates and sanitizes user input before passing it to the agents."""
    text = ""
    if node_input and hasattr(node_input, "parts") and node_input.parts:
        for part in node_input.parts:
            if part.text:
                text += part.text
    elif isinstance(node_input, str):
        text = node_input
                
    # 1. PII Scrubbing
    scrubbed_text, is_scrubbed, scrubbed_types = scrub_pii(text)
    if is_scrubbed:
        log_security_event(
            severity="WARNING",
            action="PII_REDACTION",
            details={"original_length": len(text), "scrubbed_types": scrubbed_types}
        )
        node_input = types.Content(role='user', parts=[types.Part.from_text(text=scrubbed_text)])
        
    # 2. Prompt Injection Keyword Detection
    forbidden_keywords = ["ignore instructions", "system prompt", "bypass safety", "override rules", "forget previous"]
    detected_injection = [kw for kw in forbidden_keywords if kw in scrubbed_text.lower()]
    
    if detected_injection:
        ctx.state["security_incident"] = f"Prompt injection keywords detected: {detected_injection}"
        log_security_event(
            severity="CRITICAL",
            action="PROMPT_INJECTION_BLOCKED",
            details={"detected_keywords": detected_injection, "input_snippet": scrubbed_text[:100]}
        )
        return Event(output="Security threat detected. Input rejected.", route="threat")
        
    # 3. Domain-Specific Rule: Controlled Substances Consent Check
    controlled_substances = ["fentanyl", "morphine", "oxycodone", "adderall", "ritalin"]
    detected_controlled = [drug for drug in controlled_substances if drug in scrubbed_text.lower()]
    
    if detected_controlled:
        ctx.state["security_incident"] = f"Controlled substance request: {detected_controlled}"
        log_security_event(
            severity="CRITICAL",
            action="CONTROLLED_SUBSTANCE_BLOCKED",
            details={"detected_substances": detected_controlled}
        )
        return Event(output="Controlled substance request detected. This action requires direct medical professional or caregiver authorization.", route="threat")
        
    # 4. Input Approved
    log_security_event(
        severity="INFO",
        action="INPUT_APPROVED",
        details={"text_length": len(scrubbed_text)}
    )
    return Event(output=node_input, route="safe")


def security_event(ctx: Context, node_input: Any) -> Event:
    """Handles security incident routing and alerts."""
    alert_msg = f"[SECURITY CRITICAL] Input blocked: {ctx.state.get('security_incident')}"
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=alert_msg)]))
    yield Event(output=alert_msg)


def post_coordinator_check(ctx: Context, node_input: Any) -> Event:
    """Checks if there is a pending action in the database requiring caregiver approval."""
    db = load_db()
    pending = db.get("pending_actions", [])
    if pending:
        return Event(output=pending[0], route="requires_approval")
    return Event(output=node_input, route="direct")


async def caregiver_approval_node(ctx: Context, node_input: Any):
    """Pauses the workflow to request caregiver confirmation for sensitive actions."""
    if not ctx.resume_inputs or "caregiver_approved" not in ctx.resume_inputs:
        action_type = node_input.get("type")
        if action_type == "add_medication":
            msg = f"Caregiver Approval Required: Approve adding medication '{node_input.get('med_name')}' ({node_input.get('dosage')}) at {node_input.get('time_of_day')}? (Reply 'yes' or 'no')"
        else:
            msg = f"Caregiver Approval Required: Approve doctor appointment with {node_input.get('doctor_name')} on {node_input.get('date_time')}? (Reply 'yes' or 'no')"
            
        yield RequestInput(interrupt_id="caregiver_approved", message=msg)
        return
        
    approval_resp = ctx.resume_inputs["caregiver_approved"].strip().lower()
    approved = approval_resp in ["yes", "y", "approve"]
    
    db = load_db()
    pending = db.get("pending_actions", [])
    
    action_id = node_input.get("id")
    target_action = None
    for action in pending:
        if action.get("id") == action_id:
            target_action = action
            break
            
    if target_action:
        pending.remove(target_action)
        db["pending_actions"] = pending
        
        if approved:
            if target_action["type"] == "add_medication":
                db.setdefault("medications", []).append({
                    "med_name": target_action["med_name"],
                    "dosage": target_action["dosage"],
                    "time_of_day": target_action["time_of_day"]
                })
                result_msg = f"✅ Medication '{target_action['med_name']}' ({target_action['dosage']}) at {target_action['time_of_day']} has been successfully approved and added to the schedule."
                log_security_event(
                    severity="INFO",
                    action="PENDING_ACTION_APPROVED",
                    details={"action_id": action_id, "type": "add_medication", "med_name": target_action["med_name"]}
                )
            else:
                db.setdefault("appointments", []).append({
                    "doctor_name": target_action["doctor_name"],
                    "date_time": target_action["date_time"],
                    "reason": target_action["reason"]
                })
                result_msg = f"✅ Doctor appointment with {target_action['doctor_name']} on {target_action['date_time']} has been successfully approved and scheduled."
                log_security_event(
                    severity="INFO",
                    action="PENDING_ACTION_APPROVED",
                    details={"action_id": action_id, "type": "schedule_appointment", "doctor_name": target_action["doctor_name"]}
                )
        else:
            result_msg = f"❌ Caregiver rejected scheduling the action: {target_action['type']}."
            log_security_event(
                severity="WARNING",
                action="PENDING_ACTION_REJECTED",
                details={"action_id": action_id, "type": target_action["type"]}
            )
            
        save_db(db)
    else:
        result_msg = f"⚠️ Could not find pending action ID '{action_id}' to commit."
        
    yield Event(output=result_msg)


def format_output(ctx: Context, node_input: Any):
    """Formats the final node output for display in the web UI."""
    if isinstance(node_input, types.Content):
        text = ""
        if node_input.parts:
            for p in node_input.parts:
                if p.text:
                    text += p.text
        msg_text = text
    elif isinstance(node_input, dict):
        msg_text = str(node_input)
    else:
        msg_text = str(node_input)
        
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=msg_text)]))
    yield Event(output=msg_text)


# --- WORKFLOW DEFINITION ---

root_agent = Workflow(
    name="elderly_care_workflow",
    edges=[
        ('START', security_checkpoint),
        
        # Security checkpoint routing
        (security_checkpoint, {"safe": care_coordinator, "threat": security_event}),
        
        # Care coordinator outputs to check node
        (care_coordinator, post_coordinator_check),
        
        # Check node conditional routing
        (post_coordinator_check, {"requires_approval": caregiver_approval_node, "direct": format_output}),
        
        # Caregiver approval goes to final output formatting
        (caregiver_approval_node, format_output),
        
        # Security event also goes to final output formatting
        (security_event, format_output),
    ]
)

app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True)
)
