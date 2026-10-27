# Written by cpfleet. Runs once at power-up, before code.py.
# Hides the CIRCUITPY drive from USB so the Wi-Fi web workflow can write files (CircuitPython lets
# only one side write at a time). The serial console stays on USB.
# Hold the button named in PIN while pressing Reset to get the USB drive back for one boot.
import board
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
