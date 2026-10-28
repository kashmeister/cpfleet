# cpfleet launch checklist

Goal: when someone searches, or asks an AI agent, how to manage several CircuitPython boards over Wi-Fi,
update code on a MagTag without the USB drive, or why `CIRCUITPY` doesn't mount on their Mac, cpfleet
comes up and is easy to install.

## Where it stands (2026-10-28)

- [x] Public repo, MIT licence. The description and topics use the words people search for
      (circuitpython, web-workflow, adafruit, magtag, esp32, esp32-s2, esp32-s3, e-ink, cli, …).
- [x] [Release v0.1.0](https://github.com/kashmeister/cpfleet/releases/tag/v0.1.0) with release notes.
- [x] Homebrew: `brew install kashmeister/tap/cpfleet`. It installed and passed `brew test` on a Mac Studio.
      `brew audit --strict --new` needs the tap trusted first (`brew trust kashmeister/tap`).
- [x] Tested against three Adafruit MagTags (2025 Edition, CircuitPython 10.3.1):
  - status, push, run, log, reload, reset and bootloader;
  - provisioning from scratch;
  - a firmware update;
  - an Adafruit Learn Project Bundle (MagTag Showerthoughts).

## Next (Claude)

- [ ] PyPI release (`pip install cpfleet` / `pipx install cpfleet`): needs a PyPI account and token
- [ ] GitHub Actions: run the unit tests on Python 3.9 and the newest Python for each push
- [ ] More board profiles (ESP32-S3 Feather, Qualia, Pico W). Each needs a real board to test provisioning.
- [ ] The agent pager app (`apps/pager`): MQTT, LEDs and an e-ink banner

## Links from elsewhere (you)

- [ ] A short demo GIF at the top of the README: `cpfleet status` and `push` across several boards
- [ ] Post in the Adafruit forums (CircuitPython) and on r/circuitpython
- [ ] Mention it on the Adafruit Discord's #help-with-circuitpython when web workflow or "CIRCUITPY won't mount" comes up
- [ ] Pull request adding it to [awesome-circuitpython](https://github.com/adafruit/awesome-circuitpython)
