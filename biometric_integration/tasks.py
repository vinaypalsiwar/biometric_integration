import frappe


def sync_enabled_devices():
    """Sync enabled Direct ZKTeco devices every 30 minutes."""

    try:
        settings = frappe.get_single("Biometric Sync Settings")

        if not settings.enable_automatic_sync:
            return

        devices = frappe.get_all(
            "Biometric Device",
            filters={"enabled": 1},
            fields=["name", "connection_mode"],
        )

        sync_method = frappe.get_attr(
            "biometric_integration.api.zkteco.sync_device"
        )

        for device in devices:
            device_name = device.name
            connection_mode = device.connection_mode or "Direct"

            # Local Connector devices are synced by the office connector.
            # Frappe Cloud must not try to connect directly to the local LAN device.
            if connection_mode == "Local Connector":
                frappe.logger("biometric_integration").info(
                    f"Skipping Local Connector device: {device_name}"
                )
                continue

            try:
                result = sync_method(device_name)
                frappe.logger("biometric_integration").info(
                    f"Scheduled sync for {device_name}: {result}"
                )
            except Exception:
                frappe.log_error(
                    frappe.get_traceback(),
                    f"Biometric scheduled sync failed: {device_name}",
                )

    except Exception:
        frappe.log_error(
            frappe.get_traceback(),
            "Biometric scheduled task failed",
        )
