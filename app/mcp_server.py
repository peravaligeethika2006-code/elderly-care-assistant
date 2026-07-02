import os
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Elderly Care Database Server")
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "elderly_care_db.json")

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

@mcp.tool()
def get_medications() -> dict:
    """Retrieves the list of active medications and their schedules."""
    db = load_db()
    return {"status": "success", "medications": db.get("medications", [])}

@mcp.tool()
def get_appointments() -> dict:
    """Retrieves the list of scheduled doctor visits and appointments."""
    db = load_db()
    return {"status": "success", "appointments": db.get("appointments", [])}

@mcp.tool()
def request_add_medication(med_name: str, dosage: str, time_of_day: str) -> dict:
    """Requests adding a new medication to the patient's schedule. This requires caregiver approval.

    Args:
        med_name: The name of the medication (e.g. Aspirin).
        dosage: The dosage to take (e.g. 100mg).
        time_of_day: When to take it (e.g. Morning, 8:00 AM).
    """
    db = load_db()
    pending = db.get("pending_actions", [])
    
    for action in pending:
        if action.get("type") == "add_medication" and action.get("med_name") == med_name:
            return {"status": "error", "message": f"Medication '{med_name}' addition is already pending approval."}
            
    new_pending = {
        "id": f"med_{len(pending) + 1}",
        "type": "add_medication",
        "med_name": med_name,
        "dosage": dosage,
        "time_of_day": time_of_day
    }
    pending.append(new_pending)
    db["pending_actions"] = pending
    save_db(db)
    return {"status": "pending_approval", "action_id": new_pending["id"], "message": f"Medication addition for '{med_name}' registered. Pending caregiver approval."}

@mcp.tool()
def request_schedule_appointment(doctor_name: str, date_time: str, reason: str) -> dict:
    """Requests scheduling a doctor visit. This requires caregiver approval.

    Args:
        doctor_name: Name of the physician/doctor.
        date_time: Proposed date and time (e.g. 2026-07-10 10:00 AM).
        reason: Purpose of the medical appointment.
    """
    db = load_db()
    pending = db.get("pending_actions", [])
    
    new_pending = {
        "id": f"appt_{len(pending) + 1}",
        "type": "schedule_appointment",
        "doctor_name": doctor_name,
        "date_time": date_time,
        "reason": reason
    }
    pending.append(new_pending)
    db["pending_actions"] = pending
    save_db(db)
    return {"status": "pending_approval", "action_id": new_pending["id"], "message": f"Appointment with {doctor_name} on {date_time} registered. Pending caregiver approval."}

@mcp.tool()
def commit_pending_action(action_id: str, approved: bool) -> dict:
    """Commits or rejects a pending action (medication addition or doctor appointment) after approval.

    Args:
        action_id: The ID of the pending action (e.g. med_1, appt_1).
        approved: Whether the action is approved (True) or rejected (False).
    """
    db = load_db()
    pending = db.get("pending_actions", [])
    
    target_action = None
    for action in pending:
        if action.get("id") == action_id:
            target_action = action
            break
            
    if not target_action:
        return {"status": "error", "message": f"Pending action with ID '{action_id}' not found."}
        
    pending.remove(target_action)
    db["pending_actions"] = pending
    
    if approved:
        if target_action["type"] == "add_medication":
            db.setdefault("medications", []).append({
                "med_name": target_action["med_name"],
                "dosage": target_action["dosage"],
                "time_of_day": target_action["time_of_day"]
            })
            msg = f"Successfully committed medication '{target_action['med_name']}'."
        elif target_action["type"] == "schedule_appointment":
            db.setdefault("appointments", []).append({
                "doctor_name": target_action["doctor_name"],
                "date_time": target_action["date_time"],
                "reason": target_action["reason"]
            })
            msg = f"Successfully committed appointment with {target_action['doctor_name']}."
    else:
        msg = f"Rejected pending action '{action_id}'."
        
    save_db(db)
    return {"status": "success", "message": msg}

if __name__ == "__main__":
    mcp.run()
