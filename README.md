# NOTestPilot — programmable pilots

A fresh design for testing Nuclear Option server behavior with programmable mock
players. Tasks and targets belong to the test script. Selected game flight,
aiming and weapon routines provide the mechanics.

This branch is a new orphan history. The previous headless-client implementation
is preserved on `main`.

## Development status

The new architecture is being implemented. No runtime pass, working combat
adapter, resource saving, or independent client compatibility is claimed yet.

The first milestone is two independently addressable mock players in one game
process, with native player records and aircraft ownership, explicit targets,
native attributed gun damage, cancellation and cleanup. Mock players do not
establish authentication, real client packet handling or retail Steam coverage.

The game runs only in a disposable remote lab. Proprietary assemblies and private
results are local inputs, never repository contents.
