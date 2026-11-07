#!/bin/bash
# The owner's app link: the API runs on the Vince host config, so the link is for Vince's full-read reader.
. "$(dirname "$(readlink -f "$0")")/../lib.sh"
PLX_DEPLOY_READER_ENV=PLX_EVAL_READER_VINCE PLX_DEPLOY_HOST_DIR=$HOME/.config/plx/workbench-host-vince exec $PLX_CURRENT/deploy/demo-access-link.sh "${1:-120}"
