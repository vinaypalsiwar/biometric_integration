import frappe
from datetime import datetime


def _parse_datetime(value):
    """Convert an ISO datetime string to a naive datetime."""
    if not value:
        return None

    if isinstance(value, str):
        value = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)

    return value


@frappe.whitelist()
def receive_attendance():
    """
    Receive attendance punches from the Local Biometric Connector.

    Expected JSON:

    {
        "device_name": "YOUR_BIOMETRIC_DEVICE_NAME",
        "punches": [
            {
                "user_id": "654",
                "timestamp": "2026-10-08 09:42:15"
            }
        ]
    }
    """

    if frappe.session.user == "Guest":
        frappe.throw("Authentication required.")

    if frappe.request.method != "POST":
        frappe.throw("Only POST requests are allowed.")

    data = frappe.request.get_json()

    if not data:
        frappe.throw("Request body is required.")

    device_name = data.get("device_name")
    punches = data.get("punches")

    if not device_name:
        frappe.throw("device_name is required.")

    if not isinstance(punches, list):
        frappe.throw("punches must be a list.")

    try:
        device = frappe.get_doc(
            "Biometric Device",
            device_name,
        )
    except frappe.DoesNotExistError:
        frappe.throw(
            f"Biometric Device not found: {device_name}"
        )

    if not device.enabled:
        frappe.throw("This biometric device is disabled.")

    employee_map = {
        str(row.attendance_device_id): row.name
        for row in frappe.get_all(
            "Employee",
            filters={
                "attendance_device_id": ["is", "set"],
            },
            fields=[
                "name",
                "attendance_device_id",
            ],
            limit_page_length=0,
        )
    }

    created = 0
    skipped = 0
    unknown = 0
    failed = 0

    unknown_user_ids = []
    errors = []

    zkteco_device_id = f"ZKTeco-{device.device_ip}"

    for punch in punches:
        try:
            user_id = str(
                punch.get("user_id", "")
            ).strip()

            punch_time = _parse_datetime(
                punch.get("timestamp")
            )

            if not user_id:
                failed += 1
                errors.append("Missing user_id")
                continue

            if not punch_time:
                failed += 1
                errors.append(
                    f"Invalid timestamp for user {user_id}"
                )
                continue

            employee_name = employee_map.get(user_id)

            if not employee_name:
                unknown += 1

                if user_id not in unknown_user_ids:
                    unknown_user_ids.append(user_id)

                continue

            existing = frappe.db.exists(
                "Employee Checkin",
                {
                    "employee": employee_name,
                    "time": punch_time,
                },
            )

            if existing:
                skipped += 1
                continue

            day_start = punch_time.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )

            day_end = punch_time.replace(
                hour=23,
                minute=59,
                second=59,
                microsecond=999999,
            )

            day_records = frappe.get_all(
                "Employee Checkin",
                filters={
                    "employee": employee_name,
                    "device_id": zkteco_device_id,
                    "time": [
                        "between",
                        [day_start, day_end],
                    ],
                },
                fields=[
                    "name",
                    "time",
                    "log_type",
                ],
                order_by="time asc",
                limit_page_length=0,
            )

            in_records = [
                record
                for record in day_records
                if record.log_type == "IN"
            ]

            out_records = [
                record
                for record in day_records
                if record.log_type == "OUT"
            ]

            day_in = (
                in_records[0]
                if in_records
                else None
            )

            day_out = (
                max(
                    out_records,
                    key=lambda record: record.time,
                )
                if out_records
                else None
            )

            # First valid punch of the day becomes IN.
            if day_in is None:
                clock = (
                    punch_time.hour,
                    punch_time.minute,
                )

                if not ((9, 30) <= clock <= (18, 30)):
                    skipped += 1
                    continue

                doc = frappe.get_doc({
                    "doctype": "Employee Checkin",
                    "employee": employee_name,
                    "time": punch_time,
                    "log_type": "IN",
                    "device_id": zkteco_device_id,
                })

                doc.insert(
                    ignore_permissions=True
                )

                created += 1

            # Ignore punches before or equal to IN.
            elif punch_time <= day_in.time:
                skipped += 1

            # First punch after IN becomes OUT.
            elif day_out is None:
                doc = frappe.get_doc({
                    "doctype": "Employee Checkin",
                    "employee": employee_name,
                    "time": punch_time,
                    "log_type": "OUT",
                    "device_id": zkteco_device_id,
                })

                doc.insert(
                    ignore_permissions=True
                )

                created += 1

            # Later punches update the existing OUT.
            elif punch_time > day_out.time:
                doc = frappe.get_doc(
                    "Employee Checkin",
                    day_out.name,
                )

                doc.time = punch_time

                doc.save(
                    ignore_permissions=True
                )

            else:
                skipped += 1

        except Exception as exc:
            failed += 1

            errors.append({
                "user_id": punch.get("user_id"),
                "timestamp": punch.get("timestamp"),
                "error": str(exc),
            })

    # Update device cursor only after processing
    # the complete batch.
    valid_times = [
        _parse_datetime(
            punch.get("timestamp")
        )
        for punch in punches
        if punch.get("timestamp")
    ]

    valid_times = [
        value
        for value in valid_times
        if value is not None
    ]

    if valid_times:
        latest_time = max(valid_times)

        current_cursor = _parse_datetime(
            device.datetime_qufu
        )

        if (
            current_cursor is None
            or latest_time > current_cursor
        ):
            device.datetime_qufu = latest_time

    device.datetime_sctd = (
        frappe.utils.now_datetime()
    )

    device.last_successful_sync = (
        frappe.utils.now_datetime()
    )

    device.last_error = ""

    device.save(
        ignore_permissions=True
    )

    frappe.db.commit()

    return {
        "success": True,
        "message": (
            "Attendance synchronized successfully."
        ),
        "device_name": device_name,
        "received": len(punches),
        "created_checkins": created,
        "skipped": skipped,
        "unknown_users": unknown,
        "failed": failed,
        "unknown_user_ids": unknown_user_ids,
        "errors": errors,
        "last_sync": str(
            device.datetime_qufu or ""
        ),
    }