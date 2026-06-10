"""
Toy Story 2 fuzzer hook.

Drop this file next to fuzz.py in your Archipelago-from-source checkout and run
the fuzzer with:

    python fuzz.py -r 100 -j 16 -g toystory2 -n 1 --hook ts2_fuzz_hook:Hook

WHAT IT DOES
------------
The Toy Story 2 world DELIBERATELY refuses to generate Coinsanity setups that
cannot physically fit. The classic case is a "lopsided" bundle configuration: a
tiny RECEIVED bundle size (e.g. 1) turns every coin into its own coin-bundle
ITEM (hundreds of them), while a large CHECKS bundle size (or ALL) creates only
a handful of coin-bundle CHECK LOCATIONS to hold them. There is nowhere to put
the items, so the world raises a clear, actionable error instead of dying deep
inside Fill with an opaque one.

That rejection is CORRECT behaviour, not a generation bug -- a real player would
get the same helpful message and fix their YAML. It is fundamentally an INVALID
OPTION COMBINATION, so the right bucket for it is the fuzzer's OptionError
outcome (reported under "ignored"), NOT "failure". By default the fuzzer treats
the raised exception as a hard failure, which buries the failures that actually
matter.

This hook reclassifies ONLY that specific, self-inflicted rejection from Failure
to OptionError, matching on the world's own error text so it never masks real
bugs (FillError, tracebacks, etc.). OptionError/ignored results are still
visible if you pass --dump-ignored.
"""

from fuzz import BaseHook, GenOutcome


# Substrings that uniquely identify the Toy Story 2 world's intentional
# "this configuration can't fit" rejection. Both must be present so we never
# accidentally swallow an unrelated error that merely mentions the game name.
_INTENDED_REJECTION_MARKERS = ("Toy Story 2", "too many to place")


class Hook(BaseHook):
    def reclassify_outcome(self, outcome, exception):
        # Keep this fast and side-effect free: per the fuzzer docs it may run in
        # either the worker or the main process.
        if exception is not None:
            msg = str(exception)
            if all(marker in msg for marker in _INTENDED_REJECTION_MARKERS):
                # Intended config rejection -> an invalid option combination,
                # not a generation bug. Classify it as OptionError so it lands
                # in the "ignored" tally instead of "failure".
                return GenOutcome.OptionError, exception
        # Everything else keeps whatever classification it already had.
        return outcome, exception