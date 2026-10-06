"""Phase 1 - base OS dependencies (spec section 5).

Policy: FOUND -> VERIFY -> REUSE, MISSING -> INSTALL -> VERIFY.  Nothing is
reinstalled just because the installer ran again.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from . import common
from .common import InstallError, Log, run, which

APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a"}


def installed_packages(names: List[str]) -> Dict[str, bool]:
    """Ask dpkg which of ``names`` are installed (one call, not N)."""
    result: Dict[str, bool] = {name: False for name in names}
    res = run(["dpkg-query", "-W", "-f=${Package} ${Status}\n"] + names, mutating=False, timeout=60)
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0] in result:
            result[parts[0]] = parts[-1] == "installed"
    return result


def apt_update(max_age_seconds: int = 3600) -> None:
    """Refresh the apt cache, but not on every single run."""
    import os
    import time

    stamp = "/var/lib/apt/periodic/update-success-stamp"
    lists = "/var/lib/apt/lists"
    newest = 0.0
    for path in (stamp, lists):
        if os.path.exists(path):
            newest = max(newest, os.path.getmtime(path))
    if newest and time.time() - newest < max_age_seconds:
        Log.debug("apt cache is fresh (%.0fs old); skipping update" % (time.time() - newest))
        return
    Log.step("apt-get update")
    res = run(["apt-get", "update", "-qq"], env=APT_ENV, timeout=600)
    if not res.ok:
        Log.warn("apt-get update failed; continuing with the existing cache\n%s" % res.tail(5))


def install(packages: List[str]) -> None:
    if not packages:
        return
    Log.step("installing: %s" % " ".join(packages))
    res = run(
        ["apt-get", "install", "-y", "--no-install-recommends"] + packages,
        env=APT_ENV,
        timeout=1800,
    )
    if not res.ok:
        raise InstallError("apt-get install failed for %s\n%s" % (" ".join(packages), res.tail(25)))


def verify(binaries: Dict[str, str]) -> Tuple[List[str], List[str]]:
    found, missing = [], []
    for binary in sorted(binaries):
        (found if which(binary) else missing).append(binary)
    return found, missing


def run_phase(config: Dict[str, Any]) -> Dict[str, Any]:
    """Install/verify everything in ``dependencies`` from the config."""
    deps = config.get("dependencies", {})
    wanted: List[str] = list(deps.get("apt") or [])
    binaries: Dict[str, str] = dict(deps.get("verify_binaries") or {})

    status = installed_packages(wanted)
    present = [name for name, ok in status.items() if ok]
    missing = [name for name, ok in status.items() if not ok]

    if present:
        Log.step("already installed (%d): %s" % (len(present), " ".join(sorted(present))))
    if missing:
        apt_update()
        install(sorted(missing))
    else:
        Log.step("no packages missing")

    if common.DRY_RUN:
        # Nothing was installed, so verifying the install would always fail.
        Log.info("dry run: would install %d package(s): %s" % (len(missing), " ".join(sorted(missing)) or "none"))
        return {"packages": wanted, "would_install": sorted(missing), "binaries": verify(binaries)[0]}

    # VERIFY step - packages can be "installed" and still not provide the tool.
    recheck = installed_packages(wanted)
    still_missing = [name for name, ok in recheck.items() if not ok]
    if still_missing:
        raise InstallError("these packages are still not installed after apt: %s" % " ".join(still_missing))

    found, missing_bins = verify(binaries)
    if missing_bins:
        raise InstallError(
            "packages installed but these binaries are not on PATH: %s" % " ".join(missing_bins)
        )
    Log.ok("%d packages present, %d binaries verified" % (len(wanted), len(found)))
    return {"packages": wanted, "installed_now": sorted(missing), "binaries": found}
