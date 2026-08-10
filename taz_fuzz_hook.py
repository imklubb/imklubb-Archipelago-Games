"""
Taz Wanted fuzzer hook.

LIVES AT THE REPO ROOT, not in tools/, and it is the one taz_*.py that does.
The fuzzer loads it by module name -- `--hook taz_fuzz_hook:Hook` -- so it has
to be importable from where fuzz.py runs, and it imports fuzz.py itself. Moving
it into tools/ with the others breaks both directions at once.

Run it from the Archipelago-from-source checkout:

    venv\\Scripts\\python.exe fuzz.py -r 100 -j 16 -g "Taz Wanted" -n 1 \\
        --hook taz_fuzz_hook:Hook

WHAT IT DOES
------------
Taz Wanted refuses yamls that ask for something impossible rather than quietly
rebuilding them into a different seed -- starting destruction above what the
Daffy-culty can reach, a poster goal above the pool, linear gates that do not
climb, or a goal with no condition turned on.

Refusing is correct: a real player gets a message naming exactly what to
change. But it is an INVALID OPTION COMBINATION, not a generation bug, so it
belongs in the fuzzer's OptionError bucket rather than counted as a failure --
otherwise these swamp the failures that actually matter.

The match is on the world's own error text, so it can never swallow a
FillError, a traceback, or anything else real.
"""

from fuzz import BaseHook, GenOutcome


# Both must appear, so an unrelated error that merely mentions the game is not
# quietly reclassified.
_INTENDED_REJECTION_MARKERS = ("Taz Wanted cannot generate with these options",
                               "-")


class Hook(BaseHook):
    def reclassify_outcome(self, outcome, exception):
        # Fast and side-effect free: this may run in a worker or in the main
        # process.
        if exception is not None:
            msg = str(exception)
            if all(m in msg for m in _INTENDED_REJECTION_MARKERS):
                return GenOutcome.OptionError, exception
        return outcome, exception
