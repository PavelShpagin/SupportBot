from __future__ import annotations

import io
import json
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import qrcode


@dataclass(frozen=True)
class LinkDeviceSnapshot:
    status: str  # idle | waiting_for_scan | linked | failed | cancelled | expired | already_linked
    started_at: float | None
    ended_at: float | None
    url: str | None
    exit_code: int | None
    error: str | None
    output_tail: list[str]


def _make_qr_png_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def is_account_registered(*, config_dir: str, e164: str) -> bool:
    """
    Best-effort check whether the signal-cli account is registered/linked.

    signal-cli stores:
      <config>/data/accounts.json -> list of accounts with "path"
      <config>/data/<path>        -> JSON that includes "registered": true/false
    """
    try:
        base = Path(config_dir)
        accounts_path = base / "data" / "accounts.json"
        if not accounts_path.exists():
            return False
        accounts = json.loads(accounts_path.read_text(encoding="utf-8"))
        acct_list = accounts.get("accounts") if isinstance(accounts, dict) else None
        if not isinstance(acct_list, list):
            return False
        match = None
        for a in acct_list:
            if isinstance(a, dict) and str(a.get("number") or "") == e164:
                match = a
                break
        if not match:
            return False
        rel = str(match.get("path") or "").strip()
        if not rel:
            return False
        acct_path = base / "data" / rel
        if not acct_path.exists():
            return False
        acct = json.loads(acct_path.read_text(encoding="utf-8"))
        return bool(isinstance(acct, dict) and acct.get("registered") is True)
    except Exception:
        return False


class LinkDeviceManager:
    """
    Manages `signal-cli link` lifecycle and exposes the current QR PNG bytes.

    The key property: we keep the `signal-cli link` process alive while the user scans.
    """

    def __init__(
        self,
        *,
        signal_cli_bin: str,
        config_dir: str,
        expected_e164: str,
        device_name: str = "SupportBot",
        link_timeout_seconds: int = 180,
        on_linked: Optional[Callable[[], None]] = None,
    ) -> None:
        self._signal_cli_bin = signal_cli_bin
        self._config_dir = config_dir
        self._expected_e164 = expected_e164
        self._device_name = device_name
        self._timeout_seconds = link_timeout_seconds
        self._on_linked = on_linked

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)

        self._proc: subprocess.Popen[str] | None = None
        self._qr_png: bytes | None = None
        self._snapshot = LinkDeviceSnapshot(
            status="idle",
            started_at=None,
            ended_at=None,
            url=None,
            exit_code=None,
            error=None,
            output_tail=[],
        )

    def snapshot(self) -> LinkDeviceSnapshot:
        with self._lock:
            return LinkDeviceSnapshot(
                status=self._snapshot.status,
                started_at=self._snapshot.started_at,
                ended_at=self._snapshot.ended_at,
                url=self._snapshot.url,
                exit_code=self._snapshot.exit_code,
                error=self._snapshot.error,
                output_tail=list(self._snapshot.output_tail),
            )

    def get_qr_png(self) -> bytes | None:
        with self._lock:
            return self._qr_png

    def start(self) -> LinkDeviceSnapshot:
        with self._lock:
            # If already linked, don't start a new provisioning flow.
            if is_account_registered(config_dir=self._config_dir, e164=self._expected_e164):
                self._snapshot = LinkDeviceSnapshot(
                    status="already_linked",
                    started_at=None,
                    ended_at=time.time(),
                    url=None,
                    exit_code=0,
                    error=None,
                    output_tail=["Account already linked/registered."],
                )
                self._qr_png = None
                return self.snapshot()

            # If a link process is already running, just return current state.
            if self._proc is not None and self._proc.poll() is None:
                return self.snapshot()

            # Start new provisioning process.
            cmd = [
                self._signal_cli_bin,
                "--config",
                self._config_dir,
                "link",
                "-n",
                self._device_name,
            ]

            self._qr_png = None
            self._snapshot = LinkDeviceSnapshot(
                status="waiting_for_scan",
                started_at=time.time(),
                ended_at=None,
                url=None,
                exit_code=None,
                error=None,
                output_tail=[],
            )

            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            threading.Thread(target=self._monitor_proc, daemon=True).start()
            return self.snapshot()

    def cancel(self) -> bool:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                return False
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._snapshot = LinkDeviceSnapshot(
                status="cancelled",
                started_at=self._snapshot.started_at,
                ended_at=time.time(),
                url=self._snapshot.url,
                exit_code=None,
                error=None,
                output_tail=list(self._snapshot.output_tail) + ["Cancelled."],
            )
            self._cond.notify_all()
            return True

    def wait_for_qr(self, *, timeout_seconds: float = 5.0) -> bytes | None:
        deadline = time.time() + timeout_seconds
        with self._cond:
            while self._qr_png is None and time.time() < deadline:
                remaining = max(0.0, deadline - time.time())
                self._cond.wait(timeout=remaining)
            return self._qr_png

    def _append_output(self, line: str) -> None:
        tail = list(self._snapshot.output_tail)
        tail.append(line)
        if len(tail) > 200:
            tail = tail[-200:]
        self._snapshot = LinkDeviceSnapshot(
            status=self._snapshot.status,
            started_at=self._snapshot.started_at,
            ended_at=self._snapshot.ended_at,
            url=self._snapshot.url,
            exit_code=self._snapshot.exit_code,
            error=self._snapshot.error,
            output_tail=tail,
        )

    def _monitor_proc(self) -> None:
        proc = None
        started_at = None
        with self._lock:
            proc = self._proc
            started_at = self._snapshot.started_at

        if proc is None or proc.stdout is None:
            with self._cond:
                self._snapshot = LinkDeviceSnapshot(
                    status="failed",
                    started_at=started_at,
                    ended_at=time.time(),
                    url=None,
                    exit_code=None,
                    error="Failed to start signal-cli link process.",
                    output_tail=[],
                )
                self._cond.notify_all()
            return

        deadline = (started_at or time.time()) + float(self._timeout_seconds)
        url_set = False

        try:
            for raw in proc.stdout:
                line = (raw or "").strip()
                if not line:
                    continue
                with self._cond:
                    self._append_output(line)
                    if (not url_set) and line.startswith("sgnl://linkdevice"):
                        url_set = True
                        self._snapshot = LinkDeviceSnapshot(
                            status=self._snapshot.status,
                            started_at=self._snapshot.started_at,
                            ended_at=self._snapshot.ended_at,
                            url=line,
                            exit_code=self._snapshot.exit_code,
                            error=self._snapshot.error,
                            output_tail=list(self._snapshot.output_tail),
                        )
                        self._qr_png = _make_qr_png_bytes(line)
                        self._cond.notify_all()

                if time.time() >= deadline:
                    break

            # If we exited the loop due to timeout, expire the process.
            if proc.poll() is None and time.time() >= deadline:
                try:
                    proc.terminate()
                except Exception:
                    pass
                elapsed = int(time.time() - (started_at or time.time()))
                with self._cond:
                    self._snapshot = LinkDeviceSnapshot(
                        status="expired",
                        started_at=self._snapshot.started_at,
                        ended_at=time.time(),
                        url=self._snapshot.url,
                        exit_code=None,
                        error=f"QR code scan timed out after {elapsed} seconds. Please try again and scan the QR code more quickly.",
                        output_tail=list(self._snapshot.output_tail),
                    )
                    self._cond.notify_all()
                return

            exit_code = proc.wait(timeout=5)
            with self._cond:
                final_status = "linked" if exit_code == 0 else "failed"
                self._snapshot = LinkDeviceSnapshot(
                    status=final_status,
                    started_at=self._snapshot.started_at,
                    ended_at=time.time(),
                    url=self._snapshot.url,
                    exit_code=int(exit_code),
                    error=None if exit_code == 0 else "signal-cli link failed",
                    output_tail=list(self._snapshot.output_tail),
                )
                self._cond.notify_all()

            if exit_code == 0 and self._on_linked is not None:
                try:
                    self._on_linked()
                except Exception:
                    # Don't crash the manager if callback fails.
                    pass
        except Exception as exc:
            with self._cond:
                self._snapshot = LinkDeviceSnapshot(
                    status="failed",
                    started_at=self._snapshot.started_at,
                    ended_at=time.time(),
                    url=self._snapshot.url,
                    exit_code=None,
                    error=str(exc),
                    output_tail=list(self._snapshot.output_tail),
                )
                self._cond.notify_all()
