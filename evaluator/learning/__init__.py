"""
desc: Learned preference signal. Noah's own actions (Add to Queue, up/down
    nudges, Mark applied) are logged as raw events; model.py tallies them per
    (feature, key) at scoring time and score.py adds the result as one capped
    "learned" component. Totals are never stored, so changing a weight re-counts
    the whole history. Removing from the Queue or "Not interested" record nothing.

    To add a signal kind: add it to SIGNAL_WEIGHTS in signals.py (and a verb to
        _VERBS in model.py for the explanation text), then call record_signal().
    To add a feature: write fn(app, facts) -> key string or None in features.py
        and register it in FEATURES (optionally give it a FEATURE_WEIGHTS entry
        in model.py).

args:
    (see signals.py, features.py, model.py)
output:
    (re-exports record_signal, reset_signals, learned_points)
"""

from evaluator.learning.signals import record_signal, reset_signals
from evaluator.learning.model import learned_points

__all__ = ["record_signal", "reset_signals", "learned_points"]
