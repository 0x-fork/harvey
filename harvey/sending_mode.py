"""Approval or autopilot, picked up while Harvey runs.

The dashboard switch (or a hand edit) changes channels.email.require_approval
in the config file. Whatever part of Harvey notices first, the loop, the
sender, the writer or the handler, calls sync_sending_mode, so going back to
approval always brings what autopilot approved (and hasn't sent) back to the
Outbox, whoever noticed.
"""

import logging

from harvey.config import refresh_sending_mode

logger = logging.getLogger("harvey.sending_mode")


async def sync_sending_mode(config, state) -> bool:
    """Bring ``config`` in line with its file. True when the mode changed."""
    if not refresh_sending_mode(config):
        return False
    if config.channels.email.require_approval:
        back = await state.requeue_autopilot_approved()
        logger.warning("Sending mode is now APPROVAL: every email waits in the Outbox for you."
                       + (f" {back} queued by autopilot came back to the Outbox." if back else ""))
    else:
        logger.warning("Sending mode is now AUTOPILOT: Harvey sends on its own "
                       "(emails it holds still wait for you).")
    return True
