# Written by cpfleet. Runs once at power-up, before code.py.
# Hides the CIRCUITPY drive from USB so the Wi-Fi web workflow can write files (CircuitPython lets
# only one side write at a time). The serial console stays on USB.
# Hold the button named in PIN while pressing Reset to get the USB drive back for one boot.
#
# Opt-in: CPFLEET_USB_DATA = 1 in settings.toml also turns on CircuitPython's second USB serial channel
# (usb_cdc.data), which apps such as apps/agents read when the board is plugged into a computer. The
# ESP32-S2 has too few USB endpoints for that and all the defaults, so USB keyboard/mouse (usb_hid) and
# MIDI (usb_midi) are turned off with it. The computer then sees two serial ports per board: the console
# first, the data channel second. Changes here, and to CPFLEET_USB_DATA, take effect after a reset.
# Holding PIN at Reset skips the data channel too, for one boot. Nothing here touches Wi-Fi, and a
# failure only prints a line to boot_out.txt.
import board
import os
import storage

PIN = "BUTTON_D"  # set from the board profile; "" for boards without a button

pin = getattr(board, PIN, None) if PIN else None
held = False
if pin is not None:
    import digitalio

    button = digitalio.DigitalInOut(pin)
    button.switch_to_input(pull=digitalio.Pull.UP)
    held = not button.value
    button.deinit()
if not held:
    storage.disable_usb_drive()

if not held and str(os.getenv("CPFLEET_USB_DATA") or "").lower() in ("1", "true", "yes"):
    try:
        import usb_cdc

        try:
            import usb_hid

            usb_hid.disable()
        except ImportError:
            pass
        try:
            import usb_midi

            usb_midi.disable()
        except ImportError:
            pass
        usb_cdc.enable(console=True, data=True)
        print("USB data channel on")
    except Exception as e:  # never stop the boot over this
        print("USB data channel not enabled:", e)
