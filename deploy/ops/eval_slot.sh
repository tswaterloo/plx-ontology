#!/bin/bash
# The ONE scoreboard/measurement slot on this host: waits for the slot (flock), then runs the
# command as a capped system unit (heavy.sh). Every eval run (mine or an agent's) goes through this.
#   eval_slot.sh <unit-name> <command...>      (cap 22G; one at a time)
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
name=${1:?unit name}; shift
HEAVY_WAIT=1 exec flock $PLX_SCRATCH/eval-slot.lock $PLX_OPS/heavy.sh "eval-$name" 22G "$@"
