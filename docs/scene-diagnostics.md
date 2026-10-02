# Finding out why a client fails to load

A client sometimes reaches the mission loader after a trainer aircraft has
already been destroyed. A successful join on the next attempt does not explain
that failure. This optional diagnostic records the aircraft's lifetime and the
network removal messages so the two cases can be compared.

Set `NOTESTPILOT_SCENE_LIFECYCLE_TRACE=1` in the disposable game process's
environment to enable it. The bridge's normal enable, token, role and game-build
checks still apply. An additional exact Mirage assembly check rejects unreviewed
network code before these hooks are installed.

Check `observation.sceneLifecycleTrace` in each process's status:

- `enabled` and `installationComplete` must both be true.
- `observerFailures` must be zero.
- `overflow` must be zero for a complete retained trace.
- `incomingCorrelationSkipped` reports correlation attempts that could not be
  safely matched; it is not a count of unique messages.

The trace follows only `trainer_1` through `trainer_4`. It retains at most 128
events: registration, removal scheduling, server destruction notification,
client removal messages and destruction callbacks. It checks managed object
identity before matching a message to an aircraft; a reused numeric network ID
is insufficient. Cached IDs remain readable after the Unity object is gone.

These are observations. The original methods still run, including their errors.
The diagnostic changes no registry entries, aircraft state or network messages.
It adds overhead and should not be used for performance comparisons.

## What the real tests showed

On Linux 0.34.1 / Steam build 24724541, all five processes activated the hooks.
The server recorded ordinary trainer-removal scheduling, and all four clients
recorded the matching destruction message and unspawn path. No observer failure,
overflow or ambiguous incoming match was recorded in that run.

The fourth client read the mission just before receiving the removal message.
An earlier failed join had encountered the destroyed entry instead. That
supports a timing boundary; it does not establish a fix or retail-client impact.
The immediate caller of a destruction notification may itself be an `OnDestroy`
callback, so it must not automatically be treated as the original requester.
Each process has its own clock: compare event order within a process.

The same run later failed its takeoff survival check after a missile
damaged one pilot's aircraft. The diagnostic worked; the sortie failed. An
earlier four-client diagnostic completed five minutes of flight and gun bursts.
Neither result establishes long-session stability or a performance improvement.
