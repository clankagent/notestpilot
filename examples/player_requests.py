"""Two native request players: allowance, purchase, owned spawn and isolation.

Requires an already running disposable mission with Player requests enabled.
Supply NOTESTPILOT_PORT/TOKEN and NOTESTPILOT_PASSWORD explicitly. No funds,
inventory, flight inputs or server control are fabricated. Virtual connections
exercise native request handling, not sockets, retail clients or client flight.
Optional packet_sink(name, copied_records) is for private diagnostic storage.
"""

from collections import Counter
import argparse
import copy
import json
import math
import os
import sys
import time
import uuid

from notestpilot import RequestJoinUncertain, Session, StaleActor
from explicit_target import TEXTURE_ERROR, SmokeFailure, clean_state


def row_for(state, record):
    rows = [r for r in state.get("requestPlayers", []) if r.get("name") == record["name"]]
    if len(rows) != 1:
        raise StaleActor("Owned request connection is missing or ambiguous")
    row = rows[0]
    if (state.get("instance"), row.get("mode"), row.get("connectionId")) != (
            record["instance"], "player-requests", record["connectionId"]):
        raise StaleActor("Owned request receipt/runtime/connection changed")
    if record["creationId"] is not None and row.get("creationId") != record["creationId"]:
        raise StaleActor("Owned request creation correlation changed")
    expected = record["playerId"]
    # Authentication can finish after an uncertain join was recovered while
    # still pending. Only that exact receipt may acquire its first native ID,
    # and the record remains cleanup-only; an existing ID is never replaced.
    if (expected is None and record.get("uncertainty") is not None
            and type(row.get("playerId")) is int and row["playerId"] > 0):
        record["playerId"] = expected = row["playerId"]
    if row.get("playerId") != expected and not (row.get("playerId") is None and row.get("state") in ("failed", "disconnected")):
        raise StaleActor("Owned request native player changed")
    return row


def inventory(row):
    values = row.get("inventory")
    if not isinstance(values, list) or any(not isinstance(v, dict) or not isinstance(v.get("aircraft"), str)
            or type(v.get("reserved")) is not bool for v in values):
        raise SmokeFailure("Native inventory telemetry is incomplete")
    return Counter((v["aircraft"], v["reserved"]) for v in values)


def isolation(row):
    return copy.deepcopy({k: row.get(k) for k in ("connectionId", "playerId", "faction", "allocation", "inventory", "airframeInUse", "aircraft")})


def ready_player(row):
    if (row.get("mode") != "player-requests" or row.get("host") is not False or row.get("ready") is not True
            or row.get("authenticated") is not True or row.get("registered") is not True
            or row.get("authenticatedMember") is not True or row.get("error") is not None):
        raise SmokeFailure("Native non-host request player is not healthy/ready", {"player": safe_row(row)})


def safe_row(row):
    # Deliberately omit raw packets, notices and connection/authentication data.
    return copy.deepcopy({k: row.get(k) for k in ("name", "playerId", "ready", "state", "faction", "allocation", "rank",
                "inventory", "airframeInUse", "aircraft", "nativeGameErrors", "nativeErrorBits", "spawnRequests",
                "cleanupAttempted", "cleanupIncomplete", "registered", "authenticatedMember", "nativeOwnedIdentityCount")})


def choose_native_purchase(state, rows):
    balances = [r.get("allocation") for r in rows]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in balances):
        raise SmokeFailure("Native allocation telemetry is invalid")
    faction = rows[0]["faction"]
    for definition in sorted(state.get("aircraftCatalog", []), key=lambda d: d["cost"]):
        key, cost, rank = definition["key"], definition["cost"], definition["rank"]
        if type(cost) not in (int, float) or not math.isfinite(cost) or not 0 < cost <= min(balances):
            continue
        if any(r.get("rank", -1) < rank for r in rows):
            continue
        bases = [b["name"] for b in state.get("airbases", []) if b.get("faction") == faction
                 and b.get("disabled") is False and key in b.get("availableAircraft", [])]
        if bases:
            return key, cost, [bases[0], bases[1] if len(bases) > 1 else bases[0]]
    raise SmokeFailure("No observed compatible, rank-eligible airframe is affordable for both native players")


def run(session, password, *, timeout=30, interval=0.25, allowed_errors=(), packet_sink=None,
        clock=time.monotonic, sleep=time.sleep):
    """One fresh two-peer session; every mutation is issued once.

    allowed_errors uses Session.wait_for's exact (kind,message) tuple contract.
    Default strict. Native error flags are cumulative diagnostics, not a reply.
    A spawn succeeds only with its own matched native RPC receipt and aircraft.
    """
    if not isinstance(password, str) or type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise ValueError("An explicit password and timeout in 1..60 seconds are required")
    if type(interval) not in (int, float) or not math.isfinite(interval) or not 0 < interval <= 1:
        raise ValueError("interval must be finite and in (0,1]")
    if packet_sink is not None and not callable(packet_sink):
        raise ValueError("packet_sink must be callable or None")
    if not isinstance(allowed_errors, tuple) or any(not isinstance(pair, tuple) or len(pair) != 2
            or any(not isinstance(v, str) for v in pair) for pair in allowed_errors):
        raise ValueError("allowed_errors must be a tuple of exact (kind, message) pairs")
    owned, state, stage = [], None, "live catalog"
    started = clock()
    report = {"passed": False, "coverage": "two virtual non-host native request players; no socket/client-flight coverage",
              "checks": {}, "spawnReceipts": [], "packetRecords": 0, "packetBatches": 0}

    def bind(record, snapshot):
        observed_id = row_for(snapshot, record)["playerId"]
        if record.get("uncertainty") is not None:
            # The public recovery API issues only status/disconnect capabilities.
            current = session.recover_request_player(record["uncertainty"])
            if record["playerId"] is None and type(current.player_id) is int and current.player_id > 0:
                record["playerId"] = current.player_id
        else:
            current = session.request_player(record["name"])
        if (current.instance, current.connection_id) != (record["instance"], record["connectionId"]):
            raise StaleActor("Request player changed during explicit observation rebind")
        if current.player_id != observed_id and not (record.get("uncertainty") is not None
                and observed_id is None and current.player_id == record["playerId"]):
            raise StaleActor("Request native player changed during explicit observation rebind")
        record["handle"] = current
        return current

    def drain(snapshot):
        for record in owned:
            handle = bind(record, snapshot)
            row = row_for(snapshot, record)
            initial_pending = row.get("journalPackets", 0) + row.get("queuedPackets", 0)
            consumed = 0
            # Runtime chunks are bounded; never spin unboundedly on new output.
            for _ in range(64):
                packets = handle.packets()
                consumed += len(packets)
                report["packetRecords"] += len(packets)
                if packets:
                    report["packetBatches"] += 1
                    if packet_sink:
                        packet_sink(record["name"], copy.deepcopy(packets))
                if not packets or consumed >= initial_pending:
                    break
            else:
                raise SmokeFailure("Native outgoing journal did not drain within bounded chunks")

    def poll(predicate, description):
        nonlocal state
        deadline = clock() + timeout
        while clock() < deadline:
            state = clean_state(session, min(5, max(0.001, deadline - clock())), allowed_errors)
            drain(state)
            result = predicate(state)
            if result:
                return state
            sleep(min(interval, max(0, deadline - clock())))
        raise SmokeFailure("Deadline: " + description, {"players": [safe_row(row_for(state, r)) for r in owned]})

    def own(handle, receipt=None):
        # Register the server-issued connection handle before another read can
        # fail. Uncertain joins already require the exact recovered receipt.
        record = {"name": handle.name, "instance": handle.instance, "connectionId": handle.connection_id,
                  "creationId": receipt.creation_id if receipt else None, "playerId": handle.player_id, "handle": handle,
                  "uncertainty": receipt}
        owned.append(record)
        snapshot = session.status()
        row = row_for(snapshot, record)
        creation = receipt.creation_id if receipt else row.get("creationId")
        if not isinstance(creation, str) or not creation or row.get("creationId") != creation:
            raise StaleActor("Join ownership correlation changed")
        record["creationId"] = creation
        return record

    try:
        state = clean_state(session, 5, allowed_errors)
        if state.get("serverActive") is not True or state.get("missionRunning") is not True:
            raise SmokeFailure("An already running disposable Player requests mission is required")
        economy = [e for e in state.get("factionEconomy", []) if e.get("faction") in state.get("factions", [])
                   and any(b.get("faction") == e["faction"] and b.get("disabled") is False for b in state.get("airbases", []))]
        if not economy:
            raise SmokeFailure("Native faction economy and enabled owned bases are required")
        faction = economy[0]["faction"]
        prefix = "requests-" + uuid.uuid4().hex[:12]
        for index in range(2):
            stage = "native player join " + str(index)
            frozen = isolation(row_for(state, owned[0])) if owned else None
            try:
                handle = session.join_request_player(prefix + "-" + str(index), password, allowed_errors=allowed_errors)
            except RequestJoinUncertain as uncertain:
                own(session.recover_request_player(uncertain), uncertain)
                raise
            record = own(handle)
            handle.join_faction(faction)
            state = poll(lambda s: row_for(s, record).get("faction") == faction, "native faction join")
            row = row_for(state, record)
            ready_player(row)
            if not math.isclose(row["allocation"], economy[0]["joinAllowance"], rel_tol=1e-6, abs_tol=0.001):
                raise SmokeFailure("Fresh native faction allowance differs from observed economy", {"player": safe_row(row)})
            if frozen is not None and isolation(row_for(state, owned[0])) != frozen:
                raise SmokeFailure("Second native join changed first player's economy/identity")
        if len({r["connectionId"] for r in owned}) != 2 or len({r["playerId"] for r in owned}) != 2:
            raise SmokeFailure("Two distinct native request players/connections are required")
        report["checks"]["nativeJoinAllowanceIdentityIsolation"] = True
        aircraft, cost, bases = choose_native_purchase(state, [row_for(state, r) for r in owned])
        report["selection"] = {"aircraft": aircraft, "cost": cost, "airbases": bases}
        for index, record in enumerate(owned):
            stage = "native purchase " + str(index)
            before = row_for(state, record)
            balance, before_inventory = before["allocation"], inventory(before)
            other = owned[1 - index]
            frozen = isolation(row_for(state, other))
            expected = before_inventory.copy()
            expected[(aircraft, False)] += 1
            record["handle"].purchase_airframe(aircraft)
            state = poll(lambda s: inventory(row_for(s, record)) == expected, "native inventory purchase credit")
            if not math.isclose(row_for(state, record)["allocation"], balance - cost, rel_tol=1e-6, abs_tol=0.001):
                raise SmokeFailure("Native purchase did not debit exact catalog cost", {"player": safe_row(row_for(state, record))})
            if isolation(row_for(state, other)) != frozen:
                raise SmokeFailure("Purchase changed the independent player's state")
        report["checks"]["exactCostInventoryPurchaseIsolation"] = True
        for index, record in enumerate(owned):
            stage = "correlated owned spawn " + str(index)
            before = row_for(state, record)
            known_ids = {r["replyId"] for r in before.get("spawnRequests", [])}
            before_inventory = inventory(before)
            other = owned[1 - index]
            frozen = isolation(row_for(state, other))
            record["handle"] = record["handle"].request_spawn(bases[index], aircraft)
            reply_id = None
            def completed(snapshot):
                nonlocal reply_id
                row = row_for(snapshot, record)
                if reply_id is None:
                    reply_id = row.get("lastSpawnReplyId")
                    if (type(reply_id) is not int or reply_id <= 0 or reply_id in known_ids
                            or any(r["replyId"] == reply_id for r in report["spawnReceipts"])):
                        raise SmokeFailure("Spawn request has no unique new native reply correlation")
                matches = [r for r in row.get("spawnRequests", []) if r.get("replyId") == reply_id]
                if len(matches) != 1:
                    raise SmokeFailure("Spawn receipt is missing or ambiguous")
                receipt = matches[0]
                if receipt.get("uncertain") is True or receipt.get("state") in ("uncertain", "replied-late"):
                    raise SmokeFailure("Native spawn outcome is uncertain; request was not replayed", {"receipt": receipt})
                if receipt.get("state") != "replied":
                    return False
                outcome = receipt.get("outcome")
                if not isinstance(outcome, dict) or outcome.get("matched") is not True or outcome.get("replyId") != reply_id:
                    raise SmokeFailure("Native spawn reply correlation mismatch", {"receipt": receipt})
                if outcome.get("success") is False:
                    raise SmokeFailure("Native spawn RPC returned an error, without a TrySpawnResult", {"receipt": receipt})
                if outcome.get("success") is not True or type(outcome.get("allowed")) is not bool:
                    raise SmokeFailure("Native spawn reply result is malformed", {"receipt": receipt})
                if not outcome["allowed"]:
                    raise SmokeFailure("Native spawn RPC succeeded but returned Allowed=false", {"receipt": receipt})
                air = row.get("aircraft")
                return isinstance(air, dict) and row.get("aircraftSpawnPending") is False
            state = poll(completed, "matched allowed spawn reply and linked native aircraft")
            row = row_for(state, record)
            air = row["aircraft"]
            if (air.get("localSim") is not False or air.get("remoteSim") is not True
                    or air.get("ownedByConnection") is not True or air.get("linkedPlayerId") != record["playerId"]):
                raise SmokeFailure("Spawn did not retain native remote-owner authority", {"player": safe_row(row)})
            expected = before_inventory.copy()
            expected[(aircraft, False)] -= 1
            expected += Counter()  # Drop zero-count entries.
            if inventory(row) != expected or row.get("airframeInUse") != {"aircraft": aircraft, "reserved": False}:
                raise SmokeFailure("Native owned inventory did not move into AirframeInUse", {"player": safe_row(row)})
            if isolation(row_for(state, other)) != frozen:
                raise SmokeFailure("Spawn changed the independent player's state")
            report["spawnReceipts"].append({"replyId": reply_id, "success": True, "allowed": True,
                                           "delayedSpawn": next(r for r in row["spawnRequests"] if r["replyId"] == reply_id)["outcome"]["delayedSpawn"]})
        report["checks"]["correlatedOwnedSpawnRemoteAuthorityIsolation"] = True
        stage = "independent native disconnect"
        frozen = isolation(row_for(state, owned[1]))
        owned[0]["handle"].disconnect()
        state = poll(lambda s: row_for(s, owned[0]).get("state") == "disconnected", "first native disconnect")
        if isolation(row_for(state, owned[1])) != frozen:
            raise SmokeFailure("First native disconnect changed the remaining player")
        ready_player(row_for(state, owned[1]))
        report["checks"]["disconnectIsolation"] = True
        report["observedSeconds"] = clock() - started
        report["passed"] = True
        return report
    except Exception as failure:
        players = []
        if isinstance(state, dict):
            players = [safe_row(r) for r in state.get("requestPlayers", []) if any(r.get("connectionId") == o["connectionId"] for o in owned)]
        raise SmokeFailure("Player requests scenario failed at " + stage, {"stage": stage, "cause": str(failure),
                           "details": getattr(failure, "diagnostics", {}), "players": players,
                           "completedChecks": report["checks"]}) from failure
    finally:
        primary, failures = sys.exc_info()[1], []
        for record in reversed(owned):
            try:
                snapshot = session.status()
                row = row_for(snapshot, record)
                current = bind(record, snapshot)
                if row.get("state") != "disconnected":
                    current.disconnect()
                deadline = clock() + timeout
                while True:
                    snapshot = session.status()
                    row = row_for(snapshot, record)
                    if row.get("state") in ("disconnected", "failed") and row.get("cleanupAttempted") is True:
                        if row.get("cleanupIncomplete") is True:
                            raise SmokeFailure("Native request cleanup is incomplete", {"player": safe_row(row)})
                        if (row.get("cleanupIncomplete") is False and row.get("registered") is False
                                and row.get("authenticatedMember") is False and row.get("nativeOwnedIdentityCount") == 0
                                and row.get("nativeVisibleIdentityCount") == 0 and row.get("playerId") is None):
                            break
                    if clock() >= deadline:
                        raise SmokeFailure("Native owned disconnect cleanup deadline expired", {"player": safe_row(row)})
                    sleep(min(interval, max(0, deadline - clock())))
            except Exception as cleanup_failure:
                failures.append({"name": record["name"], "error": str(cleanup_failure), "details": getattr(cleanup_failure, "diagnostics", {})})
        if failures:
            raise SmokeFailure("Owned request-player cleanup incomplete", {"cleanup": failures,
                               "primaryFailure": str(primary) if primary else None,
                               "primaryDiagnostics": getattr(primary, "diagnostics", {})}) from primary
        report["cleanup"] = {"connections": len(owned), "complete": True}
        report["checks"]["nativeDisconnectCleanup"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--allow-headless-texture-error", action="store_true",
                        help="Opt in only to the exact known rendering Error with a complete journal")
    args = parser.parse_args()
    try:
        result = run(Session(timeout=5), os.environ["NOTESTPILOT_PASSWORD"], timeout=args.timeout,
                     allowed_errors=TEXTURE_ERROR if args.allow_headless_texture_error else ())
    except Exception as failure:
        result = {"passed": False, "error": str(failure), "diagnostics": getattr(failure, "diagnostics", {})}
    print(json.dumps(result, indent=2, allow_nan=False))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
