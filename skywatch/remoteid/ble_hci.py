"""Raw-HCI Bluetooth LE scanner for Realtek RTL8761B(U) USB dongles.

Why this exists
---------------
Windows's Bluetooth stack (bthport.sys) can only bind one radio at a time. If
the host already has a working onboard BT radio (e.g. an Intel combo card),
any second USB dongle ends up in Code 31 / CM_PROB_FAILED_ADD and bleak can
never see it. This module sidesteps that by talking to the dongle directly
over WinUSB (via libusb / pyusb), without going through the OS BT stack.

That means:
  - The dongle must be re-bound to WinUSB via Zadig (one-time).
  - It will not appear under Bluetooth in Device Manager anymore.
  - The onboard radio continues to work normally for everything else; both
    can scan at the same time.

What we implement
-----------------
  1. USB enumeration (find VID 0x2C0A PID 0x8761).
  2. Realtek "Realtech" v2 epatch firmware loader. RTL8761B ships a stub ROM
     that needs the patch + config blobs uploaded over HCI before LE works.
  3. Minimal HCI command/event framing: control-transfer for commands,
     interrupt-in for events. (No ACL — we never connect; we only scan.)
  4. LE scan enable + LE Advertising Report parser.
  5. Forward each ASTM F3411 service-data payload (UUID 0xFFFA) to the same
     `_apply_message` helpers `ble.py` uses, so message decoding is shared.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time
from pathlib import Path
from typing import Optional

from ..tracker import Target, Tracker, TYPE_DRONE
from .remoteid import parse_remote_id_ie, _apply_message, _DroneState

log = logging.getLogger("skywatch.remoteid.hci")

# ── USB / device ─────────────────────────────────────────────────────────

REALTEK_VID = 0x2C0A
RTL8761BU_PID = 0x8761

# USB transfer types: dongles expose HCI cmds on EP0 (control), events on an
# interrupt-IN endpoint, and ACL on bulk endpoints. We only use control + INT.
_HCI_CMD_REQUEST_TYPE = 0x20    # host->device, class request, recipient=device
_HCI_CMD_REQUEST = 0x00
_HCI_EVENT_EP = 0x81            # interrupt-in (standard for HCI USB transport)

# ── HCI opcodes ──────────────────────────────────────────────────────────
# opcode = (OGF << 10) | OCF

def _opcode(ogf: int, ocf: int) -> int:
    return (ogf << 10) | ocf

OCF_RESET = _opcode(0x03, 0x0003)
OCF_READ_LOCAL_VERSION = _opcode(0x04, 0x0001)
OCF_SET_EVENT_MASK = _opcode(0x03, 0x0001)
OCF_LE_SET_EVENT_MASK = _opcode(0x08, 0x0001)
OCF_LE_SET_SCAN_PARAMS = _opcode(0x08, 0x000B)
OCF_LE_SET_SCAN_ENABLE = _opcode(0x08, 0x000C)

# Realtek vendor commands (OGF 0x3F)
OCF_RTK_READ_ROM_VERSION = _opcode(0x3F, 0x006D)     # 0xFC6D
OCF_RTK_DOWNLOAD = _opcode(0x3F, 0x0020)             # 0xFC20

# ── HCI event codes ──────────────────────────────────────────────────────

EVT_CMD_COMPLETE = 0x0E
EVT_CMD_STATUS = 0x0F
EVT_LE_META = 0x3E
LE_SUBEVT_ADV_REPORT = 0x02
LE_SUBEVT_EXT_ADV_REPORT = 0x0D

# AD (advertising data) types we care about
AD_TYPE_SERVICE_DATA_16 = 0x16

# ASTM F3411 Open Drone ID assigned UUID (16-bit)
ASTM_UUID_LE = 0xFFFA


def _firmware_dir() -> Path:
    """Where ble_hci.py looks for the RTL8761BU firmware blobs.

    They live next to the other bundled tools (libusb-1.0.dll, rtl_fm.exe, …)
    so the same `tools/win64/` directory _bootstrap.py already wires into PATH
    + os.add_dll_directory works for us too.
    """
    here = Path(__file__).resolve().parent.parent.parent  # project root
    return here / "tools" / "win64"


# ─────────────────────────────────────────────────────────────────────────
#  Firmware blob parsing — Realtek "Realtech" v2 epatch format
# ─────────────────────────────────────────────────────────────────────────

_EPATCH_SIGNATURE = b"Realtech"
_EPATCH_TAIL_MAGIC = 0x77FD0451


def _select_patch_for_rom(fw: bytes, rom_version: int) -> bytes:
    """Pick the patch bytes that apply to this chip from a v1 'Realtech'
    epatch blob (the format rtl_bt/rtl8761bu_fw.bin uses).

    Layout matches drivers/bluetooth/btrtl.c rtl_request_firmware / v1:

      offset 0..7   : "Realtech" signature
      offset 8..11  : u32 fw_version (little-endian)
      offset 12..13 : u16 num_patches
      then three parallel tables, each `num_patches` entries long:
        chip_id[i]  : u16  — match against (rom_version + 1)
        length[i]   : u16  — patch payload size
        offset[i]   : u32  — byte offset of payload in `fw`
      patch payloads themselves live at those offsets.
      The final 4 bytes of the file are the tail magic 0x77FD0451.

    For the selected patch, Linux overwrites its last 4 bytes with the
    header's fw_version (an SVN-marker slot the chip reads after upload).
    We do the same here.
    """
    if fw[:8] != _EPATCH_SIGNATURE:
        raise RuntimeError(f"firmware magic mismatch — expected 'Realtech', got {fw[:8]!r}")
    if len(fw) < 14:
        raise RuntimeError("firmware truncated (no patch table)")
    tail = struct.unpack_from("<I", fw, len(fw) - 4)[0]
    if tail != _EPATCH_TAIL_MAGIC:
        raise RuntimeError(f"firmware tail magic mismatch — got 0x{tail:08X}, "
                            f"expected 0x{_EPATCH_TAIL_MAGIC:08X}")

    fw_version = struct.unpack_from("<I", fw, 8)[0]
    num_patches = struct.unpack_from("<H", fw, 12)[0]
    chip_id_off = 14
    length_off = chip_id_off + num_patches * 2
    offset_off = length_off + num_patches * 2
    if offset_off + num_patches * 4 > len(fw):
        raise RuntimeError("firmware truncated walking patch table")

    # Realtek's convention: patch entry's chip_id == ROM version + 1.
    # RTL8761B's ROM stub reports ROM version 0 for "BU" silicon, which maps
    # to chip_id 1 here. The rtl8761bu_fw.bin we ship has chip_ids {1, 2}
    # covering both revisions.
    target_chip_id = (rom_version & 0xFF) + 1

    for i in range(num_patches):
        chip_id = struct.unpack_from("<H", fw, chip_id_off + i * 2)[0]
        if chip_id != target_chip_id:
            continue
        plen = struct.unpack_from("<H", fw, length_off + i * 2)[0]
        poff = struct.unpack_from("<I", fw, offset_off + i * 4)[0]
        if poff + plen > len(fw):
            raise RuntimeError(f"patch {i} offset/length out of bounds")
        patch = bytearray(fw[poff:poff + plen])
        # Stamp the fw_version into the patch's trailing SVN slot.
        if len(patch) >= 4:
            patch[-4:] = struct.pack("<I", fw_version)
        return bytes(patch)

    chip_ids = [struct.unpack_from("<H", fw, chip_id_off + i * 2)[0]
                for i in range(num_patches)]
    raise RuntimeError(f"no patch matches ROM version 0x{rom_version:02X} "
                       f"(want chip_id {target_chip_id}, blob has {chip_ids})")


# ─────────────────────────────────────────────────────────────────────────
#  Low-level HCI transport over WinUSB / libusb
# ─────────────────────────────────────────────────────────────────────────

class _HCITransport:
    """Tiny synchronous HCI shim over pyusb.

    Lives in its own worker thread (the manager owns the asyncio loop, but we
    do blocking USB I/O — that doesn't mix with async). All callers (scan
    loop + setup sequence) run on that same thread.
    """

    def __init__(self, dev) -> None:
        self.dev = dev

    def send_cmd(self, opcode: int, params: bytes = b"") -> None:
        if len(params) > 255:
            raise ValueError(f"HCI cmd params too long: {len(params)} > 255")
        payload = struct.pack("<HB", opcode, len(params)) + params
        self.dev.ctrl_transfer(_HCI_CMD_REQUEST_TYPE, _HCI_CMD_REQUEST, 0, 0, payload, 1000)

    def read_event(self, timeout_ms: int = 1000) -> Optional[tuple[int, bytes]]:
        """Read one HCI event. Returns (event_code, params) or None on timeout.

        Returns None for timeouts (so the scan loop can periodically check
        its stop flag) but raises for real errors.
        """
        try:
            # 260 = max HCI event size (256 params + 4 header slack).
            data = self.dev.read(_HCI_EVENT_EP, 260, timeout_ms)
        except Exception as e:
            # pyusb wraps libusb timeouts in usb.core.USBError with errno=110
            # on Linux, etype=10060 on Windows. Easiest just to look at the
            # repr — saves importing usb.core.USBError purely for this.
            if "timeout" in str(e).lower() or "timed out" in str(e).lower():
                return None
            raise
        if len(data) < 2:
            return None
        evt_code = data[0]
        plen = data[1]
        params = bytes(data[2:2 + plen])
        return (evt_code, params)

    def wait_for_command_complete(self, opcode: int, timeout_s: float = 2.0) -> bytes:
        """Block until a Command Complete event for `opcode` arrives. Returns
        the response bytes (status + return params)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            evt = self.read_event(timeout_ms=200)
            if evt is None:
                continue
            code, params = evt
            if code == EVT_CMD_COMPLETE and len(params) >= 3:
                # Command Complete: [num_cmd_pkts, opcode_lo, opcode_hi, status, return_params...]
                evt_opcode = params[1] | (params[2] << 8)
                if evt_opcode == opcode:
                    return params[3:]
            elif code == EVT_CMD_STATUS and len(params) >= 4:
                # Command Status: [status, num_cmd_pkts, opcode_lo, opcode_hi]
                evt_opcode = params[2] | (params[3] << 8)
                if evt_opcode == opcode:
                    return params[:1]   # just the status byte
            # Other events (vendor-specific, LE meta during init, etc.) are ignored here.
        raise TimeoutError(f"HCI command 0x{opcode:04X} did not complete within {timeout_s}s")


# ─────────────────────────────────────────────────────────────────────────
#  Firmware upload sequence
# ─────────────────────────────────────────────────────────────────────────

def _read_rom_version(t: _HCITransport) -> int:
    """Query the chip for its ROM version. RTL8761B answers FC6D with
    [status, version]. Pre-firmware-patch chips usually report 0x00."""
    t.send_cmd(OCF_RTK_READ_ROM_VERSION)
    resp = t.wait_for_command_complete(OCF_RTK_READ_ROM_VERSION, timeout_s=2.0)
    if len(resp) < 2 or resp[0] != 0:
        raise RuntimeError(f"Read ROM Version failed: {resp.hex()}")
    return resp[1]


def _download_blob(t: _HCITransport, blob: bytes) -> None:
    """Push `blob` to the controller via FC20 in 252-byte chunks.

    Each chunk: [index_byte, data...]. Index counts 0,1,2,... and the *last*
    chunk has the high bit (0x80) set on the index. The controller resets
    itself after the last chunk lands, so we don't expect a command-complete
    for that one.
    """
    chunk_size = 252
    total = (len(blob) + chunk_size - 1) // chunk_size
    log.info("uploading %d bytes in %d chunks of up to %d bytes",
             len(blob), total, chunk_size)
    for i in range(total):
        start = i * chunk_size
        end = start + chunk_size
        data = blob[start:end]
        is_last = (i == total - 1)
        index_byte = (i & 0x7F) | (0x80 if is_last else 0)
        params = bytes([index_byte]) + data
        t.send_cmd(OCF_RTK_DOWNLOAD, params)
        if not is_last:
            # Expect command-complete for every non-final chunk.
            try:
                resp = t.wait_for_command_complete(OCF_RTK_DOWNLOAD, timeout_s=2.0)
                if not resp or resp[0] != 0:
                    raise RuntimeError(f"download chunk {i} rejected: {resp.hex() if resp else 'no response'}")
            except TimeoutError:
                raise RuntimeError(f"download chunk {i}/{total - 1} timed out")
    # Brief settle — the chip is reinitializing internally.
    time.sleep(0.5)


def _load_firmware(t: _HCITransport, fw: bytes, cfg: bytes) -> None:
    rom = _read_rom_version(t)
    log.info("RTL8761BU ROM version: 0x%02X", rom)
    patch = _select_patch_for_rom(fw, rom)
    log.info("selected patch subsection: %d bytes", len(patch))
    # Realtek concatenates the chosen patch with the config blob and uploads
    # them as a single image. The config tail is what the chip reads after
    # the patch's relocation pass finishes.
    blob = patch + cfg
    _download_blob(t, blob)
    log.info("firmware download complete; controller resetting")


def _hci_reset_and_init(t: _HCITransport) -> None:
    """Standard post-patch init: Reset, set event masks. Required before LE
    Set Scan Enable will be honoured."""
    t.send_cmd(OCF_RESET)
    t.wait_for_command_complete(OCF_RESET, timeout_s=2.0)
    # Set Event Mask: enable LE Meta Events (bit 61).
    t.send_cmd(OCF_SET_EVENT_MASK, bytes.fromhex("FFFFFBFF07F8BF3D"))
    t.wait_for_command_complete(OCF_SET_EVENT_MASK, timeout_s=1.0)
    # LE Set Event Mask: enable Advertising Report (bit 1) at minimum.
    t.send_cmd(OCF_LE_SET_EVENT_MASK, bytes.fromhex("FF00000000000000"))
    t.wait_for_command_complete(OCF_LE_SET_EVENT_MASK, timeout_s=1.0)


def _start_le_scan(t: _HCITransport) -> None:
    """Active scan, 30 ms window inside a 30 ms interval — i.e. nearly 100%
    duty cycle on whichever of the three primary advertising channels we land
    on at any given moment. Active so we also capture scan-response data,
    which DJI drones use for the heftier ASTM messages."""
    # LE Set Scan Parameters: type=1 (active), interval=0x0030 (30 ms),
    # window=0x0030 (30 ms), own_addr_type=0 (public), filter_policy=0 (accept all)
    t.send_cmd(OCF_LE_SET_SCAN_PARAMS, struct.pack("<BHHBB", 1, 0x0030, 0x0030, 0, 0))
    t.wait_for_command_complete(OCF_LE_SET_SCAN_PARAMS, timeout_s=1.0)
    # LE Set Scan Enable: enable=1, filter_duplicates=0 (we WANT every advert).
    t.send_cmd(OCF_LE_SET_SCAN_ENABLE, bytes([1, 0]))
    t.wait_for_command_complete(OCF_LE_SET_SCAN_ENABLE, timeout_s=1.0)


def _stop_le_scan(t: _HCITransport) -> None:
    t.send_cmd(OCF_LE_SET_SCAN_ENABLE, bytes([0, 0]))
    try:
        t.wait_for_command_complete(OCF_LE_SET_SCAN_ENABLE, timeout_s=1.0)
    except TimeoutError:
        pass  # closing down anyway


# ─────────────────────────────────────────────────────────────────────────
#  Advertising data parser
# ─────────────────────────────────────────────────────────────────────────

def _iter_ad_structures(buf: bytes):
    """Walk a BLE AD-structure blob. Yields (ad_type, ad_data)."""
    i = 0
    while i < len(buf):
        length = buf[i]
        if length == 0 or i + 1 + length > len(buf):
            return
        ad_type = buf[i + 1]
        ad_data = buf[i + 2: i + 1 + length]
        yield ad_type, ad_data
        i += 1 + length


def _extract_astm_payload(ad_blob: bytes) -> Optional[bytes]:
    """Return the ASTM service-data payload (without the 2-byte UUID prefix)
    if this advertisement carries one, else None."""
    for ad_type, ad_data in _iter_ad_structures(ad_blob):
        if ad_type != AD_TYPE_SERVICE_DATA_16:
            continue
        if len(ad_data) < 2:
            continue
        uuid = ad_data[0] | (ad_data[1] << 8)
        if uuid == ASTM_UUID_LE:
            return ad_data[2:]
    return None


# ─────────────────────────────────────────────────────────────────────────
#  Public scanner class
# ─────────────────────────────────────────────────────────────────────────

def find_dongle() -> Optional[dict]:
    """Quick probe used by /api/remoteid/stats so the dashboard can show
    'Realtek dongle detected (firmware not loaded)' even when the scanner
    isn't running. Returns a small dict on success, None on miss."""
    try:
        import usb.core  # type: ignore
    except Exception:
        return None
    dev = usb.core.find(idVendor=REALTEK_VID, idProduct=RTL8761BU_PID)
    if dev is None:
        return None
    return {
        "vid": REALTEK_VID,
        "pid": RTL8761BU_PID,
        "bus": getattr(dev, "bus", None),
        "address": getattr(dev, "address", None),
    }


class HCIScanner:
    """LE advertising scanner that drives a Realtek RTL8761BU dongle directly
    over WinUSB. Public surface mirrors BLEScanner so the rest of SkyWatch
    can treat them interchangeably."""

    def __init__(self, tracker: Tracker) -> None:
        self.tracker = tracker
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._states: dict[str, _DroneState] = {}
        # First-time payload dump, one entry per (addr, parsed-types) combo,
        # capped at 30. Same shape as the BLE scanner's debug logging so
        # log lines from both paths are directly comparable.
        self._dbg_seen: set[tuple] = set()
        # Counters surfaced by /api/remoteid/stats.
        self.frames_total = 0
        self.frames_rid = 0
        self.last_frame_at = 0.0
        self.last_rid_at = 0.0
        self.last_error: str = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    async def start(self) -> None:
        if self.running:
            return
        self._loop = asyncio.get_event_loop()
        self._stop.clear()
        self.last_error = ""
        self._thread = threading.Thread(target=self._run, name="remoteid-hci", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=8.0)
            self._thread = None

    # ── Worker thread ──────────────────────────────────────────────────

    def _run(self) -> None:
        try:
            import usb.core  # type: ignore
            import usb.util  # type: ignore  # noqa: F401
        except Exception as e:
            self.last_error = f"pyusb unavailable: {e}"
            log.error(self.last_error)
            return

        dev = usb.core.find(idVendor=REALTEK_VID, idProduct=RTL8761BU_PID)
        if dev is None:
            self.last_error = (f"Realtek dongle (VID 0x{REALTEK_VID:04X} "
                                f"PID 0x{RTL8761BU_PID:04X}) not found over USB. "
                                "Did you run Zadig and replace its driver with WinUSB?")
            log.error(self.last_error)
            return

        try:
            # WinUSB driver auto-claims the device; we just need to know the
            # configuration is set. set_configuration is idempotent on Windows.
            try:
                dev.set_configuration()
            except Exception as e:
                log.debug("set_configuration: %s (often harmless on WinUSB)", e)

            transport = _HCITransport(dev)

            # Load firmware. The dongle's ROM stub is not a usable BT controller
            # until this finishes — every LE command returns "unknown opcode".
            fw_dir = _firmware_dir()
            fw_path = fw_dir / "rtl8761bu_fw.bin"
            cfg_path = fw_dir / "rtl8761bu_config.bin"
            if not fw_path.is_file() or not cfg_path.is_file():
                self.last_error = (f"firmware blobs missing: expected "
                                    f"{fw_path} and {cfg_path}")
                log.error(self.last_error)
                return
            fw = fw_path.read_bytes()
            cfg = cfg_path.read_bytes()
            log.info("loaded firmware (%d bytes) + config (%d bytes) from %s",
                     len(fw), len(cfg), fw_dir)

            _load_firmware(transport, fw, cfg)
            _hci_reset_and_init(transport)
            _start_le_scan(transport)
            log.info("HCI LE scan running on Realtek dongle")

            self._scan_loop(transport)
        except Exception as e:
            self.last_error = f"HCI worker crashed: {e}"
            log.exception("HCI worker crashed")
        finally:
            try:
                _stop_le_scan(_HCITransport(dev))
            except Exception:
                pass
            try:
                import usb.util  # type: ignore
                usb.util.dispose_resources(dev)
            except Exception:
                pass

    def _scan_loop(self, transport: _HCITransport) -> None:
        last_log = time.monotonic()
        while not self._stop.is_set():
            evt = transport.read_event(timeout_ms=500)
            if evt is None:
                # Heartbeat so the user can confirm the scanner is alive even
                # when no LE traffic is present.
                now = time.monotonic()
                if (now - last_log) >= 15.0:
                    log.info("HCI scan: %d adv frames (%d Drone-RID)",
                             self.frames_total, self.frames_rid)
                    last_log = now
                continue
            code, params = evt
            if code != EVT_LE_META or not params:
                continue
            sub = params[0]
            if sub == LE_SUBEVT_ADV_REPORT:
                self._handle_adv_report(params[1:])
            elif sub == LE_SUBEVT_EXT_ADV_REPORT:
                self._handle_ext_adv_report(params[1:])
            now = time.monotonic()
            if (now - last_log) >= 15.0:
                log.info("HCI scan: %d adv frames (%d Drone-RID)",
                         self.frames_total, self.frames_rid)
                last_log = now

    # ── Advertising report decoding ────────────────────────────────────

    def _handle_adv_report(self, buf: bytes) -> None:
        """Parse one or more LE Advertising Report sub-events.

        Layout per BT spec:
          [num_reports]
          repeated num_reports times:
            [evt_type, addr_type, addr(6), data_len, data..., rssi]
        """
        if not buf:
            return
        num = buf[0]
        off = 1
        for _ in range(num):
            if off + 9 > len(buf):
                return
            _evt_type = buf[off]
            _addr_type = buf[off + 1]
            addr = buf[off + 2:off + 8]
            data_len = buf[off + 8]
            data = buf[off + 9:off + 9 + data_len]
            # Skip past data + 1-byte rssi for the next iteration.
            off = off + 9 + data_len + 1
            self.frames_total += 1
            self.last_frame_at = time.time()
            payload = _extract_astm_payload(data)
            if payload is not None:
                self._ingest_astm(addr, payload)

    def _handle_ext_adv_report(self, buf: bytes) -> None:
        """Extended Advertising Report (BT 5.x). Layout differs from legacy.

        Most RID broadcasts still use the legacy report path; ext reports are
        included so newer drones broadcasting on coded PHY don't slip through.
        """
        if not buf:
            return
        num = buf[0]
        off = 1
        for _ in range(num):
            if off + 24 > len(buf):
                return
            # [evt_type:2, addr_type:1, addr:6, primary_phy:1, secondary_phy:1,
            #  sid:1, tx_power:1, rssi:1, periodic_interval:2, direct_addr_type:1,
            #  direct_addr:6, data_len:1, data:data_len]
            addr = buf[off + 3:off + 9]
            data_len = buf[off + 23]
            data = buf[off + 24:off + 24 + data_len]
            off = off + 24 + data_len
            self.frames_total += 1
            self.last_frame_at = time.time()
            payload = _extract_astm_payload(data)
            if payload is not None:
                self._ingest_astm(addr, payload)

    def _ingest_astm(self, addr: bytes, payload: bytes) -> None:
        """Feed ASTM service-data bytes into the shared state machine. Mirrors
        BLEScanner._handle_payload so the bleak path and the raw-USB HCI path
        produce identical state for the same byte stream."""
        # Standard ASTM F3411 BLE framing is 1-byte msg counter + 25-byte
        # ODID (26 total). Some beacons / BLE-5 extended-advert flows prepend
        # an extra 1-byte marker, giving 27 total. Dispatch on length so all
        # variants decode correctly instead of force-stripping one byte and
        # getting impossible lat/lon when the wire format diverges.
        n = len(payload)
        if n >= 27:
            body = payload[2:]
        elif n >= 26:
            body = payload[1:]
        elif n >= 25:
            body = payload
        else:
            return
        self.frames_rid += 1
        self.last_rid_at = time.time()
        addr_key = addr.hex()
        state = self._states.setdefault(addr_key, _DroneState())
        parsed_types: list[int] = []
        for msg_type, sub in parse_remote_id_ie(body):
            parsed_types.append(msg_type)
            _apply_message(state, msg_type, sub)
        # One debug dump per unseen (addr, types) combination, cap 30.
        seen_key = (addr_key, tuple(parsed_types))
        if seen_key not in self._dbg_seen and len(self._dbg_seen) < 30:
            self._dbg_seen.add(seen_key)
            log.info("HCI RID debug addr=%s payload_len=%d payload=%s parsed_types=%s state.uas_id=%r state.lat=%s state.lon=%s state.alt=%s",
                     addr_key, len(payload), payload.hex(), parsed_types,
                     state.uas_id, state.lat, state.lon, state.altitude_m)
        # Publish as soon as we have a position. Key by MAC, not uas_id, so
        # a beacon whose Basic-ID parses to garbage still produces a single
        # stable marker (and one whose Basic-ID is fine vs another whose
        # isn't don't collide on `DRONE-`).
        if (state.lat or state.lon) and self._loop is not None:
            mac = addr_key  # already a hex string, no colons
            target = Target(
                id=f"DRONE-{mac}",
                type=TYPE_DRONE,
                callsign=state.uas_id or state.registration or mac,
                drone_id=state.uas_id,
                registration=state.registration,
                operator=state.operator_id,
                lat=state.lat, lon=state.lon,
                altitude=state.altitude_m,
                speed=state.speed_kt,
                heading=state.heading,
            )
            asyncio.run_coroutine_threadsafe(self.tracker.upsert(target), self._loop)
